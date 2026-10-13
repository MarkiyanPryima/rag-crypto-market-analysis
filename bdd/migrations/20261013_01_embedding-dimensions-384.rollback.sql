-- Revert to 1536-dimensional OpenAI embeddings.
--
-- Only lossless while the table is empty. Any 384-dimensional vectors stored
-- in the meantime cannot be converted and must be deleted and regenerated, so
-- rolling back after ingestion has started means re-embedding the corpus.

DROP INDEX IF EXISTS embeddings_news_embedding_idx;

ALTER TABLE embeddings_news ALTER COLUMN embedding TYPE vector(1536);

ALTER TABLE embeddings_news ALTER COLUMN model SET DEFAULT 'text-embedding-3-small';

CREATE INDEX embeddings_news_embedding_idx
    ON embeddings_news USING hnsw (embedding vector_cosine_ops);
