"""Tests for the Fear & Greed client and collector.

The fixture is a real response recorded 11 Oct 2026. It is what revealed that
`value` and `timestamp` arrive as strings rather than numbers — the design doc's
code calls int(data["value"]) and happens to be right, but assumes the key
`value_classification` and ignores `metadata.error` entirely.
"""

import json
import pathlib
from datetime import datetime

import pytest

from collectors import fear_greed, sentiment_collector

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "fear_greed.json"


@pytest.fixture
def payload():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def entry(payload):
    return payload["data"][0]


class FakeRedis:
    def __init__(self):
        self.pushed: list[tuple[str, str]] = []

    def rpush(self, queue, body):
        self.pushed.append((queue, body))
        return len(self.pushed)


def _response(body, status=200):
    class Response:
        status_code = status

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return body

    return Response()


# --- normalise -------------------------------------------------------------


def test_normalise_maps_the_real_payload(entry):
    result = fear_greed.normalise(entry)

    assert result["value"] == 61
    assert result["classification"] == "Greed"
    assert result["value_at"] == "2026-10-11T00:00:00+00:00"
    assert result["fetched_at"]


def test_string_value_becomes_an_integer(entry):
    """The API sends "61", not 61. The column is INTEGER."""
    assert isinstance(entry["value"], str)
    assert isinstance(fear_greed.normalise(entry)["value"], int)


def test_value_at_is_the_readings_own_time_not_now(entry):
    """value_at is UNIQUE and drives dedupe of the 6-hourly repeats.

    Using fetch time instead would make every poll look like a new reading and
    store four copies of each day.
    """
    result = fear_greed.normalise(entry)
    value_at = datetime.fromisoformat(result["value_at"])
    fetched_at = datetime.fromisoformat(result["fetched_at"])

    assert value_at.tzinfo is not None
    assert value_at < fetched_at
    assert value_at.hour == 0, "readings publish at midnight UTC"


def test_repeated_polls_yield_the_same_value_at(entry):
    """Two polls of an unchanged reading must agree, or dedupe cannot work.

    This is the property the worker's ON CONFLICT (value_at) DO NOTHING relies
    on: polling 6-hourly sees each daily reading up to four times, and all four
    must collide on the same key.
    """
    first = fear_greed.normalise(entry)
    second = fear_greed.normalise(entry)

    assert first["value_at"] == second["value_at"]


@pytest.mark.parametrize(
    "value",
    ["101", "-1", "1000"],
    ids=["above-range", "below-range", "far-above"],
)
def test_out_of_range_value_is_dropped(entry, value):
    """The column carries CHECK (value BETWEEN 0 AND 100)."""
    assert fear_greed.normalise(dict(entry, value=value)) is None


@pytest.mark.parametrize(
    "value",
    ["", "sixty", "61.5", None],
    ids=["empty", "words", "float-string", "null"],
)
def test_non_integer_value_is_dropped(entry, value):
    assert fear_greed.normalise(dict(entry, value=value)) is None


@pytest.mark.parametrize("value", ["0", "100"], ids=["floor", "ceiling"])
def test_boundary_values_are_accepted(entry, value):
    """0 and 100 are valid — the CHECK is inclusive."""
    result = fear_greed.normalise(dict(entry, value=value))
    assert result is not None
    assert result["value"] == int(value)


@pytest.mark.parametrize("classification", ["", "   ", None], ids=["empty", "blank", "null"])
def test_missing_classification_is_dropped(entry, classification):
    assert fear_greed.normalise(dict(entry, value_classification=classification)) is None


def test_overlong_classification_is_dropped(entry):
    """classification is VARCHAR(20); a longer label must fail here, not in the worker."""
    assert fear_greed.normalise(dict(entry, value_classification="x" * 21)) is None


@pytest.mark.parametrize(
    "timestamp",
    ["", "not-a-number", None],
    ids=["empty", "words", "null"],
)
def test_bad_timestamp_is_dropped(entry, timestamp):
    assert fear_greed.normalise(dict(entry, timestamp=timestamp)) is None


def test_documented_classifications_all_fit(entry):
    for label in ("Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"):
        result = fear_greed.normalise(dict(entry, value_classification=label))
        assert result is not None, f"{label!r} rejected"
        assert result["classification"] == label


# --- fetch_raw -------------------------------------------------------------


def test_body_level_error_raises(monkeypatch):
    """An API error arrives with HTTP 200, so the status code is not enough."""
    body = {"data": [], "metadata": {"error": "rate limit exceeded"}}
    monkeypatch.setattr(fear_greed.requests, "get", lambda *_a, **_k: _response(body))

    with pytest.raises(fear_greed.FearGreedError, match="rate limit exceeded"):
        fear_greed.fetch_raw()


def test_empty_data_raises(monkeypatch):
    body = {"data": [], "metadata": {"error": None}}
    monkeypatch.setattr(fear_greed.requests, "get", lambda *_a, **_k: _response(body))

    with pytest.raises(fear_greed.FearGreedError, match="no data"):
        fear_greed.fetch_raw()


def test_non_object_payload_raises(monkeypatch):
    monkeypatch.setattr(fear_greed.requests, "get", lambda *_a, **_k: _response([1, 2, 3]))

    with pytest.raises(fear_greed.FearGreedError, match="expected a JSON object"):
        fear_greed.fetch_raw()


def test_fetch_current_returns_none_on_unusable_reading(monkeypatch, entry):
    monkeypatch.setattr(fear_greed, "fetch_raw", lambda limit=1: [dict(entry, value="999")])
    assert fear_greed.fetch_current() is None


# --- collector loop --------------------------------------------------------


def test_cycle_queues_one_reading(monkeypatch, entry):
    client = FakeRedis()
    monkeypatch.setattr(sentiment_collector, "fetch_current", lambda: fear_greed.normalise(entry))

    assert sentiment_collector.run_cycle(client) == 1
    assert len(client.pushed) == 1
    assert client.pushed[0][0] == "sentiment_queue"


def test_cycle_queues_nothing_when_unusable(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(sentiment_collector, "fetch_current", lambda: None)

    assert sentiment_collector.run_cycle(client) == 0
    assert client.pushed == []


def test_run_once_survives_a_failing_cycle(monkeypatch):
    """Nothing depends on this being fresh; degrade by logging and retrying."""
    monkeypatch.setattr(sentiment_collector, "RUN_ONCE", True)
    monkeypatch.setattr(sentiment_collector.redis_queue, "get_client", lambda: FakeRedis())

    def explode():
        raise ConnectionError("alternative.me unreachable")

    monkeypatch.setattr(sentiment_collector, "fetch_current", explode)
    sentiment_collector.main()


def test_run_once_does_not_sleep(monkeypatch, entry):
    monkeypatch.setattr(sentiment_collector, "RUN_ONCE", True)
    monkeypatch.setattr(sentiment_collector.redis_queue, "get_client", lambda: FakeRedis())
    monkeypatch.setattr(sentiment_collector, "fetch_current", lambda: fear_greed.normalise(entry))

    def no_sleep(_seconds):
        raise AssertionError("RUN_ONCE must not sleep")

    monkeypatch.setattr(sentiment_collector.time, "sleep", no_sleep)
    sentiment_collector.main()
