-- Switch embeddings from OpenAI text-embedding-3-small (1536) to a local
-- all-MiniLM-L6-v2 model (384).
--
-- Rationale: the local model costs nothing per article, runs without an API
-- key, and makes retrieval results fully reproducible — which matters for a
-- thesis that will be re-run during review. Retrieval quality is lower than
-- OpenAI's, and that is stated as a limitation rather than hidden.
--
-- Safe to run now because embeddings_news is empty. Once vectors exist this
-- same change would require re-embedding every article, since a stored
-- vector cannot be converted between dimensionalities.
--
-- The HNSW index must be dropped first: it is built over the column's type,
-- so ALTER COLUMN cannot proceed while it exists.

DROP INDEX IF EXISTS embeddings_news_embedding_idx;

ALTER TABLE embeddings_news ALTER COLUMN embedding TYPE vector(384);

ALTER TABLE embeddings_news ALTER COLUMN model SET DEFAULT 'all-MiniLM-L6-v2';

CREATE INDEX embeddings_news_embedding_idx
    ON embeddings_news USING hnsw (embedding vector_cosine_ops);
