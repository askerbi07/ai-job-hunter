"""Basic tests for cache retention / clear functions against a throwaway SQLite DB.

Run standalone (no pytest needed):  python tests/test_cache.py
Or under pytest:                   pytest tests/test_cache.py -v
"""
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import database as db


def _old_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


async def _seed(conn):
    """Insert a mix of vacancy statuses/ages plus two runs (one old, one recent)."""
    rows = [
        ("hh", "v1", "new", _old_iso(60)),      # old -> removed by clear_cache AND purge
        ("hh", "v2", "rejected", _old_iso(60)),  # old -> removed by both
        ("hh", "v3", "archived", _old_iso(60)),  # old -> removed by both
        ("hh", "v4", "liked", _old_iso(90)),     # old but liked -> kept by both
        ("hh", "v5", "applied", _old_iso(90)),   # old but applied -> kept by both
        ("hh", "v6", "new", _old_iso(3)),        # recent new -> kept by purge, removed by clear_cache
    ]
    for source, sid, status, seen in rows:
        await conn.execute(
            "INSERT INTO vacancies (source, source_id, title, status, first_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            (source, sid, f"Vac {sid}", status, seen),
        )
    await conn.execute("INSERT INTO runs (id, started_at) VALUES ('old-run', ?)", (_old_iso(60),))
    await conn.execute("INSERT INTO runs (id, started_at) VALUES ('new-run', ?)", (_old_iso(1),))
    await conn.commit()


async def _fresh_db(tmp: Path):
    db._db = None  # reset module-level connection so init uses the override path
    db.set_db_path(str(tmp / "test.db"))
    return await db.init_db()


def test_clear_cache():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            conn = await _fresh_db(Path(d))
            await _seed(conn)

            result = await db.clear_cache(max_age_days=30)

            # clear_cache removes new/rejected/archived (v1,v2,v3,v6) -> 4 vacancies,
            # keeps liked/applied (v4,v5). Runs older than 30d: only 'old-run' -> 1.
            assert result["deleted_vacancies"] == 4, result
            assert result["deleted_runs"] == 1, result

            cur = await conn.execute("SELECT status FROM vacancies ORDER BY source_id")
            kept = [r[0] for r in await cur.fetchall()]
            assert sorted(kept) == ["applied", "liked"], kept

            cur = await conn.execute("SELECT id FROM runs ORDER BY id")
            run_ids = [r[0] for r in await cur.fetchall()]
            assert run_ids == ["new-run"], run_ids

            await db.close_db()

    asyncio.run(run())


def test_purge_old_vacancies():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            conn = await _fresh_db(Path(d))
            await _seed(conn)

            result = await db.purge_old_vacancies(max_age_days=30)

            # purge removes non-liked/non-applied older than 30d: v1,v2,v3 (60d old).
            # v6 is only 3 days old -> kept. liked/applied always kept.
            assert result["deleted_vacancies"] == 3, result

            cur = await conn.execute("SELECT source_id FROM vacancies ORDER BY source_id")
            kept_ids = [r[0] for r in await cur.fetchall()]
            assert sorted(kept_ids) == ["v4", "v5", "v6"], kept_ids

            await db.close_db()

    asyncio.run(run())


if __name__ == "__main__":
    test_clear_cache()
    test_purge_old_vacancies()
    print("OK: clear_cache + purge_old_vacancies tests passed")
