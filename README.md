# AI Job Hunter (Human-in-the-Loop)

Локальный «утренний ассистент» поиска работы: собирает новые вакансии с hh.ru и выводит их в удобный дашборд. Сопроводительное письмо генерируется **только по вашему запросу** с помощью локальной нейросети (LLM) на вашем железе.

**Все данные и работа LLM происходят локально**. Никакие резюме или тексты вакансий не уходят в облачные сервисы (OpenAI, Claude и т.д.). 

## 🚀 Стек технологий

- **Backend:** Python 3.11+, FastAPI + Uvicorn
- **БД:** SQLite (WAL) через aiosqlite
- **LLM:** OpenAI-совместимый API (LM Studio / Ollama / Strata), модель `Qwen 2.5/3.8 27B`
- **Парсинг:** API hh.ru с использованием `curl_cffi` (для обхода DDoS-Guard и блокировок)
- **Frontend:** HTML + Tailwind CSS (CDN) + Vanilla JS, тёмная тема
- **Расписание:** APScheduler (ежедневный утренний сбор)

## ⚙️ Как это работает (Pipeline)

1. **fetch** — сбор новых вакансий по сохранённым поискам (таблица `saved_searches`). Обход блокировок hh.ru через маскировку TLS-отпечатка (`curl_cffi`).
2. **review** — вакансии отображаются в дашборде в виде карточек. Вы открываете ссылку на hh.ru, читаете описание и сами принимаете решение.
3. **generate** — по кнопке «Сгенерировать письмо» бэкенд отправляет запрос к локальной LLM (Qwen 27B) через API LM Studio. Модель анализирует вакансию и ваш профиль, выдавая готовый текст.
4. **apply** — вы копируете письмо и отправляете отклик на hh.ru вручную. 

## 🛠 Установка

```bash
cd ai-job-hunter

# 1. Виртуальное окружение
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 2. Зависимости
pip install -r requirements.txt
pip install curl_cffi

# 3. Конфигурация
copy .env.example .env        # Windows
# cp .env.example .env        # macOS/Linux