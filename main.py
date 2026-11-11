"""FastAPI backend приложения «AI Job Hunter»."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import aiosqlite
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from scraper import scrape_all

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = (BASE_DIR / os.getenv("DB_PATH", "data/jobs.db")).resolve()

VALID_STATUS = {"new", "liked", "rejected", "applied", "archived"}
VALID_SOURCES = {"hh", "habr", "remoteok"}
RUN_FIELDS = {
    "phase",
    "fetched",
    "filtered",
    "saved",
    "errors_json",
    "error",
    "finished_at",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL CHECK (source IN ('hh', 'habr', 'remoteok')),
    source_id     TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL,
    location      TEXT,
    salary_from   INTEGER,
    salary_to     INTEGER,
    currency      TEXT,
    salary_period TEXT CHECK (salary_period IN ('month', 'year') OR salary_period IS NULL),
    published_at  TEXT,
    description   TEXT NOT NULL DEFAULT '',
    tags_json     TEXT NOT NULL DEFAULT '[]',
    remote        INTEGER NOT NULL DEFAULT 0 CHECK (remote IN (0, 1)),
    it_score      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'new'
                  CHECK (status IN ('new', 'liked', 'rejected', 'applied', 'archived')),
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    raw_json      TEXT,
    UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_vacancies_source     ON vacancies (source);
CREATE INDEX IF NOT EXISTS idx_vacancies_status     ON vacancies (status);
CREATE INDEX IF NOT EXISTS idx_vacancies_score      ON vacancies (it_score);
CREATE INDEX IF NOT EXISTS idx_vacancies_published  ON vacancies (published_at);

CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    phase        TEXT NOT NULL DEFAULT 'queued',
    fetched      INTEGER NOT NULL DEFAULT 0,
    filtered     INTEGER NOT NULL DEFAULT 0,
    saved        INTEGER NOT NULL DEFAULT 0,
    errors_json  TEXT NOT NULL DEFAULT '[]',
    error        TEXT
);
"""

UPSERT_SQL = """
INSERT INTO vacancies (
    source, source_id, title, company, url, location,
    salary_from, salary_to, currency, salary_period, published_at,
    description, tags_json, remote, it_score, status,
    first_seen, last_seen, raw_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)
ON CONFLICT(source, source_id) DO UPDATE SET
    title = excluded.title,
    company = excluded.company,
    url = excluded.url,
    location = excluded.location,
    salary_from = excluded.salary_from,
    salary_to = excluded.salary_to,
    currency = excluded.currency,
    salary_period = excluded.salary_period,
    published_at = excluded.published_at,
    description = excluded.description,
    tags_json = excluded.tags_json,
    remote = excluded.remote,
    it_score = excluded.it_score,
    last_seen = excluded.last_seen,
    raw_json = excluded.raw_json
"""

_db: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()
_write_lock = asyncio.Lock()
_run_task: asyncio.Task | None = None
_run_id: str | None = None
_scrape_lock = asyncio.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is not None:
        return _db

    async with _db_lock:
        if _db is not None:
            return _db

        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(DB_PATH))
        try:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("PRAGMA foreign_keys=ON")
            _db = conn
        except Exception:
            await conn.close()
            raise
        return _db


async def init_db() -> None:
    db = await get_db()
    await db.executescript(SCHEMA)
    await db.commit()


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def upsert_vacancies(items: list[dict]) -> int:
    rows: list[tuple] = []
    now = now_iso()

    for item in items:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        source_id = item.get("source_id")
        title = item.get("title")
        url = item.get("url")
        if source not in VALID_SOURCES or not source_id or not title or not url:
            continue

        tags = item.get("tags")
        if not isinstance(tags, list):
            tags = []
        try:
            tags_json = json.dumps(tags, ensure_ascii=False)
        except (TypeError, ValueError):
            tags_json = "[]"

        raw = item.get("raw")
        if not isinstance(raw, dict):
            raw = {}
        try:
            raw_json = json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError):
            raw_json = "{}"

        salary_period = item.get("salary_period")
        if salary_period not in ("month", "year"):
            salary_period = None

        try:
            it_score = int(item.get("it_score") or 0)
        except (TypeError, ValueError):
            it_score = 0

        rows.append(
            (
                source,
                str(source_id),
                str(title),
                str(item.get("company") or ""),
                str(url),
                item.get("location"),
                item.get("salary_from"),
                item.get("salary_to"),
                item.get("currency"),
                salary_period,
                item.get("published_at"),
                str(item.get("description") or ""),
                tags_json,
                1 if item.get("remote") else 0,
                it_score,
                now,
                now,
                raw_json,
            )
        )

    if not rows:
        return 0

    async with _write_lock:
        db = await get_db()
        try:
            await db.executemany(UPSERT_SQL, rows)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return len(rows)


def _serialize(row: aiosqlite.Row) -> dict:
    data = dict(row)
    try:
        tags = json.loads(data.get("tags_json") or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        tags = []
    data["tags"] = tags if isinstance(tags, list) else []
    data["remote"] = bool(data.get("remote"))
    data.pop("tags_json", None)
    data.pop("raw_json", None)
    return data


async def list_vacancies(
    page: int,
    page_size: int,
    source: str | None = None,
    status: str | None = None,
    q: str | None = None,
    remote: bool | None = None,
    min_salary: int | None = None,
) -> tuple[list, int]:
    db = await get_db()
    where: list[str] = []
    params: list = []

    if source:
        where.append("source = ?")
        params.append(source)
    if status:
        where.append("status = ?")
        params.append(status)
    if q:
        pattern = f"%{q}%"
        where.append("(title LIKE ? OR company LIKE ? OR description LIKE ?)")
        params.extend([pattern, pattern, pattern])
    if remote is not None:
        where.append("remote = ?")
        params.append(1 if remote else 0)
    if min_salary is not None:
        where.append("(salary_from >= ? OR salary_to >= ?)")
        params.extend([min_salary, min_salary])

    where_sql = f" WHERE {' AND '.join(where)}" if where else ""

    count_cursor = await db.execute(
        f"SELECT COUNT(*) AS total FROM vacancies{where_sql}", params
    )
    count_row = await count_cursor.fetchone()
    await count_cursor.close()
    total = int(count_row["total"]) if count_row else 0

    offset = (page - 1) * page_size
    cursor = await db.execute(
        f"""
        SELECT * FROM vacancies{where_sql}
        ORDER BY (published_at IS NULL) ASC, published_at DESC, first_seen DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, page_size, offset],
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return rows, total


async def get_vacancy(vacancy_id: int):
    db = await get_db()
    cursor = await db.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,))
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def set_status(vacancy_id: int, status: str):
    async with _write_lock:
        db = await get_db()
        try:
            cursor = await db.execute(
                "UPDATE vacancies SET status = ? WHERE id = ?",
                (status, vacancy_id),
            )
            await db.commit()
            changed = cursor.rowcount
            await cursor.close()
        except Exception:
            await db.rollback()
            raise
    if not changed:
        return None
    return await get_vacancy(vacancy_id)


async def stats() -> dict:
    db = await get_db()
    by_source = {name: 0 for name in VALID_SOURCES}
    by_status = {name: 0 for name in VALID_STATUS}

    cursor = await db.execute(
        "SELECT source, COUNT(*) AS cnt FROM vacancies GROUP BY source"
    )
    for row in await cursor.fetchall():
        if row["source"] in by_source:
            by_source[row["source"]] = int(row["cnt"])
    await cursor.close()

    cursor = await db.execute(
        "SELECT status, COUNT(*) AS cnt FROM vacancies GROUP BY status"
    )
    for row in await cursor.fetchall():
        if row["status"] in by_status:
            by_status[row["status"]] = int(row["cnt"])
    await cursor.close()

    return {
        "total": sum(by_source.values()),
        "by_source": by_source,
        "by_status": by_status,
    }


async def create_run(run_id: str) -> None:
    async with _write_lock:
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO runs (id, started_at, phase) VALUES (?, ?, 'queued')",
                (run_id, now_iso()),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def update_run(run_id: str, **fields) -> None:
    invalid = set(fields) - RUN_FIELDS
    if invalid:
        raise ValueError(f"Недопустимые поля прогона: {', '.join(sorted(invalid))}")
    if not fields:
        return

    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = [value for value in fields.values()]

    async with _write_lock:
        db = await get_db()
        try:
            await db.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?",
                [*values, run_id],
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def get_run(run_id: str):
    db = await get_db()
    cursor = await db.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def last_run():
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def _run_scrape(run_id: str) -> None:
    async def on_progress(phase: str, fetched: int, filtered: int) -> None:
        await update_run(run_id, phase=phase, fetched=fetched, filtered=filtered)

    try:
        report = await scrape_all(on_progress)
        await update_run(
            run_id,
            phase="save",
            fetched=report.fetched,
            filtered=report.filtered,
            errors_json=json.dumps(report.errors, ensure_ascii=False),
        )
        saved = await upsert_vacancies(report.items)
        await update_run(
            run_id,
            phase="done",
            finished_at=now_iso(),
            saved=saved,
        )
    except Exception as exc:
        log.exception("Прогон сбора %s завершился ошибкой", run_id)
        await update_run(
            run_id,
            phase="error",
            finished_at=now_iso(),
            error=str(exc)[:1000],
        )


class StatusUpdate(BaseModel):
    status: str


class VacancyOut(BaseModel):
    id: int
    source: Literal["hh", "habr", "remoteok"]
    source_id: str
    title: str
    company: str
    url: str
    location: str | None = None
    salary_from: int | None = None
    salary_to: int | None = None
    currency: str | None = None
    salary_period: Literal["month", "year"] | None = None
    published_at: str | None = None
    description: str
    tags: list[str] = Field(default_factory=list)
    remote: bool
    it_score: int
    status: Literal["new", "liked", "rejected", "applied", "archived"]
    first_seen: str
    last_seen: str


class PageOut(BaseModel):
    items: list[VacancyOut]
    page: int
    page_size: int
    total: int
    pages: int


class RunOut(BaseModel):
    id: str
    started_at: str
    finished_at: str | None = None
    phase: str
    fetched: int
    filtered: int
    saved: int
    errors_json: str
    error: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    task = _run_task
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await close_db()


app = FastAPI(title="AI Job Hunter", version="2.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "templates" / "index.html")


@app.get("/api/vacancies", response_model=PageOut)
async def api_vacancies(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    source: str | None = None,
    status: str | None = None,
    q: str | None = Query(None, max_length=200),
    remote: bool | None = None,
    min_salary: int | None = Query(None, ge=0),
):
    if source is not None and source not in VALID_SOURCES:
        raise HTTPException(400, f"Недопустимый источник: {source}")
    if status is not None and status not in VALID_STATUS:
        raise HTTPException(400, f"Недопустимый статус: {status}")

    rows, total = await list_vacancies(
        page=page,
        page_size=page_size,
        source=source,
        status=status,
        q=q.strip() if q else None,
        remote=remote,
        min_salary=min_salary,
    )
    pages = (total + page_size - 1) // page_size if total else 0
    return {
        "items": [_serialize(row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
    }


@app.get("/api/vacancies/{vacancy_id}", response_model=VacancyOut)
async def api_vacancy(vacancy_id: int):
    row = await get_vacancy(vacancy_id)
    if row is None:
        raise HTTPException(404, "vacancy не найдена")
    return _serialize(row)


@app.patch("/api/vacancies/{vacancy_id}", response_model=VacancyOut)
async def api_set_status(vacancy_id: int, payload: StatusUpdate):
    if payload.status not in VALID_STATUS:
        raise HTTPException(400, f"Недопустимый статус: {payload.status}")
    row = await set_status(vacancy_id, payload.status)
    if row is None:
        raise HTTPException(404, "vacancy не найдена")
    return _serialize(row)


@app.post("/api/scrape")
async def api_scrape():
    global _run_task, _run_id

    async with _scrape_lock:
        if _run_task is not None and not _run_task.done():
            return {"run_id": _run_id, "started": False}

        rid = uuid.uuid4().hex[:12]
        await create_run(rid)
        _run_id = rid
        _run_task = asyncio.create_task(_run_scrape(rid))
        return {"run_id": rid, "started": True}


@app.get("/api/runs/{run_id}", response_model=RunOut)
async def api_run(run_id: str):
    row = await get_run(run_id)
    if row is None:
        raise HTTPException(404, "run не найден")
    return dict(row)


@app.get("/api/stats")
async def api_stats():
    return await stats()


@app.get("/api/health")
async def api_health():
    current = await stats()
    row = await last_run()
    return {
        "status": "ok",
        "total": current["total"],
        "last_run": dict(row) if row is not None else None,
    }
