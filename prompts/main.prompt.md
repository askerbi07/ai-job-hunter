# ПРОМПТ: main.py

Ты — Senior FastAPI-архитектор. Пишешь production-ready backend для локального приложения AI Job Hunter.

Верни **только исходный код файла `main.py`**. Без markdown-обёрток, без языковых меток, без пояснений, без вопросов, без TODO и заглушек. Код должен запускаться командой:

```bash
uvicorn main:app --reload --port 8000
```

Общие контракты — в `prompts/00_MASTER.md`. Ниже — самодостаточное ТЗ на `main.py`.

---

## 1. Назначение файла

`main.py` — единственная точка входа приложения. Он содержит:

1. FastAPI-приложение и lifespan.
2. Асинхронный слой SQLite на `aiosqlite` (WAL).
3. Фоновый прогон сбора через `scraper.scrape_all`.
4. REST API по контракту.
5. Раздачу `templates/index.html` и `/static`.

**Не содержит** парсеров площадок, HTML-регексов, LLM-кода, планировщика, генерации писем.

Разрешённые импорты: stdlib, `fastapi`, `pydantic`, `aiosqlite`, `scraper`. Запрещены `database.py`, `config.py`, `ai_agent.py`, `scheduler.py`, `requests`, `httpx`, `SQLAlchemy`.

---

## 2. Конфигурация и константы

```python
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = (BASE_DIR / os.getenv("DB_PATH", "data/jobs.db")).resolve()

VALID_STATUS = {"new", "liked", "rejected", "applied", "archived"}
VALID_SOURCES = {"hh", "habr", "remoteok"}
```

`DB_PATH.parent.mkdir(parents=True, exist_ok=True)` при инициализации.

Схема рассчитана на новую БД. Не делать миграцию старой схемы. При наличии старой несовместимой `data/jobs.db` пользователь удаляет её или задаёт другой `DB_PATH`.

Логирование:

```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")
```

`now_iso()`:

```python
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

---

## 3. SQLite-слой

### 3.1. Схема

Использовать ровно DDL из мастер-промпта. Создавать через `executescript` в `init_db()`.

Дополнительно при подключении:

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

### 3.2. Соединение

- Одно глобальное соединение на процесс: `_db: aiosqlite.Connection | None = None`.
- `asyncio.Lock` для операций записи: `_write_lock = asyncio.Lock()`.
- `row_factory = aiosqlite.Row`.
- Ленивое создание через `async def get_db() -> aiosqlite.Connection`.
- `async def init_db() -> None`, `async def close_db() -> None`.

### 3.3. Функции

```python
async def upsert_vacancies(items: list[dict]) -> int: ...
async def list_vacancies(
    page: int,
    page_size: int,
    source: str | None = None,
    status: str | None = None,
    q: str | None = None,
    remote: bool | None = None,
    min_salary: int | None = None,
) -> tuple[list, int]: ...          # (rows, total)
async def get_vacancy(vacancy_id: int): ...
async def set_status(vacancy_id: int, status: str): ...
async def stats() -> dict: ...
async def create_run(run_id: str) -> None: ...
async def update_run(run_id: str, **fields) -> None: ...
async def get_run(run_id: str): ...
async def last_run(): ...
```

### 3.4. Upsert

- Пропускать элементы без `source`/`source_id`/`title`/`url`.
- `tags_json = json.dumps(tags, ensure_ascii=False)`.
- `raw_json = json.dumps(raw or {}, ensure_ascii=False)`.
- `remote` хранить как `1`/`0`.
- `first_seen = last_seen = now_iso()` для новой строки.
- SQL:

```sql
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
```

`status` и `first_seen` при конфликте **не обновлять**.
Выполнять в одной транзакции под `_write_lock`; вернуть количество обработанных элементов.

### 3.5. Список вакансий

- Динамический `WHERE` только из перечисленных параметров; значения — через `?`.
- `q`: `title LIKE ? OR company LIKE ? OR description LIKE ?`, шаблон `%q%`.
- `remote`: `remote = 1` / `remote = 0`.
- `min_salary`: `(salary_from >= ? OR salary_to >= ?)`.
- Сортировка: `ORDER BY (published_at IS NULL) ASC, published_at DESC, first_seen DESC, id DESC`.
- `LIMIT ? OFFSET ?`: `(page - 1) * page_size`.
- `total` — отдельный `SELECT COUNT(*)` с теми же условиями.
- Никакой конкатенации пользовательских значений в SQL.

### 3.6. Сериализация строки

`_serialize(row)`:
- `dict(row)`;
- `tags` = `json.loads(tags_json)` (при ошибке — `[]`);
- `remote` = `bool(remote)`;
- удалить `raw_json`;
- вернуть все поля контракта `VacancyOut`.

---

## 4. Pydantic-модели

Pydantic v2 (FastAPI >= 0.110).

```python
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
```

`StatusUpdate.status` — строка; валидировать вручную, чтобы вернуть `400`, а не `422`.

---

## 5. Фоновый прогон

Глобальные переменные:

```python
_run_task: asyncio.Task | None = None
_run_id: str | None = None
```

`async def _run_scrape(run_id: str) -> None`:

1. Строка `runs` уже создана в `POST /api/scrape` (до запуска задачи), поэтому `GET /api/runs/{id}` сразу находит её.
2. Определить callback:

```python
async def on_progress(phase: str, fetched: int, filtered: int) -> None:
    await update_run(run_id, phase=phase, fetched=fetched, filtered=filtered)
```

3. `report = await scrape_all(on_progress)`.
4. `await update_run(run_id, phase="save", fetched=report.fetched, filtered=report.filtered, errors_json=json.dumps(report.errors, ensure_ascii=False))`.
5. `saved = await upsert_vacancies(report.items)`.
6. `await update_run(run_id, phase="done", finished_at=now_iso(), saved=saved)`.
7. Любое исключение: `log.exception`, `await update_run(run_id, phase="error", finished_at=now_iso(), error=str(e)[:1000])`. Не пробрасывать наружу.

Ошибки отдельных источников не являются фатальными: они лежат в `report.errors`, прогон завершается `phase="done"`, `error` остаётся `None`.

---

## 6. Lifespan и приложение

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    # при завершении: отменить фоновую задачу, если жива
    task = _run_task
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await close_db()

app = FastAPI(title="AI Job Hunter", version="2.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
```

- `GET /` -> `FileResponse(BASE_DIR / "templates" / "index.html")`.
- Если `templates/index.html` отсутствует, FastAPI вернёт 404/500 — это ожидаемо, HTML вне скоупа.
- CORS не добавлять: same-origin.

---

## 7. Эндпоинты

### `GET /api/vacancies`

Query:

```python
page: int = Query(1, ge=1)
page_size: int = Query(20, ge=1, le=100)
source: str | None = None
status: str | None = None
q: str | None = Query(None, max_length=200)
remote: bool | None = None
min_salary: int | None = Query(None, ge=0)
```

Валидация:
- `source` не из `VALID_SOURCES` -> `HTTPException(400, ...)`;
- `status` не из `VALID_STATUS` -> `HTTPException(400, ...)`.

Ответ — `PageOut`. `pages = 0`, если `total == 0`, иначе `(total + page_size - 1) // page_size`.

### `GET /api/vacancies/{vacancy_id}`

`404` — `{"detail": "vacancy не найдена"}`. Иначе `VacancyOut`.

### `PATCH /api/vacancies/{vacancy_id}`

Тело `StatusUpdate`.
- Невалидный статус -> `400`.
- Нет вакансии -> `404`.
- Успех -> обновлённый `VacancyOut`.

### `POST /api/scrape`

```python
global _run_task, _run_id

if _run_task is not None and not _run_task.done():
    return {"run_id": _run_id, "started": False}

rid = uuid.uuid4().hex[:12]
await create_run(rid)
_run_id = rid
_run_task = asyncio.create_task(_run_scrape(rid))
return {"run_id": rid, "started": True}
```

Не использовать `BackgroundTasks` (нет доступа к статусу). Задачу хранить в глобальной переменной, чтобы её не собрал GC. Не забыть `global _run_task, _run_id` в функции эндпоинта.

### `GET /api/runs/{run_id}`

`404`, если нет. Иначе `RunOut` (dict строки; `errors_json` — строка, как в БД).

### `GET /api/stats`

```json
{
  "total": 0,
  "by_source": {"hh": 0, "habr": 0, "remoteok": 0},
  "by_status": {"new": 0, "liked": 0, "rejected": 0, "applied": 0, "archived": 0}
}
```

Все ключи присутствуют всегда.

### `GET /api/health`

```json
{"status": "ok", "total": 0, "last_run": null}
```

`last_run` — `RunOut` или `null`. Не делать внешних запросов, чтобы health был быстрым.

---

## 8. Требования к качеству

- Все endpoint-функции `async def`.
- Все обращения к БД — через `await`.
- Никаких блокирующих вызовов в event loop.
- Все SQL-параметры — через `?`. Динамически собирать только имена полей из фиксированного whitelist.
- `update_run` принимает только разрешённые поля: `phase`, `fetched`, `filtered`, `saved`, `errors_json`, `error`, `finished_at`. Иначе `ValueError`.
- `stats()` и `health` не должны падать при пустой БД.
- `_serialize` не отдаёт `raw_json`.
- `description` не обрезать повторно в API (scraper уже ограничил 20 000).
- `POST /api/scrape` не ждёт завершения сбора.
- При повторном сборе дубли не создаются.
- При падении одной площадки прогон завершается, две другие данные сохраняются.
- Логи не содержат секретов и полных ответов площадок.

---

## 9. Definition of Done для main.py

- [ ] `uvicorn main:app --reload --port 8000` стартует без ошибок.
- [ ] `GET /api/health` возвращает `status: "ok"` на пустой БД.
- [ ] `GET /api/vacancies` возвращает конверт `PageOut`.
- [ ] `POST /api/scrape` возвращает `run_id` мгновенно.
- [ ] `GET /api/runs/{run_id}` доступен сразу после `POST`.
- [ ] Повторный `POST /api/scrape` во время прогона возвращает `started: false` и тот же `run_id`.
- [ ] Upsert идемпотентен, `status` и `first_seen` не перезаписываются.
- [ ] Пагинация и фильтры работают через SQL, а не в Python.
- [ ] Нет импортов `database`, `config`, `ai_agent`, `scheduler`.
- [ ] Нет `TODO`, `pass`, `NotImplementedError`, `print`.
- [ ] Нет синхронных сетевых вызовов и синхронного SQLite.
