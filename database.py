"""
Unified SQLite layer for all sources and run state.
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4
from datetime import datetime, timezone

import aiosqlite

from config import settings

SOURCE_ORDER = ("hh", "habr", "remoteok")
STATUS_ORDER = ("new", "liked", "rejected", "applied", "archived")
VALID_SOURCES = set(SOURCE_ORDER)
VALID_STATUS = set(STATUS_ORDER)

RUN_FIELDS = {
    "phase",
    "fetched",
    "filtered",
    "saved",
    "known",
    "analyzed",
    "letters",
    "errors_json",
    "error",
    "finished_at",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    location TEXT,
    salary_from INTEGER,
    salary_to INTEGER,
    currency TEXT,
    salary_period TEXT,
    published_at TEXT,
    description TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    remote INTEGER NOT NULL DEFAULT 0,
    it_score INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',
    first_seen TEXT,
    last_seen TEXT,
    raw_json TEXT,
    ai_score INTEGER,
    ai_verdict TEXT,
    matched_json TEXT,
    missing_json TEXT,
    red_flags_json TEXT,
    ai_reason TEXT,
    analyzed_at TEXT,
    profile_hash TEXT,
    cover_letter TEXT,
    letter_status TEXT NOT NULL DEFAULT 'none'
);

CREATE INDEX IF NOT EXISTS idx_vacancies_status ON vacancies (status);
CREATE INDEX IF NOT EXISTS idx_vacancies_source ON vacancies (source);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vacancies_source_id ON vacancies (source, source_id);
CREATE INDEX IF NOT EXISTS idx_vacancies_published ON vacancies (published_at, first_seen);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    phase TEXT NOT NULL DEFAULT 'queued',
    fetched INTEGER NOT NULL DEFAULT 0,
    filtered INTEGER NOT NULL DEFAULT 0,
    saved INTEGER NOT NULL DEFAULT 0,
    known INTEGER NOT NULL DEFAULT 0,
    analyzed INTEGER NOT NULL DEFAULT 0,
    letters INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT NOT NULL DEFAULT '{}',
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at);
"""

_VACANCY_ADD_COLUMNS = {
    "tags_json": "TEXT",
    "remote": "INTEGER",
    "it_score": "INTEGER",
    "status": "TEXT",
    "first_seen": "TEXT",
    "last_seen": "TEXT",
    "raw_json": "TEXT",
    "ai_score": "INTEGER",
    "ai_verdict": "TEXT",
    "matched_json": "TEXT",
    "missing_json": "TEXT",
    "red_flags_json": "TEXT",
    "ai_reason": "TEXT",
    "analyzed_at": "TEXT",
    "profile_hash": "TEXT",
    "cover_letter": "TEXT",
    "letter_status": "TEXT",
}

_VACANCY_NORMALIZE_DEFAULTS = {
    "company": "",
    "url": "",
    "description": "",
    "tags_json": "[]",
    "remote": 0,
    "it_score": 0,
    "status": "new",
    "first_seen": None,
    "last_seen": None,
    "raw_json": "{}",
    "matched_json": "[]",
    "missing_json": "[]",
    "red_flags_json": "[]",
    "letter_status": "none",
}

_RUN_ADD_COLUMNS = {
    "known": "INTEGER",
    "analyzed": "INTEGER",
    "letters": "INTEGER",
}

_RUN_NORMALIZE_DEFAULTS = {
    "phase": "queued",
    "fetched": 0,
    "filtered": 0,
    "saved": 0,
    "known": 0,
    "analyzed": 0,
    "letters": 0,
    "errors_json": "{}",
}

_db: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_text(value, default="") -> str:
    return default if value is None else str(value)


def _safe_int(value, default=None):
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _ensure_columns(conn: aiosqlite.Connection, table: str, columns: dict[str, str]) -> None:
    cur = await conn.execute(f"SELECT * FROM {table} LIMIT 0")
    cols = {row[0] for row in cur.description}
    await cur.close()
    for name, sql_type in columns.items():
        if name not in cols:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


async def _normalize_vacancies(conn: aiosqlite.Connection) -> None:
    now = now_iso()
    for column, default in _VACANCY_NORMALIZE_DEFAULTS.items():
        value = now if default is None else default
        await conn.execute(f"UPDATE vacancies SET {column} = COALESCE({column}, :{column})", {column: value})

    await conn.execute(
        """
        UPDATE vacancies SET status = CASE status
        WHEN 'new' THEN 'new'
        WHEN 'liked' THEN 'liked'
        WHEN 'rejected' THEN 'rejected'
        WHEN 'applied' THEN 'applied'
        WHEN 'archived' THEN 'archived'
        ELSE 'new'
        END
        """
    )
    await conn.execute(
        """
        UPDATE vacancies SET letter_status = CASE letter_status
        WHEN 'none' THEN 'none'
        WHEN 'ready' THEN 'ready'
        WHEN 'failed' THEN 'failed'
        WHEN 'pending' THEN 'ready'
        WHEN 'generated' THEN 'ready'
        ELSE 'none'
        END
        """
    )


async def _normalize_runs(conn: aiosqlite.Connection) -> None:
    for column, default in _RUN_NORMALIZE_DEFAULTS.items():
        await conn.execute(f"UPDATE runs SET {column} = COALESCE({column}, :{column})", {column: default})


async def _migrate(conn: aiosqlite.Connection) -> None:
    await _ensure_columns(conn, "vacancies", _VACANCY_ADD_COLUMNS)
    await _normalize_vacancies(conn)
    await _ensure_columns(conn, "runs", _RUN_ADD_COLUMNS)
    await _normalize_runs(conn)
    await conn.commit()


async def init_db() -> aiosqlite.Connection:
    global _db
    if _db is not None:
        return _db

    async with _db_lock:
        if _db is not None:
            return _db

        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(settings.db_path))
        try:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.executescript(SCHEMA)
            await _migrate(conn)
            _db = conn
            return _db
        except Exception:
            try:
                await conn.close()
            except Exception:
                pass
            raise


async def close_db() -> None:
    global _db
    db = _db
    _db = None
    if db is None:
        return
    try:
        await db.close()
    except Exception:
        pass


async def get_db() -> aiosqlite.Connection:
    if _db is not None:
        return _db
    return await init_db()


def _parse_json_list(value) -> list[str]:
    if value is None:
        return []
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if x is not None]


def _row_to_dict(row) -> dict:
    data = dict(row) if hasattr(row, "keys") else {}

    data.setdefault("id", None)
    data.setdefault("source", "")
    data.setdefault("source_id", "")
    data.setdefault("title", "")
    data.setdefault("company", "")
    data.setdefault("url", "")

    data["source"] = _ensure_text(data.get("source"), "")
    data["source_id"] = _ensure_text(data.get("source_id"), "")
    data["title"] = _ensure_text(data.get("title"), "")
    data["company"] = _ensure_text(data.get("company"), "")
    data["url"] = _ensure_text(data.get("url"), "")
    data["description"] = _ensure_text(data.get("description"), "")
    data["location"] = _ensure_text(data.get("location"), "") if data.get("location") is not None else None
    data["currency"] = _ensure_text(data.get("currency"), "") if data.get("currency") is not None else None
    data["salary_period"] = _ensure_text(data.get("salary_period"), "") if data.get("salary_period") is not None else None
    data["published_at"] = _ensure_text(data.get("published_at"), "") if data.get("published_at") is not None else None
    data["first_seen"] = _ensure_text(data.get("first_seen"), "") if data.get("first_seen") is not None else None
    data["last_seen"] = _ensure_text(data.get("last_seen"), "") if data.get("last_seen") is not None else None
    data["ai_verdict"] = _ensure_text(data.get("ai_verdict"), "") if data.get("ai_verdict") is not None else None
    data["ai_reason"] = _ensure_text(data.get("ai_reason"), "") if data.get("ai_reason") is not None else None
    data["analyzed_at"] = _ensure_text(data.get("analyzed_at"), "") if data.get("analyzed_at") is not None else None
    data["profile_hash"] = _ensure_text(data.get("profile_hash"), "") if data.get("profile_hash") is not None else None
    data["cover_letter"] = _ensure_text(data.get("cover_letter"), "") if data.get("cover_letter") is not None else None

    data["salary_from"] = _safe_int(data.get("salary_from"), None)
    data["salary_to"] = _safe_int(data.get("salary_to"), None)
    data["it_score"] = _safe_int(data.get("it_score"), 0) or 0
    data["ai_score"] = _safe_int(data.get("ai_score"), None)

    data["remote"] = bool(data.get("remote"))

    status = _ensure_text(data.get("status"), "new")
    data["status"] = status if status in VALID_STATUS else "new"

    letter_status = _ensure_text(data.get("letter_status"), "none")
    data["letter_status"] = letter_status if letter_status in {"none", "ready", "failed"} else "none"

    for key in ("tags_json", "matched_json", "missing_json", "red_flags_json", "raw_json"):
        data.pop(key, None)

    data["tags"] = _parse_json_list(row["tags_json"] if hasattr(row, "keys") and "tags_json" in row.keys() else data.get("tags"))
    data["matched"] = _parse_json_list(row["matched_json"] if hasattr(row, "keys") and "matched_json" in row.keys() else None)
    data["missing"] = _parse_json_list(row["missing_json"] if hasattr(row, "keys") and "missing_json" in row.keys() else None)
    data["red_flags"] = _parse_json_list(row["red_flags_json"] if hasattr(row, "keys") and "red_flags_json" in row.keys() else None)

    return data


def _sql_like_term(value: str) -> str:
    return "%" + str(value).replace("%", "").replace("_", "") + "%"


def _where_sql(clauses: list[str]) -> str:
    return " AND ".join(clauses) if clauses else "1 = 1"


async def list_vacancies(
    status: str | None = None,
    source: str | None = None,
    q: str | None = None,
    remote: bool | None = None,
    page: int = 1,
    page_size: int = 100,
    min_salary: int | None = None,
) -> tuple[list[dict], int]:
    db = await get_db()
    where: list[str] = []
    params: dict[str, object] = {}

    if source and source in VALID_SOURCES:
        where.append("source = :source")
        params["source"] = source

    if status and status in VALID_STATUS:
        where.append("status = :status")
        params["status"] = status

    if q:
        like = _sql_like_term(q)
        where.append("(title LIKE :q OR description LIKE :q OR location LIKE :q)")
        params["q"] = like

    if remote is not None:
        where.append("remote = :remote")
        params["remote"] = 1 if remote else 0

    if min_salary:
        where.append(
            """
            (salary_from IS NOT NULL AND salary_from >= :min_salary) OR
            (salary_to IS NOT NULL AND salary_to >= :min_salary)
            """
        )
        params["min_salary"] = min_salary

    clause = _where_sql(where)
    count_sql = f"SELECT COUNT(*) FROM vacancies WHERE {clause}"
    cur = await db.execute(count_sql, params)
    total = _safe_int((await cur.fetchone())[0], 0) or 0
    await cur.close()

    offset = max(0, (page - 1) * page_size)
    sql = (
        f"SELECT * FROM vacancies WHERE {clause} "
        "ORDER BY COALESCE(published_at, first_seen) DESC, id DESC "
        "LIMIT :limit OFFSET :offset"
    )
    cur = await db.execute(sql, {**params, "limit": max(0, page_size), "offset": offset})
    rows = [_row_to_dict(row) for row in await cur.fetchall()]
    await cur.close()
    return rows, total


async def get_vacancy(vacancy_id: int | str) -> dict | None:
    db = await get_db()
    cur = await db.execute("SELECT * FROM vacancies WHERE id = :id", {"id": int(vacancy_id)})
    row = await cur.fetchone()
    await cur.close()
    return _row_to_dict(row) if row else None


async def set_status(vacancy_id: int | str, status: str) -> dict | None:
    if status not in VALID_STATUS:
        raise ValueError("unknown status")

    db = await get_db()
    await db.execute(
        "UPDATE vacancies SET status = :status WHERE id = :id",
        {"status": status, "id": int(vacancy_id)},
    )
    await db.commit()
    return await get_vacancy(vacancy_id)


async def upsert_vacancies(items: list[dict]) -> int:
    db = await get_db()
    now = now_iso()

    keyed: dict[tuple[str, str], dict] = {}
    for item in items:
        source = _ensure_text(item.get("source"), "")
        source_id = _ensure_text(item.get("source_id"), "")
        title = _ensure_text(item.get("title"), "")
        url = _ensure_text(item.get("url"), "")
        if source not in VALID_SOURCES or not source_id or not title or not url:
            continue

        key = (source, source_id)
        old_len = len(_ensure_text(keyed.get(key, {}).get("description"), ""))
        new_len = len(_ensure_text(item.get("description"), ""))
        if key not in keyed or new_len >= old_len:
            keyed[key] = item

    saved = 0
    for item in keyed.values():
        source = _ensure_text(item.get("source"), "")
        source_id = _ensure_text(item.get("source_id"), "")
        key = (source, source_id)

        new_tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        new_tags_json = json.dumps(new_tags, ensure_ascii=False)
        raw_json = json.dumps(item.get("raw") if isinstance(item.get("raw"), dict) else {}, ensure_ascii=False)
        remote = 1 if bool(item.get("remote")) else 0
        it_score = _safe_int(item.get("it_score"), 0) or 0
        salary_from = _safe_int(item.get("salary_from"), None)
        salary_to = _safe_int(item.get("salary_to"), None)
        currency = item.get("currency") if item.get("currency") else None
        salary_period = item.get("salary_period") if item.get("salary_period") else None
        published_at = item.get("published_at") if item.get("published_at") else None

        cur = await db.execute("SELECT * FROM vacancies WHERE source = :source AND source_id = :source_id", {"source": source, "source_id": source_id})
        row = await cur.fetchone()
        await cur.close()

        if row:
            old = _row_to_dict(row)
            old_tags_json = row["tags_json"] if hasattr(row, "keys") and "tags_json" in row.keys() else "[]"
            tags_json = new_tags_json if new_tags else _ensure_text(old_tags_json, "[]")
            old_description = _ensure_text(old.get("description"), "")
            new_description = _ensure_text(item.get("description"), "")
            description = new_description if len(new_description) > len(old_description) else old_description

            company = _ensure_text(item.get("company"), "") or _ensure_text(old.get("company"), "")
            url = _ensure_text(item.get("url"), "") or _ensure_text(old.get("url"), "")
            location = _ensure_text(item.get("location"), "") or _ensure_text(old.get("location"), "")
            salary_from = salary_from if salary_from is not None else _safe_int(old.get("salary_from"), None)
            salary_to = salary_to if salary_to is not None else _safe_int(old.get("salary_to"), None)
            currency = currency or _ensure_text(old.get("currency"), "") or None
            salary_period = salary_period or _ensure_text(old.get("salary_period"), "") or None
            published_at = published_at or _ensure_text(old.get("published_at"), "") or None
            first_seen = _ensure_text(old.get("first_seen"), "") or now
            last_seen = now
            status = _ensure_text(old.get("status"), "new") if _ensure_text(old.get("status"), "") in VALID_STATUS else "new"
            it_score = max(it_score, _safe_int(old.get("it_score"), 0) or 0)
            remote = 1 if remote or bool(old.get("remote")) else 0

            await db.execute(
                """
                UPDATE vacancies SET
                    title = :title,
                    company = :company,
                    url = :url,
                    location = :location,
                    salary_from = :salary_from,
                    salary_to = :salary_to,
                    currency = :currency,
                    salary_period = :salary_period,
                    published_at = :published_at,
                    description = :description,
                    tags_json = :tags_json,
                    remote = :remote,
                    it_score = :it_score,
                    status = :status,
                    first_seen = :first_seen,
                    last_seen = :last_seen,
                    raw_json = :raw_json
                WHERE source = :source AND source_id = :source_id
                """,
                {
                    "title": _ensure_text(item.get("title"), ""),
                    "company": company,
                    "url": url,
                    "location": location,
                    "salary_from": salary_from,
                    "salary_to": salary_to,
                    "currency": currency,
                    "salary_period": salary_period,
                    "published_at": published_at,
                    "description": description,
                    "tags_json": tags_json,
                    "remote": remote,
                    "it_score": it_score,
                    "status": status,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                    "raw_json": raw_json,
                    "source": source,
                    "source_id": source_id,
                },
            )
        else:
            tags_json = new_tags_json
            await db.execute(
                """
                INSERT INTO vacancies (
                    source, source_id, title, company, url, location, salary_from, salary_to,
                    currency, salary_period, published_at, description, tags_json, remote,
                    it_score, status, first_seen, last_seen, raw_json
                ) VALUES (
                    :source, :source_id, :title, :company, :url, :location, :salary_from, :salary_to,
                    :currency, :salary_period, :published_at, :description, :tags_json, :remote,
                    :it_score, :status, :first_seen, :last_seen, :raw_json
                )
                """,
                {
                    "source": source,
                    "source_id": source_id,
                    "title": _ensure_text(item.get("title"), ""),
                    "company": _ensure_text(item.get("company"), ""),
                    "url": _ensure_text(item.get("url"), ""),
                    "location": item.get("location") if item.get("location") else None,
                    "salary_from": salary_from,
                    "salary_to": salary_to,
                    "currency": currency,
                    "salary_period": salary_period,
                    "published_at": published_at,
                    "description": _ensure_text(item.get("description"), ""),
                    "tags_json": tags_json,
                    "remote": remote,
                    "it_score": it_score,
                    "status": "new",
                    "first_seen": now,
                    "last_seen": now,
                    "raw_json": raw_json,
                },
            )
        saved += 1

    await db.commit()
    return saved


async def touch_seen(keys: set[tuple[str, str]] | list[tuple[str, str]]) -> None:
    if not keys:
        return

    db = await get_db()
    now = now_iso()
    for key in keys:
        if not key:
            continue
        source = _ensure_text(key[0], "")
        source_id = _ensure_text(key[1], "")
        if source not in VALID_SOURCES or not source_id:
            continue
        await db.execute(
            "UPDATE vacancies SET last_seen = :last_seen WHERE source = :source AND source_id = :source_id",
            {"last_seen": now, "source": source, "source_id": source_id},
        )
    await db.commit()


async def complete_known_keys() -> set[tuple[str, str]]:
    db = await get_db()
    cur = await db.execute("SELECT source, source_id FROM vacancies WHERE description IS NOT NULL AND description != ''")
    rows = await cur.fetchall()
    await cur.close()
    return {(str(row["source"]), str(row["source_id"])) for row in rows if str(row["source"]) in VALID_SOURCES}


async def create_run(run_id: str | None = None) -> str:
    db = await get_db()
    rid = run_id or uuid4().hex
    await db.execute(
        """
        INSERT INTO runs (
            id, started_at, finished_at, phase, fetched, filtered, saved, known, analyzed, letters, errors_json, error
        ) VALUES (
            :id, :started_at, NULL, 'queued', 0, 0, 0, 0, 0, 0, '{}', NULL
        )
        """,
        {"id": rid, "started_at": now_iso()},
    )
    await db.commit()
    return rid


async def update_run(run_id: str, **fields) -> None:
    db = await get_db()
    sql: list[str] = []
    params: dict[str, object] = {"id": run_id}

    for key, value in fields.items():
        if key not in RUN_FIELDS:
            continue

        if key == "errors_json" and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)

        if key in {"fetched", "filtered", "saved", "known", "analyzed", "letters"}:
            value = _safe_int(value, 0) or 0

        sql.append(f"{key} = :{key}")
        params[key] = value

    if not sql:
        return

    await db.execute(
        f"UPDATE runs SET {', '.join(sql)} WHERE id = :id",
        params,
    )
    await db.commit()


async def get_run(run_id: str) -> dict | None:
    db = await get_db()
    cur = await db.execute("SELECT * FROM runs WHERE id = :id", {"id": run_id})
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def last_run() -> dict | None:
    db = await get_db()
    cur = await db.execute("SELECT * FROM runs ORDER BY COALESCE(started_at, id) DESC, id DESC LIMIT 1")
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def score_pending(profile_hash: str | None, limit: int) -> list[dict]:
    db = await get_db()
    where = [
        "status != 'archived'",
        "(ai_score IS NULL OR profile_hash IS NULL OR profile_hash != :profile_hash)",
    ]
    sql = (
        "SELECT * FROM vacancies WHERE "
        + _where_sql(where)
        + " ORDER BY COALESCE(published_at, first_seen) DESC, id DESC LIMIT :limit"
    )
    cur = await db.execute(sql, {"profile_hash": profile_hash, "limit": max(0, int(limit or 0))})
    rows = [_row_to_dict(row) for row in await cur.fetchall()]
    await cur.close()
    return rows


async def pending_letters(profile_hash: str | None, threshold: int, limit: int) -> list[dict]:
    db = await get_db()
    where = [
        "status != 'archived'",
        "ai_score IS NOT NULL",
        "ai_score >= :threshold",
        "(letter_status IS NULL OR letter_status = 'none' OR letter_status = 'failed' OR profile_hash IS NULL OR profile_hash != :profile_hash)",
    ]
    sql = (
        "SELECT * FROM vacancies WHERE "
        + _where_sql(where)
        + " ORDER BY COALESCE(published_at, first_seen) DESC, id DESC LIMIT :limit"
    )
    cur = await db.execute(sql, {"profile_hash": profile_hash, "threshold": int(threshold or 0), "limit": max(0, int(limit or 0))})
    rows = [_row_to_dict(row) for row in await cur.fetchall()]
    await cur.close()
    return rows


async def save_analysis(vacancy_id: int | str, analysis: dict, profile_hash: str | None) -> bool:
    score = analysis.get("score")
    if score is None:
        return False

    db = await get_db()
    await db.execute(
        """
        UPDATE vacancies SET
            ai_score = :ai_score,
            ai_verdict = :ai_verdict,
            matched_json = :matched_json,
            missing_json = :missing_json,
            red_flags_json = :red_flags_json,
            ai_reason = :ai_reason,
            analyzed_at = :analyzed_at,
            profile_hash = :profile_hash
        WHERE id = :id
        """,
        {
            "id": int(vacancy_id),
            "ai_score": _safe_int(score, None),
            "ai_verdict": analysis.get("verdict") if analysis.get("verdict") else None,
            "matched_json": json.dumps(analysis.get("matched") or [], ensure_ascii=False),
            "missing_json": json.dumps(analysis.get("missing") or [], ensure_ascii=False),
            "red_flags_json": json.dumps(analysis.get("red_flags") or [], ensure_ascii=False),
            "ai_reason": analysis.get("reason") if analysis.get("reason") else None,
            "analyzed_at": now_iso(),
            "profile_hash": profile_hash,
        },
    )
    await db.commit()
    return True


async def set_letter(vacancy_id: int | str, letter: str | None, status: str) -> None:
    status = status if status in {"ready", "failed"} else "failed"
    if status == "ready" and not _ensure_text(letter, "").strip():
        status = "failed"

    db = await get_db()
    await db.execute(
        "UPDATE vacancies SET cover_letter = :cover_letter, letter_status = :letter_status WHERE id = :id",
        {"id": int(vacancy_id), "cover_letter": letter, "letter_status": status},
    )
    await db.commit()


async def stats() -> dict:
    db = await get_db()
    total = 0
    by_source: dict[str, int] = {}
    by_status: dict[str, int] = {}
    scored = 0
    letters_ready = 0

    cur = await db.execute("SELECT COUNT(*) FROM vacancies")
    total = _safe_int((await cur.fetchone())[0], 0) or 0
    await cur.close()

    for source in SOURCE_ORDER:
        cur = await db.execute("SELECT COUNT(*) FROM vacancies WHERE source = :source", {"source": source})
        by_source[source] = _safe_int((await cur.fetchone())[0], 0) or 0
        await cur.close()

    for status in STATUS_ORDER:
        cur = await db.execute("SELECT COUNT(*) FROM vacancies WHERE status = :status", {"status": status})
        by_status[status] = _safe_int((await cur.fetchone())[0], 0) or 0
        await cur.close()

    cur = await db.execute("SELECT COUNT(*) FROM vacancies WHERE ai_score IS NOT NULL")
    scored = _safe_int((await cur.fetchone())[0], 0) or 0
    await cur.close()

    cur = await db.execute("SELECT COUNT(*) FROM vacancies WHERE letter_status = 'ready'")
    letters_ready = _safe_int((await cur.fetchone())[0], 0) or 0
    await cur.close()

    return {
        "total": total,
        "by_source": by_source,
        "by_status": by_status,
        "scored": scored,
        "letters_ready": letters_ready,
    }
