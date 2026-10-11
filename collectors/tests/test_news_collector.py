"""Tests for the news collector loop."""

import pytest

from collectors import news_collector


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
def articles():
    return [
        {"title": "Bitcoin rallies", "url": "https://x.test/1", "symbol": "BTC"},
        {"title": "Markets wobble", "url": "https://x.test/2", "symbol": None},
        {"title": "Solana outage", "url": "https://x.test/3", "symbol": "SOL"},
    ]


def _stub(monkeypatch, *, aliases=None, articles=None):
    """Stub both dependencies so no test reaches Postgres or the network."""
    monkeypatch.setattr(
        news_collector, "fetch_symbol_aliases", lambda: aliases if aliases is not None else {}
    )
    monkeypatch.setattr(
        news_collector.rss, "fetch_articles", lambda _a: articles if articles is not None else []
    )


def test_cycle_queues_one_message_per_article(monkeypatch, client, articles):
    """Per-article messages, not one batched blob.

    The reference pushes the whole batch as a single JSON list, so one
    unparseable story would discard every article in the cycle.
    """
    _stub(monkeypatch, aliases={"bitcoin": "BTC"}, articles=articles)

    assert news_collector.run_cycle(client) == 3
    assert len(client.pushed) == 3
    assert {queue for queue, _ in client.pushed} == {"news_queue"}


def test_untagged_articles_are_still_queued(monkeypatch, client, articles):
    """58% of articles carry no symbol; they remain valid market-wide news."""
    _stub(monkeypatch, aliases={"bitcoin": "BTC"}, articles=articles)
    news_collector.run_cycle(client)

    bodies = [body for _queue, body in client.pushed]
    assert any("Markets wobble" in body for body in bodies)


def test_aliases_are_passed_to_the_fetcher(monkeypatch, client, articles):
    """Aliases come from the database, merged with the curated extras."""
    seen = {}

    def capture(aliases):
        seen["aliases"] = aliases
        return articles

    monkeypatch.setattr(news_collector, "fetch_symbol_aliases", lambda: {"bitcoin": "BTC"})
    monkeypatch.setattr(news_collector.rss, "fetch_articles", capture)

    news_collector.run_cycle(client)

    assert seen["aliases"]["bitcoin"] == "BTC"
    assert seen["aliases"]["ripple"] == "XRP", "curated extras should be merged in"


def test_empty_alias_table_pushes_nothing(monkeypatch, client):
    """A missing seed is a setup error, not an empty-but-successful cycle."""

    def explode(_aliases):
        raise AssertionError("should not fetch feeds with no aliases")

    monkeypatch.setattr(news_collector, "fetch_symbol_aliases", lambda: {})
    monkeypatch.setattr(news_collector.rss, "fetch_articles", explode)

    assert news_collector.run_cycle(client) == 0
    assert client.pushed == []


def test_no_articles_pushes_nothing(monkeypatch, client):
    _stub(monkeypatch, aliases={"bitcoin": "BTC"}, articles=[])

    assert news_collector.run_cycle(client) == 0
    assert client.pushed == []


@pytest.mark.parametrize(
    "failing",
    ["fetch_symbol_aliases", "rss"],
    ids=["database-down", "feeds-down"],
)
def test_run_once_survives_a_failing_cycle(monkeypatch, failing, articles):
    """main() must return, not raise — a crash ends collection silently."""
    monkeypatch.setattr(news_collector, "RUN_ONCE", True)
    monkeypatch.setattr(news_collector.redis_queue, "get_client", lambda: FakeRedis())
    _stub(monkeypatch, aliases={"bitcoin": "BTC"}, articles=articles)

    def explode(*_args):
        raise ConnectionError("simulated outage")

    if failing == "rss":
        monkeypatch.setattr(news_collector.rss, "fetch_articles", explode)
    else:
        monkeypatch.setattr(news_collector, failing, explode)

    news_collector.main()


def test_run_once_does_not_sleep(monkeypatch, articles):
    monkeypatch.setattr(news_collector, "RUN_ONCE", True)
    monkeypatch.setattr(news_collector.redis_queue, "get_client", lambda: FakeRedis())
    _stub(monkeypatch, aliases={"bitcoin": "BTC"}, articles=articles)

    def no_sleep(_seconds):
        raise AssertionError("RUN_ONCE must not sleep")

    monkeypatch.setattr(news_collector.time, "sleep", no_sleep)
    news_collector.main()
