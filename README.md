# Crypto RAG — аналіз ринку BTC/ETH з RAG-архітектурою

Магістерська робота «Система аналізу даних криптовалютного ринку з використанням
RAG-архітектури».

Гібридна RAG-система для BTC і ETH, яка:

1. **пояснює рухи ціни**, поєднуючи новини (The Guardian, CoinDesk) з ринковими даними Binance;
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
GDELT  ──┘   + pgvector    + tsv   └─ get_forecast ─────┘   generate            відмова)
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
docker compose run --rm collector python -m crypto_rag.ingestion.gdelt api --days 90
```

**4. Обробити статті й побудувати похідні таблиці**

```bash
docker compose run --rm collector python -m crypto_rag.processing.pipeline
docker compose run --rm collector python -m crypto_rag.tools.episodes build
docker compose run --rm collector python -m crypto_rag.forecasting.features build
```

`pipeline` робить фрагменти, embeddings (`text-embedding-3-large`, 1536) і мітки
теми/тону (`gpt-5.6-luna`). Це платні виклики OpenAI; витрати пишуться в таблицю
`llm_usage` і видно на екрані «Статистика». Для ≈800 статей — близько $0.20.

**5. Увімкнути автоматичну обробку нових статей** — `LLM_PROCESSING_ENABLED=true`
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
  ingestion/         binance, guardian, coindesk_rss, gdelt
  processing/        очищення, фрагменти, активи, embeddings, розмітка
  tools/             числові інструменти: market, news_reaction, episodes, forecast
  retrieval/         гібридний пошук новин (вектор + повнотекстовий, RRF, затухання)
  agent/             граф LangGraph, промпти, стан
  verification/      перевірка відповіді без мовної моделі
  forecasting/       ознаки, цілі, моделі прогнозу
  api/               FastAPI
  scheduler/         процеси APScheduler (collector, forecaster)
migrations/          міграції Alembic
ui/                  React + Vite + TypeScript
tests/               pytest (модульні та інтеграційні)
evaluation/          набори запитань і результати експериментів
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

- **Час GDELT** — це момент, коли GDELT побачив статтю, а не публікація; позначено
  в `raw.time_source`. Покриття CoinDesk через DOC API неповне.
- **CoinDesk RSS** не містить повних текстів: такі статті — один фрагмент
  «заголовок + опис». Матеріали з автором «AI Boost» позначено `ai_assisted`.
- **Мережа:** деякі мережі блокують `fapi.binance.com` (funding) і `coindesk.com`;
  збирач повторює спроби за розкладом і не зупиняє інші задачі.
