"""Tests for news ingestion with sentiment and embeddings."""

import pytest

from worker import db, embedding, ingest_news, tokens
from worker.ingest_news import InvalidNewsPayload


def _payload(**overrides):
    base = {
        "title": "Bitcoin rallies after regulatory clarity",
        "content": "Investors welcomed the decision with excellent enthusiasm.",
        "url": "https://news.test/article-1",
        "source": "decrypt",
        "published_at": "2026-10-11T13:00:00+00:00",
        "scraped_at": "2026-10-11T14:01:39.232806+00:00",
        "symbol": "BTC",
    }
    base.update(overrides)
    return base


# --- payload validation (no database) --------------------------------------


@pytest.mark.parametrize("missing", ["title", "url", "scraped_at"])
def test_missing_required_field_raises(missing):
    payload = _payload()
    del payload[missing]

    with pytest.raises(InvalidNewsPayload, match=missing):
        ingest_news.handle(payload)


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "spaces"])
def test_blank_title_raises(blank):
    """news.title is NOT NULL, and a blank title is unusable anyway."""
    with pytest.raises(InvalidNewsPayload):
        ingest_news.handle(_payload(title=blank))


# --- integration -----------------------------------------------------------


@pytest.fixture
def clean_db():
    tokens.clear_cache()
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings_news")
        cur.execute("DELETE FROM news")
    yield
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings_news")
        cur.execute("DELETE FROM news")
    tokens.clear_cache()
    db.close_pool()


def _articles():
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT n.title, n.content, n.url, n.source, n.published_at, n.scraped_at,
                      n.sentiment_label, n.sentiment_score, n.sentiment_model, t.symbol
               FROM news n LEFT JOIN tokens t ON t.id = n.token_id
               ORDER BY n.scraped_at"""
        )
        return cur.fetchall()


def _embedding_count(url=None):
    with db.get_connection() as conn, conn.cursor() as cur:
        if url:
            cur.execute(
                "SELECT count(*) FROM embeddings_news e JOIN news n ON n.id = e.news_id "
                "WHERE n.url = %s",
                (url,),
            )
        else:
            cur.execute("SELECT count(*) FROM embeddings_news")
        return cur.fetchone()[0]


@pytest.mark.integration
def test_article_is_stored_with_sentiment_and_embedding(clean_db):
    ingest_news.handle(_payload())

    rows = _articles()
    assert len(rows) == 1
    row = rows[0]
    assert row[2] == "https://news.test/article-1"
    assert row[3] == "decrypt"
    assert row[9] == "BTC"
    # All three sentiment columns written together — news_sentiment_complete.
    assert row[6] in ("positive", "neutral", "negative")
    assert row[7] is not None
    assert row[8].startswith("vader-")

    assert _embedding_count() == 1


@pytest.mark.integration
def test_embedding_has_the_schema_dimensionality(clean_db):
    ingest_news.handle(_payload())

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT vector_dims(embedding), model FROM embeddings_news")
        dims, model = cur.fetchone()

    assert dims == embedding.DIMENSIONS == 384
    assert model == embedding.MODEL_NAME


@pytest.mark.integration
def test_observation_time_comes_from_the_payload(clean_db):
    observed = "2026-10-09T01:02:03+00:00"
    ingest_news.handle(_payload(scraped_at=observed))

    (row,) = _articles()
    assert row[5].isoformat() == observed


# --- deduplication, the behaviour the backlog depends on -------------------


@pytest.mark.integration
def test_same_url_twice_stores_one_article(clean_db):
    """The collectors deduplicate only within a cycle.

    The same article is re-queued every 15 minutes; the backlog held 9683
    messages covering a few hundred unique URLs. ON CONFLICT (url) is what
    collapses them.
    """
    ingest_news.handle(_payload())
    ingest_news.handle(_payload())

    assert len(_articles()) == 1
    assert _embedding_count() == 1


@pytest.mark.integration
def test_repeat_does_not_run_the_embedding_model_again(clean_db):
    """The cost optimisation that makes draining the backlog feasible.

    Without this check, 9683 messages would mean 9683 model runs instead of a
    few hundred.
    """
    ingest_news.handle(_payload())

    calls = {"n": 0}
    original = ingest_news.embedding.embed_article

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    ingest_news.embedding.embed_article = counting
    try:
        ingest_news.handle(_payload())
    finally:
        ingest_news.embedding.embed_article = original

    assert calls["n"] == 0, "model ran again for an already-embedded article"


@pytest.mark.integration
def test_reingestion_rescores_sentiment(clean_db):
    """DO UPDATE, not DO NOTHING: a changed title must be rescored.

    This is also why sentiment is folded into the INSERT rather than applied
    as a follow-up UPDATE — the row is never briefly inconsistent.
    """
    ingest_news.handle(_payload(title="Exchange fraud sparks fears of collapse", content=None))
    (first,) = _articles()
    assert first[6] == "negative"

    ingest_news.handle(
        _payload(title="Excellent gains delight investors", content="wonderful news")
    )
    (second,) = _articles()
    assert second[6] == "positive", "re-ingestion did not rescore"


@pytest.mark.integration
def test_body_is_not_overwritten_with_null(clean_db):
    """COALESCE on content: a later message lacking a body must not erase one."""
    ingest_news.handle(_payload(content="the full article body"))
    ingest_news.handle(_payload(content=None))

    (row,) = _articles()
    assert row[1] == "the full article body"


# --- untagged and bodyless articles ----------------------------------------


@pytest.mark.integration
def test_article_without_a_symbol_is_stored_with_null_token(clean_db):
    """58% of collected articles carry no symbol; they are still useful context."""
    ingest_news.handle(_payload(symbol=None))

    (row,) = _articles()
    assert row[9] is None
    assert _embedding_count() == 1


@pytest.mark.integration
def test_untracked_symbol_leaves_token_null(clean_db):
    ingest_news.handle(_payload(symbol="NOTACOIN"))

    (row,) = _articles()
    assert row[9] is None


@pytest.mark.integration
def test_headline_only_article_is_stored_and_embedded(clean_db):
    """One of the three feeds supplies no body at all — 27% of the corpus."""
    ingest_news.handle(_payload(content=None))

    (row,) = _articles()
    assert row[1] is None
    assert row[6] is not None, "headline-only article must still be classified"
    assert _embedding_count() == 1, "headline-only article must still be embedded"


@pytest.mark.integration
def test_duplicate_url_across_sources_is_one_row(clean_db):
    """Syndicated stories appear in several feeds under the same URL."""
    ingest_news.handle(_payload(source="coindesk"))
    ingest_news.handle(_payload(source="decrypt"))

    rows = _articles()
    assert len(rows) == 1
    assert rows[0][3] == "decrypt", "the later source should win"
