from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from crypto_rag.db.models import Article
from crypto_rag.processing import chunk, embed, label
from crypto_rag.processing.assets import detect_assets
from crypto_rag.processing.clean import clean_text
from crypto_rag.processing.label_prompt import ArticleLabelOut
from tests.fakes import FakeOpenAI

PUB = datetime(2025, 3, 3, 14, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Bitcoin hits a new high", ["BTC"]),
        ("BTC and ETH slide together", ["BTC", "ETH"]),
        ("Ether's rally fades", ["ETH"]),
        ("Ethereum developers ship Pectra", ["ETH"]),
        ("whether or not the Fed cuts", []),
        ("Together with partners, the bank expands", []),
        ("bitcoins seized by police", ["BTC"]),
        ("XBT futures volumes", ["BTC"]),
        ("Solana outage", []),
        ("eth staking yields and BITCOIN miners", ["BTC", "ETH"]),
    ],
)
def test_asset_dictionary(text, expected):
    assert detect_assets(text) == expected


def test_clean_text_removes_html_and_signoffs():
    raw = "<p>Bitcoin   rose.</p><p>Sign up to our newsletter for daily updates</p>\n\nReuse this content"
    assert clean_text(raw) == "Bitcoin rose."


def _article(body: str | None, standfirst: str | None = "Short summary about ether.") -> Article:
    return Article(
        id="t:1", source="guardian", channel="api", url="u", title="Bitcoin falls",
        standfirst=standfirst, body_text=body, published_at=PUB, content_hash="h",
    )  # fmt: skip


def test_every_chunk_starts_with_title_and_date():
    body = " ".join(f"Sentence {i} about bitcoin markets and regulation." for i in range(400))
    drafts = chunk.build_chunks(_article(body))
    assert len(drafts) > 1
    for d in drafts:
        assert d.text.startswith("Bitcoin falls — 2025-03-03 14:05 UTC\n\n")


def test_chunks_are_within_token_bounds_and_overlap():
    body = " ".join(f"word{i}" for i in range(3000))
    pieces = chunk.split_tokens(body)
    enc = chunk._encoder()
    sizes = [len(enc.encode(p)) for p in pieces]
    assert all(300 <= s <= chunk.MAX_TOKENS + 2 for s in sizes)
    assert pieces[0].split()[-1] in pieces[1]  # neighbours share the overlap


def test_bodyless_article_is_one_chunk_with_standfirst():
    (only,) = chunk.build_chunks(_article(None))
    assert only.text == "Bitcoin falls — 2025-03-03 14:05 UTC\n\nShort summary about ether."
    assert only.assets == ["BTC", "ETH"]


def test_label_input_truncates_body():
    art = _article("x" * 5000)
    assert len(label.label_input(art)) < 1200


@pytest.mark.integration
def test_pipeline_chunks_embeds_and_labels_idempotently(engine):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM articles"))
        conn.execute(text("DELETE FROM llm_usage"))
        conn.execute(
            text(
                "INSERT INTO articles (id, source, channel, url, title, standfirst, published_at, content_hash) "
                "VALUES ('p:1', 'coindesk', 'rss', 'https://p/1', 'Bitcoin ETF inflows', 'Funds buy BTC', :t, 'h1')"
            ),
            {"t": PUB},
        )
    fake = FakeOpenAI([ArticleLabelOut(topic="institutional", tone="positive")])
    assert chunk.chunk_pending(engine) == 1
    assert chunk.chunk_pending(engine) == 0  # unchanged article is not re-chunked
    assert embed.embed_pending(engine, client=fake) == 1
    assert embed.embed_pending(engine, client=fake) == 0
    assert label.label_pending(engine, client=fake) == 1
    with engine.connect() as conn:
        c = conn.execute(
            text("SELECT assets, embedding_model, embedding IS NOT NULL AS e FROM chunks")
        ).one()
        lab = conn.execute(text("SELECT topic, tone FROM article_labels")).one()
        usage = conn.execute(text("SELECT count(*) FROM llm_usage")).scalar()
    assert c.assets == ["BTC"] and c.e and c.embedding_model == "text-embedding-3-large"
    assert (lab.topic, lab.tone) == ("institutional", "positive")
    assert usage == 2
    # Content change -> re-chunk.
    with engine.begin() as conn:
        conn.execute(text("UPDATE articles SET content_hash = 'h2' WHERE id = 'p:1'"))
    assert chunk.chunk_pending(engine) == 1


@pytest.mark.integration
def test_failed_label_is_retried_next_run(engine):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM article_labels"))
    fake = FakeOpenAI([RuntimeError("boom"), ArticleLabelOut(topic="market", tone="neutral")])
    assert label.label_pending(engine, client=fake) == 0
    assert label.label_pending(engine, client=fake) == 1
