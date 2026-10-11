"""RSS parsing and symbol tagging.

Reads three crypto news feeds, normalises their differing shapes into one
article dict, and tags each article with the coin it is about.

The three feeds genuinely differ, verified against live responses on 11 Oct:

    coindesk       summary AND content both empty — titles only, no body text
    cointelegraph  summary contains HTML (<p>, <img>) that must be stripped
    decrypt        summary is plain text

The CoinDesk case matters for the thesis: a third of sources provide no body,
so those articles are classified on their headline alone. That is the gap the
scraper fallback service (step 16) exists to close, and it belongs in the
limitations section either way.
"""

import logging
import re
from datetime import UTC, datetime
from typing import Any

import feedparser
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# (source name stored in news.source, feed URL)
FEEDS: list[tuple[str, str]] = [
    ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("cointelegraph", "https://cointelegraph.com/rss"),
    ("decrypt", "https://decrypt.co/feed"),
]

# Some feeds reject the default python-requests/feedparser agent.
USER_AGENT = "Mozilla/5.0 (compatible; crypto-rag-thesis/1.0)"

# Names that are neither tokens.symbol nor tokens.name but appear in headlines.
# Everything else is derived from the database; this covers only what the schema
# cannot express today. A tokens.aliases TEXT[] column would fold these in and
# is a reasonable use of the migration tooling if the list grows.
EXTRA_ALIASES: dict[str, str] = {
    "ripple": "XRP",
    "binance coin": "BNB",
}


def build_aliases(db_aliases: dict[str, str]) -> dict[str, str]:
    """Combine database-derived aliases with the curated extras.

    Takes db.fetch_symbol_aliases() output. The database is authoritative, so
    its entries win on conflict; the extras only fill gaps the schema cannot
    express.
    """
    return {**EXTRA_ALIASES, **db_aliases}


def clean_html(raw: str | None) -> str:
    """Strip markup and collapse whitespace.

    CoinTelegraph summaries embed a floated <img> and <p> wrappers. Left in
    place, the tags reach the embedding model and the sentiment classifier as
    noise, and VADER would score punctuation from the markup.
    """
    if not raw:
        return ""
    text = BeautifulSoup(raw, "lxml").get_text(separator=" ")
    return re.sub(r"\s+", " ", text).strip()


def _entry_body(entry: Any) -> str:
    """Best available body text, accounting for per-feed differences.

    feedparser exposes `content` as a list of dicts; CoinDesk populates neither
    it nor `summary`, so this legitimately returns "" for that source.
    """
    content = entry.get("content") or []
    for item in content:
        cleaned = clean_html(item.get("value"))
        if cleaned:
            return cleaned
    return clean_html(entry.get("summary"))


def _published_at(entry: Any) -> str | None:
    """Return the publication time as an ISO string, or None if absent.

    news.published_at is nullable precisely because this can fail. feedparser
    gives a UTC time struct; it is made timezone-aware explicitly rather than
    left naive, which would later be misread as local time.
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=UTC).isoformat()


def detect_symbol(title: str, body: str, aliases: dict[str, str]) -> str | None:
    """Return the coin an article is about, or None.

    Two deliberate differences from the reference implementation:

    1. The title is searched before the body. A coin named in the headline is
       what the article is actually about; one mentioned in passing lower down
       usually is not.
    2. Within a field the *earliest mention* wins, not the first entry of a
       hardcoded list. The reference iterates a fixed list starting at "BTC",
       so an article titled "Ethereum upgrade shakes bitcoin" is tagged BTC
       purely because of list order.

    Word boundaries prevent "ADA" matching "Adam" and "SOL" matching "solution"
    — a real false-positive source, since news.token_id drives every per-symbol
    sentiment aggregate.
    """
    for field in (title, body):
        if not field:
            continue
        best: tuple[int, str] | None = None
        for alias, symbol in aliases.items():
            match = re.search(rf"\b{re.escape(alias)}\b", field, re.IGNORECASE)
            if match and (best is None or match.start() < best[0]):
                best = (match.start(), symbol)
        if best:
            return best[1]
    return None


def parse_feed(source: str, raw: bytes | str, aliases: dict[str, str]) -> list[dict[str, Any]]:
    """Parse one feed's payload into article dicts."""
    # agent matters when `raw` is a URL — some feeds reject the default
    # feedparser agent with a 403. Harmless when `raw` is already bytes.
    feed = feedparser.parse(raw, agent=USER_AGENT)
    if feed.bozo and not feed.entries:
        # bozo with entries present means a recoverable quirk; with none it is
        # a genuine parse failure.
        raise ValueError(f"{source}: unparseable feed ({feed.get('bozo_exception')})")

    articles = []
    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        url = (entry.get("link") or "").strip()
        if not title or not url:
            # news.url is NOT NULL and UNIQUE — it is the dedupe key, so an
            # entry without one cannot be stored.
            logger.warning("%s: skipping entry with no title or link", source)
            continue

        body = _entry_body(entry)
        articles.append(
            {
                "title": title,
                "url": url,
                "content": body or None,
                "source": source,
                "published_at": _published_at(entry),
                "symbol": detect_symbol(title, body, aliases),
                # Observation time stamped here, not left to the column
                # default, so a backlogged queue keeps truthful timestamps.
                "scraped_at": datetime.now(UTC).isoformat(),
            }
        )

    return articles


def fetch_articles(
    aliases: dict[str, str], feeds: list[tuple[str, str]] | None = None
) -> list[dict[str, Any]]:
    """Fetch every feed and return deduplicated articles.

    A failing feed is logged and skipped rather than aborting the cycle: two
    working sources beat none.
    """
    articles: list[dict[str, Any]] = []
    seen: set[str] = set()

    for source, url in feeds or FEEDS:
        try:
            feed_articles = parse_feed(source, url, aliases)
        except Exception:
            logger.exception("%s: feed failed, skipping", source)
            continue

        fresh = 0
        for article in feed_articles:
            # Deduplicate within the cycle. Syndicated stories appear in more
            # than one feed, and the database would reject the second copy on
            # news.url anyway — better to not queue work that must fail.
            if article["url"] in seen:
                continue
            seen.add(article["url"])
            articles.append(article)
            fresh += 1

        logger.info("%s: %d articles (%d new this cycle)", source, len(feed_articles), fresh)

    tagged = sum(1 for a in articles if a["symbol"])
    bodyless = sum(1 for a in articles if not a["content"])
    logger.info(
        "%d articles total, %d tagged with a symbol, %d without body text",
        len(articles),
        tagged,
        bodyless,
    )
    return articles
