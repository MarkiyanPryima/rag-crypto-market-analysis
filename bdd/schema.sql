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

-- Serves both "latest price per token" (DISTINCT ON) and per-token history
-- range scans. Named explicitly rather than auto-named so a later migration can
-- reference it.
CREATE INDEX IF NOT EXISTS price_snapshots_token_scraped_idx
    ON price_snapshots (token_id, scraped_at DESC);
