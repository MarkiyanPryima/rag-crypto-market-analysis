-- Baseline.
--
-- bdd/schema.sql creates the initial schema, but only on an empty data
-- directory. This migration records that baseline so every later schema change
-- is a numbered migration applied to a live database, with no data loss.
--
-- It performs no DDL. Its only job is to fail loudly if applied to a database
-- that never received schema.sql, which would otherwise leave migration 0002
-- altering tables that do not exist.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'tokens'
    ) THEN
        RAISE EXCEPTION
            'Baseline missing: bdd/schema.sql has not been applied to this database.';
    END IF;
END $$;
