from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from crypto_rag.ingestion import coindesk_rss as rss
from tests.conftest import FIXTURES

FETCHED = datetime(2026, 10, 21, 19, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def items():
    return rss.parse_feed((FIXTURES / "coindesk_rss.xml").read_bytes(), FETCHED)


def test_parses_all_items_with_description(items):
    assert len(items) == 25
    first = items[0]
    assert first["title"].startswith("Trump to throw spotlight")
    assert first["published_at"] == datetime(2026, 10, 21, 16, 23, 44, tzinfo=UTC)
    # The real <description> wins over the empty <dc:description/>.
    assert all(i["standfirst"] for i in items)
    assert all(i["body_text"] is None for i in items)
    assert first["section"] == "Policy"
    assert "Donald Trump" in first["tags"]


def test_ai_boost_items_are_flagged(items):
    flagged = [i for i in items if i["ai_assisted"]]
    assert len(flagged) == 5
    assert all("AI Boost" in i["raw"]["creators"] for i in flagged)


def test_url_normalisation_and_ids():
    url = "https://www.CoinDesk.com/markets/2026/10/10/some-story/?utm_source=rss#x"
    assert rss.normalize_url(url) == "https://www.coindesk.com/markets/2026/10/10/some-story"
    assert rss.article_id(url) == "coindesk:markets/2026/10/10/some-story"


def test_liveblog_category_is_detected():
    xml = b"""<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item>
      <title>Markets live</title><link>https://www.coindesk.com/live/x</link>
      <pubDate>Wed, 21 Oct 2026 10:00:00 +0000</pubDate>
      <category domain="tag">live_news</category></item></channel></rss>"""
    (item,) = rss.parse_feed(xml, FETCHED)
    assert item["is_liveblog"] is True


@pytest.mark.integration
def test_second_poll_creates_no_duplicates(engine, items):
    assert rss.upsert_rss(engine, items) == 25
    assert rss.upsert_rss(engine, items) == 0
    with engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM articles WHERE source = 'coindesk'")).scalar()
        fetched = conn.execute(text("SELECT min(fetched_at) FROM articles")).scalar()
    assert n == 25
    assert fetched == FETCHED
