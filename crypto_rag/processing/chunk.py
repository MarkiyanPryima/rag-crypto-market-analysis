"""Split articles into 300-500 token chunks with ~50 tokens of overlap.

Every chunk starts with "[title] — [published_at, UTC]" so a chunk retrieved
on its own still says what it belongs to and when it was published.
Articles without body text (CoinDesk RSS, short dataset headlines) become one chunk:
header + standfirst.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

import tiktoken
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from crypto_rag.db.models import Article, Chunk
from crypto_rag.processing.assets import detect_assets
from crypto_rag.processing.clean import clean_text

logger = logging.getLogger(__name__)

MAX_TOKENS = 500
OVERLAP = 50
# cl100k_base is the tokenizer of the text-embedding-3 models.
ENCODING = "cl100k_base"


@lru_cache
def _encoder() -> tiktoken.Encoding:
    return tiktoken.get_encoding(ENCODING)


@dataclass(frozen=True)
class ChunkDraft:
    index: int
    text: str
    assets: list[str]


def header(title: str, published_at: datetime) -> str:
    return f"{title.strip()} — {published_at:%Y-%m-%d %H:%M} UTC"


def split_tokens(body: str, max_tokens: int = MAX_TOKENS, overlap: int = OVERLAP) -> list[str]:
    """Equal-size windows of <= max_tokens with `overlap` tokens shared between neighbours.

    Equal sizing avoids a tiny trailing chunk: 520 tokens become two ~285-token
    windows, not 500 + 70.
    """
    enc = _encoder()
    tokens = enc.encode(body)
    if len(tokens) <= max_tokens:
        return [body] if body.strip() else []
    k = math.ceil((len(tokens) - overlap) / (max_tokens - overlap))
    size = math.ceil((len(tokens) - overlap) / k) + overlap
    step = size - overlap
    return [enc.decode(tokens[i * step : i * step + size]).strip() for i in range(k)]


def build_chunks(article: Article) -> list[ChunkDraft]:
    head = header(article.title, article.published_at)
    body = clean_text(article.body_text)
    if not body:
        standfirst = clean_text(article.standfirst)
        text = f"{head}\n\n{standfirst}" if standfirst else head
        return [ChunkDraft(0, text, detect_assets(text))]
    pieces = split_tokens(body)
    drafts = []
    for i, piece in enumerate(pieces):
        text = f"{head}\n\n{piece}"
        drafts.append(ChunkDraft(i, text, detect_assets(text)))
    return drafts


def articles_needing_chunks(session: Session, limit: int | None = None) -> list[Article]:
    """New articles, or articles whose content changed since they were chunked."""
    chunk_hash = (
        select(Chunk.content_hash)
        .where(Chunk.article_id == Article.id, Chunk.chunk_index == 0)
        .scalar_subquery()
    )
    stmt = (
        select(Article)
        .where((chunk_hash.is_(None)) | (chunk_hash != Article.content_hash))
        .order_by(Article.published_at)
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


BATCH = 2000


def chunk_pending(engine: Engine, limit: int | None = None) -> int:
    """(Re)chunk new or changed articles in committed batches. Embeddings come from embed.py.

    Every article yields at least one chunk, so a processed article is never selected again.
    """
    created = processed = 0
    while limit is None or processed < limit:
        size = BATCH if limit is None else min(BATCH, limit - processed)
        with Session(engine) as session:
            articles = articles_needing_chunks(session, size)
            if not articles:
                break
            created += _chunk_batch(session, articles)
            session.commit()
        processed += len(articles)
        logger.info("chunked %d articles so far (%d chunks)", processed, created)
    return created


def _chunk_batch(session: Session, articles: list[Article]) -> int:
    created = 0
    session.execute(delete(Chunk).where(Chunk.article_id.in_([a.id for a in articles])))
    for article in articles:
        for draft in build_chunks(article):
            session.add(
                Chunk(
                    article_id=article.id,
                    chunk_index=draft.index,
                    text=draft.text,
                    published_at=article.published_at,
                    assets=draft.assets,
                    content_hash=article.content_hash or "",
                )
            )
            created += 1
    return created
