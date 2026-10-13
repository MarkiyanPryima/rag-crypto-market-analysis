"""Tests for price ingestion."""

import datetime

import pytest

from worker import db, ingest_prices, tokens
from worker.ingest_prices import InvalidPricePayload


def _payload(**overrides):
    base = {
        "symbol": "BTC",
        "slug": "bitcoin",
        "price_usd": 83750.5,
        "market_cap": 1668295111273,
        "volume_24h": 33906422896,
        "change_24h": -0.95239,
        "scraped_at": "2026-10-11T10:53:31.366226+00:00",
        "source": "coingecko",
    }
    base.update(overrides)
    return base


# --- payload validation (no database) --------------------------------------


@pytest.mark.parametrize(
    "missing",
    ["symbol", "price_usd", "scraped_at"],
    ids=["symbol", "price", "scraped_at"],
)
def test_missing_required_field_raises(missing):
    """Raising lets the consumer log and drop the message rather than guess."""
    payload = _payload()
    del payload[missing]

    with pytest.raises(InvalidPricePayload, match=missing):
        ingest_prices.handle(payload)


def test_null_required_field_raises():
    with pytest.raises(InvalidPricePayload, match="price_usd"):
        ingest_prices.handle(_payload(price_usd=None))


# --- integration -----------------------------------------------------------


@pytest.fixture
def clean_db():
    """Start from no snapshots, and leave none behind."""
    tokens.clear_cache()
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM price_snapshots")
    yield
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM price_snapshots")
    tokens.clear_cache()
    db.close_pool()


def _snapshots(symbol="BTC"):
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT p.price_usd, p.market_cap, p.volume_24h, p.change_24h,
                      p.scraped_at, p.source
               FROM price_snapshots p JOIN tokens t ON t.id = p.token_id
               WHERE t.symbol = %s ORDER BY p.scraped_at""",
            (symbol,),
        )
        return cur.fetchall()


@pytest.mark.integration
def test_snapshot_is_stored_with_every_column(clean_db):
    ingest_prices.handle(_payload())

    rows = _snapshots()
    assert len(rows) == 1
    price, cap, volume, change, scraped_at, source = rows[0]
    assert float(price) == 83750.5
    assert int(cap) == 1668295111273
    assert int(volume) == 33906422896
    assert source == "coingecko"
    assert scraped_at.isoformat() == "2026-10-11T10:53:31.366226+00:00"
    # change_24h is NUMERIC(8,4): four decimal places, so -0.95239 from
    # CoinGecko is stored as -0.9524. Harmless for the correlation analysis
    # (0.0001 percentage points), but worth pinning so the rounding is a
    # documented property rather than a surprise.
    assert float(change) == pytest.approx(-0.9524, abs=1e-9)


@pytest.mark.integration
def test_change_is_rounded_to_four_decimals(clean_db):
    """NUMERIC(8,4) truncates CoinGecko's five-decimal percentages."""
    ingest_prices.handle(_payload(change_24h=-2.77225))

    (row,) = _snapshots()
    assert float(row[3]) == pytest.approx(-2.7723, abs=1e-9), (
        "expected the database to round to 4 decimal places"
    )


@pytest.mark.integration
def test_observation_time_comes_from_the_payload(clean_db):
    """The whole reason collectors stamp scraped_at themselves.

    A message sitting in the queue for two days must still record when it was
    observed, not when it was drained. Using the column default would flatten
    a backlog into a single instant and destroy the price history.
    """
    observed = "2026-10-09T01:02:03+00:00"
    ingest_prices.handle(_payload(scraped_at=observed))

    (row,) = _snapshots()
    stored = row[4]
    assert stored.isoformat() == observed

    drained_now = datetime.datetime.now(datetime.UTC)
    assert (drained_now - stored).total_seconds() > 3600, (
        "stored time is suspiciously close to now — the column default was used"
    )


@pytest.mark.integration
def test_untracked_symbol_is_skipped_not_raised(clean_db):
    """A coin we do not track is a config difference, not a bad message."""
    ingest_prices.handle(_payload(symbol="NOTACOIN"))

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM price_snapshots")
        assert cur.fetchone() == (0,)


@pytest.mark.integration
def test_lowercase_symbol_still_resolves(clean_db):
    """tokens.symbol has CHECK (symbol = upper(symbol)); lookup must uppercase."""
    ingest_prices.handle(_payload(symbol="btc"))
    assert len(_snapshots()) == 1


@pytest.mark.integration
def test_optional_fields_may_be_null(clean_db):
    ingest_prices.handle(_payload(market_cap=None, volume_24h=None, change_24h=None))

    (row,) = _snapshots()
    assert row[1] is None and row[2] is None and row[3] is None


@pytest.mark.integration
def test_non_positive_price_is_rejected_by_the_database(clean_db):
    """price_snapshots_price_positive is the last line of defence.

    The collector already drops these at the edge, but if one reaches here the
    constraint must stop it rather than storing a zero that would produce an
    infinite forward return in the correlation analysis.
    """
    with pytest.raises(Exception, match="price_positive"):
        ingest_prices.handle(_payload(price_usd=0))

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM price_snapshots")
        assert cur.fetchone() == (0,), "rollback did not happen"


@pytest.mark.integration
def test_repeated_observations_all_stored(clean_db):
    """price_snapshots is append-only: the same coin accumulates a time series."""
    for minute in range(3):
        ingest_prices.handle(_payload(scraped_at=f"2026-10-11T10:5{minute}:00+00:00"))

    assert len(_snapshots()) == 3


@pytest.mark.integration
def test_token_lookup_is_cached(clean_db):
    """Thousands of queued messages cover eight coins; one query each is enough."""
    tokens.clear_cache()
    assert tokens.cache_size() == 0

    for _ in range(5):
        ingest_prices.handle(_payload())

    assert tokens.cache_size() == 1, "symbol was looked up more than once"


@pytest.mark.integration
def test_unknown_symbol_is_not_cached(clean_db):
    """Negative results stay uncached so a newly added token is picked up."""
    tokens.clear_cache()
    ingest_prices.handle(_payload(symbol="NOTACOIN"))
    assert tokens.cache_size() == 0
