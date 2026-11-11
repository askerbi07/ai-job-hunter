# ПРОМПТ: static/app.js

Ты — Senior Frontend-инженер. Пишешь production-ready Vanilla JS для локального дашборда AI Job Hunter.

Верни **только исходный код файла `static/app.js`**. Без markdown-обёрток, без языковых меток, без пояснений, без вопросов, без TODO и заглушек.

Общие контракты — в `prompts/00_MASTER.md`. Ниже — самодостаточное ТЗ на `app.js`.

---

## 1. Назначение файла

`app.js` — весь клиентский слой:

1. Загружает вакансии с `GET /api/vacancies` (серверная пагинация).
2. Рендерит карточки с цветными бейджами.
3. Управляет фильтрами: статус, источник, поиск, удалёнка.
4. Меняет статус вакансии через `PATCH /api/vacancies/{id}`.
5. Запускает сбор через `POST /api/scrape` и опрашивает `GET /api/runs/{run_id}`.
6. Показывает статистику, health, пустые состояния, toast-ошибки.
7. Поддерживает горячие клавиши.

**Не содержит** парсинга, работы с БД, HTML-шаблонов, LLM-кода.

Ограничения: ES2020+, без фреймворков, без npm, без TypeScript, без внешних библиотек. Tailwind CSS уже подключён в `templates/index.html` через CDN. `"use strict";` обязателен.

---

## 2. Контракт DOM

`app.js` должен работать с существующим `templates/index.html`, но не падать, если каких-то элементов нет. Функция `ensureLayout()` создаёт **только отсутствующие** элементы с минимальной Tailwind-разметкой.

Обязательные/опциональные элементы:

| ID | Назначение | Если нет |
|---|---|---|
| `#cards` | контейнер карточек | создать `<section id="cards" class="space-y-3">` внутри `main` (или в `body`) |
| `#empty` | пустое состояние | создать после `#cards` |
| `#count` | счётчик найденного | создать рядом с фильтрами |
| `#tabs` | табы статусов | создать `nav` с кнопками |
| `#f-q` | поиск | создать `input[type=search]` |
| `#f-source` | фильтр источника | создать `select` с опциями `all/hh/habr/remoteok` |
| `#f-remote` | чекбокс «Удалёнка» | создать `label > input[type=checkbox]` |
| `#btn-scrape` | кнопка запуска | использовать `#btn-run`, иначе создать кнопку |
| `#progress-wrap` | обёртка прогресса | создать |
| `#progress-phase` | текст фазы | создать |
| `#progress-bar` | полоса прогресса | создать |
| `#progress-count` | счётчики | создать |
| `#pagination` | контейнер пагинации | создать после `#cards` |
| `#pg-prev` | кнопка «Назад» | создать внутри `#pagination` |
| `#page-buttons` | кнопки страниц | создать внутри `#pagination` |
| `#pg-next` | кнопка «Вперёд» | создать внутри `#pagination` |
| `#page-info` | текст «Страница X из Y» | создать после `#pagination` |
| `#stats` | статистика по источникам | создать |
| `#health-dot` | индикатор здоровья | использовать `#ollama-dot`, иначе создать |
| `#health-label` | текст здоровья | использовать `#ollama-label`, иначе создать |
| `#toasts` | контейнер уведомлений | создать `fixed bottom-4 right-4` |
| `#attribution` | атрибуция RemoteOK | создать в футере |

Для обратной совместимости:

```javascript
const $scrape = $("#btn-scrape") || $("#btn-run");
const $healthDot = $("#health-dot") || $("#ollama-dot");
const $healthLabel = $("#health-label") || $("#ollama-label");
```

`ensureLayout()` вызывается один раз в `DOMContentLoaded` до остальной инициализации. Он не должен дублировать существующие элементы.

---

## 3. Состояние и константы

```javascript
const state = {
  page: 1,
  pageSize: 20,
  source: "all",
  status: "all",
  q: "",
  remote: null,          // null = без фильтра, true = только удалёнка
  items: [],
  total: 0,
  pages: 0,
  loading: false,
  requestSeq: 0,
  activeIndex: -1,
  run: null,             // { id, timer }
  stats: null,
};
```

Константы:

```javascript
const SOURCE_META = {
  hh:       { label: "hh.ru",       cls: "border-red-500/40 bg-red-500/10 text-red-300" },
  habr:     { label: "Habr Career", cls: "border-sky-500/40 bg-sky-500/10 text-sky-300" },
  remoteok: { label: "RemoteOK",    cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300" },
};

const STATUS_META = {
  new:      { label: "Новая",     cls: "border-indigo-500/40 bg-indigo-500/10 text-indigo-300" },
  liked:    { label: "Лайк",      cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300" },
  rejected: { label: "Скип",      cls: "border-rose-500/40 bg-rose-500/10 text-rose-300" },
  applied:  { label: "Отклик",    cls: "border-amber-500/40 bg-amber-500/10 text-amber-300" },
  archived: { label: "Архив",     cls: "border-slate-500/40 bg-slate-500/10 text-slate-300" },
};

const TABS = [
  ["all", "Все"], ["new", "Новые"], ["liked", "Лайки"],
  ["rejected", "Скипы"], ["applied", "Отклики"], ["archived", "Архив"],
];

const PHASES = {
  queued: "В очереди…",
  fetch_hh: "Сбор hh.ru…",
  fetch_habr: "Сбор Habr Career…",
  fetch_remoteok: "Сбор RemoteOK…",
  save: "Сохранение в базу…",
  done: "Готово",
  error: "Ошибка",
};

const PHASE_PCT = {
  queued: 5, fetch_hh: 25, fetch_habr: 45,
  fetch_remoteok: 65, save: 85, done: 100, error: 100,
};
```

---

## 4. Базовые утилиты

```javascript
const $ = (sel) => document.querySelector(sel);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
```

`api(path, options)`:
- `fetch` с заголовком `Content-Type: application/json`;
- при `!res.ok` попытаться прочитать `detail` из JSON, иначе `statusText`;
- бросить `Error(detail)`;
- `204` -> `null`;
- иначе `res.json()`.

`debounce(fn, ms)` — стандартная реализация.

`fmtSalary(v)`:
- если нет `salary_from` и `salary_to` — `""`;
- символы: `RUB` -> `₽`, `USD` -> `$`, `EUR` -> `€`;
- `Intl.NumberFormat("ru-RU")`;
- суффикс: `month` -> `/мес`, `year` -> `/год`;
- варианты: `от X`, `до Y`, `X – Y`;
- пример: `120 000 – 180 000 ₽/мес`, `$120 000 – $180 000/год`.

`fmtDate(iso)` — `toLocaleDateString("ru-RU", {day:"2-digit", month:"short", year:"numeric"})`; при невалидной дате — `""`.

`toast(message, type = "info")`:
- контейнер `#toasts`;
- типы: `ok` (emerald), `err` (rose), `info` (slate);
- удалить через 4 секунды;
- `role="status"`.

`copyText` не нужен — писем в этом скоупе нет.

---

## 5. Загрузка данных

### 5.1. `loadPage()`

- Не блокировать новые запросы через `state.loading`: при быстрой смене фильтров старый ответ не должен перетирать новый. Использовать счётчик:
  - `const seq = ++state.requestSeq;`
  - `state.loading = true;`
- `#count` -> `"Загрузка…"`.
- Собрать `URLSearchParams`:
  - `page`, `page_size` всегда;
  - `source`, если не `all`;
  - `status`, если не `all`;
  - `q`, если непустой;
  - `remote=true`, если `state.remote === true`.
- `GET /api/vacancies?...`.
- Если `seq !== state.requestSeq` — ответ устарел, выйти без рендера.
- Записать `state.items`, `state.total`, `state.pages`, `state.page` (из ответа).
- `renderCards()`, `renderPagination()`, `renderStats()`.
- `#count` -> `Найдено: ${total}`.
- Ошибка: если `seq === state.requestSeq` — `toast(e.message, "err")` и показать пустое состояние с текстом ошибки.
- `finally`: если `seq === state.requestSeq`, `state.loading = false`.

### 5.2. `loadStats()`

`GET /api/stats`; сохранить в `state.stats`; `renderStats()`. Ошибку игнорировать (не критично), но логировать в `console.warn`.

### 5.3. `loadHealth()`

`GET /api/health`:
- зелёная точка, если `status === "ok"`, иначе красная;
- `#health-label` -> `Вакансий: ${total}`;
- если `last_run` есть — можно показать `последний сбор: дата` в `title` или отдельном `#last-run`, если он есть. Не создавай лишнюю сущность без необходимости.
- Запускать сразу и далее `setInterval(loadHealth, 20000)`.

---

## 6. Рендер карточек

`card(v)` возвращает `<article>`:

- классы: `fade-in rounded-2xl border border-slate-800 bg-slate-900/60 p-4`;
- `dataset.id = v.id`;
- **шапка**: заголовок-ссылка (`v.title` или `"Без названия"`), `href=v.url`, `target="_blank"`, `rel="noopener noreferrer"`, классы `font-semibold text-slate-100 hover:text-indigo-300`;
- **бейджи**: источник, статус, `Удалёнка` (если `v.remote`), `IT ${v.it_score}`;
- **мета**: компания, локация, зарплата, дата публикации — `text-xs text-slate-400`;
- **теги**: первые 8, чипы `rounded-md bg-slate-800 px-2 py-0.5 text-[11px] text-slate-300`; если больше — `+N`;
- **описание**: первые 260 символов `v.description`, `text-sm text-slate-400 leading-relaxed`; добавить `…`, если обрезано;
- **действия**:
  - кнопки статусов: `Лайк` (liked), `Скип` (rejected), `Отклик` (applied), `Архив` (archived);
  - активная кнопка получает соответствующий цвет из `STATUS_META` + `border-...`, неактивная — `border-slate-700 text-slate-300 hover:bg-slate-800`;
  - кнопка `Открыть ↗` — ссылка на `v.url`, `target="_blank"`, `rel="noopener noreferrer"`;
- **бейдж функции**:

```javascript
function badge(text, cls) {
  return el("span", `inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium ${cls}`, text);
}
```

- IT-бейдж:
  - `it_score >= 80` -> `border-emerald-500/40 bg-emerald-500/10 text-emerald-300`;
  - `65..79` -> `border-amber-500/40 bg-amber-500/10 text-amber-300`;
  - иначе -> `border-slate-500/40 bg-slate-500/10 text-slate-300`.

**Безопасность:** все текстовые данные — только через `textContent`/`el(...)`. `innerHTML` с `v.title`, `v.company`, `v.description`, `v.tags` запрещён.

`renderCards()`:
- очистить `#cards` (`replaceChildren()` или `innerHTML = ""` — только для очистки);
- если `state.items.length === 0` — показать `#empty`, скрыть `#cards`? Лучше оставить `#cards` пустым, `#empty` показать;
- иначе `#empty` скрыть, добавить карточки через `DocumentFragment`;
- сбросить `state.activeIndex = -1`.

`renderStats()`:
- если `state.stats` нет — выйти;
- отсутствующие ключи `by_source` считать нулями;
- `#stats` -> `Всего: N · hh.ru: X · Habr: Y · RemoteOK: Z`;
- использовать `textContent`.

---

## 7. Фильтры

### 7.1. Табы статусов

`renderTabs()`:
- очистить `#tabs`;
- для каждого `[status, label]` из `TABS` создать `button type="button"`;
- активный: `bg-slate-700 text-slate-50`;
- неактивный: `text-slate-400 hover:bg-slate-800`;
- клик: `state.status = status; state.page = 1; renderTabs(); loadPage();`.

### 7.2. Источник

`#f-source`:
- опции: `all` — «Все источники», `hh` — hh.ru, `habr` — Habr Career, `remoteok` — RemoteOK;
- `change`: `state.source = value; state.page = 1; loadPage();`.

### 7.3. Поиск

`#f-q`:
- `input` с `debounce(300)`;
- `state.q = value.trim(); state.page = 1; loadPage();`.

### 7.4. Удалёнка

`#f-remote`:
- `change`: `state.remote = checked ? true : null; state.page = 1; loadPage();`.

---

## 8. Пагинация

`renderPagination()`:
- `#pagination` скрывать, если `state.pages <= 1`;
- кнопка `#pg-prev` — `‹ Назад`, disabled на первой странице;
- кнопка `#pg-next` — `Вперёд ›`, disabled на последней;
- `#page-buttons` — номера страниц;
- `#page-info` -> `Страница ${page} из ${pages} · всего ${total}` (или `Нет вакансий`).

Алгоритм номеров:

```javascript
function pageNumbers(current, pages) {
  if (pages <= 7) {
    return Array.from({ length: pages }, (_, i) => i + 1);
  }
  const set = new Set([1, pages, current - 1, current, current + 1]);
  const sorted = [...set].filter((p) => p >= 1 && p <= pages).sort((a, b) => a - b);
  const out = [];
  for (let i = 0; i < sorted.length; i += 1) {
    if (i > 0 && sorted[i] - sorted[i - 1] > 1) out.push("…");
    out.push(sorted[i]);
  }
  return out;
}
```

Кнопки страниц:
- активная: `bg-indigo-600 text-white`;
- неактивная: `border border-slate-700 text-slate-300 hover:bg-slate-800`;
- `aria-current="page"` для активной;
- клик: `state.page = p; loadPage(); window.scrollTo({top: 0, behavior: "smooth"})`.

`#pg-prev`/`#pg-next` обрабатывать через `goToPage(state.page - 1/+1)`.

---

## 9. Смена статуса

`setStatus(id, status)`:
- `PATCH /api/vacancies/${id}` с телом `{status}`;
- обновить элемент в `state.items` (если есть);
- `await loadPage()` и `await loadStats()`;
- если текущая страница опустела и `state.page > 1` — уменьшить `state.page` и загрузить заново;
- `toast("Статус обновлён", "ok")`;
- ошибка -> `toast(e.message, "err")`.

---

## 10. Запуск сбора и прогресс

### 10.1. `startScrape()`

- `#btn-scrape`/`#btn-run`: disabled, текст `Запуск…`;
- `POST /api/scrape`;
- `showProgress(true)`;
- `pollRun(res.run_id)`;
- если `res.started === false` — `toast("Сбор уже идёт", "info")`, всё равно `pollRun`;
- в `finally` не включать кнопку, пока прогон не завершён; включить в `pollRun` при `finished_at`;
- при исключении `POST /api/scrape` — включить кнопку, вернуть текст `Запустить поиск`, `showProgress(false)`, `toast(e.message, "err")`.

### 10.2. `pollRun(runId)`

- очистить предыдущий таймер;
- каждые 1000 мс `GET /api/runs/${runId}`;
- `renderProgress(run)`;
- если `finished_at` пуст — продолжить опрос;
- иначе:
  - остановить таймер, `showProgress(false)`, включить кнопку, вернуть текст `Запустить поиск`;
  - если `error` — `toast("Сбор завершён с ошибкой: ...", "err")`;
  - иначе `toast(`Сбор завершён: сохранено ${run.saved} из ${run.fetched}`, "ok")`;
  - если `errors_json` парсится в непустой объект — `toast("Часть источников недоступна", "info")`;
  - `state.page = 1; await loadPage(); await loadStats(); await loadHealth();`.
- Ошибка `GET /api/runs/{runId}`: остановить опрос, `showProgress(false)`, включить кнопку, вернуть текст `Запустить поиск`, `toast(e.message, "err")`.

### 10.3. `renderProgress(run)`

- `#progress-phase` -> `PHASES[run.phase] || run.phase`;
- `#progress-bar` width -> `PHASE_PCT[run.phase]` (0–100);
- `#progress-count` -> `${run.fetched} собрано · ${run.filtered} IT · ${run.saved} сохранено`;
- `showProgress(true/false)` переключает `hidden` у `#progress-wrap`.

---

## 11. Горячие клавиши

Глобальный `keydown`:
- `Esc` обрабатывать **до** проверки тега: если фокус в `#f-q` — очистить `#f-q`, `state.q = ""`, `state.page = 1`, `loadPage()`; иначе снять активную карточку;
- затем игнорировать `INPUT`, `TEXTAREA`, `SELECT`, `isContentEditable`;
- `J` / `ArrowDown` — следующая карточка;
- `K` / `ArrowUp` — предыдущая;
- `A` — лайк активной;
- `R` — скип активной;
- `O` — открыть URL активной в новой вкладке;
- `/` — `preventDefault()`, фокус в `#f-q`.

`moveActive(delta)`:
- получить все `article` в `#cards`;
- ограничить индекс;
- класс `card-active` (рамка indigo) только у активной;
- `scrollIntoView({block:"nearest", behavior:"smooth"})`.

---

## 12. Атрибуция

В `ensureLayout()` или `init()` создать/дополнить `#attribution`:

```text
Источники: hh.ru, Habr Career, RemoteOK.
Данные RemoteOK предоставлены Remote OK.
```

Ссылка на `https://remoteok.com` без `nofollow`, `target="_blank"`, `rel="noopener"`. Текст «Remote OK» обязателен. Не скрывать атрибуцию.

---

## 13. Инициализация

`DOMContentLoaded`:

1. `ensureLayout()`;
2. если в шаблоне остался `#ollama-banner` от старой версии — скрыть его (`classList.add("hidden")`): LLM вне скоупа;
3. `renderTabs()`;
4. навесить обработчики на `#f-q`, `#f-source`, `#f-remote`, `#btn-scrape`/`#btn-run`, `#pg-prev`, `#pg-next`;
5. `loadPage()`, `loadStats()`, `loadHealth()`;
6. `setInterval(loadHealth, 20000)`;
7. глобальный `keydown`.

Не запускать сбор автоматически.

---

## 14. Требования к качеству

- Весь код — в одном файле, без модулей и импортов.
- Никаких глобальных переменных, кроме `state`, `SOURCE_META`, `STATUS_META`, `TABS`, `PHASES`, `PHASE_PCT` и функций.
- Никаких `alert`, `confirm`, `prompt`.
- Никаких `innerHTML` с данными API.
- Никаких `console.log` в продакшн-ветках; допустим `console.warn` при некритичных ошибках.
- Все запросы обёрнуты в `try/catch`.
- Кнопки имеют `type="button"`.
- `select`/`input` имеют `aria-label`.
- Пагинация не сбрасывается при смене статуса, если страница ещё существует.
- При смене фильтра страница всегда сбрасывается на 1.
- При пустом ответе показывается `#empty` с текстом «Вакансий не найдено. Измените фильтры или запустите сбор.».
- При ошибке загрузки — toast и пустое состояние, без падения скрипта.
- Никаких обращений к `v.raw`, `v.raw_json`.
- Никаких полей, которых нет в контракте `VacancyOut`.

---

## 15. Definition of Done для app.js

- [ ] Файл подключается как `<script src="/static/app.js"></script>` и не падает.
- [ ] `GET /api/vacancies` вызывается с `page` и `page_size`.
- [ ] Пагинация работает: назад/вперёд/номера/многоточия.
- [ ] Фильтры статуса, источника, поиска и удалёнки работают и сбрасывают страницу.
- [ ] Бейджи источников и статусов используют точные классы из ТЗ.
- [ ] Нет XSS: данные API вставляются только через `textContent`.
- [ ] `POST /api/scrape` и опрос `GET /api/runs/{id}` работают; прогресс виден.
- [ ] После завершения сбора страница и статистика обновляются.
- [ ] `PATCH /api/vacancies/{id}` меняет статус.
- [ ] Атрибуция RemoteOK присутствует.
- [ ] Нет `TODO`, `pass`, заглушек, `innerHTML` с данными.
