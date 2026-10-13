"""Tests for market sentiment ingestion."""

import pytest

from worker import db, ingest_market_sentiment
from worker.ingest_market_sentiment import InvalidSentimentPayload


def _payload(**overrides):
    base = {
        "value": 61,
        "classification": "Greed",
        "value_at": "2026-10-11T00:00:00+00:00",
        "fetched_at": "2026-10-11T14:05:07.765797+00:00",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "missing", ["value", "classification", "value_at"], ids=["value", "class", "value_at"]
)
def test_missing_required_field_raises(missing):
    payload = _payload()
    del payload[missing]

    with pytest.raises(InvalidSentimentPayload, match=missing):
        ingest_market_sentiment.handle(payload)


@pytest.fixture
def clean_db():
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM fear_greed_index")
    yield
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM fear_greed_index")
    db.close_pool()


def _readings():
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT value, classification, value_at, fetched_at "
            "FROM fear_greed_index ORDER BY value_at"
        )
        return cur.fetchall()


@pytest.mark.integration
def test_reading_is_stored(clean_db):
    ingest_market_sentiment.handle(_payload())

    rows = _readings()
    assert len(rows) == 1
    value, classification, value_at, fetched_at = rows[0]
    assert value == 61
    assert classification == "Greed"
    assert value_at.isoformat() == "2026-10-11T00:00:00+00:00"
    assert fetched_at is not None


@pytest.mark.integration
def test_repeated_poll_of_the_same_reading_stores_one_row(clean_db):
    """The reason value_at is UNIQUE.

    The index publishes daily, the collector polls every six hours, so each
    reading arrives up to four times. Without deduplication the table would
    hold four copies of every day and the correlation analysis would have to
    undo that before it could compute anything.
    """
    for _ in range(4):
        ingest_market_sentiment.handle(_payload())

    assert len(_readings()) == 1


@pytest.mark.integration
def test_first_fetched_at_is_preserved_on_repeat(clean_db):
    """DO NOTHING, not DO UPDATE — so "when first seen" stays recoverable."""
    ingest_market_sentiment.handle(_payload(fetched_at="2026-10-11T06:00:00+00:00"))
    ingest_market_sentiment.handle(_payload(fetched_at="2026-10-11T18:00:00+00:00"))

    (row,) = _readings()
    assert row[3].isoformat() == "2026-10-11T06:00:00+00:00"


@pytest.mark.integration
def test_different_days_are_separate_rows(clean_db):
    ingest_market_sentiment.handle(_payload(value_at="2026-10-10T00:00:00+00:00", value=64))
    ingest_market_sentiment.handle(_payload(value_at="2026-10-11T00:00:00+00:00", value=61))

    rows = _readings()
    assert [row[0] for row in rows] == [64, 61]


@pytest.mark.integration
@pytest.mark.parametrize("value", [101, -1], ids=["above", "below"])
def test_out_of_range_value_is_rejected_by_the_database(clean_db, value):
    """fear_greed_value_range is the last line of defence; the client drops these first."""
    with pytest.raises(Exception, match="fear_greed_value_range"):
        ingest_market_sentiment.handle(_payload(value=value))

    assert _readings() == []


@pytest.mark.integration
@pytest.mark.parametrize("value", [0, 100], ids=["floor", "ceiling"])
def test_boundary_values_are_accepted(clean_db, value):
    ingest_market_sentiment.handle(_payload(value=value))
    assert _readings()[0][0] == value
