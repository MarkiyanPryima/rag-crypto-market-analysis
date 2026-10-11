"""Tests for RSS parsing and symbol tagging.

Fixtures are real feed responses recorded on 11 Oct 2026. That matters more
here than for the CoinGecko tests: the three feeds disagree about where body
text lives, and a handwritten fixture would have encoded my assumption rather
than their actual behaviour. CoinDesk supplying no body at all is the clearest
example — it would never have occurred to me to invent that.
"""

import pathlib

import pytest

from collectors import rss

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# As built by db.fetch_symbol_aliases() from the seeded tokens table, plus the
# curated extras.
ALIASES = {
    "btc": "BTC",
    "bitcoin": "BTC",
    "eth": "ETH",
    "ethereum": "ETH",
    "sol": "SOL",
    "solana": "SOL",
    "bnb": "BNB",
    "xrp": "XRP",
    "ada": "ADA",
    "cardano": "ADA",
    "doge": "DOGE",
    "dogecoin": "DOGE",
    "dot": "DOT",
    "polkadot": "DOT",
    **rss.EXTRA_ALIASES,
}


@pytest.fixture(params=["coindesk", "cointelegraph", "decrypt"])
def feed(request):
    return request.param, (FIXTURES / f"{request.param}.xml").read_bytes()


# --- parsing ---------------------------------------------------------------


def test_every_feed_parses(feed):
    source, raw = feed
    articles = rss.parse_feed(source, raw, ALIASES)
    assert articles, f"{source} produced no articles"


def test_required_fields_present(feed):
    """news.title is NOT NULL and news.url is NOT NULL UNIQUE."""
    source, raw = feed
    for article in rss.parse_feed(source, raw, ALIASES):
        assert article["title"]
        assert article["url"].startswith("http")
        assert article["source"] == source
        assert article["scraped_at"]


def test_coindesk_supplies_no_body_text():
    """Documents a real limitation, and pins it so a feed change is noticed.

    All 25 recorded CoinDesk entries have empty summary and empty content, so
    those articles can only be classified on their headline.
    """
    articles = rss.parse_feed("coindesk", (FIXTURES / "coindesk.xml").read_bytes(), ALIASES)
    assert all(a["content"] is None for a in articles)


def test_cointelegraph_html_is_stripped():
    """Summaries embed <p> and a floated <img>; markup must not reach VADER."""
    articles = rss.parse_feed(
        "cointelegraph", (FIXTURES / "cointelegraph.xml").read_bytes(), ALIASES
    )
    bodies = [a["content"] for a in articles if a["content"]]
    assert bodies, "expected cointelegraph to supply body text"
    for body in bodies:
        assert "<" not in body
        assert "  " not in body, "whitespace should be collapsed"


def test_decrypt_plain_text_survives():
    articles = rss.parse_feed("decrypt", (FIXTURES / "decrypt.xml").read_bytes(), ALIASES)
    assert any(a["content"] for a in articles)


def test_published_at_is_timezone_aware(feed):
    """A naive timestamp would be misread as local time on insert."""
    source, raw = feed
    stamps = [a["published_at"] for a in rss.parse_feed(source, raw, ALIASES)]
    assert any(stamps), f"{source} supplied no publication dates"
    for stamp in filter(None, stamps):
        assert "+" in stamp or stamp.endswith("Z")


def test_entry_without_link_is_skipped():
    """url is the dedupe key and NOT NULL, so such an entry cannot be stored."""
    xml = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>No link here</title></item>
      <item><title>Has a link</title><link>https://x.test/1</link></item>
    </channel></rss>"""
    articles = rss.parse_feed("test", xml, ALIASES)
    assert [a["title"] for a in articles] == ["Has a link"]


def test_unparseable_feed_raises():
    with pytest.raises(ValueError, match="unparseable feed"):
        rss.parse_feed("test", b"\x00\x01not xml at all", ALIASES)


# --- symbol tagging --------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Bitcoin rallies past $90k", "BTC"),
        ("BTC rallies past $90k", "BTC"),
        ("Ethereum upgrade goes live", "ETH"),
        ("Ripple wins court case", "XRP"),
        ("Binance Coin hits new high", "BNB"),
        ("Cardano founder speaks", "ADA"),
        ("CFTC proposes new rules", None),
    ],
)
def test_symbol_detected_from_title(title, expected):
    assert rss.detect_symbol(title, "", ALIASES) == expected


@pytest.mark.parametrize(
    "title",
    ["Adam Back on custody", "A solution to scaling", "Dotted lines in policy", "Adapt or die"],
)
def test_word_boundaries_prevent_false_positives(title):
    """ADA must not fire on "Adam", SOL on "solution", DOT on "Dotted".

    These drive news.token_id, so a false positive silently pollutes that
    coin's sentiment aggregate.
    """
    assert rss.detect_symbol(title, "", ALIASES) is None


def test_earliest_mention_wins_not_list_order():
    """The reference iterates a list starting at BTC, so this returns BTC.

    Scanning by position instead makes the tag reflect the headline.
    """
    assert rss.detect_symbol("Ethereum upgrade shakes bitcoin", "", ALIASES) == "ETH"
    assert rss.detect_symbol("Bitcoin slips as ethereum climbs", "", ALIASES) == "BTC"


def test_title_takes_precedence_over_body():
    """A coin in the headline is the subject; one mentioned in passing is not."""
    assert rss.detect_symbol("Solana outage", "bitcoin was unaffected", ALIASES) == "SOL"


def test_body_is_used_when_title_has_no_symbol():
    assert rss.detect_symbol("Markets wobble", "dogecoin led the decline", ALIASES) == "DOGE"


def test_detection_is_case_insensitive():
    for variant in ("BITCOIN", "bitcoin", "BiTcOiN"):
        assert rss.detect_symbol(f"{variant} news", "", ALIASES) == "BTC"


# --- alias building --------------------------------------------------------


def test_build_aliases_includes_database_and_extras():
    merged = rss.build_aliases({"btc": "BTC", "bitcoin": "BTC"})
    assert merged["bitcoin"] == "BTC"
    assert merged["ripple"] == "XRP", "curated extra missing"


def test_database_wins_over_extras():
    """The seeded table is authoritative; extras only fill gaps."""
    merged = rss.build_aliases({"ripple": "XRPL"})
    assert merged["ripple"] == "XRPL"


# --- clean_html ------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [(None, ""), ("", ""), ("   ", "")])
def test_clean_html_handles_empty(raw, expected):
    assert rss.clean_html(raw) == expected


def test_clean_html_strips_tags_and_collapses_space():
    raw = '<p>Bitcoin   rose</p>\n\n<img src="x.png"/><span>  sharply</span>'
    assert rss.clean_html(raw) == "Bitcoin rose sharply"


# --- fetch_articles --------------------------------------------------------


def test_duplicates_across_feeds_are_dropped(monkeypatch):
    """Syndicated stories appear in several feeds; news.url is UNIQUE."""
    shared = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Shared story</title><link>https://x.test/same</link></item>
    </channel></rss>"""
    monkeypatch.setattr(rss, "FEEDS", [("a", shared), ("b", shared)])

    articles = rss.fetch_articles(ALIASES)
    assert len(articles) == 1
    assert articles[0]["source"] == "a"


def test_one_failing_feed_does_not_abort_the_cycle(monkeypatch):
    """Two working sources beat none."""
    good = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Bitcoin news</title><link>https://x.test/1</link></item>
    </channel></rss>"""
    monkeypatch.setattr(rss, "FEEDS", [("broken", b"\x00not xml"), ("good", good)])

    articles = rss.fetch_articles(ALIASES)
    assert len(articles) == 1
    assert articles[0]["source"] == "good"
