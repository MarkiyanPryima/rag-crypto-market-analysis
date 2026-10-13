"""Local text embeddings via fastembed.

Generates the 384-dimensional vectors stored in embeddings_news and searched
by the RAG retrieval step.

Runs entirely in-process: no API key, nothing billed per article, and the same
input always produces the same vector — so retrieval results are reproducible
when the work is reviewed. fastembed runs the model through ONNX rather than
PyTorch, which keeps the worker image around 150 MB instead of adding ~2 GB.

The trade-off against OpenAI's text-embedding-3-small (1536 dimensions) is
lower retrieval quality, stated as a limitation rather than hidden. Step 52
measures precision@k so the cost is a number rather than an assumption.
"""

import logging
import os

logger = logging.getLogger(__name__)

MODEL_NAME = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "384"))

# all-MiniLM-L6-v2 accepts 256 tokens and silently truncates beyond that.
# Cutting by character count first keeps the truncation explicit and visible in
# the logs — roughly four characters per token, so this is a safe upper bound.
MAX_CHARS = 1000

_model = None


def get_model():
    """Load the model on first use.

    Lazy because loading takes a few seconds and reads from disk; importing
    this module must stay cheap so unit tests and the API can import it
    without paying that cost.
    """
    global _model
    if _model is None:
        from fastembed import TextEmbedding

        logger.info("loading embedding model %s", MODEL_NAME)
        _model = TextEmbedding(model_name=MODEL_NAME)
        logger.info("embedding model ready (%d dimensions)", DIMENSIONS)
    return _model


def truncate(text: str) -> str:
    """Cut text to the model's usable length, logging when it happens."""
    if len(text) <= MAX_CHARS:
        return text
    logger.debug("truncating %d chars to %d for embedding", len(text), MAX_CHARS)
    return text[:MAX_CHARS]


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts, returning one vector per input.

    Batched because fastembed amortises model overhead across a call; embedding
    one article at a time is markedly slower over thousands of messages.
    """
    if not texts:
        return []

    prepared = [truncate(text) for text in texts]
    vectors = [vector.tolist() for vector in get_model().embed(prepared)]

    for vector in vectors:
        if len(vector) != DIMENSIONS:
            # Would otherwise fail at INSERT with a confusing pgvector error.
            raise ValueError(
                f"model returned {len(vector)} dimensions, schema expects {DIMENSIONS} — "
                f"EMBEDDING_MODEL and EMBEDDING_DIMENSIONS disagree"
            )

    return vectors


def embed_one(text: str | None) -> list[float] | None:
    """Embed a single text, or None if there is nothing to embed."""
    if text is None or not text.strip():
        return None
    return embed([text])[0]


def to_pgvector(vector: list[float]) -> str:
    """Format a vector as a pgvector literal.

    psycopg has no native adapter for the vector type, so it is passed as the
    text form pgvector parses: '[0.1,0.2,...]'. Avoids depending on the
    pgvector Python package for one conversion.
    """
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def embed_article(title: str | None, content: str | None) -> list[float] | None:
    """Embed an article from its title and body.

    Both are used when present. For the feed that supplies no body this is a
    title-only vector, which is weaker but still searchable — and step 52
    reports retrieval quality split by whether a body was available.
    """
    parts = [part.strip() for part in (title, content) if part and part.strip()]
    if not parts:
        return None
    return embed_one(" ".join(parts))
