"""CoinDesk publication history from GDELT.

Two inputs, one output shape:
  * DOC 2.0 API (rolling last ~3 months): `mode=artlist`, one query per day,
    at most one request every 5 s (GDELT answers 429 otherwise).
  * GKG 2.1 archive files (every 15 min since 2015) for the older period.
    Fields used: 2 DATE (batch time, 15-min resolution), 4 SourceCommonName,
    5 DocumentIdentifier (URL), 27 V2.1EXTRASXML (<PAGE_TITLE>).
    Files are streamed into memory and discarded (nothing large is kept).

GDELT's time is when GDELT SAW the article, not when CoinDesk published it.
It is stored as published_at (the only time available) and flagged in raw as
`time_source = "gdelt_seen"`. If the URL is already known from RSS, the RSS
row wins and only the GDELT sighting is appended to raw.

Usage:
    python -m crypto_rag.ingestion.gdelt api [--days 90]
    python -m crypto_rag.ingestion.gdelt archive --start 2025-01-20 --end 2026-07-20 [--workers 6]
    python -m crypto_rag.ingestion.gdelt report
"""

import argparse
import csv
import io
import json
import logging
import re
import sys
import zipfile
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import Engine, text
from sqlalchemy.dialects.postgresql import insert

from crypto_rag.config import setup_logging
from crypto_rag.db.models import Article
from crypto_rag.db.session import get_engine
from crypto_rag.ingestion.coindesk_rss import article_id, content_hash, normalize_url
from crypto_rag.ingestion.http import PoliteClient

logger = logging.getLogger(__name__)

DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
GKG_BASE = "http://data.gdeltproject.org/gdeltv2"
DOMAIN = "coindesk.com"
# CoinDesk article URLs carry the publication date: /<section>/YYYY/MM/DD/<slug>
ARTICLE_PATH = re.compile(r"^/(?:[a-z-]+/)+(\d{4})/(\d{2})/(\d{2})/([^/]+)$")
TITLE_RE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)
PROGRESS_FILE = Path("data/gdelt_archive_done.txt")

csv.field_size_limit(sys.maxsize)


def is_article_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.netloc.lower().endswith(DOMAIN) and bool(
        ARTICLE_PATH.match(parts.path.rstrip("/"))
    )


def title_from_slug(url: str) -> str:
    slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    words = slug.replace("-", " ").strip()
    return words[:1].upper() + words[1:]


def make_row(url: str, title: str | None, seen: datetime, origin: str) -> dict[str, Any]:
    url = normalize_url(url)
    clean_title = (title or "").strip()
    recovered = not clean_title
    if recovered:
        clean_title = title_from_slug(url)
    path = urlsplit(url).path
    return {
        "id": article_id(url),
        "source": "coindesk",
        "channel": "gdelt",
        "url": url,
        "title": clean_title,
        "standfirst": None,
        "body_text": None,
        "published_at": seen,
        "section": path.strip("/").split("/", 1)[0] or None,
        "tags": [],
        "is_liveblog": "live-updates" in path or "/live/" in path,
        "ai_assisted": False,
        "raw": {
            "time_source": "gdelt_seen",
            "gdelt_seen": seen.isoformat(),
            "gdelt_origin": origin,
            "title_from_slug": recovered,
        },
        "content_hash": content_hash(clean_title, None, None),
    }


def upsert_gdelt(engine: Engine, rows: Sequence[dict[str, Any]]) -> int:
    """Insert unseen URLs; for known URLs only record the GDELT sighting. Returns inserts."""
    if not rows:
        return 0
    # Keep the earliest sighting per URL within the batch.
    unique: dict[str, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: r["published_at"]):
        unique.setdefault(r["url"], r)
    stmt = insert(Article).values(list(unique.values()))
    stmt = stmt.on_conflict_do_update(
        index_elements=[Article.url],
        set_={
            # Earliest sighting wins; never overwrite RSS fields.
            "raw": text(
                "articles.raw || jsonb_build_object('gdelt_seen', "
                "LEAST(COALESCE(articles.raw->>'gdelt_seen', excluded.raw->>'gdelt_seen'), "
                "excluded.raw->>'gdelt_seen'))"
            ),
            "published_at": text(
                "CASE WHEN articles.channel = 'gdelt' "
                "THEN LEAST(articles.published_at, excluded.published_at) "
                "ELSE articles.published_at END"
            ),
        },
    ).returning(text("(xmax = 0)"))
    with engine.begin() as conn:
        return sum(1 for (inserted,) in conn.execute(stmt) if inserted)


# --- DOC API -----------------------------------------------------------------


def parse_doc_api(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for a in payload.get("articles", []):
        url = a.get("url", "")
        if not is_article_url(url):
            continue
        seen = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        rows.append(make_row(url, a.get("title"), seen, "doc_api"))
    return rows


def fetch_doc_api_day(client: PoliteClient, day: date) -> list[dict[str, Any]]:
    params = {
        "query": f"domainis:{DOMAIN}",
        "mode": "artlist",
        "format": "json",
        "maxrecords": 250,
        "startdatetime": f"{day:%Y%m%d}000000",
        "enddatetime": f"{day + timedelta(days=1):%Y%m%d}000000",
    }
    response = client.get(DOC_API, params=params)
    try:
        payload = response.json()
    except json.JSONDecodeError:
        # GDELT returns an empty body for days with no matches.
        return []
    rows = parse_doc_api(payload)
    if len(payload.get("articles", [])) >= 250:
        logger.warning("%s: hit the 250-record cap; day may be incomplete", day)
    return rows


def run_doc_api(engine: Engine, days: int, today: date | None = None) -> None:
    today = today or datetime.now(UTC).date()
    with PoliteClient(min_interval=10.0) as client:
        for i in range(days, -1, -1):
            day = today - timedelta(days=i)
            try:
                rows = fetch_doc_api_day(client, day)
            except httpx.HTTPError:
                # Rerunning the command fills the day in; inserts are idempotent.
                logger.exception("DOC API %s failed after retries; skipped", day)
                continue
            logger.info(
                "DOC API %s: %d articles, %d new", day, len(rows), upsert_gdelt(engine, rows)
            )


# --- GKG archive ---------------------------------------------------------------


def parse_gkg(content: bytes) -> list[dict[str, Any]]:
    rows = []
    reader = csv.reader(io.StringIO(content.decode("utf-8", errors="replace")), delimiter="\t")
    for rec in reader:
        if len(rec) < 5 or DOMAIN not in rec[3]:
            continue
        url = rec[4]
        if not is_article_url(url):
            continue
        seen = datetime.strptime(rec[1], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        extras = rec[26] if len(rec) > 26 else ""
        m = TITLE_RE.search(extras)
        rows.append(make_row(url, m.group(1) if m else None, seen, "gkg"))
    return rows


def gkg_slots(day: date) -> list[str]:
    start = datetime.combine(day, datetime.min.time(), UTC)
    return [f"{start + timedelta(minutes=15 * i):%Y%m%d%H%M%S}" for i in range(96)]


def fetch_gkg_slot(client: httpx.Client, slot: str) -> list[dict[str, Any]]:
    url = f"{GKG_BASE}/{slot}.gkg.csv.zip"
    for attempt in range(4):
        try:
            r = client.get(url)
            if r.status_code == 404:
                return []  # GDELT occasionally skips a 15-minute slot
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                return parse_gkg(zf.read(zf.namelist()[0]))
        except (httpx.HTTPError, zipfile.BadZipFile) as exc:
            logger.warning("GKG %s attempt %d failed: %s", slot, attempt + 1, exc)
    logger.error("GKG %s skipped after retries", slot)
    return []


def _done_days() -> set[str]:
    return set(PROGRESS_FILE.read_text().split()) if PROGRESS_FILE.exists() else set()


def run_archive(engine: Engine, start: date, end: date, workers: int) -> None:
    """Process GKG files day by day (resumable via PROGRESS_FILE)."""
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    done = _done_days()
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    todo = [d for d in days if d.isoformat() not in done]
    logger.info(
        "GKG archive: %d days to process (%d already done)", len(todo), len(days) - len(todo)
    )
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        for day in todo:
            rows: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(fetch_gkg_slot, client, s) for s in gkg_slots(day)]
                for f in as_completed(futures):
                    rows.extend(f.result())
            new = upsert_gdelt(engine, rows)
            logger.info("GKG %s: %d sightings, %d unique urls, %d new", day, len(rows),
                        len({r["url"] for r in rows}), new)  # fmt: skip
            with PROGRESS_FILE.open("a") as fh:
                fh.write(day.isoformat() + "\n")


# --- Report ------------------------------------------------------------------


def report(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        per_day = conn.execute(
            text(
                "SELECT published_at::date d, count(*) FROM articles "
                "WHERE source = 'coindesk' GROUP BY 1 ORDER BY 1"
            )
        ).all()
        overlap = conn.execute(
            text(
                "SELECT count(*) FILTER (WHERE raw ? 'gdelt_seen'), count(*) FROM ("
                "  SELECT raw FROM articles WHERE channel = 'rss' "
                "  AND published_at >= now() - interval '7 days' "
                "  AND published_at < now() - interval '1 day' "
                "  ORDER BY random() LIMIT 20) s"
            )
        ).one()
    counts = [n for _, n in per_day]
    empty_days = []
    if per_day:
        have = {d for d, _ in per_day}
        first, last = per_day[0][0], per_day[-1][0]
        empty_days = [
            (first + timedelta(days=i)).isoformat()
            for i in range((last - first).days + 1)
            if first + timedelta(days=i) not in have
        ]
    return {
        "days": len(per_day),
        "articles": sum(counts),
        "median_per_day": sorted(counts)[len(counts) // 2] if counts else 0,
        "empty_days": empty_days,
        "rss_sample_found_in_gdelt": f"{overlap[0]}/{overlap[1]}",
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    api = sub.add_parser("api")
    api.add_argument("--days", type=int, default=90)
    arc = sub.add_parser("archive")
    arc.add_argument("--start", type=date.fromisoformat, required=True)
    arc.add_argument("--end", type=date.fromisoformat, required=True)
    arc.add_argument("--workers", type=int, default=6)
    sub.add_parser("report")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> None:
    setup_logging()
    args = _parse_args(list(argv) if argv is not None else None)
    engine = get_engine()
    if args.command == "api":
        run_doc_api(engine, args.days)
    elif args.command == "archive":
        run_archive(engine, args.start, args.end, args.workers)
    logger.info("report: %s", json.dumps(report(engine), indent=2))


if __name__ == "__main__":
    main()
