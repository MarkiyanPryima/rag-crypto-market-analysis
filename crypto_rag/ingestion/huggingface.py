"""Historical crypto news from Hugging Face datasets.

Two public datasets cover the periods the live sources cannot reach:
  * maryamfakhari/crypto-news-coindesk-2020-2025 – CoinDesk Data news feed (many
    publishers), 2019-10 → 2025-02, title + short body, exact publication time;
  * katelabs/crypto-news-scored – headlines from ~20 publishers, 2026-02 → 2026-04,
    exact publication time, tagged coins.

Only articles about BTC or ETH published on or after settings.start_date are kept.
`source` is the publisher's short name, `channel` is 'hf', and `raw` records the
dataset and the original row id. Downloads are cached under data/hf/ with their
SHA-256 so an import can be repeated and audited.

Usage:
    python -m crypto_rag.ingestion.huggingface import [--dataset maryamfakhari|katelabs] [--limit N]
"""

import argparse
import hashlib
import logging
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert

from crypto_rag.config import get_settings, setup_logging
from crypto_rag.db.models import Article
from crypto_rag.db.session import get_engine
from crypto_rag.ingestion.coindesk_rss import content_hash, normalize_url
from crypto_rag.processing.assets import detect_assets

logger = logging.getLogger(__name__)

CACHE = Path("data/hf")
BATCH = 5000
ASSETS = {"BTC", "ETH"}


@dataclass(frozen=True)
class Dataset:
    key: str
    repo: str
    url: str
    file: str
    licence: str


DATASETS = {
    "maryamfakhari": Dataset(
        key="maryamfakhari",
        repo="maryamfakhari/crypto-news-coindesk-2020-2025",
        url="https://huggingface.co/api/datasets/maryamfakhari/crypto-news-coindesk-2020-2025/parquet/default/train/0.parquet",
        file="maryamfakhari_2020_2025.parquet",
        licence="CC BY-NC 4.0",
    ),
    "katelabs": Dataset(
        key="katelabs",
        repo="katelabs/crypto-news-scored",
        url="https://huggingface.co/datasets/katelabs/crypto-news-scored/resolve/main/headlines_2026-02_2026-04.parquet",
        file="katelabs_headlines_2026-02_2026-04.parquet",
        licence="CC BY-NC 4.0",
    ),
}


def publisher_key(name: str) -> str:
    """'AMBCrypto' -> 'ambcrypto', 'Crypto Economy' -> 'crypto-economy'."""
    key = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return key or "unknown"


def download(ds: Dataset) -> tuple[Path, str]:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / ds.file
    if not path.exists():
        logger.info("downloading %s", ds.repo)
        with httpx.stream("GET", ds.url, follow_redirects=True, timeout=600) as r:
            r.raise_for_status()
            with path.open("wb") as fh:
                for block in r.iter_bytes(1 << 20):
                    fh.write(block)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    logger.info(
        "%s: %s (%.1f MB, sha256 %s)", ds.repo, path, path.stat().st_size / 1e6, digest[:16]
    )
    return path, digest


def _clean(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def rows_maryamfakhari(df: pd.DataFrame, ds: Dataset, digest: str) -> Iterator[dict[str, Any]]:
    # published_on is UTC without an offset (CoinDesk Data publishes Unix time).
    published = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    for r, ts in zip(df.itertuples(index=False), published, strict=True):
        title, body, url = _clean(r.title), _clean(r.body), _clean(r.url)
        if not (title and url) or pd.isna(ts):
            continue
        if not ASSETS & set(detect_assets(f"{title} {(body or '')[:3000]}")):
            continue
        yield {
            "id": f"hf:{ds.key}:{r.id}",
            "source": publisher_key(r.source),
            "channel": "hf",
            "url": normalize_url(url),
            "title": title,
            "standfirst": None,
            "body_text": body,
            "published_at": ts.to_pydatetime(),
            "section": None,
            "tags": [t for t in (_clean(r.tags) or "").split("|") if t],
            "raw": {
                "dataset": ds.repo, "dataset_sha256": digest, "hf_id": int(r.id),
                "source_name": r.source, "categories": _clean(r.categories),
                "upvotes": int(r.upvotes), "downvotes": int(r.downvotes),
            },
            "content_hash": content_hash(title, None, body),
        }  # fmt: skip


def rows_katelabs(df: pd.DataFrame, ds: Dataset, digest: str) -> Iterator[dict[str, Any]]:
    for r in df.itertuples(index=False):
        headline, url = _clean(r.headline), _clean(r.url)
        if not (headline and url):
            continue
        coins = {str(c).upper() for c in (r.coins if r.coins is not None else [])}
        if not ASSETS & (coins | set(detect_assets(headline))):
            continue
        ts = pd.Timestamp(r.published_at)
        ts = ts.tz_localize(UTC) if ts.tzinfo is None else ts.tz_convert(UTC)
        yield {
            "id": f"hf:{ds.key}:{r.id}",
            "source": publisher_key(r.source),
            "channel": "hf",
            "url": normalize_url(url),
            "title": headline,
            "standfirst": None,
            "body_text": None,
            "published_at": ts.to_pydatetime(),
            "section": None,
            "tags": sorted(coins),
            "raw": {
                "dataset": ds.repo, "dataset_sha256": digest, "hf_id": str(r.id),
                "source_name": r.source, "domain": _clean(r.domain), "coins": sorted(coins),
                "news_type": _clean(r.news_type), "score": None if pd.isna(r.score) else float(r.score),
                "ingested_at": pd.Timestamp(r.ingested_at).isoformat(),
            },
            "content_hash": content_hash(headline, None, None),
        }  # fmt: skip


MAPPERS = {"maryamfakhari": rows_maryamfakhari, "katelabs": rows_katelabs}


def insert_articles(engine: Engine, rows: Sequence[dict[str, Any]]) -> int:
    """Insert unseen URLs; an article already known from another source is left as is."""
    if not rows:
        return 0
    stmt = (
        insert(Article)
        .values(list(rows))
        .on_conflict_do_nothing(index_elements=[Article.url])
        .returning(Article.id)
    )
    with engine.begin() as conn:
        return len(conn.execute(stmt).all())


def import_dataset(engine: Engine, key: str, limit: int | None = None) -> dict[str, int]:
    ds = DATASETS[key]
    path, digest = download(ds)
    df = pd.read_parquet(path)
    start = get_settings().start_date
    seen: set[str] = set()
    batch: list[dict[str, Any]] = []
    stats = {"rows": len(df), "kept": 0, "inserted": 0, "duplicate_url": 0, "before_start": 0}
    for row in MAPPERS[key](df, ds, digest):
        if row["published_at"] < start:
            stats["before_start"] += 1
            continue
        if row["url"] in seen:
            stats["duplicate_url"] += 1
            continue
        seen.add(row["url"])
        batch.append(row)
        stats["kept"] += 1
        if len(batch) >= BATCH:
            stats["inserted"] += insert_articles(engine, batch)
            batch = []
        if limit and stats["kept"] >= limit:
            break
    stats["inserted"] += insert_articles(engine, batch)
    logger.info("%s (%s, %s): %s", ds.repo, ds.licence, datetime.now(UTC).date(), stats)
    return stats


def main(argv: Sequence[str] | None = None) -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["import"])
    parser.add_argument("--dataset", choices=sorted(DATASETS), action="append")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    engine = get_engine()
    for key in args.dataset or sorted(DATASETS):
        import_dataset(engine, key, args.limit)


if __name__ == "__main__":
    main()
