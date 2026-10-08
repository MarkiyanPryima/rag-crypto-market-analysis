"""Tests for the price collector loop.

The loop is the part that must not fall over: a collector that dies on one bad
cycle stops collecting silently, and lost collection time cannot be recovered.
"""

import pytest

from collectors import price_collector


class FakeRedis:
    """Records pushes instead of talking to Redis."""

    def __init__(self):
        self.pushed: list[tuple[str, str]] = []

    def rpush(self, queue, body):
        self.pushed.append((queue, body))
        return len(self.pushed)


@pytest.fixture
def client():
    return FakeRedis()


@pytest.fixture
def prices():
    return [
        {"symbol": "BTC", "slug": "bitcoin", "price_usd": 83750.0},
        {"symbol": "ETH", "slug": "ethereum", "price_usd": 2580.0},
    ]


def test_cycle_queues_one_message_per_coin(monkeypatch, client, prices):
    """One entry per coin, so the worker can fail one without losing the batch."""
    monkeypatch.setattr(price_collector, "fetch_tracked_coin_ids", lambda: ["bitcoin", "ethereum"])
    monkeypatch.setattr(price_collector, "fetch_prices", lambda _ids: prices)

    assert price_collector.run_cycle(client) == 2
    assert len(client.pushed) == 2
    assert {queue for queue, _ in client.pushed} == {"prices_queue"}


def test_cycle_passes_database_slugs_to_the_client(monkeypatch, client, prices):
    """The tracked list comes from tokens.slug, not a hardcoded constant."""
    seen = {}
    monkeypatch.setattr(price_collector, "fetch_tracked_coin_ids", lambda: ["solana", "cardano"])

    def capture(coin_ids):
        seen["ids"] = coin_ids
        return prices

    monkeypatch.setattr(price_collector, "fetch_prices", capture)
    price_collector.run_cycle(client)

    assert seen["ids"] == ["solana", "cardano"]


def test_empty_token_table_pushes_nothing(monkeypatch, client):
    """A missing seed is a setup error; do not report a successful empty cycle."""
    monkeypatch.setattr(price_collector, "fetch_tracked_coin_ids", lambda: [])

    def explode(_ids):
        raise AssertionError("should not call CoinGecko with no coins")

    monkeypatch.setattr(price_collector, "fetch_prices", explode)

    assert price_collector.run_cycle(client) == 0
    assert client.pushed == []


def test_no_usable_prices_pushes_nothing(monkeypatch, client):
    """Every coin dropped by validation means nothing to queue."""
    monkeypatch.setattr(price_collector, "fetch_tracked_coin_ids", lambda: ["bitcoin"])
    monkeypatch.setattr(price_collector, "fetch_prices", lambda _ids: [])

    assert price_collector.run_cycle(client) == 0
    assert client.pushed == []


@pytest.mark.parametrize(
    "failing",
    ["fetch_tracked_coin_ids", "fetch_prices"],
    ids=["database-down", "coingecko-down"],
)
def test_run_once_survives_a_failing_cycle(monkeypatch, failing):
    """main() must exit cleanly, not propagate — error isolation per cycle."""
    monkeypatch.setattr(price_collector, "RUN_ONCE", True)
    monkeypatch.setattr(price_collector.redis_queue, "get_client", lambda: FakeRedis())

    # Stub BOTH dependencies first, then break one. Patching only the failing
    # dependency leaves the other reaching the real Postgres or CoinGecko, which
    # makes the test slow, network-dependent and no longer a unit test.
    monkeypatch.setattr(price_collector, "fetch_tracked_coin_ids", lambda: ["bitcoin"])
    monkeypatch.setattr(price_collector, "fetch_prices", lambda _ids: [])

    def explode(*_args):
        raise ConnectionError("simulated outage")

    monkeypatch.setattr(price_collector, failing, explode)

    # Must return rather than raise: the real loop would carry on to the next
    # interval, and crashing would end collection entirely.
    price_collector.main()


def test_run_once_exits_after_one_cycle(monkeypatch, prices):
    monkeypatch.setattr(price_collector, "RUN_ONCE", True)
    monkeypatch.setattr(price_collector.redis_queue, "get_client", lambda: FakeRedis())
    monkeypatch.setattr(price_collector, "fetch_tracked_coin_ids", lambda: ["bitcoin"])

    calls = []

    def once(ids):
        calls.append(ids)
        return prices

    monkeypatch.setattr(price_collector, "fetch_prices", once)

    def no_sleep(_seconds):
        raise AssertionError("RUN_ONCE must not sleep")

    monkeypatch.setattr(price_collector.time, "sleep", no_sleep)

    price_collector.main()
    assert len(calls) == 1
