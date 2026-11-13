"""Сбор IT-вакансий с hh.ru, Habr Career и RemoteOK.

Модуль не работает с БД и не зависит от FastAPI. На выходе — список
нормализованных словарей, прошедших жёсткий IT-фильтр и дедупликацию.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from curl_cffi import requests

log = logging.getLogger("scraper")

HH = "https://hh.ru"
HABR = "https://career.habr.com"
HABR_API = f"{HABR}/api/frontend/vacancies"
REMOTEOK_API = "https://remoteok.com/api"

PAGE_PAUSE = 0.35
MAX_RETRIES = 4
IMPERSONATE = "chrome"

_TRACKING_KEYS = {"from", "hhtmfrom", "gh_src", "trk", "ref"}
_WS = "\u00a0\u202f"


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


REQUEST_TIMEOUT = _env_int("REQUEST_TIMEOUT", 30, 1)
REMOTEOK_TIMEOUT = _env_int("REMOTEOK_TIMEOUT", 60, 1)
DETAIL_CONCURRENCY = _env_int("DETAIL_CONCURRENCY", 4, 1)
HH_MAX_PAGES = _env_int("HH_MAX_PAGES", 3, 1)
HABR_MAX_PAGES = _env_int("HABR_MAX_PAGES", 3, 1)
REMOTEOK_LIMIT = _env_int("REMOTEOK_LIMIT", 300, 0)
VIBE_CODING_QUERY = os.getenv("VIBE_CODING_QUERY", "vibe coding")
VIBE_MAX_PAGES = _env_int("VIBE_MAX_PAGES", 2, 1)

HH_QUERY = os.getenv("HH_QUERY", '(Python OR Backend OR Frontend OR DevOps OR Data OR ML OR QA OR "Vibe Coding")')
HH_AREA = os.getenv("HH_AREA", "113")
HH_EXPERIENCE = os.getenv("HH_EXPERIENCE", "").strip()
SCRAPE_QUERIES = [
    item.strip()
    for item in os.getenv(
        "SCRAPE_QUERIES", "python,backend,frontend,devops,data,ml,vibe coding"
    ).split(",")
    if item.strip()
][:8]

ProgressCallback = Callable[[str, int, int, int], Awaitable[None]]


@dataclass(slots=True)
class SourceResult:
    items: list[dict] = field(default_factory=list)
    fetched: int = 0
    errors: list[str] = field(default_factory=list)
    known: set[tuple[str, str]] = field(default_factory=set)


@dataclass(slots=True)
class ScrapeReport:
    items: list[dict] = field(default_factory=list)
    fetched: int = 0
    filtered: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    known: set[tuple[str, str]] = field(default_factory=set)


class ScraperError(RuntimeError):
    pass


def new_session() -> requests.AsyncSession:
    return requests.AsyncSession(
        impersonate=IMPERSONATE,
        headers={
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )


async def _get(
    session: requests.AsyncSession,
    url: str,
    params: dict | None = None,
    timeout: int | None = None,
    headers: dict | None = None,
):
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await session.get(
                url,
                params=params,
                timeout=timeout or REQUEST_TIMEOUT,
                headers=headers,
                impersonate=IMPERSONATE,
            )
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                wait = min(2 ** attempt + random.random(), 8)
                log.warning("%s -> HTTP %s, повтор через %.1fs", url, resp.status_code, wait)
                last = ScraperError(f"HTTP {resp.status_code}")
                await asyncio.sleep(wait)
                continue
            if 400 <= resp.status_code < 500:
                raise ScraperError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp
        except ScraperError:
            raise
        except Exception as exc:
            last = exc
            wait = min(2 ** attempt + random.random(), 8)
            log.warning("%s: %s, повтор через %.1fs", url, exc, wait)
            await asyncio.sleep(wait)
    raise ScraperError(f"{url}: {last}")


# ------------------------------------------------------------------ text


class _TextExtractor(HTMLParser):
    _BLOCK = {
        "p", "div", "section", "article", "header", "footer", "ul", "ol",
        "h1", "h2", "h3", "h4", "h5", "h6", "table", "tr", "blockquote",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" or tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(html: str | None, limit: int = 20_000) -> str:
    if not html:
        return ""
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", str(html))
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        return ""
    text = "".join(parser.parts)
    text = text.replace("\u00a0", " ").replace("\u202f", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def _clean_line(value, max_len: int) -> str:
    if value is None:
        return ""
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", str(value)))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len].strip()


def _to_utc_iso(value) -> str | None:
    if value is None or value == "":
        return None

    dt: datetime | None = None
    if isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.strptime(text[:10], "%Y-%m-%d")
            except ValueError:
                return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_url(url: str | None, source: str, source_id: str) -> str | None:
    if source == "hh":
        return f"{HH}/vacancy/{source_id}"
    if source == "habr":
        return f"{HABR}/vacancies/{source_id}"

    raw = (url or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None

    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        low = key.lower()
        if low.startswith("utm_") or low in _TRACKING_KEYS:
            continue
        query.append((key, value))

    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", parts.netloc.lower(), path, urlencode(query), ""))


def _int_or_none(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _currency(value) -> str | None:
    if not value:
        return None
    return {
        "rur": "RUB",
        "rub": "RUB",
        "usd": "USD",
        "eur": "EUR",
    }.get(str(value).strip().lower())


# --------------------------------------------------------------- filtering


NON_IT_TITLE_STEMS = (
    "менеджер по продажам", "руководитель отдела продаж", "директор по продажам",
    "sales manager", "sales", "продаж", "продавец", "кассир", "курьер",
    "водитель", "повар", "грузчик", "уборщик", "охранник", "колл-центр",
    "call-центр", "оператор call", "бухгалтер", "юрист", "рекрут", "hr",
    "маркетолог", "smm", "копирайтер", "seo", "таргетолог", "промоутер",
    "сварщик", "электрик", "сантехник", "механик", "медсестр", "врач",
    "фармацевт", "учител", "воспитател", "логопед", "психолог", "массаж",
    "официант", "бармен", "кладовщ", "логист", "диспетчер", "риелтор",
    "агент по недвижимости", "страховой агент", "дизайнер интерьера",
    "ландшафтный дизайнер", "офис-менеджер", "менеджер по персоналу",
    "менеджер по маркетингу", "аккаунт-менеджер", "менеджер по работе с клиентами",
)

IT_TOKENS = (
    "python", "java", "javascript", "typescript", "golang", "go", "rust",
    "c++", "c#", ".net", "php", "ruby", "kotlin", "swift", "scala", "sql",
    "postgresql", "mysql", "mongodb", "redis", "react", "vue", "angular",
    "node.js", "node", "django", "flask", "fastapi", "spring", "laravel",
    "symfony", "backend", "frontend", "fullstack", "full-stack", "full stack",
    "mobile", "mobile developer", "developer", "software engineer", "dev",
    "data science", "android", "ios", "ml", "ai", "llm", "rag", "nlp",
    "computer vision", "data scientist", "data engineer", "data analyst",
    "аналитик данных", "системный аналитик", "бизнес-аналитик",
    "продуктовый аналитик", "ml engineer", "mlops", "devops", "sre",
    "kubernetes", "docker", "terraform", "ansible", "linux", "git", "ci/cd",
    "aws", "azure", "gcp", "qa", "sdet", "тестировщик", "тестирование",
    "автотесты", "автоматизатор тестирования", "разработчик", "программист",
    "инженер-программист", "инженер по тестированию", "инженер данных",
    "системный администратор", "сисадмин", "информационная безопасность",
    "пентестер", "1с", "битрикс", "архитектор по", "tech lead", "team lead",
    "api", "rest", "graphql", "микросервисы",
    # Vibe coding / AI-assisted development (user wants these in search results)
    "vibe coding", "vibe-coding", "ai-assisted development",
    "ai assisted development", "ai-ассистированная разработка",
    "ai ассистент", "ai-ассистент", "копиласт",
)

NON_IT_BODY_PHRASES = (
    "холодные звонки", "холодных звонков", "план продаж", "плана продаж",
    "входящие звонки", "исходящие звонки", "работа с клиентами",
    "привлечение клиентов", "выполнение плана", "опыт в продажах",
    "active sales", "cold calls", "sales pipeline", "lead generation",
)


def _norm(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    text = text.replace("\u00a0", " ").replace("\u202f", " ")
    return re.sub(r"\s+", " ", text).strip()


def _token_hits(text: str, tokens: tuple[str, ...]) -> list[str]:
    low = _norm(text)
    hits: list[str] = []
    for token in tokens:
        pattern = rf"(?<![a-zа-я0-9]){re.escape(token)}(?![a-zа-я0-9])"
        if re.search(pattern, low):
            hits.append(token)
    return hits


def _contains_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    low = _norm(text)
    return any(phrase in low for phrase in phrases)


def _is_non_it_title(title: str) -> bool:
    low = _norm(title)
    for stem in NON_IT_TITLE_STEMS:
        if re.fullmatch(r"[a-z0-9 .+#/-]+", stem):
            pattern = rf"(?<![a-zа-я0-9]){re.escape(stem)}(?![a-zа-я0-9])"
            if re.search(pattern, low):
                return True
        elif stem in low:
            return True
    return False


def is_it_vacancy(
    title: str, tags: list[str], description: str
) -> tuple[bool, int, str | None]:
    if _is_non_it_title(title):
        return False, 0, "non-it title"

    title_hits = _token_hits(title, IT_TOKENS)
    tag_hits = _token_hits(" ".join(tags or []), IT_TOKENS)
    desc_hits = _token_hits(description or "", IT_TOKENS)

    if not title_hits and _contains_phrase(description or "", NON_IT_BODY_PHRASES):
        return False, 0, "non-it body"
    if title_hits:
        return True, 100, None
    if len(tag_hits) >= 2:
        return True, 75, None
    if tag_hits and len(desc_hits) >= 3:
        return True, 65, None
    return False, 0, "no it signals"


# -------------------------------------------------------------- normalized


def _build_vacancy(
    *,
    source: str,
    source_id: str,
    title: str,
    company: str,
    url: str | None,
    location: str | None = None,
    salary_from: int | None = None,
    salary_to: int | None = None,
    currency: str | None = None,
    salary_period: str | None = None,
    published_at: str | None = None,
    description: str = "",
    tags: list[str] | None = None,
    remote: bool = False,
    it_score: int = 0,
    raw: dict | None = None,
) -> dict | None:
    source = str(source)
    if source not in ("hh", "habr", "remoteok"):
        return None

    source_id = str(source_id or "").strip()
    title = _clean_line(title, 300)
    if not source_id or not title:
        return None

    canonical = _canonical_url(url, source, source_id)
    if not canonical:
        return None

    salary_from = _int_or_none(salary_from)
    salary_to = _int_or_none(salary_to)
    if salary_from is not None and salary_to is not None and salary_from > salary_to:
        salary_from, salary_to = salary_to, salary_from

    clean_tags: list[str] = []
    seen_tags: set[str] = set()
    for tag in tags or []:
        cleaned = _clean_line(tag, 40)
        key = cleaned.casefold()
        if cleaned and key not in seen_tags:
            seen_tags.add(key)
            clean_tags.append(cleaned)
        if len(clean_tags) >= 20:
            break

    if salary_period not in ("month", "year"):
        salary_period = None

    return {
        "source": source,
        "source_id": source_id,
        "title": title,
        "company": _clean_line(company, 200) or "Не указана",
        "url": canonical,
        "location": _clean_line(location, 200) or None,
        "salary_from": salary_from,
        "salary_to": salary_to,
        "currency": _currency(currency),
        "salary_period": salary_period,
        "published_at": _to_utc_iso(published_at),
        "description": html_to_text(description, 20_000),
        "tags": clean_tags,
        "remote": bool(remote),
        "it_score": max(0, min(100, int(it_score or 0))),
        "raw": raw if isinstance(raw, dict) else {},
    }


def _dedupe(items: list[dict]) -> list[dict]:
    by_key: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for item in items:
        key = (item["source"], item["source_id"])
        if key not in by_key:
            by_key[key] = item
            order.append(key)
        elif len(item.get("description") or "") > len(by_key[key].get("description") or ""):
            by_key[key] = item
    return [by_key[key] for key in order]


def _cross_source_dedupe(items: list[dict]) -> list[dict]:
    """Drop the same vacancy that shows up on more than one source (hh + habr, ...).

    Fingerprint is normalized title + company. We only merge when we are confident it is
    the identical posting — a salary conflict means two distinct roles and both are kept.
    Keeps the richest copy (longest description, then higher it_score).
    """
    best: dict[str, dict] = {}
    order: list[str] = []
    for item in items:
        title = _norm(item.get("title") or "")
        company = _norm(item.get("company") or "")
        if not title or not company:
            continue
        fp = f"{title} | {company}"
        cur = best.get(fp)
        if cur is None:
            best[fp] = item
            order.append(fp)
            continue

        a_from, a_to = cur.get("salary_from"), cur.get("salary_to")
        b_from, b_to = item.get("salary_from"), item.get("salary_to")
        salary_conflict = (a_from is not None and b_from is not None and a_from != b_from) or (
            a_to is not None and b_to is not None and a_to != b_to
        )
        if salary_conflict:
            # Different pay => likely distinct roles; keep this one as its own entry.
            fp = f"{fp}#{item.get('source')}-{item.get('source_id')}"
            best[fp] = item
            order.append(fp)
            continue

        new_len = len(item.get("description") or "")
        cur_len = len(cur.get("description") or "")
        if new_len > cur_len or (new_len == cur_len and item.get("it_score", 0) > cur.get("it_score", 0)):
            best[fp] = item
    return [best[key] for key in order]


# --------------------------------------------------------------------- hh


def _hh_field(block: str, qa: str) -> str:
    match = re.search(
        r'data-qa="' + re.escape(qa) + r'"[^>]*>(.*?)</(?:span|a|div|h1)>',
        block,
        re.S,
    )
    if not match:
        return ""
    return _clean_line(match.group(1), 300)


def _num(value: str) -> int | None:
    digits = re.sub(r"\D", "", value or "")
    return int(digits) if digits else None


_RUB = r"(?:₽|руб\.?|рубл\w*)"


def _hh_salary(block: str) -> tuple[int | None, int | None, str]:
    """Return (salary_from, salary_to, period). Period is 'month' or 'year'.

    Handles the common hh formats that the old regex missed:
      * «150–200 тыс. руб./мес» and «от 80 до 120 тыс.» (thousands after numbers)
      * currency words рублей/рубля, not just ₽ / руб.
    """
    text = html_to_text(block, 10_000)
    low = re.sub(r"\s+", " ", (text or "").lower().replace("\u00a0", " "))
    period = "year" if re.search(r"(в\s+год|/год\b|yearly|annual)", low) else "month"

    # Thousands stated after the numbers: «N–M тыс.» / «от N до M тыс.» / «до N тыс.».
    m = re.search(r"(\d[\d ]{2,12})\s*[–-]\s*(\d[\d ]{2,12})\s*тыс\.?", low)
    if m:
        return (_num(m.group(1)) or 0) * 1000, (_num(m.group(2)) or 0) * 1000, period
    m = re.search(r"от\s+(\d[\d ]{2,12})(?:\s+до\s+(\d[\d ]{2,12}))?\s*тыс\.?", low)
    if m:
        return (
            (_num(m.group(1)) or 0) * 1000,
            ((_num(m.group(2)) or 0) * 1000 if m.group(2) else None),
            period,
        )
    m = re.search(r"до\s+(\d[\d ]{2,12})\s*тыс\.?", low)
    if m:
        return None, (_num(m.group(1)) or 0) * 1000, period

    # Currency-marked amounts (₽ / руб. / рублей ...).
    match = re.search(r"(\d[\d ]{2,12})\s*[–-]\s*(\d[\d ]{2,12})\s*" + _RUB, low)
    if match:
        return _num(match.group(1)), _num(match.group(2)), period
    match = re.search(r"от\s+(\d[\d ]{2,12})(?:\s*до\s*(\d[\d ]{2,12}))?\s*" + _RUB, low)
    if match:
        return (
            _num(match.group(1)),
            (_num(match.group(2)) if match.group(2) else None),
            period,
        )
    match = re.search(r"до\s+(\d[\d ]{2,12})\s*" + _RUB, low)
    if match:
        return None, _num(match.group(1)), period
    match = re.search(r"(\d[\d ]{2,12})\s*" + _RUB, low)
    if match:
        return _num(match.group(1)), None, period
    return None, None, "month"


def _hh_remote(area: str | None) -> bool:
    """Remote detection for hh: the address field says «удалённая работа» or similar."""
    low = (area or "").lower()
    return any(marker in low for marker in ("удал", "remote"))


def _parse_hh_search(html_text: str) -> list[dict]:
    out: list[dict] = []
    blocks = html_text.split('data-qa="vacancy-serp__vacancy"')[1:]
    for block in blocks:
        match = re.search(
            r'href="(?:(?:https?:)?//(?:[a-z0-9-]+\.)*hh\.ru)?/vacancy/(\d+)',
            block,
        )
        if not match:
            continue
        hh_id = match.group(1)
        title = _hh_field(block, "serp-item__title-text")
        if not title:
            continue
        company = _hh_field(block, "vacancy-serp__vacancy-employer-text")
        area = _hh_field(block, "vacancy-serp__vacancy-address")
        salary_from, salary_to, salary_period = _hh_salary(block)
        out.append(
            {
                "source": "hh",
                "source_id": hh_id,
                "title": title,
                "company": company,
                "location": area,
                "salary_from": salary_from,
                "salary_to": salary_to,
                "currency": "RUB" if (salary_from or salary_to) else None,
                "salary_period": salary_period,
                "published_at": None,
                "description": "",
                "tags": [],
                "remote": _hh_remote(area),
                "raw": {"search_block": block[:2000]},
            }
        )
    return out


def _extract_hh_description(text: str) -> str:
    match = re.search(r'<div[^>]*data-qa="vacancy-description"[^>]*>', text)
    if not match:
        return ""
    start = match.end()
    depth = 1
    pos = start
    end = len(text)
    # \b after "div" so we never match <divider>/<dialog>; case-insensitive close.
    open_re = re.compile(r"<div\b[^>]*>", re.I)
    close_re = re.compile(r"</div\s*>", re.I)
    while pos < len(text) and depth:
        next_open = open_re.search(text, pos)
        next_close = close_re.search(text, pos)
        if not next_close:
            break
        if next_open is not None and next_open.start() < next_close.start():
            depth += 1
            pos = next_open.end()
        else:
            depth -= 1
            if depth == 0:
                end = next_close.start()
                break
            pos = next_close.end()
    return html_to_text(text[start:end], 20_000)


def _hh_published(text: str) -> str | None:
    patterns = (
        r'<meta[^>]+property="article:published_time"[^>]+content="([^"]+)"',
        r'<meta[^>]+content="([^"]+)"[^>]+property="article:published_time"',
        r"&#34;publicationTimeIso&#34;:&#34;([^&]+)&#34;",
        r'"publicationTimeIso"\s*:\s*"([^"]+)"',
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            published = _to_utc_iso(match.group(1))
            if published:
                return published
    return None


async def _hh_detail(session: requests.AsyncSession, hh_id: str) -> dict:
    resp = await _get(session, f"{HH}/vacancy/{hh_id}")
    text = resp.text
    return {
        "title": _hh_field(text, "vacancy-title"),
        "company": _hh_field(text, "vacancy-company-name"),
        "description": _extract_hh_description(text),
        "published_at": _hh_published(text),
    }


def _hh_tags(description: str) -> list[str]:
    return _token_hits(description, IT_TOKENS)[:20]


async def scrape_hh(
    session: requests.AsyncSession,
    known_keys: set[tuple[str, str]] | None = None,
) -> SourceResult:
    result = SourceResult()
    known_hh = {
        source_id
        for source, source_id in known_keys or set()
        if source == "hh"
    }
    params = {
        "text": HH_QUERY,
        "area": HH_AREA,
        "resultsPerPage": "50",
        "order_by": "publication_time",
    }
    if HH_EXPERIENCE:
        params["experience"] = HH_EXPERIENCE

    raw: list[dict] = []
    seen: set[str] = set()
    for page in range(HH_MAX_PAGES):
        try:
            resp = await _get(session, f"{HH}/search/vacancy", {**params, "page": str(page)})
        except Exception as exc:
            result.errors.append(f"search page {page}: {str(exc)[:200]}")
            break

        batch = _parse_hh_search(resp.text)
        if not batch:
            break
        fresh = [item for item in batch if item["source_id"] not in seen]
        if not fresh:
            break
        seen.update(item["source_id"] for item in fresh)
        raw.extend(fresh)
        await asyncio.sleep(PAGE_PAUSE)

    result.fetched = len(raw)
    semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def process(item: dict) -> dict | None:
        if _is_non_it_title(item["title"]):
            return None

        is_known = item["source_id"] in known_hh
        detail: dict = {}

        if is_known:
            result.known.add(("hh", item["source_id"]))
        else:
            async with semaphore:
                try:
                    detail = await _hh_detail(session, item["source_id"])
                except Exception as exc:
                    if len(result.errors) < 3:
                        result.errors.append(f"detail {item['source_id']}: {str(exc)[:160]}")

        title = detail.get("title") or item["title"]
        company = detail.get("company") or item["company"]
        description = detail.get("description") or ""
        tags = _hh_tags(description)
        accepted, score, _ = is_it_vacancy(title, tags, description)
        if not accepted:
            if is_known:
                accepted, score = True, 0
            else:
                return None

        return _build_vacancy(
            source="hh",
            source_id=item["source_id"],
            title=title,
            company=company,
            url=item["url"] if "url" in item else None,
            location=item.get("location"),
            salary_from=item.get("salary_from"),
            salary_to=item.get("salary_to"),
            currency=item.get("currency"),
            salary_period=item.get("salary_period") or "month",
            published_at=detail.get("published_at") or item.get("published_at"),
            description=description,
            tags=tags,
            remote=bool(item.get("remote")),
            it_score=score,
            raw={**item.get("raw", {}), "detail": detail},
        )

    built = await asyncio.gather(*(process(item) for item in raw))
    result.items = _dedupe([item for item in built if item])
    return result


# ------------------------------------------------------------------- hh vibe coding


async def scrape_hh_vibe_coding(session, profile: Profile) -> SourceResult:
    """Extra HH pass with a fixed 'vibe coding' query.

    Runs alongside the keyword-driven HH search so that AI-assisted /
    vibe-coding roles are always in the candidate pool regardless of what
    the profile keywords happen to match.  Uses the same HTML endpoint and
    parsing as scrape_hh; only the query text differs.
    """
    result = SourceResult(source="hh")
    if not session:
        return result

    async def process(item: dict) -> dict | None:
        return await _build_hh_vacancy(session, item, profile)

    raw: list[dict] = []
    for page in range(1, VIBE_MAX_PAGES + 1):
        try:
            resp = await session.get(
                f"{HH}/search/vacancy",
                params={
                    "text": VIBE_CODING_QUERY,
                    "area": profile.location_id or "",
                    "page": page,
                    "resultsPerPage": HH_PAGE_SIZE,
                },
                headers={"Accept": "text/html"},
            )
        except Exception as exc:  # noqa: BLE001 - network errors are expected
            log.warning("HH vibe-coding search failed on page %d: %s", page, exc)
            break
        if resp.status_code != 200:
            log.warning(
                "HH vibe-coding search returned HTTP %d on page %d",
                resp.status_code,
                page,
            )
            break

        items = _parse_hh_search(resp.text)
        result.fetched += len(items)
        raw.extend(items)
        if not items:
            break
        await asyncio.sleep(profile.request_delay)

    built = await asyncio.gather(*(process(item) for item in raw))
    result.items = _dedupe([item for item in built if item])
    return result


# ------------------------------------------------------------------- habr


def _parse_habr_item(item: dict) -> dict | None:
    source_id = str(item.get("id") or "").strip()
    title = _clean_line(item.get("title"), 300)
    href = str(item.get("href") or "").strip()
    if not source_id or not title or not href:
        return None

    company_obj = item.get("company") or {}
    salary = item.get("salary") or {}
    locations = item.get("locations") or []
    location = locations[0].get("title") if locations and isinstance(locations[0], dict) else None

    tags: list[str] = []
    for group in ("skills", "divisions"):
        for entry in item.get(group) or []:
            if isinstance(entry, dict) and entry.get("title"):
                tags.append(entry["title"])

    return {
        "source": "habr",
        "source_id": source_id,
        "title": title,
        "company": _clean_line(company_obj.get("title"), 200),
        "url": href if href.startswith("http") else f"{HABR}{href}",
        "location": location,
        "salary_from": _int_or_none(salary.get("from")),
        "salary_to": _int_or_none(salary.get("to")),
        "currency": _currency(salary.get("currency")),
        "salary_period": "month",
        "published_at": _to_utc_iso((item.get("publishedDate") or {}).get("date")),
        "description": "",
        "tags": tags,
        "remote": bool(item.get("remoteWork")) or "удал" in (location or "").lower(),
        "raw": item,
    }


def _find_jobposting(data) -> dict | None:
    if isinstance(data, dict):
        if data.get("@type") == "JobPosting":
            return data
        for key in ("@graph",):
            graph = data.get(key)
            if isinstance(graph, list):
                for entry in graph:
                    found = _find_jobposting(entry)
                    if found:
                        return found
        for value in data.values():
            if isinstance(value, (dict, list)):
                found = _find_jobposting(value)
                if found:
                    return found
    elif isinstance(data, list):
        for entry in data:
            found = _find_jobposting(entry)
            if found:
                return found
    return None


async def _habr_detail(session: requests.AsyncSession, url: str) -> dict:
    resp = await _get(session, url)
    text = resp.text
    scripts = re.finditer(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        text,
        re.S,
    )
    for match in scripts:
        try:
            job = _find_jobposting(json.loads(match.group(1)))
        except (json.JSONDecodeError, TypeError):
            job = None
        if job:
            return {
                "description": html_to_text(job.get("description"), 20_000),
                "published_at": _to_utc_iso(job.get("datePosted")),
            }

    fallback = re.search(
        r'(<div[^>]*class="[^"]*(?:vacancy-description__text|vacancy-description)[^"]*"[^>]*>.*?</div>)',
        text,
        re.S,
    )
    return {
        "description": html_to_text(fallback.group(1), 20_000) if fallback else "",
        "published_at": None,
    }


async def scrape_habr(
    session: requests.AsyncSession,
    known_keys: set[tuple[str, str]] | None = None,
) -> SourceResult:
    result = SourceResult()
    known_habr = {
        source_id
        for source, source_id in known_keys or set()
        if source == "habr"
    }
    raw_by_id: dict[str, dict] = {}
    order: list[str] = []

    for query in SCRAPE_QUERIES:
        for page in range(1, HABR_MAX_PAGES + 1):
            try:
                resp = await _get(
                    session,
                    HABR_API,
                    params={"q": query, "page": page},
                    headers={"Accept": "application/json"},
                )
                data = resp.json()
            except Exception as exc:
                result.errors.append(f"{query} page {page}: {str(exc)[:200]}")
                break

            items = data.get("list") or []
            if not items:
                break
            for item in items:
                source_id = str(item.get("id") or "").strip()
                if source_id and source_id not in raw_by_id:
                    raw_by_id[source_id] = item
                    order.append(source_id)

            meta = data.get("meta") or {}
            total_pages = int(meta.get("totalPages") or 0)
            if total_pages and page >= total_pages:
                break
            await asyncio.sleep(PAGE_PAUSE)

    result.fetched = len(order)
    semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def process(item: dict) -> dict | None:
        parsed = _parse_habr_item(item)
        if not parsed:
            return None

        is_known = parsed["source_id"] in known_habr
        detail: dict = {}

        if is_known:
            result.known.add(("habr", parsed["source_id"]))
        else:
            async with semaphore:
                try:
                    detail = await _habr_detail(session, parsed["url"])
                except Exception as exc:
                    if len(result.errors) < 3:
                        result.errors.append(f"detail {parsed['source_id']}: {str(exc)[:160]}")

        description = detail.get("description") or ""
        accepted, score, _ = is_it_vacancy(parsed["title"], parsed["tags"], description)
        if not accepted:
            if is_known:
                accepted, score = True, 0
            else:
                return None

        return _build_vacancy(
            source="habr",
            source_id=parsed["source_id"],
            title=parsed["title"],
            company=parsed["company"],
            url=parsed["url"],
            location=parsed["location"],
            salary_from=parsed["salary_from"],
            salary_to=parsed["salary_to"],
            currency=parsed["currency"],
            salary_period="month",
            published_at=parsed["published_at"] or detail.get("published_at"),
            description=description,
            tags=parsed["tags"],
            remote=parsed["remote"],
            it_score=score,
            raw=item,
        )

    built = await asyncio.gather(*(process(raw_by_id[key]) for key in order))
    result.items = _dedupe([item for item in built if item])
    return result


# --------------------------------------------------------------- remoteok


async def scrape_remoteok(
    session: requests.AsyncSession,
    known_keys: set[tuple[str, str]] | None = None,
) -> SourceResult:
    result = SourceResult()
    known_remoteok = {
        source_id
        for source, source_id in known_keys or set()
        if source == "remoteok"
    }
    try:
        resp = await _get(
            session,
            REMOTEOK_API,
            timeout=REMOTEOK_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        data = resp.json()
        if not isinstance(data, list):
            raise ScraperError("RemoteOK: ожидался JSON-массив")
    except Exception as exc:
        result.errors.append(str(exc)[:300])
        return result

    raw = [
        item
        for item in data
        if isinstance(item, dict) and item.get("id") and item.get("position")
    ][:REMOTEOK_LIMIT]
    result.fetched = len(raw)

    built: list[dict] = []
    for item in raw:
        title = _clean_line(item.get("position"), 300)
        tags = [str(tag) for tag in (item.get("tags") or []) if tag]
        description = html_to_text(item.get("description"), 20_000)
        accepted, score, _ = is_it_vacancy(title, tags, description)
        if not accepted:
            continue

        source_id = str(item.get("id"))
        if source_id in known_remoteok:
            result.known.add(("remoteok", source_id))

        salary_from = _int_or_none(item.get("salary_min"))
        salary_to = _int_or_none(item.get("salary_max"))
        if salary_from == 0:
            salary_from = None
        if salary_to == 0:
            salary_to = None
        has_salary = salary_from is not None or salary_to is not None

        url = item.get("url") or item.get("apply_url")
        slug = item.get("slug")
        if not url and slug:
            url = f"https://remoteok.com/remote-jobs/{slug}"

        vacancy = _build_vacancy(
            source="remoteok",
            source_id=str(item.get("id")),
            title=title,
            company=item.get("company"),
            url=url,
            location=item.get("location"),
            salary_from=salary_from,
            salary_to=salary_to,
            currency="USD" if has_salary else None,
            salary_period="year",
            published_at=item.get("date") or item.get("epoch"),
            description=description,
            tags=tags,
            remote=True,
            it_score=score,
            raw=item,
        )
        if vacancy:
            built.append(vacancy)

    result.items = _dedupe(built)
    return result


# ------------------------------------------------------------------- all


async def _notify(
    callback: ProgressCallback | None,
    phase: str,
    fetched: int,
    filtered: int,
    known: int,
) -> None:
    if callback is None:
        return
    try:
        await callback(phase, fetched, filtered, known)
    except Exception:
        log.warning("progress callback failed", exc_info=True)


async def scrape_all(
    on_progress: ProgressCallback | None = None,
    known_keys: set[tuple[str, str]] | None = None,
) -> ScrapeReport:
    report = ScrapeReport()
    report.known = set()
    session = new_session()
    sources = (
        ("hh", scrape_hh),
        ("habr", scrape_habr),
        ("remoteok", scrape_remoteok),
        ("hh_vibe", scrape_hh_vibe_coding),
    )
    try:
        for name, func in sources:
            await _notify(on_progress, f"fetch_{name}", report.fetched, report.filtered, len(report.known))
            try:
                source_result = await func(session, known_keys)
            except Exception as exc:
                log.exception("источник %s недоступен", name)
                report.errors[name] = str(exc)[:300]
                await _notify(on_progress, f"fetch_{name}", report.fetched, report.filtered, len(report.known))
                continue

            report.fetched += source_result.fetched
            report.filtered += len(source_result.items)
            report.items.extend(source_result.items)
            report.known.update(source_result.known)
            if source_result.errors:
                report.errors[name] = "; ".join(source_result.errors[:3])[:300]
            log.info(
                "%s: fetched=%d accepted=%d known=%d errors=%d",
                name,
                source_result.fetched,
                len(source_result.items),
                len(source_result.known),
                len(source_result.errors),
            )
            await _notify(on_progress, f"fetch_{name}", report.fetched, report.filtered, len(report.known))
    finally:
        await session.close()

    report.items = _dedupe(report.items)
    report.items = _cross_source_dedupe(report.items)
    report.filtered = len(report.items)
    return report


__all__ = [
    "ScrapeReport",
    "SourceResult",
    "is_it_vacancy",
    "html_to_text",
    "new_session",
    "scrape_all",
    "scrape_hh",
    "scrape_habr",
    "scrape_remoteok",
    "VIBE_CODING_QUERY",
]
