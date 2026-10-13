"""Ingest news articles from news_queue.

One message is one article. Everything for that article happens in a single
transaction: upsert the row, classify its sentiment, generate its embedding.
A failure anywhere leaves nothing behind, so the consumer can drop the message
and continue.

Sentiment is written **as part of the INSERT**, not as a follow-up UPDATE. The
design document being worked from suggests the UPDATE form, which has two
problems: a re-ingested article keeps its old score until the second statement
runs, and the schema's news_sentiment_complete constraint would be violated in
between.

Embeddings are skipped when the article already has one for the current model.
This matters more than it sounds: the collectors deduplicate only within a
cycle, so the same article arrives every 15 minutes. The backlog held 9683
messages covering a few hundred unique URLs — embedding on every message
would mean thousands of redundant model runs.
"""

import logging
from typing import Any

from worker import db, embedding, sentiment, tokens

logger = logging.getLogger(__name__)

# ON CONFLICT (url) because url is the dedupe key: the same article is
# re-queued every cycle. DO UPDATE rather than DO NOTHING so a corrected title
# or a body that appeared later is picked up, and so re-ingestion rescores
# sentiment with the current model.
UPSERT_ARTICLE = """
    INSERT INTO news
        (token_id, title, content, url, source, published_at, scraped_at,
         sentiment_label, sentiment_score, sentiment_model)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (url) DO UPDATE SET
        token_id        = EXCLUDED.token_id,
        title           = EXCLUDED.title,
        content         = COALESCE(EXCLUDED.content, news.content),
        source          = EXCLUDED.source,
        published_at    = COALESCE(EXCLUDED.published_at, news.published_at),
        sentiment_label = EXCLUDED.sentiment_label,
        sentiment_score = EXCLUDED.sentiment_score,
        sentiment_model = EXCLUDED.sentiment_model
    RETURNING id
"""

# UNIQUE (news_id, model) makes this idempotent, but the point of checking
# first is to avoid running the model at all.
HAS_EMBEDDING = """
    SELECT 1 FROM embeddings_news WHERE news_id = %s AND model = %s
"""

INSERT_EMBEDDING = """
    INSERT INTO embeddings_news (news_id, embedding, model)
    VALUES (%s, %s::vector, %s)
    ON CONFLICT (news_id, model) DO NOTHING
"""


class InvalidNewsPayload(ValueError):
    """The message cannot be turned into a row."""


def _require(payload: dict[str, Any], field: str) -> Any:
    value = payload.get(field)
    if value is None:
        raise InvalidNewsPayload(f"missing required field {field!r}")
    return value


def handle(payload: dict[str, Any]) -> None:
    """Upsert one article with its sentiment, then embed it if needed."""
    title = str(_require(payload, "title")).strip()
    url = str(_require(payload, "url")).strip()
    # scraped_at from the payload, never the column default — the queue can be
    # days behind and now() would flatten the collection history.
    scraped_at = _require(payload, "scraped_at")

    if not title or not url:
        raise InvalidNewsPayload("title and url must be non-empty")

    content = payload.get("content")
    symbol = payload.get("symbol")

    # Classified before the insert so all three sentiment columns are written
    # together, satisfying news_sentiment_complete in one statement.
    scores = sentiment.classify_article(title, content)

    with db.get_connection() as conn:
        # A symbol the feed mentioned but we do not track leaves token_id NULL.
        # That is normal: 58% of collected articles carry no symbol at all, and
        # they remain valid market-wide context for retrieval.
        token_id = tokens.resolve(conn, symbol) if symbol else None

        with conn.cursor() as cur:
            cur.execute(
                UPSERT_ARTICLE,
                (
                    token_id,
                    title,
                    content,
                    url,
                    payload.get("source"),
                    payload.get("published_at"),
                    scraped_at,
                    scores["label"] if scores else None,
                    scores["score"] if scores else None,
                    scores["model"] if scores else None,
                ),
            )
            news_id = cur.fetchone()[0]

            cur.execute(HAS_EMBEDDING, (news_id, embedding.MODEL_NAME))
            already_embedded = cur.fetchone() is not None

        if already_embedded:
            logger.debug("%s already embedded, skipping model run", url)
            return

        vector = embedding.embed_article(title, content)
        if vector is None:
            logger.warning("no text to embed for %s", url)
            return

        with conn.cursor() as cur:
            cur.execute(
                INSERT_EMBEDDING,
                (news_id, embedding.to_pgvector(vector), embedding.MODEL_NAME),
            )

    logger.debug(
        "stored %s (%s, sentiment=%s)",
        url,
        symbol or "market-wide",
        scores["label"] if scores else "none",
    )
