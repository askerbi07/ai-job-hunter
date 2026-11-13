"""LLM-часть: оценка вакансии и генерация письма через OpenAI-совместимый API
(Ollama / LM Studio, base_url из .env).

Надёжность:
- строгий JSON-контракт + репарация (срез ```json, поиск первого {...});
- ретрай с напоминанием «верни только JSON»;
- regex-фолбэк для score, если JSON не спасти;
- функции НЕ бросают исключения наружу — пайплайн не падает никогда.
"""
from __future__ import annotations

import json
import logging
import re

from openai import AsyncOpenAI, BadRequestError

from config import settings

log = logging.getLogger("ai")

_client: AsyncOpenAI | None = None
DESC_LIMIT = 4000  # сколько символов описания отправляем в LLM (экономия контекста)
MAX_TOKENS = 2048  # generation limit: prevents JSON and letter truncation
LLM_TIMEOUT = 120.0  # hard per-request ceiling so a hung LM Studio can't block the pipeline for 10 minutes


def client() -> AsyncOpenAI:
    """Лениво создаёт общий клиент (openai SDK, OpenAI-совместимый endpoint)."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=settings.ollama_base_url,
            api_key=settings.ollama_api_key,
            timeout=LLM_TIMEOUT,  # bounded wait; a stuck local model fails fast instead of hanging
            max_retries=0,   # ретраи делаем сами, с ремонтом промпта
        )
    return _client


async def check_ollama() -> dict:
    """Проверяет доступность LLM (лёгкий GET /v1/models, таймаут 3 с)."""
    try:
        res = await client().models.list(timeout=3.0)
        models = [m.id for m in res.data]
        return {
            "online": True,
            "models": models,
            "configured": settings.ollama_model in models,
        }
    except Exception as e:  # noqa: BLE001 — любая ошибка означает «офлайн»
        return {"online": False, "models": [], "configured": False, "error": str(e)[:200]}


SCORING_SYSTEM = """Ты — опытный HR-рекрутер IT-компаний. Оцени, насколько вакансия подходит кандидату.

ПРОФИЛЬ КАНДИДАТА:
{profile}

РУБРИКА (1-10):
10 — junior/без опыта, полный must-have стек (Java Core), AI/LLM/Python в стеке, удалёнка, совпадают проекты.
8-9 — сильное совпадение, не более 1-2 незначительных разрывов.
6-7 — stretch: часть стека не совпадает или желателен опыт 1-2 года.
5 и ниже — другая роль, senior/lead, несовместимый стек или город.
Если в вакансии есть dealbreakers из профиля — снижай оценку минимум на 2 балла и перенеси их в red_flags.

ВЕРНИ СТРОГО ОДИН JSON БЕЗ МАРКДАУНА И КОММЕНТАРИЕВ:
{"score": 8, "verdict": "strong", "matched": ["Java", "LLM"], "missing": ["Spring Boot"], "red_flags": [], "reason": "1-2 предложения"}
Поле verdict: strong (8-10), stretch (6-7), skip (1-5)."""

LETTER_SYSTEM = """Ты пишешь сопроводительные письма от лица кандидата.

ПРОФИЛЬ КАНДИДАТА:
{profile}

ПРАВИЛА:
- 120-180 слов, русский язык, от первого лица.
- 2-3 конкретных совпадения с текстом вакансии (технологии, задачи).
- Один проект из профиля как доказательство компетенций.
- В конце — CTA: предложение созвона или интервью.
- Без клише ("уважаемый работодатель", "я ответственный и целеустремлённый").
- НИКАКОГО выдуманного опыта, цифр и сертификатов.
- Подпись: имя кандидата из профиля.

ВЕРНИ СТРОГО ОДИН JSON БЕЗ МАРКДАУНА:
{"letter": "Текст письма. Переносы строк — как \\n"}"""


# ---------------------------------------------------------------- парсинг JSON

def _extract_json(raw: str) -> dict | None:
    """Вытаскивает JSON из ответа LLM: режет ```json-обёртки и комментарии вокруг."""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    candidate = text[start : end + 1]
    # вторая попытка — убрать висячие запятые перед } и ]
    for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
        try:
            data = json.loads(attempt)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            continue
    return None


_SCORE_RE = re.compile(r'"?score"?\s*[:=]\s*(\d{1,2})', re.I)
_LETTER_RE = re.compile(r'"?letter"?\s*:\s*"((?:[^"\\]|\\)+)"', re.S | re.I)


def _fallback_score(raw: str) -> int | None:
    """Regex-спасение score, если JSON не распарсился."""
    m = _SCORE_RE.search(raw or "")
    if not m:
        return None
    return max(1, min(10, int(m.group(1))))


def _fallback_letter(raw: str) -> str | None:
    """Regex-спасение письма (unescape через json.loads)."""
    m = _LETTER_RE.search(raw or "")
    if not m:
        return None
    try:
        return json.loads(f'"{m.group(1)}"').strip()
    except json.JSONDecodeError:
        return m.group(1).strip()


def _as_list(x) -> list[str]:
    if isinstance(x, str):
        return [x]
    if isinstance(x, list):
        return [str(i) for i in x][:12]
    return []


# ------------------------------------------------------------------- вызовы LLM

async def _chat(messages: list[dict], temperature: float) -> str:
    """Один вызов chat.completions. JSON-режим опционален: старые Ollama/LM Studio
    могут не поддерживать response_format — тогда ретраим без него."""
    kwargs = {
        "model": settings.ollama_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": MAX_TOKENS,
    }
    try:
        resp = await client().chat.completions.create(
            response_format={"type": "json_object"}, **kwargs
        )
        return resp.choices[0].message.content or ""
    except BadRequestError as e:
        # Server rejected the request with HTTP 400. Decide by status code (typed exception),
        # not by substring search in message text (LM Studio returns a bare "Error code: 400").
        # Retry once without response_format; if the retry also fails — the exception goes up.
        body = getattr(e, "body", None)
        log.warning(
            "JSON mode rejected (HTTP %s); retrying without response_format; body: %s",
            getattr(e, "status_code", 400),
            str(body)[:200],
        )
        resp = await client().chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""


def _profile_block(profile: dict) -> str:
    try:
        return json.dumps(profile, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(profile)


def _render(template: str, **values: str) -> str:
    """Substitutes {placeholders} in template and verifies there are no leftovers:
    unsubstituted placeholders (e.g. {job_description}) silently go literally to LLM — the bug."""
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    leftover = sorted({m for m in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", out)})
    if leftover:
        log.error("prompt template: unsubstituted placeholders: %s", ", ".join(leftover))
    return out


def _safe_description(row) -> str:
    """Нормализует описание: collapse whitespace; returns '' if missing/empty."""
    return " ".join(str(row.get("description") or "").split())


def _vacancy_block(row) -> str:
    salary_from = row.get("salary_from")
    salary_to = row.get("salary_to")
    salary = f"{salary_from if salary_from is not None else '?'} - {salary_to if salary_to is not None else '?'} {row.get('currency') or ''}".strip()
    salary_period = row.get("salary_period")
    if salary_period:
        salary = f"{salary} per {salary_period}".strip()
    description = _safe_description(row)
    return json.dumps(
        {
            "title": row.get("title") or "",
            "company": row.get("company") or "",
            "location": row.get("location") or "",
            "salary": salary,
            "salary_period": salary_period or "",
            "tags": row.get("tags") or [],
            "remote": bool(row.get("remote")),
            "description": (description or "(нет описание — детали не loaded; оцен только на title/tags)")[:DESC_LIMIT],
        },
        ensure_ascii=False,
        indent=2,
    )


async def analyze_vacancy(profile: dict, row) -> dict:
    """Score the vacancy (1-10 + matched/missing/red_flags). NEVER throws exceptions."""
    if not _safe_description(row):
        log.warning(
            "vacancy %s: description empty — skip LLM scoring (retried when detail loads)",
            row.get("id"),
        )
        return {
            "score": None,
            "verdict": None,
            "matched": [],
            "missing": [],
            "red_flags": [],
            "reason": "нет описание: cannot score (details not loaded or empty)",
        }

    system = _render(SCORING_SYSTEM, profile=_profile_block(profile))
    user = _vacancy_block(row)
    last_raw = ""

    for attempt in range(3):  # первая попытка + 2 ретрая
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if attempt:
            messages.append({"role": "user", "content": "Верни ТОЛЬКО валидный JSON, без markdown и комментариев."})
        try:
            raw = await _chat(messages, temperature=0.2)
        except Exception as e:  # noqa: BLE001 — офлайн/таймаут: пробуем ещё
            log.warning("LLM вызов не удался (попытка %d): %s", attempt + 1, e)
            continue

        last_raw = raw
        data = _extract_json(raw)
        if data and data.get("score") is not None:
            try:
                score = max(1, min(10, int(float(data["score"]))))
            except (TypeError, ValueError):
                score = None
            if score is not None:
                return {
                    "score": score,
                    "verdict": data.get("verdict"),
                    "matched": _as_list(data.get("matched")),
                    "missing": _as_list(data.get("missing")),
                    "red_flags": _as_list(data.get("red_flags")),
                    "reason": str(data.get("reason") or "")[:500],
                }
        log.warning("LLM вернула не-JSON (попытка %d): %s", attempt + 1, raw[:200])

    # regex-фолбэк: спасаем хотя бы score
    score = _fallback_score(last_raw)
    return {
        "score": score,
        "verdict": "stretch" if (score and score >= 6) else ("skip" if score else None),
        "matched": [],
        "missing": [],
        "red_flags": [],
        "reason": ("LLM вернула не-JSON, score спасён regex-фолбэком"
                   if score else "LLM вернула не-JSON и без score"),
    }


async def generate_letter(profile: dict, row) -> tuple[str | None, str | None]:
    """Генерирует сопроводительное письмо. Возвращает (letter, error). НЕ бросает."""
    if not _safe_description(row):
        return None, "empty description: cannot write a letter without content"
    system = _render(LETTER_SYSTEM, profile=_profile_block(profile))
    user = _vacancy_block(row) + "\n\nНапиши сопроводительное письмо под ЭТУ вакансию."
    last_raw = ""

    for attempt in range(3):
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if attempt:
            messages.append({"role": "user", "content": 'Верни ТОЛЬКО валидный JSON вида {"letter": "..."}.'})
        try:
            raw = await _chat(messages, temperature=0.6)
        except Exception as e:  # noqa: BLE001
            log.warning("LLM письмо не удалось (попытка %d): %s", attempt + 1, e)
            continue

        last_raw = raw
        data = _extract_json(raw)
        if data and isinstance(data.get("letter"), str) and data["letter"].strip():
            return data["letter"].strip(), None
        letter = _fallback_letter(raw)
        if letter:
            return letter, None
        log.warning("LLM письмо: не-JSON (попытка %d): %s", attempt + 1, raw[:200])

    return None, f"не удалось получить письмо: {(last_raw or 'пустой ответ')[:150]}"
