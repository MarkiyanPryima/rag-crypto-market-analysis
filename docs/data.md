# Дані системи

Стан на 2026-11-08. Усі числа — з бази даних (запити наведено внизу).

## Джерела

| Джерело | Дані | Канал (`channel`) | Період | Обсяг |
|---|---|---|---|---|
| Binance, спот | годинні свічки BTCUSDT, ETHUSDT | — | 2020-01-01 → тепер | 60 054 свічки на актив |
| Binance, ф'ючерси | ставки фінансування (кожні 8 год) | — | 2020-01-01 → тепер | 7 511 на актив |
| The Guardian Open Platform | статті з повним текстом, теги `technology/cryptocurrencies`, `technology/bitcoin` | `api` | 2020-01-01 → тепер | 920 |
| Hugging Face `maryamfakhari/crypto-news-coindesk-2020-2025` | заголовок + короткий текст, 78 видань | `hf` | 2020-01-01 → 2025-02-01 | 212 693 |
| Hugging Face `katelabs/crypto-news-scored` | лише заголовки, 48 видань | `hf` | 2026-02-01 → 2026-04-30 | 14 189 |
| CoinDesk RSS | заголовок + опис | `rss` | 2026-10-09 → тепер | 434 |

- Свічки: архів `data.binance.vision` (місячні файли з перевіркою SHA-256), далі
  програмний інтерфейс щогодини. Пропуски біржі (напр. 2023-03-24) показує
  `python -m crypto_rag.ingestion.binance report`.
- `articles.source` — коротка назва видання (`guardian`, `coindesk`, `ambcrypto`, …),
  `channel` — шлях надходження. Стаття з однаковою адресою зберігається один раз.
- Набори Hugging Face: ліцензія **CC BY-NC 4.0** (лише некомерційне використання).
  Імпортовано лише статті про BTC/ETH (словник назв у заголовку й тексті або колонка
  `coins`), опубліковані від 2020-01-01. У `raw` — назва набору, SHA-256 файлу, номер
  рядка. Файли кешуються в `data/hf/` (не в git).

## Відомі прогалини

- **02.2025 – 01.2026 і 05–09.2026**: є лише The Guardian (≈10 статей на місяць).
  Відкритих наборів із точним часом публікації за ці періоди не знайдено; RSS CoinDesk
  не віддає історії (25 останніх матеріалів), сторінки архіву захищені від ботів.
- The Guardian за 2020 рік — лише 21 стаття за обраними тегами.
- Набір katelabs містить лише заголовки: фрагмент = заголовок.
- RSS CoinDesk, опублікований під час сну комп'ютера довше ніж 25 матеріалів, може
  бути втрачено; свічки довантажуються автоматично.

## Обробка

| Крок | Хто | Результат |
|---|---|---|
| фрагменти 300–500 токенів, перекриття 50 | код | 253 705 фрагментів |
| embeddings `text-embedding-3-large`, 1536 | OpenAI | усі фрагменти (≈$2.9 за набори HF) |
| тема й тон | `gpt-5.6-luna` | 1 354 статті Guardian і CoinDesk; набори HF — ні (`LABEL_HF_ARTICLES=false`) |
| епізоди 48 год | код | 118 604 |
| ознаки й цілі прогнозу | код | 2 474 дні на актив (з 2020-01-31) |

Ознаки новин для прогнозу беруть лише статті The Guardian (`FEATURE_NEWS_SOURCES`):
це єдине джерело з однаковим покриттям у навчанні (2020–2023), налаштуванні (2024) і
перевірці (2025 → тепер).

## Відтворення

```bash
uv run python -m crypto_rag.ingestion.binance backfill
uv run python -m crypto_rag.ingestion.guardian backfill
uv run python -m crypto_rag.ingestion.huggingface import
uv run python -m crypto_rag.processing.pipeline
uv run python -m crypto_rag.tools.episodes build
uv run python -m crypto_rag.forecasting.features build
```

Обсяги за джерелами:

```sql
SELECT channel, coalesce(raw->>'dataset', source), count(*), min(published_at), max(published_at)
FROM articles GROUP BY 1, 2;
```
