from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from crypto_rag.retrieval.hybrid import HybridNewsRetriever, decay_factor, search_news

pytestmark = pytest.mark.integration
AS_OF = datetime(2025, 9, 10, 12, tzinfo=UTC)
DIM = 1536


def _vec(x: float) -> str:
    return "[" + ",".join([str(x), "1"] + ["0"] * (DIM - 2)) + "]"


@pytest.fixture
def corpus(engine):
    docs = [
        # id, hours before as_of (negative = after), text, assets, vector-x, liveblog
        ("r:old", 30, "Bitcoin ETF inflows surge as institutions buy", ["BTC"], 0.5, False),
        ("r:new", 2, "Bitcoin ETF inflows surge as institutions buy", ["BTC"], 0.5, False),
        ("r:future", -1, "Bitcoin ETF inflows surge as institutions buy", ["BTC"], 0.5, False),
        ("r:eth", 3, "Ethereum upgrade goes live", ["ETH"], 0.9, False),
        ("r:live", 1, "Bitcoin ETF inflows live blog", ["BTC"], 0.5, True),
        ("r:ancient", 500, "Bitcoin ETF inflows surge as institutions buy", ["BTC"], 0.5, False),
    ]
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM articles"))
        for aid, h, body, assets, x, live in docs:
            ts = AS_OF - timedelta(hours=h)
            conn.execute(
                text("INSERT INTO articles (id, source, channel, url, title, published_at, is_liveblog) "
                     "VALUES (:i, 'coindesk', 'rss', :u, :t, :p, :l)"),
                {"i": aid, "u": f"https://x/{aid}", "t": body, "p": ts, "l": live},
            )  # fmt: skip
            conn.execute(
                text("INSERT INTO chunks (article_id, chunk_index, text, published_at, assets, embedding, content_hash) "
                     "VALUES (:i, 0, :b, :p, :a, CAST(:e AS vector), 'h')"),
                {"i": aid, "b": body, "p": ts, "a": assets, "e": _vec(x)},
            )  # fmt: skip


def fake_embed(_query: str) -> list[float]:
    return [0.5, 1.0] + [0.0] * (DIM - 2)


def test_future_articles_never_returned(engine, corpus):
    hits = search_news(
        engine,
        "bitcoin ETF inflows",
        AS_OF,
        window_hours=1000,
        embed_query=fake_embed,
        with_reaction=False,
    )
    ids = {h.article_id for h in hits}
    assert "r:future" not in ids
    assert all(datetime.fromisoformat(h.published_at) <= AS_OF for h in hits)


def test_fresher_article_ranks_higher_at_equal_similarity(engine, corpus):
    hits = search_news(
        engine,
        "bitcoin ETF inflows",
        AS_OF,
        asset="BTC",
        window_hours=1000,
        embed_query=fake_embed,
        with_reaction=False,
    )
    order = [h.article_id for h in hits]
    assert order.index("r:new") < order.index("r:old") < order.index("r:ancient")


def test_filters_window_asset_and_liveblog(engine, corpus):
    hits = search_news(
        engine,
        "bitcoin ETF",
        AS_OF,
        asset="BTC",
        window_hours=48,
        embed_query=fake_embed,
        with_reaction=False,
    )
    ids = {h.article_id for h in hits}
    assert ids == {"r:new", "r:old"}
    assert all(h.id.startswith("c_") for h in hits)


def test_full_text_only_when_no_embedder(engine, corpus):
    hits = search_news(engine, "ethereum upgrade", AS_OF, window_hours=48, with_reaction=False)
    assert [h.article_id for h in hits] == ["r:eth"]
    assert hits[0].rank_vec is None and hits[0].rank_fts == 1


def test_decay_halves_at_half_life():
    assert decay_factor(AS_OF - timedelta(hours=48), AS_OF, 48) == pytest.approx(0.5)


def test_langchain_retriever_uses_fixed_as_of(engine, corpus):
    r = HybridNewsRetriever(
        engine=engine, as_of=AS_OF - timedelta(hours=10), window_hours=1000, embed_query=fake_embed
    )
    docs = r.invoke("bitcoin ETF inflows")
    assert docs and "r:new" not in {d.metadata["article_id"] for d in docs}


def test_fts_query_or_joins_words_and_keeps_phrases():
    from crypto_rag.retrieval.hybrid import fts_query

    assert fts_query("Bitcoin ETF inflows") == "Bitcoin or ETF or inflows"
    assert fts_query('"spot ETF" approval, SEC') == '"spot ETF" or approval or SEC'


def test_long_model_query_still_hits_full_text(engine, corpus):
    # Real failure: an all-terms AND query returned no full-text candidates at all.
    q = "Bitcoin BTC market price macro regulation institutional flows November 2026"
    hits = search_news(engine, q, AS_OF, window_hours=1000, with_reaction=False)
    assert any(h.rank_fts for h in hits)
