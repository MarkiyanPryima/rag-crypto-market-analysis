# Crypto RAG — аналіз ринку BTC/ETH з RAG-архітектурою

Магістерська робота «Система аналізу даних криптовалютного ринку з використанням
RAG-архітектури».

Гібридна RAG-система для BTC і ETH, яка:

1. **пояснює рухи ціни**, поєднуючи новини (The Guardian, CoinDesk, історичні набори
   Hugging Face) з ринковими даними Binance;
2. **показує схожі епізоди** з історії (48-годинні вікна з найближчою формою ціни);
3. **щодня прогнозує** волатильність на добу та ймовірність зростання за 7 днів.

Мовна модель лише пояснює: **усі числа рахує код**, а модель посилається на них за
ідентифікаторами `[m…]`, `[f…]`, `[c_…]`. Кожну відповідь перевіряє звичайний код
(числа, посилання, дати джерел, порядок у часі). Жодна функція не бачить даних
пізніших за `as_of`.

> Не є фінансовою порадою.

## Як це працює

```
Binance ─┐                         ┌─ get_market_stats ─┐
Guardian ├─> PostgreSQL ─> chunks ─┼─ search_news ──────┼─> LangGraph ─> verify ─> відповідь
CoinDesk │   TimescaleDB   + vec   ├─ similar_episodes ─┤   router →            (або чесна
HF     ──┘   + pgvector    + tsv   └─ get_forecast ─────┘   generate            відмова)
```

| Процес | Що робить |
|---|---|
| `db` | PostgreSQL 17 + TimescaleDB + pgvector |
| `migrate` | одноразово застосовує міграції Alembic |
| `collector` | за розкладом: RSS CoinDesk (15 хв), свічки Binance (щогодини), funding (8 год), Guardian (щодня), епізоди (щогодини) |
| `forecaster` | ознаки й прогноз щодня о 00:05 UTC, донавчання щотижня |
| `api` | FastAPI, http://localhost:8001 (документація: `/docs`) |
| `ui` | React-інтерфейс, http://localhost:8090 |

## Запуск з нуля

Потрібні Docker (з Compose v2) і ключі API: OpenAI (платний) та The Guardian
Open Platform (безкоштовний developer key).

**1. Налаштування**

```bash
cp .env.example .env
```

У `.env` задайте пароль бази (`DB_PASSWORD` і той самий пароль у `DATABASE_URL`,
`TEST_DATABASE_URL`), `OPENAI_API_KEY` і `GUARDIAN_API_KEY`. Файл `.env` не
комітиться.

**2. Підняти систему** (база, міграції, усі сервіси)

```bash
make up
```

**3. Завантажити історію** (один раз; повторний запуск не створює дублікатів)

```bash
docker compose run --rm collector python -m crypto_rag.ingestion.binance backfill
docker compose run --rm collector python -m crypto_rag.ingestion.guardian backfill
docker compose run --rm collector python -m crypto_rag.ingestion.coindesk_rss once
docker compose run --rm collector python -m crypto_rag.ingestion.huggingface import
```

**4. Обробити статті й побудувати похідні таблиці**

```bash
docker compose run --rm collector python -m crypto_rag.processing.pipeline
docker compose run --rm collector python -m crypto_rag.tools.episodes build
docker compose run --rm collector python -m crypto_rag.forecasting.features build
```

`pipeline` робить фрагменти, embeddings (`text-embedding-3-large`, 1536) і мітки
теми/тону (`gpt-5.6-luna`). Це платні виклики OpenAI; витрати пишуться в таблицю
`llm_usage` і видно на екрані «Статистика». Статті з наборів Hugging Face отримують
лише embeddings (`LABEL_HF_ARTICLES=false`): ознаки прогнозу беруть новини тільки з
The Guardian.

**5. Прогнозні моделі** (етап 13): оцінювання на підсумковому періоді (2025-01 → кінець
даних) і щоденні прогнози ковзної перевірки в таблицю `forecasts`:

```bash
docker compose run --rm collector python -m crypto_rag.forecasting.train evaluate
```

Результати — у `evaluation/results/forecast_*`. Далі `forecaster` щодня о 00:05 UTC
навчає моделі на даних, відомих на цей момент, і записує прогноз на день.

**6. Оцінювання пояснень** (етап 17, платні виклики, ≈$10 за повний прогін):

```bash
uv run python evaluation/build_questions.py              # evaluation/questions.jsonl
uv run python -m crypto_rag.evaluation.runs answers      # 4 конфігурації × запитання
uv run python -m crypto_rag.evaluation.runs retrieval    # пошук за різних T½
uv run python -m crypto_rag.evaluation.judge             # faithfulness, суддя gpt-5.6-sol
uv run python -m crypto_rag.evaluation.annotate build    # завдання для ручної розмітки
```

Ручна розмітка — вкладка «Розмітка» в UI (релевантність статей, перевірка тверджень
судді, тема й тон). Після неї:

```bash
uv run python -m crypto_rag.evaluation.annotate export   # мітки -> evaluation/manual/
uv run python -m crypto_rag.evaluation.report            # таблиці й графіки -> evaluation/results/
```

**7. Увімкнути автоматичну обробку нових статей** — `LLM_PROCESSING_ENABLED=true`
у `.env`, потім:

```bash
docker compose up -d --force-recreate collector
```

Готово: відкрийте http://localhost:8090.

## Структура репозиторію

```
crypto_rag/
  config.py          налаштування (pydantic-settings, читає .env)
  db/                моделі SQLAlchemy, сесії
  ingestion/         binance, guardian, coindesk_rss, huggingface
  processing/        очищення, фрагменти, активи, embeddings, розмітка
  tools/             числові інструменти: market, news_reaction, episodes, forecast
  retrieval/         гібридний пошук новин (вектор + повнотекстовий, RRF, затухання)
  agent/             граф LangGraph, промпти, стан
  verification/      перевірка відповіді без мовної моделі
  forecasting/       ознаки, цілі, LightGBM, GARCH, перевірка на історії
  evaluation/        прогони конфігурацій, суддя, метрики, ручна розмітка
  api/               FastAPI
  scheduler/         процеси APScheduler (collector, forecaster)
migrations/          міграції Alembic
ui/                  React + Vite + TypeScript
tests/               pytest (модульні та інтеграційні)
evaluation/          запитання, результати експериментів, ручні мітки
```

## Розробка

```bash
uv sync                 # залежності (Python 3.12, версії зафіксовані в uv.lock)
make test               # усі тести; інтеграційні — лише на базі *_test
make lint               # ruff
make ui-dev             # UI з гарячим перезавантаженням на http://localhost:5173
make psql               # консоль бази
```

Інтеграційні тести перестворюють схему, тому запускаються **лише** на базі з
`test` у назві (`TEST_DATABASE_URL`) і ніколи не торкаються робочих даних.

## Відомі обмеження

- **Покриття новин:** до 01.2025 — набір `maryamfakhari/crypto-news-coindesk-2020-2025`,
  02–04.2026 — `katelabs/crypto-news-scored` (обидва CC BY-NC 4.0, лише BTC/ETH),
  з 10.2026 — RSS CoinDesk; The Guardian — увесь період. Для 02.2025–01.2026 і
  05–09.2026 є лише The Guardian. Набір katelabs містить тільки заголовки.
- **CoinDesk RSS** не містить повних текстів: такі статті — один фрагмент
  «заголовок + опис». Матеріали з автором «AI Boost» позначено `ai_assisted`.
- **Мережа:** деякі мережі блокують `fapi.binance.com` (funding) і `coindesk.com`;
  збирач повторює спроби за розкладом і не зупиняє інші задачі.
