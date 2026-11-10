"""Клиент hh.ru: пагинированный сбор через HTML-страницы, backoff на 403/429/5xx.

curl_cffi + impersonate="chrome" — обход DDoS-Guard: запросы выглядят как
запросы настоящего Chrome (TLS-отпечаток и заголовки).

Важно: JSON-API api.hh.ru/vacancies и /vacancies/{id} блокируются hh.ru
на 403 даже с корректным отпечатком (собственная анти-бот защита на эти
пути). HTML-страницы hh.ru (search/vacancy, /vacancy/{id}) при этом
отдаются normally — поэтому парсер работает по HTML.
Автоотклики не отправляем — такого API у hh.ru нет (ToS).
"""
from __future__ import annotations

import asyncio
import html as htmllib
import logging
import re

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

log = logging.getLogger("hh")

HH = "https://hh.ru"
MAX_PAGES = 20      # пагинация HTML-поиска
PAGE_PAUSE = 0.25   # вежливая пауза между страницами, сек
MAX_RETRIES = 4
IMPERSONATE = "chrome"  # отпечаток Chrome — обходит DDoS-Guard

_WS = "\u00a0\u202f"  # неразрывные пробелы в числах hh
_SPACE = f" {_WS}"


def new_session() -> requests.AsyncSession:
    """Асинхронная сессия curl_cffi, выдающая себя за Chrome."""
    return requests.AsyncSession(
        impersonate=IMPERSONATE,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )


async def _get(session: requests.AsyncSession, url: str, params: dict | None) -> str:
    """GET с ретраями и экспоненциальным backoff на 403/429/5xx/ошибки сети."""
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await session.get(url, params=params, timeout=30.0, impersonate=IMPERSONATE)
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                wait = 1.0 * (2 ** attempt)
                log.warning("hh.ru %s — повтор через %ss", resp.status_code, wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.text
        except RequestException as e:
            last = e
            await asyncio.sleep(1.0 * (2 ** attempt))
    raise RuntimeError(f"hh.ru недоступен: {url}")


def _text(fragment: str) -> str:
    """Strip tags and unescape entities."""
    t = re.sub(r"<[^>]+>", " ", fragment)
    t = htmllib.unescape(t)
    return re.sub(f"[{_WS}]", " ", t).strip()


def _search_field(block: str, qa: str) -> str | None:
    m = re.search(r'data-qa="' + re.escape(qa) + r'"[^>]*>(.*?)</(?:span|a|div|h1)>', block, re.S)
    if not m:
        return None
    t = _text(m.group(1))
    return t or None


def _salary(block: str) -> tuple[int | None, int | None]:
    m = re.search(
        r"(\d[\d" + _SPACE + r"]{2,15}?)\s*₽(?:\s*[–-]\s*(\d[\d" + _SPACE + r"]{2,15}?)\s*₽)?",
        block,
    )
    if not m:
        return None, None

    def num(s: str) -> int:
        return int(re.sub(r"\D", "", s))

    return num(m.group(1)), num(m.group(2)) if m.group(2) else None


def parse_search_page(text: str) -> list[dict]:
    """Parse HTML поиска hh.ru (blocks with data-qa="vacancy-serp__vacancy")."""
    out: list[dict] = []
    blocks = text.split('data-qa="vacancy-serp__vacancy"')[1:]
    for b in blocks:
        href = re.search(r'href="https?://(?:[a-z0-9-]+\.)*hh\.ru/vacancy/(\d+)', b)
        if not href:
            continue
        hh_id = href.group(1)
        title = _search_field(b, "serp-item__title-text")
        company = _search_field(b, "vacancy-serp__vacancy-employer-text")
        area = _search_field(b, "vacancy-serp__vacancy-address")
        salary_from, salary_to = _salary(b)
        out.append({
            "hh_id": hh_id,
            "title": title,
            "company": company,
            "url": f"{HH}/vacancy/{hh_id}",
            "area": area,
            "salary_from": salary_from,
            "salary_to": salary_to,
            "currency": "RUR",
            "published_at": None,
            "description": "",
        })
    return out


async def fetch_search(search: dict, session: requests.AsyncSession | None = None) -> list[dict]:
    """Собирает вакансии сохранённого поиска по HTML-страницам (до MAX_PAGES)."""
    search = dict(search)
    params = {
        "text": search["text"],
        "area": search.get("area") or "113",
        "experience": search.get("experience") or "noExperience",
        "resultsPerPage": "50",
    }
    own_session = session is None
    session = session or new_session()
    items: list[dict] = []
    seen: set[str] = set()
    try:
        for page in range(MAX_PAGES):
            text = await _get(session, f"{HH}/search/vacancy", {**params, "page": str(page)})
            batch = parse_search_page(text)
            if not batch:
                break
            ids = {v["hh_id"] for v in batch}
            if ids <= seen:
                break
            seen |= ids
            items.extend(batch)
            await asyncio.sleep(PAGE_PAUSE)
    finally:
        if own_session:
            await session.close()
    log.info("поиск %r: получено %d", search.get("name"), len(items))
    return items


def _extract_desc(text: str) -> str:
    """Вырезает блок описания (div с data-qa="vacancy-description") целиком."""
    m = re.search(r'<div[^>]*data-qa="vacancy-description"[^>]*>', text)
    if not m:
        return ""
    start = m.end()
    depth = 1
    pos = start
    while pos < len(text) and depth:
        nxt_open = text.find("<div", pos)
        nxt_close = text.find("</div>", pos)
        if nxt_close == -1:
            break
        if nxt_open != -1 and nxt_open < nxt_close:
            depth += 1
            pos = nxt_open + 4
        else:
            depth -= 1
            pos = nxt_close + 6
    frag = text[start : pos - 6 if pos > start else start]
    frag = re.sub(r"<br\s*/?>", "\n", frag)
    frag = re.sub(r"</p\s*>", "\n\n", frag)
    frag = re.sub(r"</li\s*>", "\n", frag)
    frag = re.sub(r"<li[^>]*>", "\n- ", frag)
    frag = _text(frag)
    frag = re.sub(r"\n{3,}", "\n\n", frag)
    return frag.strip()


async def fetch_details(hh_id: str, session: requests.AsyncSession | None = None) -> dict | None:
    """Полное описание вакансии со страницы hh.ru/vacancy/{id}."""
    own_session = session is None
    session = session or new_session()
    try:
        try:
            text = await _get(session, f"{HH}/vacancy/{hh_id}", None)
        except RuntimeError as e:
            log.warning("детали %s: %s", hh_id, e)
            return None
    finally:
        if own_session:
            await session.close()
    title = _search_field(text, "vacancy-title")
    company = _search_field(text, "vacancy-company-name")
    if not title:
        m = re.search(r"<title[^>]*>Вакансия (.*?) в [\wё-]+, работа в компании (.*?)</title>", text, re.S)
        if m:
            title, company = _text(m.group(1)), _text(m.group(2))
    return {
        "title": title,
        "company": company,
        "description": _extract_desc(text),
    }


def prefilter(text: str, dealbreakers: list[str] | None) -> list[str]:
    """Дешёвый фильтр без LLM: возвращает dealbreakers, найденные в тексте."""
    if not text or not dealbreakers:
        return []
    low = text.lower()
    return [kw for kw in dealbreakers if kw and kw.lower() in low]
