"""Binance market data: hourly spot candles and perpetual funding rates.

History comes from the monthly archives on data.binance.vision (SHA-256
verified); the current month and hourly updates come from the public market
data API. Only CLOSED candles are stored.

Usage:
    python -m crypto_rag.ingestion.binance backfill
    python -m crypto_rag.ingestion.binance update
    python -m crypto_rag.ingestion.binance report
"""

import argparse
import csv
import hashlib
import io
import json
import logging
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import Engine, func, select, text
from sqlalchemy.dialects.postgresql import insert

from crypto_rag.config import get_settings, setup_logging
from crypto_rag.db.models import Candle, FundingRate
from crypto_rag.db.session import get_engine
from crypto_rag.ingestion.http import PoliteClient

logger = logging.getLogger(__name__)

ARCHIVE_BASE = "https://data.binance.vision/data/spot/monthly/klines"
KLINES_API = "https://data-api.binance.vision/api/v3/klines"
FUNDING_API = "https://fapi.binance.com/fapi/v1/fundingRate"
INTERVAL = "1h"
HOUR = timedelta(hours=1)
API_LIMIT = 1000


@dataclass(frozen=True)
class Kline:
    symbol: str
    ts: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trades: int
    taker_buy_volume: Decimal
    taker_buy_quote_volume: Decimal

    def row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ts": self.ts,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "quote_volume": self.quote_volume,
            "trades": self.trades,
            "taker_buy_volume": self.taker_buy_volume,
            "taker_buy_quote_volume": self.taker_buy_quote_volume,
        }


def epoch_to_dt(value: int | str) -> datetime:
    """Binance epoch → aware UTC datetime.

    Spot archives switched to MICROSECONDS from 2025-01-01; the API still
    returns milliseconds. Anything above 1e14 cannot be a millisecond
    timestamp before the year 5000, so the unit is inferred from magnitude.
    """
    n = int(value)
    seconds = n / 1_000_000 if n > 10**14 else n / 1_000
    return datetime.fromtimestamp(seconds, tz=UTC)


def parse_kline(symbol: str, fields: Sequence[Any]) -> Kline:
    return Kline(
        symbol=symbol,
        ts=epoch_to_dt(fields[0]),
        open=Decimal(str(fields[1])),
        high=Decimal(str(fields[2])),
        low=Decimal(str(fields[3])),
        close=Decimal(str(fields[4])),
        volume=Decimal(str(fields[5])),
        close_time=epoch_to_dt(fields[6]),
        quote_volume=Decimal(str(fields[7])),
        trades=int(fields[8]),
        taker_buy_volume=Decimal(str(fields[9])),
        taker_buy_quote_volume=Decimal(str(fields[10])),
    )


def closed_only(klines: Iterable[Kline], now: datetime) -> list[Kline]:
    """Drop the candle that is still forming (close_time >= now)."""
    return [k for k in klines if k.close_time < now]


def parse_archive_csv(symbol: str, content: bytes) -> list[Kline]:
    rows = csv.reader(io.StringIO(content.decode()))
    # Some older archives carry a header row; data rows start with a digit.
    return [parse_kline(symbol, r) for r in rows if r and r[0].isdigit()]


def verify_checksum(payload: bytes, checksum_file: str) -> None:
    expected = checksum_file.split()[0].strip().lower()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ValueError(f"checksum mismatch: expected {expected}, got {actual}")


def _months(start: date, end_exclusive: date) -> list[date]:
    out, cur = [], start.replace(day=1)
    while cur < end_exclusive:
        out.append(cur)
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def fetch_month_archive(client: PoliteClient, symbol: str, month: date) -> list[Kline]:
    name = f"{symbol}-{INTERVAL}-{month:%Y-%m}.zip"
    url = f"{ARCHIVE_BASE}/{symbol}/{INTERVAL}/{name}"
    payload = client.get(url).content
    verify_checksum(payload, client.get(url + ".CHECKSUM").text)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        content = zf.read(zf.namelist()[0])
    klines = parse_archive_csv(symbol, content)
    logger.info(
        "%s %s: %d candles from archive (checksum ok)", symbol, f"{month:%Y-%m}", len(klines)
    )
    return klines


def fetch_api_klines(
    client: PoliteClient, symbol: str, start: datetime, now: datetime
) -> list[Kline]:
    """Paginate the klines endpoint from `start` up to `now`, closed candles only."""
    out: list[Kline] = []
    cursor = int(start.timestamp() * 1000)
    while True:
        params = {"symbol": symbol, "interval": INTERVAL, "startTime": cursor, "limit": API_LIMIT}
        batch = [parse_kline(symbol, r) for r in client.get(KLINES_API, params=params).json()]
        out.extend(closed_only(batch, now))
        if len(batch) < API_LIMIT:
            break
        cursor = int((batch[-1].ts + HOUR).timestamp() * 1000)
    return out


def upsert_candles(engine: Engine, klines: Sequence[Kline]) -> int:
    if not klines:
        return 0
    rows = [k.row() for k in klines]
    stmt = insert(Candle)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Candle.symbol, Candle.ts],
        set_={c: stmt.excluded[c] for c in rows[0] if c not in ("symbol", "ts")},
    )
    with engine.begin() as conn:
        for i in range(0, len(rows), 5000):
            conn.execute(stmt, rows[i : i + 5000])
    return len(rows)


def fetch_funding(client: PoliteClient, symbol: str, start: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor = int(start.timestamp() * 1000)
    while True:
        params = {"symbol": symbol, "startTime": cursor, "limit": API_LIMIT}
        batch = client.get(FUNDING_API, params=params).json()
        out.extend(
            {
                "symbol": symbol,
                "ts": epoch_to_dt(r["fundingTime"]),
                "rate": Decimal(r["fundingRate"]),
            }
            for r in batch
        )
        if len(batch) < API_LIMIT:
            break
        cursor = int(batch[-1]["fundingTime"]) + 1
    return out


def upsert_funding(engine: Engine, rows: Sequence[dict[str, Any]]) -> int:
    if not rows:
        return 0
    stmt = insert(FundingRate)
    stmt = stmt.on_conflict_do_update(
        index_elements=[FundingRate.symbol, FundingRate.ts], set_={"rate": stmt.excluded.rate}
    )
    with engine.begin() as conn:
        conn.execute(stmt, list(rows))
    return len(rows)


def backfill(engine: Engine, client: PoliteClient, now: datetime | None = None) -> None:
    settings = get_settings()
    now = now or datetime.now(UTC)
    start = settings.start_date
    current_month = now.date().replace(day=1)
    for symbol in settings.symbols:
        for month in _months(start.date(), current_month):
            klines = [k for k in fetch_month_archive(client, symbol, month) if k.ts >= start]
            upsert_candles(engine, closed_only(klines, now))
        recent = fetch_api_klines(
            client, symbol, datetime.combine(current_month, datetime.min.time(), UTC), now
        )
        logger.info("%s: %d candles from API for the current month", symbol, len(recent))
        upsert_candles(engine, recent)
        funding = fetch_funding(client, symbol, start)
        logger.info("%s: %d funding records", symbol, upsert_funding(engine, funding))


def last_candle_ts(engine: Engine, symbol: str) -> datetime | None:
    with engine.connect() as conn:
        return conn.execute(select(func.max(Candle.ts)).where(Candle.symbol == symbol)).scalar()


def last_funding_ts(engine: Engine, symbol: str) -> datetime | None:
    with engine.connect() as conn:
        return conn.execute(
            select(func.max(FundingRate.ts)).where(FundingRate.symbol == symbol)
        ).scalar()


def update(engine: Engine, client: PoliteClient, now: datetime | None = None) -> None:
    """Fetch candles and funding newer than what is stored (hourly job)."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    with engine.connect() as conn:
        for symbol in settings.symbols:
            last = conn.execute(select(func.max(Candle.ts)).where(Candle.symbol == symbol)).scalar()
            start = (last + HOUR) if last else settings.start_date
            n = upsert_candles(engine, fetch_api_klines(client, symbol, start, now))
            last_f = conn.execute(
                select(func.max(FundingRate.ts)).where(FundingRate.symbol == symbol)
            ).scalar()
            f_start = (last_f + timedelta(milliseconds=1)) if last_f else settings.start_date
            try:
                nf = upsert_funding(engine, fetch_funding(client, symbol, f_start))
            except httpx.HTTPError:
                # fapi.binance.com is blocked on some networks; candles must still update.
                logger.exception("%s: funding update failed; retried on the next run", symbol)
                nf = 0
            logger.info("%s: +%d candles, +%d funding", symbol, n, nf)


QUALITY_SQL = """
WITH hours AS (
    SELECT s.symbol, g.ts
    FROM unnest(CAST(:symbols AS text[])) AS s(symbol)
    CROSS JOIN generate_series(CAST(:start AS timestamptz), CAST(:end AS timestamptz), interval '1 hour') AS g(ts)
)
SELECT h.symbol, h.ts
FROM hours h
LEFT JOIN candles c ON c.symbol = h.symbol AND c.ts = h.ts
WHERE c.ts IS NULL
ORDER BY h.symbol, h.ts
"""


def quality_report(engine: Engine, now: datetime | None = None) -> dict[str, Any]:
    """Report gaps, zero volumes and per-day counts. Reports only; never 'fixes'."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    last_closed = now.replace(minute=0, second=0, microsecond=0) - HOUR
    report: dict[str, Any] = {"generated_at": now.isoformat(), "symbols": {}}
    with engine.connect() as conn:
        missing = conn.execute(
            text(QUALITY_SQL),
            {"symbols": settings.symbols, "start": settings.start_date, "end": last_closed},
        ).all()
        for symbol in settings.symbols:
            stats = conn.execute(
                text(
                    "SELECT count(*), min(ts), max(ts), "
                    "count(*) FILTER (WHERE volume = 0) FROM candles WHERE symbol = :s"
                ),
                {"s": symbol},
            ).one()
            days_short = conn.execute(
                text(
                    "SELECT d::date, n FROM (SELECT date_trunc('day', ts) d, count(*) n "
                    "FROM candles WHERE symbol = :s GROUP BY 1) x WHERE n <> 24 AND d < :today "
                    "ORDER BY 1"
                ),
                {"s": symbol, "today": last_closed.replace(hour=0)},
            ).all()
            funding = conn.execute(
                text(
                    "SELECT count(*), count(DISTINCT date_trunc('day', ts)) "
                    "FROM funding_rates WHERE symbol = :s"
                ),
                {"s": symbol},
            ).one()
            gaps = [m.ts.isoformat() for m in missing if m.symbol == symbol]
            report["symbols"][symbol] = {
                "candles": stats[0],
                "first": stats[1].isoformat() if stats[1] else None,
                "last": stats[2].isoformat() if stats[2] else None,
                "missing_hours": len(gaps),
                "missing_hours_list": gaps[:200],
                "zero_volume_hours": stats[3],
                "days_not_24": [{"day": d.isoformat(), "count": n} for d, n in days_short],
                "funding_records": funding[0],
                "funding_per_day": round(funding[0] / funding[1], 2) if funding[1] else None,
            }
    return report


def main(argv: Sequence[str] | None = None) -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["backfill", "update", "report"])
    args = parser.parse_args(argv)
    engine = get_engine()
    if args.command == "report":
        logger.info("quality report:\n%s", json.dumps(quality_report(engine), indent=2))
        return
    with PoliteClient(min_interval=0.25) as client:
        (backfill if args.command == "backfill" else update)(engine, client)
    logger.info("quality report:\n%s", json.dumps(quality_report(engine), indent=2)[:4000])


if __name__ == "__main__":
    main()
