"""FastAPI backend for AI Job Hunter."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import settings, load_profile, profile_hash
from database import (
    close_db,
    complete_known_keys,
    create_run,
    get_vacancy,
    init_db,
    last_run,
    get_run,
    list_vacancies,
    now_iso,
    pending_letters,
    save_analysis,
    score_pending,
    set_letter,
    set_status,
    stats,
    touch_seen,
    update_run,
    upsert_vacancies,
)
from scraper import scrape_all
from ai_agent import analyze_vacancy, check_ollama, generate_letter

BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")

_ai_health_cache: dict | None = None
_ai_health_ts: float | None = None
_profile_cache: tuple[dict | None, str | None] | None = None
_profile_ts: float | None = None
_run_task: asyncio.Task | None = None
_run_id: str | None = None
_scheduler = None

_SCRAPE_LOCK = asyncio.Lock()


async def _get_profile_cached(force: bool = False) -> tuple[dict | None, str | None]:
    global _profile_cache, _profile_ts

    now = time.monotonic()
    if not force and _profile_cache is not None and _profile_ts is not None:
        if now - _profile_ts < 30.0:
            return _profile_cache

    try:
        prof = load_profile()
        result = (prof, profile_hash(prof))
    except Exception as exc:
        log.warning("AI: load profile failed: %s", exc)
        result = (None, None)

    _profile_ts = now
    _profile_cache = result
    return result


async def _ai_health() -> dict:
    global _ai_health_cache, _ai_health_ts

    now = time.monotonic()
    if _ai_health_cache is not None and _ai_health_ts is not None:
        if now - _ai_health_ts < 10.0:
            return _ai_health_cache

    try:
        check = await check_ollama()
    except Exception as exc:
        check = {"online": False, "models": [], "configured": False, "error": str(exc)[:200]}

    profile, current_hash = await _get_profile_cached(force=False)
    online = bool(check.get("online"))
    configured = bool(check.get("configured"))
    ready = online and configured and profile is not None

    if ready:
        ai_status = "ready"
    elif not online:
        ai_status = "offline"
    elif not configured:
        ai_status = "no model"
    else:
        ai_status = "no profile"

    result = {
        "ai_status": ai_status,
        "ai_ready": ready,
        "ai_model": settings.ollama_model,
        "ai_error": check.get("error") if not online else None,
    }

    _ai_health_ts = now
    _ai_health_cache = result
    return result


async def _ai_pipeline(run_id: str) -> tuple[int, int]:
    health = await _ai_health()
    if not health["ai_ready"]:
        return 0, 0

    profile, current_hash = await _get_profile_cached(force=True)
    if profile is None:
        return 0, 0

    await update_run(run_id, phase="score")
    analyzed = 0

    for vac in await score_pending(current_hash, settings.max_score_per_run):
        try:
            analysis = await analyze_vacancy(profile, vac)
        except Exception as exc:
            log.warning("AI scoring failed for %s: %s", vac.get("id"), exc)
            continue

        if await save_analysis(vac["id"], analysis, current_hash):
            analyzed += 1

    await update_run(run_id, phase="letters")
    letters = 0

    for vac in await pending_letters(current_hash, settings.score_threshold, settings.max_letters_per_run):
        letter, error = await generate_letter(profile, vac)
        if letter and not error:
            await set_letter(vac["id"], letter, "ready")
            letters += 1
        else:
            await set_letter(vac["id"], None, "failed")

    if analyzed:
        await update_run(run_id, analyzed=analyzed)
    if letters:
        await update_run(run_id, letters=letters)

    return analyzed, letters


async def _run_pipeline(run_id: str) -> None:
    async def on_progress(phase: str, fetched: int, filtered: int, known: int) -> None:
        await update_run(run_id, phase=phase, fetched=fetched, filtered=filtered, known=known)

    try:
        known_keys = await complete_known_keys()
        report = await scrape_all(on_progress, known_keys=known_keys)

        known = getattr(report, "known", set()) or set()
        await touch_seen(known)

        saved = await upsert_vacancies(report.items)
        known_count = len(known)
        errors = getattr(report, "errors", {}) or {}

        await update_run(
            run_id,
            phase="score",
            fetched=report.fetched,
            filtered=report.filtered,
            saved=saved,
            known=known_count,
            errors_json=errors,
        )

        analyzed, letters = await _ai_pipeline(run_id)

        await update_run(
            run_id,
            phase="done",
            finished_at=now_iso(),
            fetched=report.fetched,
            filtered=report.filtered,
            saved=saved,
            known=known_count,
            analyzed=analyzed,
            letters=letters,
        )
    except Exception as exc:
        log.exception("Run %s failed", run_id, exc_info=True)
        await update_run(
            run_id,
            phase="error",
            finished_at=now_iso(),
            error=str(exc)[:300],
        )


async def _start_run_task() -> tuple[str, bool]:
    global _run_task, _run_id

    async with _SCRAPE_LOCK:
        if _run_task is not None and not _run_task.done():
            return _run_id or "", False

        rid = await create_run()
        _run_id = rid
        _run_task = asyncio.create_task(_run_pipeline(rid))
        return rid, True


async def _scheduled_run() -> None:
    try:
        await _start_run_task()
    except Exception:
        log.exception("Scheduled run start failed", exc_info=True)


async def _shutdown_scheduler() -> None:
    global _run_task, _scheduler

    if _run_task is not None and not _run_task.done():
        _run_task.cancel()
        with suppress(asyncio.CancelledError):
            await _run_task

    sched = _scheduler
    if sched is not None:
        try:
            sched.shutdown()
        except Exception:
            log.exception("Scheduler shutdown failed", exc_info=True)


class StatusUpdate(BaseModel):
    status: str


class VacancyOut(BaseModel):
    id: int
    source: str
    source_id: str
    title: str
    company: str
    url: str
    location: str | None = None
    salary_from: int | None = None
    salary_to: int | None = None
    currency: str | None = None
    salary_period: str | None = None
    published_at: str | None = None
    description: str
    tags: list[str] = Field(default_factory=list)
    remote: bool
    it_score: int
    status: str
    first_seen: str | None = None
    last_seen: str | None = None
    ai_score: int | None = None
    ai_verdict: str | None = None
    matched: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    ai_reason: str | None = None
    analyzed_at: str | None = None
    profile_hash: str | None = None
    cover_letter: str | None = None
    letter_status: str | None = None


class PageOut(BaseModel):
    items: list[VacancyOut]
    page: int
    page_size: int
    total: int
    pages: int


class RunOut(BaseModel):
    id: str
    started_at: str | None = None
    finished_at: str | None = None
    phase: str
    fetched: int = 0
    filtered: int = 0
    saved: int = 0
    known: int = 0
    analyzed: int = 0
    letters: int = 0
    errors_json: str = "{}"
    error: str | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _scheduler

    await init_db()

    if settings.scheduler_enabled:
        try:
            from scheduler import start_scheduler

            _scheduler = start_scheduler(_scheduled_run)
        except Exception:
            log.exception("Scheduler startup failed", exc_info=True)
            _scheduler = None

    try:
        yield
    finally:
        await _shutdown_scheduler()
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
    if source is not None and source not in {"hh", "habr", "remoteok"}:
        raise HTTPException(400, "unsupported source value")
    if status is not None and status not in {"new", "liked", "rejected", "applied", "archived"}:
        raise HTTPException(400, "unsupported status value")

    rows, total = await list_vacancies(
        status=status,
        source=source,
        q=q.strip() if q else None,
        remote=remote,
        page=page,
        page_size=page_size,
        min_salary=min_salary,
    )
    pages = (total + page_size - 1) // page_size if total else 0
    return {
        "items": rows,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
    }


@app.get("/api/vacancies/{vacancy_id}", response_model=VacancyOut)
async def api_vacancy(vacancy_id: int):
    row = await get_vacancy(vacancy_id)
    if row is None:
        raise HTTPException(404, "vacancy not found")
    return row


@app.patch("/api/vacancies/{vacancy_id}", response_model=VacancyOut)
async def api_set_status(vacancy_id: int, payload: StatusUpdate):
    if payload.status not in {"new", "liked", "rejected", "applied", "archived"}:
        raise HTTPException(400, "unsupported status value")
    row = await set_status(vacancy_id, payload.status)
    if row is None:
        raise HTTPException(404, "vacancy not found")
    return row


@app.post("/api/scrape")
async def api_scrape():
    run_id, started = await _start_run_task()
    return {"run_id": run_id, "started": started}


@app.get("/api/runs/{run_id}", response_model=RunOut)
async def api_run(run_id: str):
    row = await get_run(run_id)
    if row is None:
        raise HTTPException(404, "run not found")
    return row


@app.get("/api/stats")
async def api_stats():
    return await stats()


@app.get("/api/health")
async def api_health():
    current = await stats()
    run = await last_run()
    ai = await _ai_health()
    return {
        "status": "ok",
        "total": current["total"],
        "by_source": current["by_source"],
        "by_status": current["by_status"],
        "scored": current["scored"],
        "letters_ready": current["letters_ready"],
        "last_run": run,
        "ai_status": ai["ai_status"],
        "ai_ready": ai["ai_ready"],
        "ai_model": ai["ai_model"],
        "ai_error": ai["ai_error"],
    }
