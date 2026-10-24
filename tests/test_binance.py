from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from crypto_rag.db.models import Candle
from crypto_rag.ingestion import binance
from tests.conftest import FIXTURES

# Two API rows (milliseconds): 2026-10-21 18:00 and 19:00 UTC.
API_ROWS = [
    [1792605600000, "84680.56", "84876.01", "84400.00", "84416.47", "736.66", 1792609199999,
     "62350216.82", 114124, "330.86", "27997384.53", "0"],
    [1792609200000, "84416.47", "84549.99", "84180.09", "84549.98", "746.46", 1792612799999,
     "62932303.22", 124178, "261.67", "22065119.99", "0"],
]  # fmt: skip


def test_epoch_units_are_inferred():
    expected = datetime(2025, 1, 1, tzinfo=UTC)
    assert binance.epoch_to_dt(1735689600000) == expected  # milliseconds (API)
    assert binance.epoch_to_dt(1735689600000000) == expected  # microseconds (archives 2025+)


def test_archive_csv_parses_microsecond_rows():
    klines = binance.parse_archive_csv(
        "BTCUSDT", (FIXTURES / "BTCUSDT-1h-2025-01-head.csv").read_bytes()
    )
    assert len(klines) == 30
    assert klines[0].ts == datetime(2025, 1, 1, tzinfo=UTC)
    assert klines[1].ts - klines[0].ts == timedelta(hours=1)
    assert klines[0].close == Decimal("94401.14000000")
    assert klines[0].trades == 93525


def test_unclosed_candle_is_dropped():
    klines = [binance.parse_kline("BTCUSDT", r) for r in API_ROWS]
    # 19:30 — the 19:00 candle is still forming.
    now = datetime(2026, 10, 21, 19, 30, tzinfo=UTC)
    kept = binance.closed_only(klines, now)
    assert [k.ts.hour for k in kept] == [18]


def test_checksum_mismatch_raises():
    with pytest.raises(ValueError, match="checksum"):
        binance.verify_checksum(b"payload", "00" * 32 + "  file.zip")


def test_month_list_spans_period():
    months = binance._months(date(2025, 1, 20), date(2025, 4, 1))
    assert [m.month for m in months] == [1, 2, 3]


@pytest.mark.integration
def test_repeated_upsert_does_not_duplicate(engine, db):
    klines = [binance.parse_kline("TESTUSDT", r) for r in API_ROWS]
    binance.upsert_candles(engine, klines)
    binance.upsert_candles(engine, klines)
    with engine.connect() as conn:
        n = conn.execute(
            select(func.count()).select_from(Candle).where(Candle.symbol == "TESTUSDT")
        ).scalar_one()
    assert n == 2
