"""Хранилище SQLite (WAL): вакансии, сохранённые поиски, прогоны агента.

aiosqlite — все функции асинхронные; одно соединение на процесс
(локальное приложение, конкурентность не нужна).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite

from config import settings

# Поисковый запрос hh.ru: Junior НЕ дублируем — experience=noExperience уже фильтрует.
DEFAULT_SEARCH_TEXT = (
    '(AI OR ML OR LLM OR RAG OR Python OR Java OR TypeScript '
    'OR "машинное обучение" OR "нейросети" OR "AI-инженер" OR "Data Scientist")'
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    text TEXT NOT NULL,
    area TEXT DEFAULT '113',
    experience TEXT DEFAULT 'noExperience',
    per_page INTEGER DEFAULT 100,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS vacancies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hh_id TEXT UNIQUE NOT NULL,
    title TEXT,
    company TEXT,
    url TEXT,
    area TEXT,
    salary_from INTEGER,
    salary_to INTEGER,
    currency TEXT,
    published_at TEXT,
    description TEXT,
    score INTEGER,
    verdict TEXT,
    matched_json TEXT,
    missing_json TEXT,
    red_flags_json TEXT,
    reason TEXT,
    cover_letter TEXT,
    letter_status TEXT DEFAULT 'none',   -- none | pending | ready | failed
    status TEXT DEFAULT 'new',          -- new | liked | rejected | applied | archived
    first_seen TEXT,
    analyzed_at TEXT,
    profile_hash TEXT,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_vacancies_status ON vacancies (status);
CREATE INDEX IF NOT EXISTS idx_vacancies_score ON vacancies (score);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    phase TEXT DEFAULT 'init',          -- init | fetch | score | letters | done
    processed INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    fetched INTEGER DEFAULT 0,
    analyzed INTEGER DEFAULT 0,
    letters INTEGER DEFAULT 0,
    error TEXT
);
"""

_db: aiosqlite.Connection | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def get_db() -> aiosqlite.Connection:
    """Лениво создаёт соединение, применяет схему и сидит дефолтный поиск."""
    global _db
    if _db is None:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(settings.db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript(SCHEMA)
        cur = await conn.execute("SELECT COUNT(*) AS c FROM saved_searches")
        row = await cur.fetchone()
        if row["c"] == 0:
            await conn.execute(
                "INSERT INTO saved_searches (name, text, area, experience, per_page, active) "
                "VALUES (?, ?, '113', 'noExperience', 100, 1)",
                ("AI/ML Junior — Россия", DEFAULT_SEARCH_TEXT),
            )
        await conn.commit()
        _db = conn
    return _db


async def init_db() -> None:
    await get_db()


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


# ------------------------------------------------------------- saved_searches

async def active_searches() -> list:
    conn = await get_db()
    cur = await conn.execute("SELECT * FROM saved_searches WHERE active = 1 ORDER BY id")
    return await cur.fetchall()


# --------------------------------------------------------------- vacancies

async def vacancy_exists(hh_id: str) -> bool:
    """Дедупликация: hh_id UNIQUE — повторный запуск не создаёт дублей."""
    conn = await get_db()
    cur = await conn.execute("SELECT 1 FROM vacancies WHERE hh_id = ?", (hh_id,))
    return await cur.fetchone() is not None


async def upsert_vacancy(v: dict) -> None:
    """Сохраняет вакансию; существующие hh_id игнорируются."""
    conn = await get_db()
    await conn.execute(
        """
        INSERT OR IGNORE INTO vacancies
            (hh_id, title, company, url, area, salary_from, salary_to, currency,
             published_at, description, letter_status, status, first_seen, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'none', 'new', ?, ?)
        """,
        (
            v["hh_id"], v.get("title"), v.get("company"), v.get("url"), v.get("area"),
            v.get("salary_from"), v.get("salary_to"), v.get("currency"),
            v.get("published_at"), v.get("description"), now_iso(),
            json.dumps(v, ensure_ascii=False),
        ),
    )
    await conn.commit()


async def get_vacancy(vacancy_id: int):
    conn = await get_db()
    cur = await conn.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,))
    return await cur.fetchone()


async def list_vacancies(status: str | None = None, min_score: int | None = None,
                         q: str | None = None) -> list:
    conn = await get_db()
    sql = "SELECT * FROM vacancies"
    cond: list[str] = []
    params: list = []
    if status and status != "all":
        cond.append("status = ?")
        params.append(status)
    if min_score:
        cond.append("score >= ?")
        params.append(int(min_score))
    if q:
        cond.append("(title LIKE ? OR company LIKE ? OR description LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    # сначала оценённые (по убыванию score), потом не оценённые (свежие)
    sql += " ORDER BY (score IS NULL) ASC, score DESC, first_seen DESC"
    cur = await conn.execute(sql, params)
    return await cur.fetchall()


async def needs_analysis(profile_hash: str) -> list:
    """Вакансии, которые нужно (пере)оценить: без score или профиль изменился."""
    conn = await get_db()
    cur = await conn.execute(
        "SELECT * FROM vacancies WHERE status != 'archived' "
        "AND (score IS NULL OR profile_hash != ?)",
        (profile_hash,),
    )
    return await cur.fetchall()


async def save_analysis(vacancy_id: int, a: dict) -> None:
    """Сохраняет результат LLM-оценки (score/verdict/skills/reason)."""
    conn = await get_db()
    await conn.execute(
        """
        UPDATE vacancies SET
            score = ?, verdict = ?, matched_json = ?, missing_json = ?,
            red_flags_json = ?, reason = ?, analyzed_at = ?, profile_hash = ?
        WHERE id = ?
        """,
        (
            a.get("score"), a.get("verdict"),
            json.dumps(a.get("matched") or [], ensure_ascii=False),
            json.dumps(a.get("missing") or [], ensure_ascii=False),
            json.dumps(a.get("red_flags") or [], ensure_ascii=False),
            a.get("reason"), now_iso(), a.get("profile_hash"),
            vacancy_id,
        ),
    )
    await conn.commit()


async def needs_letter(threshold: int) -> list:
    """Ваканции с score >= threshold, которым нужно письмо."""
    conn = await get_db()
    cur = await conn.execute(
        "SELECT * FROM vacancies WHERE score IS NOT NULL AND score >= ? "
        "AND letter_status IN ('none', 'failed') AND status != 'archived'",
        (threshold,),
    )
    return await cur.fetchall()


async def set_letter(vacancy_id: int, letter: str | None, status: str) -> None:
    conn = await get_db()
    await conn.execute(
        "UPDATE vacancies SET cover_letter = ?, letter_status = ? WHERE id = ?",
        (letter, status, vacancy_id),
    )
    await conn.commit()


async def set_status(vacancy_id: int, status: str):
    conn = await get_db()
    await conn.execute("UPDATE vacancies SET status = ? WHERE id = ?", (status, vacancy_id))
    await conn.commit()
    return await get_vacancy(vacancy_id)


async def reset_analysis(vacancy_id: int) -> None:
    """Сбрасывает оценку и письмо — для кнопки «Анализ заново»."""
    conn = await get_db()
    await conn.execute(
        """
        UPDATE vacancies SET score = NULL, verdict = NULL, matched_json = NULL,
            missing_json = NULL, red_flags_json = NULL, reason = NULL,
            analyzed_at = NULL, cover_letter = NULL, letter_status = 'none',
            status = 'new'
        WHERE id = ?
        """,
        (vacancy_id,),
    )
    await conn.commit()


# --------------------------------------------------------------------- runs

async def create_run(run_id: str, started_at: str) -> None:
    conn = await get_db()
    await conn.execute("INSERT INTO runs (id, started_at) VALUES (?, ?)", (run_id, started_at))
    await conn.commit()


async def update_run(run_id: str, **fields) -> None:
    """Динамическое обновление полей runs (phase/processed/total/error/...)."""
    if not fields:
        return
    conn = await get_db()
    keys = ", ".join(f"{k} = ?" for k in fields)
    await conn.execute(f"UPDATE runs SET {keys} WHERE id = ?", (*fields.values(), run_id))
    await conn.commit()


async def get_run(run_id: str):
    conn = await get_db()
    cur = await conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
    return await cur.fetchone()


async def last_run():
    conn = await get_db()
    cur = await conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")
    return await cur.fetchone()
