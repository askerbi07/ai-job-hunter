# ПРОМПТ: scraper.py

Ты — Senior Python-инженер, специалист по асинхронному скрапингу и очистке данных.

Верни **только исходный код файла `scraper.py`**. Без markdown-обёрток, без языковых меток, без пояснений, без вопросов, без TODO и заглушек. Код должен быть полным и запускаемым.

Контекст проекта и общие контракты — в `prompts/00_MASTER.md`. Ниже — самодостаточное ТЗ именно на `scraper.py`.

---

## 1. Назначение файла

`scraper.py` — единственное место, где приложение общается с внешними площадками. Он:

1. Собирает вакансии с hh.ru, Habr Career и RemoteOK.
2. Приводит их к единому словарю `NormalizedVacancy`.
3. Жёстко фильтрует не-IT мусор.
4. Дедуплицирует элементы внутри батча.
5. Возвращает результат в `main.py`.

Файл **не импортирует** `main.py`, `database.py`, `config.py`, `ai_agent.py`, FastAPI, aiosqlite. Никакой записи в БД здесь нет.

Разрешённые импорты: stdlib + `curl_cffi`. Запрещены `requests`, `httpx`, `bs4`, `lxml`, `selenium`, `playwright`, `pandas`.

---

## 2. Публичный API файла

Обязательные имена и сигнатуры:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

ProgressCallback = Callable[[str, int, int], Awaitable[None]]
# Аргументы: phase, fetched_total, filtered_total

@dataclass(slots=True)
class SourceResult:
    items: list[dict]          # уже нормализованные и прошедшие IT-фильтр
    fetched: int               # сколько сырых вакансий получено ДО фильтра
    errors: list[str] = field(default_factory=list)

@dataclass(slots=True)
class ScrapeReport:
    items: list[dict]          # объединённый дедуплицированный результат
    fetched: int               # суммарно сырых вакансий
    filtered: int              # сколько прошло IT-фильтр
    errors: dict[str, str] = field(default_factory=dict)  # source -> сообщение

async def scrape_all(on_progress: ProgressCallback | None = None) -> ScrapeReport: ...
async def scrape_hh(session) -> SourceResult: ...
async def scrape_habr(session) -> SourceResult: ...
async def scrape_remoteok(session) -> SourceResult: ...

def is_it_vacancy(title: str, tags: list[str], description: str) -> tuple[bool, int, str | None]: ...
def html_to_text(html: str, limit: int = 20_000) -> str: ...
```

`scrape_all`:
- создаёт одну `AsyncSession` на весь прогон и закрывает её в `finally`;
- выполняет источники **последовательно**: `hh`, `habr`, `remoteok` (вежливость к площадкам и детерминированный прогресс);
- перед каждым источником и после него вызывает `await on_progress("fetch_hh", fetched_total, filtered_total)` и аналогично для `fetch_habr`, `fetch_remoteok`;
- изолирует ошибки: падение одного источника не мешает остальным, сообщение попадает в `errors[source]`;
- объединяет результаты и дедуплицирует по `(source, source_id)`, оставляя запись с более длинным `description`;
- возвращает `ScrapeReport`.

`main.py` после `scrape_all` сам выставит `phase="save"` и сохранит `report.items`.

---

## 3. Конфигурация через окружение

Только `os.getenv`, без импорта `config.py`. Все значения приводить к `int`, обрабатывать мусор безопасно.

```python
DB_PATH не нужен.
SCRAPE_QUERIES   = "python,backend,frontend,devops,data,ml"
HH_QUERY         = '(Python OR Backend OR Frontend OR DevOps OR Data OR ML OR QA)'
HH_AREA          = "113"
HH_EXPERIENCE    = ""          # пусто = не передавать параметр
HH_MAX_PAGES     = 3
HABR_MAX_PAGES   = 3
REMOTEOK_LIMIT   = 300
REQUEST_TIMEOUT  = 30
REMOTEOK_TIMEOUT = 60
DETAIL_CONCURRENCY = 4
```

Дополнительные константы:

```python
HH = "https://hh.ru"
HABR = "https://career.habr.com"
HABR_API = f"{HABR}/api/frontend/vacancies"
REMOTEOK_API = "https://remoteok.com/api"
PAGE_PAUSE = 0.35
MAX_RETRIES = 4
IMPERSONATE = "chrome"
```

---

## 4. HTTP-слой

```python
from curl_cffi import requests

def new_session() -> requests.AsyncSession:
    return requests.AsyncSession(
        impersonate=IMPERSONATE,
        headers={
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
```

`_get(session, url, params=None, timeout=None, headers=None)`:
- GET с `impersonate="chrome"`, таймаут по умолчанию `REQUEST_TIMEOUT`;
- ретраи до `MAX_RETRIES` на `403`, `429`, `5xx`, таймауты и сетевые исключения;
- экспоненциальный backoff: `min(2 ** attempt + random.random(), 8)` секунд;
- после успеха `resp.raise_for_status()` и вернуть `resp`;
- после исчерпания попыток поднять `ScraperError(f"{url}: {last_error}")`;
- не логировать тело ответа и заголовки с куками.

Для Habr и RemoteOK передавать `headers={"Accept": "application/json"}`.
Для RemoteOK таймаут `REMOTEOK_TIMEOUT`.

Логирование: `log = logging.getLogger("scraper")`. Не логировать каждый успешный запрос; только итоги источника, ретраи и ошибки.

---

## 5. Нормализация и общие утилиты

### 5.1. `html_to_text`

- Удалить `<script>...</script>` и `<style>...</style>` (regex с `re.S`).
- Заменить `<br>` на `\n`.
- Блочные теги `p`, `div`, `h1`–`h6`, `tr` — на `\n`.
- `<li>` — на `\n- `, `</li>` — на `\n`.
- Распарсить через `html.parser.HTMLParser` с `convert_charrefs=True`, собрать текст.
- Нормализовать пробелы: `[ \t\r\f\v]+` -> один пробел; `\n{3,}` -> `\n\n`; strip.
- Обрезать до `limit` (20 000) символов.
- `None` -> `""`.

### 5.2. `_clean_line(value, max_len)`

Для `title`, `company`, `location`, тегов: убрать теги, `html.unescape`, схлопнуть все пробелы/переводы строк в один пробел, strip, обрезать.

### 5.3. `_to_utc_iso(value)`

Поддерживает:
- `datetime` (naive считать UTC);
- строку ISO 8601 с `Z` или offset;
- строку `YYYY-MM-DD`;
- int/float epoch (секунды).

Возвращает `YYYY-MM-DDTHH:MM:SSZ` или `None`. Невалидное значение — `None`, без исключения.

### 5.4. `_canonical_url(url, source, source_id)`

- `hh`: всегда `https://hh.ru/vacancy/{source_id}`.
- `habr`: всегда `https://career.habr.com/vacancies/{source_id}`.
- `remoteok`: взять переданный URL, привести схему к `https`, host к нижнему регистру, удалить fragment и tracking-параметры: все `utm_*`, `from`, `hhtmFrom`, `gh_src`, `trk`, `ref`.
- Для остальных — общая очистка.
- Если URL пустой/невалидный — вернуть `None`.

### 5.5. `_build_vacancy(...)`

Единая точка сборки словаря. Принимает нормализованные поля и `raw`. Возвращает `dict | None`.

Правила:
- `source` — только `hh`/`habr`/`remoteok`;
- `source_id` — непустая строка;
- `title` — до 300 символов; пустой -> `None`;
- `company` — до 200; пустой -> `"Не указана"`;
- `url` — абсолютный `https://`; иначе `None`;
- `location` — до 200 или `None`;
- `salary_from`, `salary_to` — `int >= 0` или `None`; если оба заданы и `from > to` — поменять;
- `currency` — только `RUB`/`USD`/`EUR` или `None`;
- `salary_period` — только `month`/`year` или `None`;
- `published_at` — ISO UTC или `None`;
- `description` — `html_to_text`, до 20 000;
- `tags` — до 20 уникальных (case-insensitive), каждый до 40 символов, без пустых;
- `remote` — `bool`;
- `it_score` — переданный score;
- `raw` — исходный dict.

Финальный словарь содержит **ровно** ключи из контракта `NormalizedVacancy` (см. мастер-промпт), включая `raw`.

### 5.6. Дедупликация батча

`_dedupe(items)`:
- ключ `(source, source_id)`;
- при конфликте оставить запись с более длинным `description`;
- порядок первого появления сохранить.

---

## 6. Жёсткая IT-фильтрация

Реализовать ровно по мастер-промпту. Ниже полные списки — используй их в коде.

### 6.1. Нормализация для поиска

```python
def _norm(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    text = text.replace("\u00a0", " ").replace("\u202f", " ")
    return re.sub(r"\s+", " ", text).strip()
```

Поиск токенов:

```python
def _token_hits(text: str, tokens: tuple[str, ...]) -> list[str]:
    low = _norm(text)
    hits = []
    for token in tokens:
        pattern = rf"(?<![a-zа-я0-9]){re.escape(token)}(?![a-zа-я0-9])"
        if re.search(pattern, low):
            hits.append(token)
    return hits
```

### 6.2. Чёрный список заголовков

Русские стемы проверять **подстрокой**, латинские токены — по границам слова. Любое совпадение — немедленный отказ `"non-it title"`.

```python
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
```

### 6.3. IT-токены

```python
IT_TOKENS = (
    "python", "java", "javascript", "typescript", "golang", "go", "rust",
    "c++", "c#", ".net", "php", "ruby", "kotlin", "swift", "scala", "sql",
    "postgresql", "mysql", "mongodb", "redis", "react", "vue", "angular",
    "node.js", "node", "django", "flask", "fastapi", "spring", "laravel",
    "symfony", "backend", "frontend", "fullstack", "full-stack", "full stack",
    "mobile", "mobile developer", "developer", "software engineer", "dev",
    "data science", "android", "ios", "ml", "ai", "llm", "rag", "nlp",
    "computer vision", "data scientist", "data engineer", "data analyst",
    "аналитик данных",
    "системный аналитик", "бизнес-аналитик", "продуктовый аналитик",
    "ml engineer", "mlops", "devops", "sre", "kubernetes", "docker",
    "terraform", "ansible", "linux", "git", "ci/cd", "aws", "azure", "gcp",
    "qa", "sdet", "тестировщик", "тестирование", "автотесты",
    "автоматизатор тестирования", "разработчик", "программист",
    "инженер-программист", "инженер по тестированию", "инженер данных",
    "системный администратор", "сисадмин", "информационная безопасность",
    "пентестер", "1с", "битрикс", "архитектор по", "tech lead", "team lead",
    "api", "rest", "graphql", "микросервисы",
)
```

### 6.4. Не-IT признаки в описании

Проверять подстрокой, только если в заголовке нет IT-токенов.

```python
NON_IT_BODY_PHRASES = (
    "холодные звонки", "холодных звонков", "план продаж", "плана продаж",
    "входящие звонки", "исходящие звонки", "работа с клиентами",
    "привлечение клиентов", "выполнение плана", "опыт в продажах",
    "active sales", "cold calls", "sales pipeline", "lead generation",
)
```

### 6.5. Алгоритм `is_it_vacancy`

```text
1. title_norm = _norm(title)
2. Если _is_non_it_title(title):
       return False, 0, "non-it title"
3. title_hits = _token_hits(title, IT_TOKENS)
4. tag_hits   = _token_hits(" ".join(tags), IT_TOKENS)
5. desc_hits  = _token_hits(description, IT_TOKENS)
6. Если title_hits пуст и в description есть любая фраза NON_IT_BODY_PHRASES:
       return False, 0, "non-it body"
7. Если title_hits непуст:  return True, 100, None
8. Если len(tag_hits) >= 2: return True, 75, None
9. Если len(tag_hits) >= 1 и len(desc_hits) >= 3: return True, 65, None
10. return False, 0, "no it signals"
```

Считать уникальные токены, не количество вхождений.

---

## 7. hh.ru

Официальный JSON API `api.hh.ru` в этом окружении отдаёт 403, поэтому использовать **HTML-страницы** через `curl_cffi` с `impersonate="chrome"`.

### 7.1. Поиск

URL: `https://hh.ru/search/vacancy`.

Параметры:
```python
{
    "text": HH_QUERY,
    "area": HH_AREA,
    "resultsPerPage": "50",
    "order_by": "publication_time",
    "page": str(page),          # 0..HH_MAX_PAGES-1
}
# если HH_EXPERIENCE непустой: params["experience"] = HH_EXPERIENCE
```

Между страницами — `await asyncio.sleep(PAGE_PAUSE)`. Остановиться, если страница не дала новых ID.

### 7.2. Парсинг поисковой страницы

```python
def _parse_hh_search(html: str) -> list[dict]:
    out = []
    blocks = html.split('data-qa="vacancy-serp__vacancy"')[1:]
    for block in blocks:
        m = re.search(r'href="(?:https?://(?:[a-z0-9-]+\.)*hh\.ru)?/vacancy/(\d+)', block)
        if not m:
            continue
        hh_id = m.group(1)
        title = _hh_field(block, "serp-item__title-text")
        company = _hh_field(block, "vacancy-serp__vacancy-employer-text")
        area = _hh_field(block, "vacancy-serp__vacancy-address")
        salary_from, salary_to = _hh_salary(block)
        out.append({
            "source": "hh",
            "source_id": hh_id,
            "title": title,
            "company": company,
            "location": area,
            "salary_from": salary_from,
            "salary_to": salary_to,
            "currency": "RUB" if (salary_from or salary_to) else None,
            "salary_period": "month",
            "published_at": None,
            "description": "",
            "tags": [],
            "remote": "удал" in (area or "").lower(),
            "raw": {"search_block": block[:2000]},
        })
    return out
```

`_hh_field(block, qa)` — regex по `data-qa="{qa}"` с захватом содержимого до `</span>`, `</a>`, `</div>` или `</h1>`, затем `_clean_line`.

`_hh_salary(block)` — regex по числам с `₽`, поддержать неразрывные пробелы `\u00a0`/`\u202f` и диапазон `100 000 – 150 000 ₽`. Вернуть `(int|None, int|None)`, цифры извлечь через `re.sub(r"\D", "", s)`.

### 7.3. Детали

URL: `https://hh.ru/vacancy/{id}`.

- `title` — `data-qa="vacancy-title"`;
- `company` — `data-qa="vacancy-company-name"`;
- `description` — вырезать блок `<div ... data-qa="vacancy-description" ...>` с балансировкой вложенных `<div>` (как в существующем scraper.py), затем `html_to_text`;
- `published_at` — попробовать `<meta property="article:published_time" content="...">`, иначе `None`;
- если детальная страница не загрузилась — не ронять весь источник: использовать пустое описание и логировать warning (не более 3 ошибок в `SourceResult.errors`).

### 7.4. Обработка

- `fetched = len(raw_items)` — до фильтра.
- Перед деталями отбросить записи с `"non-it title"` (вызвать `is_it_vacancy(title, [], "")` и проверить причину).
- Остальные обработать с `asyncio.Semaphore(DETAIL_CONCURRENCY)`: загрузить детали, объединить поля, вызвать `is_it_vacancy(title, tags, description)`.
- Если принято — `_build_vacancy(..., it_score=score)`; если `None` — пропустить.
- `tags` для hh: извлечь из описания до 20 известных IT-токенов, которые реально встречаются (без выдумывания). Если не получилось — `[]`.
- `source_id` — строка цифр; `url` — канонический.
- `errors` — до 3 сообщений на источник.

---

## 8. Habr Career

Использовать внутренний JSON API (проверен, отдаёт JSON):

```text
GET https://career.habr.com/api/frontend/vacancies?q=python&page=1
```

Заголовки: `Accept: application/json`. Ответ:

```json
{
  "list": [
    {
      "id": 1000168585,
      "href": "/vacancies/1000168585",
      "title": "Python Developer",
      "remoteWork": true,
      "publishedDate": {"date": "2026-11-06T10:27:19+03:00"},
      "company": {"title": "IBS"},
      "salary": {"from": null, "to": null, "currency": null},
      "divisions": [{"title": "ML"}],
      "skills": [{"title": "Python"}],
      "locations": [{"title": "Москва"}]
    }
  ],
  "meta": {"totalResults": 421, "perPage": 25, "currentPage": 1, "totalPages": 17}
}
```

### 8.1. Сбор

- `queries = [q.strip() for q in SCRAPE_QUERIES.split(",") if q.strip()][:8]`.
- Для каждого запроса страницы `1..HABR_MAX_PAGES`.
- Остановиться, если `list` пуст или `page >= meta.totalPages`.
- Между страницами `await asyncio.sleep(PAGE_PAUSE)`.
- Дедуп по `id` между запросами.
- `fetched` — количество уникальных сырых элементов до фильтра.

### 8.2. Поля

- `source_id = str(item["id"])`;
- `title = item["title"]`;
- `company = item["company"]["title"]`;
- `href = item["href"]`;
- `url = f"https://career.habr.com{href}"` (далее канонизировать);
- `remote = bool(item.get("remoteWork"))` или `"удал"` в локации;
- `location` — первый `locations[].title` или `None`;
- `published_at = _to_utc_iso((item.get("publishedDate") or {}).get("date"))`;
- `salary_from/to` — из `salary.from`/`salary.to`; `currency` — `rur`/`rub` -> `RUB`, `usd` -> `USD`, `eur` -> `EUR`, иначе `None`;
- `salary_period = "month"`;
- `tags` — `skills[].title` + `divisions[].title`, дедуп;
- `description = ""` до загрузки деталей;
- `raw = item`.

### 8.3. Детали

URL: `https://career.habr.com{href}` (HTML).

- Найти `<script type="application/ld+json">...</script>`.
- `json.loads`; поддержать:
  - dict с `"@type": "JobPosting"`;
  - список, в котором найти `JobPosting`;
  - dict с `"@graph"`, в котором найти `JobPosting`.
- Взять `description` (HTML -> `html_to_text`) и `datePosted` (если `published_at` пуст).
- Fallback: regex по `<div class="vacancy-description__text">...</div>` (или `vacancy-description`), затем `html_to_text`.
- Если детальная страница не загрузилась — использовать `fetched` данные и `tags`; фильтр всё равно выполнить.

### 8.4. Обработка

- Предварительная проверка: `is_it_vacancy(title, tags, "")`.
  - причина `"non-it title"` — сразу пропустить, детали не грузить;
  - причина `"no it signals"` — пропустить (у Habr богатые теги; не тратим запросы);
  - иначе — загрузить детали и выполнить финальный `is_it_vacancy(title, tags, description)`.
- Для принятых — `_build_vacancy(..., it_score=score)`.
- Детали грузить с тем же `DETAIL_CONCURRENCY` семафором.

---

## 9. RemoteOK

API: `GET https://remoteok.com/api`, заголовок `Accept: application/json`, таймаут `REMOTEOK_TIMEOUT`.

Ответ — JSON-массив. Первый элемент — legal notice без `id`, его пропустить. Далее элементы:

```json
{
  "id": "1137713",
  "slug": "remote-senior-software-engineer",
  "position": "Senior Software Engineer",
  "company": "Buoyant",
  "description": "<p>...</p>",
  "tags": ["golang", "dev", "senior"],
  "location": "",
  "salary_min": 0,
  "salary_max": 0,
  "date": "2026-11-09T22:00:02+00:00",
  "url": "https://remoteok.com/remote-jobs/...",
  "apply_url": "https://remoteok.com/remote-jobs/..."
}
```

Правила:
- Отобрать только `dict` с непустыми `id` и `position`; взять первые `REMOTEOK_LIMIT` элементов.
- `source_id = str(item["id"])`;
- `title = item["position"]`;
- `company = item.get("company") or "Не указана"`;
- `description = html_to_text(item.get("description", ""))`;
- `tags = [str(t) for t in item.get("tags", []) if t]`;
- `location = item.get("location") or None`;
- `remote = True`;
- `salary_from/to`: `int` только если `> 0`, иначе `None`;
- `currency = "USD"` если есть хотя бы одна зарплата, иначе `None`;
- `salary_period = "year"`;
- `published_at = _to_utc_iso(item.get("date") or item.get("epoch"))`;
- `url = item.get("url") or item.get("apply_url") or f"https://remoteok.com/remote-jobs/{slug}"`;
- `raw = item`;
- `fetched` — количество отобранных сырых элементов;
- фильтр `is_it_vacancy(title, tags, description)`; принятые — `_build_vacancy`.

Детальные запросы для RemoteOK не делать: описание уже в API.

Важно: API RemoteOK требует атрибуции. Код `scraper.py` её не рендерит — это делает `app.js` в футере. Не добавляй ссылку в данные вакансии.

---

## 10. Обработка ошибок и границы

- Ошибка одной страницы/вакансии не должна ронять источник.
- Если источник полностью недоступен — вернуть `SourceResult(items=[], fetched=0, errors=["..."])`, не бросать исключение наружу (кроме `scrape_all`, где ошибка источника перехватывается и попадает в `ScrapeReport.errors`).
- `SourceResult.errors` — не более 3 коротких сообщений (до 300 символов каждое).
- Не логировать персональные данные, куки, токены.
- Не выполнять auto-apply, не отправлять формы, не авторизовываться.
- Не обходить CAPTCHA. При 403/429 использовать backoff и завершать источник с ошибкой.
- Не использовать `time.sleep`; только `asyncio.sleep`.

---

## 11. Definition of Done для scraper.py

- [ ] Файл импортируется без побочных эффектов (никаких сетевых запросов на import).
- [ ] `scrape_all` возвращает `ScrapeReport` и корректно закрывает сессию.
- [ ] Все три источника реализованы, ошибки изолированы.
- [ ] `is_it_vacancy` строго по алгоритму; продажи/рекрутинг/логистика отсекаются.
- [ ] Нет дублей `(source, source_id)` в результате.
- [ ] Все поля соответствуют `NormalizedVacancy`.
- [ ] `salary_period` у hh/Habr — `month`, у RemoteOK — `year`.
- [ ] `description` очищен от HTML и обрезан до 20 000.
- [ ] `url` абсолютный, без tracking-параметров.
- [ ] Нет `TODO`, `pass`, `NotImplementedError`, `print`.
- [ ] Нет импортов `main`, `database`, `config`, FastAPI, aiosqlite.
