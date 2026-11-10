"""Точка входа: FastAPI-приложение «AI Job Hunter».

Запуск:
    uvicorn main:app --reload --port 8000

Разделение логики:
    POST /api/run-agent                — ТОЛЬКО сбор вакансий с hh.ru и сохранение в БД
    POST /api/vacancies/{id}/generate-letter — LLM-письмо по одной вакансии (по кнопке)

Важно: run_id создаётся ОДИН раз в api_run_agent и пробрасывается в
run_agent() — фронт опрашивает именно его по GET /api/run/{run_id}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database as db
from ai_agent import check_ollama, generate_letter
from config import BASE_DIR, load_profile, profile_hash, settings
from scheduler import start_scheduler
from scraper import fetch_details, fetch_search, new_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

VALID_STATUS = {"new", "liked", "rejected", "applied", "archived"}

# Глобальный «бегунок» — одновременно только один прогон.
_running: dict = {"task": None, "run_id": None}


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


# ------------------------------------------------------------------ pipeline

async def run_agent(run_id: str) -> dict:
    """Прогон сбора вакансий. Возвращает сводку. НЕ бросает исключения наружу:
    строка runs всегда получает finished_at (и error при сбое) —
    фронт опрашивает GET /api/run/{run_id} до finished_at.
    """
    started = time.time()
    summary: dict = {"run_id": run_id, "fetched": 0, "new": 0, "errors": []}
    await db.create_run(run_id, db.now_iso())
    try:
        await _pipeline(run_id, summary)
        await db.update_run(run_id, phase="done", finished_at=db.now_iso())
    except Exception as e:  # noqa: BLE001 — не роняем сервер, пишем ошибку в run
        log.exception("прогон %s упал", run_id)
        summary["errors"].append(str(e))
        await db.update_run(run_id, phase="done", finished_at=db.now_iso(), error=str(e)[:500])
    log.info("прогон %s завершён за %.1fs", run_id, time.time() - started)
    return summary


async def _pipeline(run_id: str, summary: dict) -> None:
    """Шаги прогона: только сбор (hh.ru) и сохранение в БД. LLM здесь не вызывается."""
    # --- 1. сбор
    await db.update_run(run_id, phase="fetch")
    searches = await db.active_searches()
    collected: list[dict] = []
    async with new_session() as session:
        for s in searches:
            try:
                collected.extend(await fetch_search(s, session=session))
            except Exception as e:  # noqa: BLE001
                log.error("поиск %r: %s", s["name"], e)
                summary["errors"].append(f"search {s['name']}: {e}")
        summary["fetched"] = len(collected)

        # дедуп по hh_id (и внутри батча, и с БД)
        seen: set[str] = set()
        fresh: list[dict] = []
        for v in collected:
            if v["hh_id"] not in seen:
                seen.add(v["hh_id"])
                fresh.append(v)
        new = [v for v in fresh if not await db.vacancy_exists(v["hh_id"])]
        summary["new"] = len(new)

        # --- 2. детали (полное описание) + сохранение
        sem = asyncio.Semaphore(3)

        async def save_one(v: dict) -> None:
            async with sem:
                detail = await fetch_details(v["hh_id"], session=session)
                if detail and detail.get("description"):
                    v["description"] = detail["description"]
                await db.upsert_vacancy(v)

        if new:
            await asyncio.gather(*[save_one(v) for v in new])
        await db.update_run(run_id, fetched=len(new), phase="done")


# -------------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    if settings.scheduler_enabled:
        async def scheduled():
            # не запускаем, если уже идёт прогон
            if _running["task"] is None or _running["task"].done():
                rid = _new_run_id()
                _running["run_id"] = rid
                _running["task"] = asyncio.create_task(run_agent(rid))
        scheduler = start_scheduler(scheduled)
    else:
        scheduler = None
    yield
    if scheduler:
        scheduler.shutdown(wait=False)
    await db.close_db()


app = FastAPI(title="AI Job Hunter", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
async def index():
    """Дашборд (templates/index.html)."""
    return FileResponse(BASE_DIR / "templates" / "index.html")


@app.get("/api/health")
async def health():
    """Статус приложения + доступность Ollama."""
    ollama = await check_ollama()
    last = await db.last_run()
    return {
        "app": "ok",
        "ollama_online": ollama["online"],
        "ollama_configured": ollama.get("configured", False),
        "ollama_models": ollama.get("models", []),
        "model": settings.ollama_model,
        "threshold": settings.score_threshold,
        "last_run": dict(last) if last else None,
    }


@app.get("/api/profile")
async def api_profile():
    """Активный профиль кандидата (для футера дашборда)."""
    try:
        p = load_profile()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e))
    return {"profile": p, "hash": profile_hash(p), "threshold": settings.score_threshold}


@app.get("/api/vacancies")
async def api_vacancies(status: str | None = None, min_score: int | None = None,
                        q: str | None = None):
    """Вакансии из БД (JSON) с фильтрами."""
    rows = await db.list_vacancies(status=status, min_score=min_score, q=q)
    return [serialize(v) for v in rows]


@app.post("/api/run-agent")
async def api_run_agent():
    """Запускает СБОР вакансий в фоне и сразу возвращает run_id (UI не зависает).

    Только hh.ru + сохранение в БД — LLM в этом эндпоинте не вызывается
    (письма генерируются по одной кнопкой /api/vacancies/{id}/generate-letter).

    ВАЖНО: run_id создаётся здесь ОДИН раз и пробрасывается в run_agent() —
    именно его фронт потом опрашивает по GET /api/run/{run_id}.
    """
    task = _running["task"]
    if task is not None and not task.done():
        return {"run_id": _running["run_id"], "started": False,
                "message": "сбор уже идёт"}
    rid = _new_run_id()
    _running["run_id"] = rid
    _running["task"] = asyncio.create_task(run_agent(rid))
    return {"run_id": rid, "started": True}


@app.get("/api/run/{run_id}")
async def api_run(run_id: str):
    """Статус прогона: phase, fetched, finished_at, error.
    Опрашивается фронтом каждые 1,5 с, пока finished_at пуст."""
    row = await db.get_run(run_id)
    if row is None:
        raise HTTPException(404, "run не найден")
    return dict(row)


class StatusIn(BaseModel):
    status: str


@app.patch("/api/vacancies/{vacancy_id}")
async def api_set_status(vacancy_id: int, body: StatusIn):
    """Триаж: new / liked / rejected / applied / archived."""
    if body.status not in VALID_STATUS:
        raise HTTPException(400, f"status должен быть из {sorted(VALID_STATUS)}")
    row = await db.set_status(vacancy_id, body.status)
    if row is None:
        raise HTTPException(404, "vacancy не найдена")
    return serialize(row)


@app.get("/api/vacancies/{vacancy_id}")
async def api_vacancy_one(vacancy_id: int):
    """Одна вакансия из БД."""
    row = await db.get_vacancy(vacancy_id)
    if row is None:
        raise HTTPException(404, "vacancy не найдена")
    return serialize(row)


@app.post("/api/vacancies/{vacancy_id}/generate-letter")
async def api_generate_letter(vacancy_id: int):
    """Генерация письма по ОДНОЙ вакансии (по кнопке в карточке).

    Берёт текст вакансии из БД -> ai_agent.generate_letter -> сохраняет в БД
    -> возвращает текст письма. Одна генерация за раз не перегружает GPU.
    """
    row = await db.get_vacancy(vacancy_id)
    if row is None:
        raise HTTPException(404, "vacancy не найдена")
    try:
        profile = load_profile()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"профиль: {e}")
    await db.set_letter(vacancy_id, None, "pending")
    letter, err = await generate_letter(profile, row)
    if letter and err is None:
        await db.set_letter(vacancy_id, letter, "ready")
        return {"ok": True, "letter": letter, "letter_status": "ready"}
    await db.set_letter(vacancy_id, None, "failed")
    log.warning("письмо %s не удалось: %s", vacancy_id, err)
    raise HTTPException(502, err or "LLM не вернула письмо (Ollama недоступна?)")


def serialize(row) -> dict:
    """Row → JSON-совместимый словарь (распаковываем *_json поля)."""

    def _load(key: str):
        raw = row[key]
        if not raw:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []

    d = dict(row)
    d["matched"] = _load("matched_json")
    d["missing"] = _load("missing_json")
    d["red_flags"] = _load("red_flags_json")
    for k in ("matched_json", "missing_json", "red_flags_json", "raw_json"):
        d.pop(k, None)
    return d
