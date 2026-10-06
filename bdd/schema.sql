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
