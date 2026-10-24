"""Fill chunks.embedding with OpenAI vectors (model and width from config)."""

import logging
from collections.abc import Sequence

from openai import OpenAI
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from crypto_rag.config import get_settings
from crypto_rag.db.models import Chunk
from crypto_rag.llm import get_client, log_usage

logger = logging.getLogger(__name__)

BATCH = 100


def embed_texts(
    engine: Engine, texts: Sequence[str], purpose: str = "embed", client: OpenAI | None = None
) -> list[list[float]]:
    """Embed a batch; the SDK retries transient errors (max_retries in get_client)."""
    settings = get_settings()
    client = client or get_client()
    response = client.embeddings.create(
        model=settings.embedding_model,
        input=list(texts),
        dimensions=settings.embedding_dimensions,
    )
    log_usage(engine, purpose, settings.embedding_model, response.usage.prompt_tokens)
    return [d.embedding for d in sorted(response.data, key=lambda d: d.index)]


def embed_pending(engine: Engine, client: OpenAI | None = None, limit: int | None = None) -> int:
    settings = get_settings()
    done = 0
    with Session(engine) as session:
        stmt = select(Chunk.id, Chunk.text).where(Chunk.embedding.is_(None)).order_by(Chunk.id)
        if limit:
            stmt = stmt.limit(limit)
        rows = session.execute(stmt).all()
        for i in range(0, len(rows), BATCH):
            batch = rows[i : i + BATCH]
            vectors = embed_texts(engine, [r.text for r in batch], client=client)
            for row, vector in zip(batch, vectors, strict=True):
                session.execute(
                    update(Chunk)
                    .where(Chunk.id == row.id)
                    .values(embedding=vector, embedding_model=settings.embedding_model)
                )
            session.commit()
            done += len(batch)
            logger.info("embedded %d/%d chunks", done, len(rows))
    return done
