-- Seed data: the tracked coins.
--
-- Kept separate from schema.sql so structure and data stay distinct, and so
-- this file can be re-run safely against a live database:
--
--     docker compose exec -T postgres psql -U "$POSTGRES_USER" \
--         -d "$POSTGRES_DB" < bdd/seed.sql
--
-- `slug` holds the CoinGecko API id, which is not derivable from the symbol
-- (BNB -> "binancecoin", XRP -> "ripple"). The collectors read the tracked list
-- from this table rather than hardcoding it, so adding a coin means one INSERT
-- here and no code change.

INSERT INTO tokens (symbol, name, slug) VALUES
    ('BTC',  'Bitcoin',  'bitcoin'),
    ('ETH',  'Ethereum', 'ethereum'),
    ('SOL',  'Solana',   'solana'),
    ('BNB',  'BNB',      'binancecoin'),
    ('XRP',  'XRP',      'ripple'),
    ('ADA',  'Cardano',  'cardano'),
    ('DOGE', 'Dogecoin', 'dogecoin'),
    ('DOT',  'Polkadot', 'polkadot')
-- DO NOTHING rather than DO UPDATE: `slug` is also UNIQUE, so an update could
-- collide with another row's slug. Re-running is a no-op; changing a name or
-- slug is a deliberate UPDATE, not a side effect of seeding.
ON CONFLICT (symbol) DO NOTHING;
