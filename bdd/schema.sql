-- Database schema for the crypto market RAG application.
--
-- Executed by the Postgres entrypoint only on an EMPTY data directory. Editing
-- this file does not affect a database that has already been initialised; to
-- pick up changes during development, recreate the volume:
--
--     docker compose down -v && docker compose up -d
--
-- That destroys all collected data, which is why schema changes after
-- collection begins need a migration rather than an edit here.

-- pgvector, required for the news embedding column added later in this file.
CREATE EXTENSION IF NOT EXISTS vector;

-- Tracked coins. One row per asset; everything else references this table.
CREATE TABLE IF NOT EXISTS tokens (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol     VARCHAR(20) NOT NULL UNIQUE,
    name       VARCHAR(100) NOT NULL,
    slug       VARCHAR(100) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The worker resolves tokens by symbol on every ingested price and article,
    -- and the API filters by it. Forcing uppercase at the database level means a
    -- lowercase 'btc' from a feed can never create a second, silently separate
    -- token row.
    CONSTRAINT tokens_symbol_uppercase CHECK (symbol = upper(symbol))
);

-- Time series of observed prices. Append-only; one row per token per collector
-- cycle. CASCADE because a snapshot is meaningless without its token.
CREATE TABLE IF NOT EXISTS price_snapshots (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_id   UUID NOT NULL REFERENCES tokens(id) ON DELETE CASCADE,
    price_usd  NUMERIC(20, 8) NOT NULL,
    market_cap NUMERIC(30, 2),
    volume_24h NUMERIC(30, 2),
    change_24h NUMERIC(8, 4),
    -- NOT NULL: the API selects the latest price per token with
    -- DISTINCT ON ... ORDER BY scraped_at DESC, and a NULL would sort first
    -- under DESC and masquerade as the most recent observation.
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source     VARCHAR(100) NOT NULL DEFAULT 'coingecko',

    -- A zero or negative price is always bad data, and it does real damage
    -- downstream: the correlation analysis divides by the earlier price to get
    -- a forward return, so a zero would yield a division error or an infinite
    -- return rather than a visibly wrong number.
    CONSTRAINT price_snapshots_price_positive CHECK (price_usd > 0),
    CONSTRAINT price_snapshots_market_cap_non_negative
        CHECK (market_cap IS NULL OR market_cap >= 0),
    CONSTRAINT price_snapshots_volume_non_negative
        CHECK (volume_24h IS NULL OR volume_24h >= 0)
);

-- Serves per-token history range scans, and "latest price per token" when that
-- is written as a lateral join (one bounded index scan per token). A
-- DISTINCT ON over token_id cannot use this index — Postgres has no loose index
-- scan, so with only 8 distinct tokens it sorts the whole table instead.
-- Named explicitly rather than auto-named so a later migration can reference it.
CREATE INDEX IF NOT EXISTS price_snapshots_token_scraped_idx
    ON price_snapshots (token_id, scraped_at DESC);

-- News articles, with per-article sentiment as first-class columns rather than
-- a later ALTER TABLE. token_id is SET NULL rather than CASCADE: an article
-- about a coin we stop tracking is still valid market context for retrieval.
CREATE TABLE IF NOT EXISTS news (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_id     UUID REFERENCES tokens(id) ON DELETE SET NULL,
    title        TEXT NOT NULL,
    content      TEXT,
    -- NOT NULL, unlike the reference. The worker dedupes with
    -- ON CONFLICT (url), and UNIQUE permits unlimited NULLs — so a nullable url
    -- would let the same article be inserted repeatedly, inflating both the
    -- embedding table and every sentiment aggregate.
    url          TEXT NOT NULL UNIQUE,
    source       VARCHAR(100),
    -- Nullable: some RSS feeds omit it. Queries must COALESCE to scraped_at.
    published_at TIMESTAMPTZ,
    scraped_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Per-article sentiment. Nullable because an article exists briefly before
    -- classification, and a classifier failure must not block ingestion.
    sentiment_label VARCHAR(10),
    sentiment_score NUMERIC(4, 3),
    -- Which classifier produced the scores, e.g. 'vader-3.3.2'. Present from the
    -- start so a second model can be scored over the same corpus and compared,
    -- rather than overwriting the first model's results.
    sentiment_model VARCHAR(50),

    -- A CHECK with IN passes for NULL (unknown, not false), so these permit
    -- unclassified rows while rejecting invalid labels.
    CONSTRAINT news_sentiment_label_valid
        CHECK (sentiment_label IN ('positive', 'neutral', 'negative')),
    -- VADER's compound score is bounded to [-1, 1]; anything outside it means a
    -- scaling bug, which would quietly distort every average.
    CONSTRAINT news_sentiment_score_range
        CHECK (sentiment_score BETWEEN -1 AND 1),
    -- All three sentiment columns are written together or not at all. A
    -- half-classified row (label without score, or score without model) would
    -- silently corrupt the accuracy metrics and the model comparison.
    CONSTRAINT news_sentiment_complete CHECK (
        (sentiment_label IS NULL AND sentiment_score IS NULL AND sentiment_model IS NULL)
        OR (sentiment_label IS NOT NULL AND sentiment_score IS NOT NULL
            AND sentiment_model IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS news_token_published_idx
    ON news (token_id, published_at DESC);
CREATE INDEX IF NOT EXISTS news_scraped_idx ON news (scraped_at DESC);
CREATE INDEX IF NOT EXISTS news_source_idx  ON news (source);

-- Supports filtering the feed by label ("show negative news"). Measured as a
-- Bitmap Index Scan at ~23% selectivity on 8k rows; as the table grows and
-- selectivity stays near a third, the planner may prefer a sequential scan.
CREATE INDEX IF NOT EXISTS news_sentiment_label_idx ON news (sentiment_label);

-- Serves the per-symbol sentiment aggregation, which filters on
-- COALESCE(published_at, scraped_at) because published_at is nullable. A plain
-- index on published_at cannot satisfy that expression, and the index above does
-- not help it — only this one turns that query into an index scan. Partial,
-- since rows without sentiment are never counted by that query.
CREATE INDEX IF NOT EXISTS news_token_effective_time_idx
    ON news (token_id, COALESCE(published_at, scraped_at) DESC)
    WHERE sentiment_label IS NOT NULL;
