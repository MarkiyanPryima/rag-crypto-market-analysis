"""CoinDesk RSS poller (~25 latest items, polled every 15 minutes).

The feed is parsed with ElementTree rather than feedparser: CoinDesk items
carry an empty <dc:description/> next to the real <description>, and
feedparser lets the empty one win, so every summary came back blank in v0.

The feed has no full text: body_text stays NULL and standfirst = description.

Usage:
    python -m crypto_rag.ingestion.coindesk_rss once
    python -m crypto_rag.ingestion.coindesk_rss run        # loop forever
    python -m crypto_rag.ingestion.coindesk_rss import-v0  # one-off, see D-01
"""

import argparse
import hashlib
import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from sqlalchemy import Engine, literal_column
from sqlalchemy.dialects.postgresql import insert

from crypto_rag.config import setup_logging
from crypto_rag.db.models import Article
from crypto_rag.db.session import get_engine
from crypto_rag.ingestion.http import PoliteClient

logger = logging.getLogger(__name__)

FEED_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml"
POLL_SECONDS = 15 * 60
AI_AUTHOR = "AI Boost"
NS = {"dc": "http://purl.org/dc/elements/1.1/"}


def normalize_url(url: str) -> str:
    """Drop query, fragment and trailing slash so the same article always has one URL."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def article_id(url: str) -> str:
    return "coindesk:" + urlsplit(normalize_url(url)).path.lstrip("/")


def content_hash(*parts: str | None) -> str:
    return hashlib.sha256("\x1f".join(p or "" for p in parts).encode()).hexdigest()


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_feed(xml: bytes, fetched_at: datetime) -> list[dict[str, Any]]:
    root = ET.fromstring(xml)  # noqa: S314
    out: list[dict[str, Any]] = []
    for item in root.iter("item"):
        title, link = _text(item.find("title")), _text(item.find("link"))
        pub = _text(item.find("pubDate"))
        if not (title and link and pub):
            logger.warning("skipping RSS item without title/link/pubDate")
            continue
        categories = [
            {"term": _text(c), "domain": c.get("domain", "")} for c in item.findall("category")
        ]
        creators = [
            name.strip()
            for c in item.findall("dc:creator", NS)
            for name in _text(c).split(",")
            if name.strip()
        ]
        description = _text(item.find("description")) or None
        section = next(
            (c["term"] for c in categories if c["domain"].startswith("https://www.coindesk.com/")
             and c["domain"].rstrip("/") != "https://www.coindesk.com"),
            None,
        )  # fmt: skip
        url = normalize_url(link)
        out.append(
            {
                "id": article_id(url),
                "source": "coindesk",
                "channel": "rss",
                "url": url,
                "title": title,
                "standfirst": description,
                "body_text": None,
                "published_at": parsedate_to_datetime(pub).astimezone(UTC),
                "fetched_at": fetched_at,
                "section": section,
                "tags": [c["term"] for c in categories],
                "is_liveblog": any(
                    "live_news" in (c["term"] + c["domain"]).lower().replace("-", "_")
                    for c in categories
                ),
                "ai_assisted": AI_AUTHOR in creators,
                "raw": {
                    "guid": _text(item.find("guid")),
                    "pubDate": pub,
                    "creators": creators,
                    "categories": categories,
                },
                "content_hash": content_hash(title, description, None),
            }
        )
    return out


def upsert_rss(engine: Engine, rows: Sequence[dict[str, Any]]) -> int:
    """Insert new items; refresh mutable fields of known ones. Returns new-row count.

    fetched_at keeps the FIRST time the item was seen; raw metadata from an
    earlier sighting is merged, not replaced.
    """
    if not rows:
        return 0
    stmt = insert(Article).values(list(rows))
    ex = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[Article.url],
        set_={
            "channel": ex.channel,
            "title": ex.title,
            "standfirst": ex.standfirst,
            "published_at": ex.published_at,
            "section": ex.section,
            "tags": ex.tags,
            "is_liveblog": ex.is_liveblog,
            "ai_assisted": ex.ai_assisted,
            "raw": Article.raw.op("||")(ex.raw),
            "content_hash": ex.content_hash,
        },
    ).returning(literal_column("(xmax = 0)"))  # true when inserted, not updated
    with engine.begin() as conn:
        return sum(1 for (inserted,) in conn.execute(stmt) if inserted)


def poll_once(engine: Engine, client: PoliteClient) -> int:
    fetched_at = datetime.now(UTC)
    rows = parse_feed(client.get(FEED_URL).content, fetched_at)
    new = upsert_rss(engine, rows)
    logger.info("CoinDesk RSS: %d items, %d new", len(rows), new)
    return new


def import_v0(engine: Engine, v0_url: str) -> int:
    """Copy CoinDesk rows collected by the v0 stack (title, url, times only)."""
    from sqlalchemy import create_engine, text

    v0 = create_engine(v0_url)
    with v0.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT title, url, published_at, scraped_at FROM news "
                "WHERE source = 'coindesk' AND published_at IS NOT NULL"
            )
        ).all()
    articles = [
        {
            "id": article_id(r.url),
            "source": "coindesk",
            "channel": "rss",
            "url": normalize_url(r.url),
            "title": r.title,
            "published_at": r.published_at,
            "fetched_at": r.scraped_at,
            "raw": {"imported_from": "v0.news"},
            "content_hash": content_hash(r.title, None, None),
        }
        for r in rows
    ]
    if not articles:
        return 0
    stmt = (
        insert(Article)
        .values(articles)
        .on_conflict_do_nothing(index_elements=[Article.url])
        .returning(Article.id)
    )
    with engine.begin() as conn:
        n = len(conn.execute(stmt).all())
    logger.info("imported %d of %d v0 CoinDesk rows", n, len(articles))
    return n


def main(argv: Sequence[str] | None = None) -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["once", "run", "import-v0"])
    parser.add_argument("--v0-url", help="SQLAlchemy URL of the v0 database (import-v0)")
    args = parser.parse_args(argv)
    engine = get_engine()
    if args.command == "import-v0":
        import_v0(engine, args.v0_url)
        return
    with PoliteClient(min_interval=1.0) as client:
        while True:
            try:
                poll_once(engine, client)
            except Exception:
                logger.exception("CoinDesk RSS poll failed; retrying next cycle")
            if args.command == "once":
                return
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
