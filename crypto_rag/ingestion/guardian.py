"""The Guardian Open Platform: full-text articles about crypto.

Steps (stage 4):
  1. `tags` lists candidate keyword tags for the author to confirm; the
     confirmed list lives in settings.guardian_tags.
  2. `backfill` pages through /search from settings.start_date, filtering by
     FIRST publication date (use-date=first-publication) and storing that date
     as published_at.
  3. `update` fetches new articles; `modified` re-fetches articles changed in
     the last day (lastModified), so corrections reach the corpus.
Live blogs (type=liveblog) are stored but flagged is_liveblog; retrieval skips them.
A developer key allows ~1 request/s and 500/day, so calls are paced at 1.1 s.

Usage:
    python -m crypto_rag.ingestion.guardian tags [--q bitcoin]
    python -m crypto_rag.ingestion.guardian backfill
    python -m crypto_rag.ingestion.guardian update | modified | report
"""

import argparse
import json
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, select, text
from sqlalchemy.dialects.postgresql import insert

from crypto_rag.config import get_settings, setup_logging
from crypto_rag.db.models import Article
from crypto_rag.db.session import get_engine
from crypto_rag.ingestion.coindesk_rss import content_hash
from crypto_rag.ingestion.http import PoliteClient

logger = logging.getLogger(__name__)

API = "https://content.guardianapis.com"
FIELDS = "headline,trailText,bodyText,firstPublicationDate,lastModified,liveBloggingNow"
PAGE_SIZE = 50


def _key() -> str:
    key = get_settings().guardian_api_key.get_secret_value()
    if not key:
        raise RuntimeError("GUARDIAN_API_KEY is not set in .env")
    return key


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def parse_result(r: dict[str, Any], fetched_at: datetime) -> dict[str, Any]:
    f = r.get("fields", {})
    first = _ts(f.get("firstPublicationDate")) or _ts(r["webPublicationDate"])
    title = f.get("headline") or r["webTitle"]
    standfirst = f.get("trailText")
    body = f.get("bodyText") or None
    return {
        "id": f"guardian:{r['id']}",
        "source": "guardian",
        "channel": "api",
        "url": r["webUrl"],
        "title": title,
        "standfirst": standfirst,
        "body_text": body,
        "published_at": first,
        "fetched_at": fetched_at,
        "section": r.get("sectionName"),
        "tags": [t["id"] for t in r.get("tags", [])],
        "is_liveblog": r.get("type") == "liveblog",
        "ai_assisted": False,
        "raw": {
            "guardian_id": r["id"],
            "type": r.get("type"),
            "webPublicationDate": r.get("webPublicationDate"),
            "lastModified": f.get("lastModified"),
            "pillarName": r.get("pillarName"),
        },
        "content_hash": content_hash(title, standfirst, body),
    }


def upsert_guardian(engine: Engine, rows: Sequence[dict[str, Any]]) -> int:
    """Insert or refresh by url. fetched_at keeps the first fetch. Returns new-row count."""
    if not rows:
        return 0
    stmt = insert(Article).values(list(rows))
    ex = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[Article.url],
        set_={
            c: ex[c]
            for c in (
                "title",
                "standfirst",
                "body_text",
                "published_at",
                "section",
                "tags",
                "is_liveblog",
                "raw",
                "content_hash",
            )
        },
    ).returning(text("(xmax = 0)"))
    with engine.begin() as conn:
        return sum(1 for (inserted,) in conn.execute(stmt) if inserted)


def search_pages(
    client: PoliteClient,
    tags: Sequence[str],
    from_date: date,
    use_date: str,
    to_date: date | None = None,
):
    """Yield (page payload) for an OR over tags, oldest first."""
    page = 1
    while True:
        params = {
            "api-key": _key(),
            "tag": "|".join(tags),
            "from-date": from_date.isoformat(),
            "use-date": use_date,
            "order-by": "oldest",
            "show-fields": FIELDS,
            "show-tags": "keyword",
            "page-size": PAGE_SIZE,
            "page": page,
        }
        if to_date:
            params["to-date"] = to_date.isoformat()
        payload = client.get(f"{API}/search", params=params).json()["response"]
        yield payload
        if page >= payload.get("pages", 0):
            return
        page += 1


def fetch(engine: Engine, from_date: date, use_date: str = "first-publication") -> dict[str, int]:
    s = get_settings()
    if not s.guardian_tags:
        raise RuntimeError("GUARDIAN_TAGS is empty: confirm tags from `guardian tags` first")
    start = s.start_date
    stats = {"total": 0, "saved": 0, "new": 0, "skipped_before_start": 0}
    with PoliteClient(min_interval=1.1) as client:
        for payload in search_pages(client, s.guardian_tags, from_date, use_date):
            stats["total"] = payload.get("total", 0)
            now = datetime.now(UTC)
            rows = [parse_result(r, now) for r in payload.get("results", [])]
            kept = [r for r in rows if r["published_at"] >= start]
            stats["skipped_before_start"] += len(rows) - len(kept)
            stats["new"] += upsert_guardian(engine, kept)
            stats["saved"] += len(kept)
            logger.info(
                "Guardian page %s/%s: %d saved",
                payload.get("currentPage"),
                payload.get("pages"),
                len(kept),
            )
    return stats


def last_published(engine: Engine) -> date | None:
    with engine.connect() as conn:
        ts = conn.execute(
            select(func.max(Article.published_at)).where(Article.source == "guardian")
        ).scalar()
    return ts.date() if ts else None


def list_tags(q: str) -> list[dict[str, Any]]:
    with PoliteClient(min_interval=1.1) as client:
        payload = client.get(
            f"{API}/tags", params={"api-key": _key(), "q": q, "type": "keyword", "page-size": 50}
        ).json()
    return [{"id": t["id"], "title": t["webTitle"], "section": t.get("sectionName")}
            for t in payload["response"]["results"]]  # fmt: skip


def report(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        months = conn.execute(text(
            "SELECT to_char(date_trunc('month', published_at), 'YYYY-MM') m, count(*), "
            "count(*) FILTER (WHERE is_liveblog) live FROM articles WHERE source = 'guardian' GROUP BY 1 ORDER BY 1"
        )).all()  # fmt: skip
        early = conn.execute(text(
            "SELECT count(*) FROM articles WHERE source = 'guardian' AND published_at < :s"
        ), {"s": get_settings().start_date}).scalar()  # fmt: skip
    return {
        "per_month": [{"month": m, "articles": n, "liveblogs": live} for m, n, live in months],
        "before_start_date": early,
    }


def main(argv: Sequence[str] | None = None) -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Guardian ingestion")
    parser.add_argument("command", choices=["tags", "backfill", "update", "modified", "report"])
    parser.add_argument("--q", default="bitcoin")
    args = parser.parse_args(argv)
    engine = get_engine()
    if args.command == "tags":
        for t in list_tags(args.q):
            logger.info("%-45s %-40s %s", t["id"], t["title"], t["section"])
        return
    if args.command == "backfill":
        stats = fetch(engine, get_settings().start_date.date())
    elif args.command == "update":
        stats = fetch(engine, last_published(engine) or get_settings().start_date.date())
    elif args.command == "modified":
        stats = fetch(
            engine, (datetime.now(UTC) - timedelta(days=1)).date(), use_date="last-modified"
        )
    else:
        stats = {}
    logger.info("stats: %s\nreport: %s", stats, json.dumps(report(engine), indent=2))


if __name__ == "__main__":
    main()
