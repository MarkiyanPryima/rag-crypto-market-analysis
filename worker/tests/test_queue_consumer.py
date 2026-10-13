"""Tests for the queue consumer loop.

The loop is the part that must not fall over. A consumer that dies on one bad
message stops draining entirely, and the queues then grow without bound while
the container still reports healthy.
"""

import json
import signal

import pytest
import redis

from worker import queue_consumer
from worker.queue_consumer import Shutdown, consume


class FakeRedis:
    """Serves a scripted list of (queue, raw) tuples, then returns None."""

    def __init__(self, items):
        self.items = list(items)
        self.blpop_calls = 0

    def blpop(self, _queues, timeout=None):
        self.blpop_calls += 1
        if not self.items:
            return None
        return self.items.pop(0)


@pytest.fixture
def shutdown():
    return Shutdown()


def _handlers(record, failing=()):
    def make(queue):
        def handler(payload):
            if queue in failing:
                raise RuntimeError(f"{queue} handler exploded")
            record.append((queue, payload))

        return handler

    return {q: make(q) for q in ("prices_queue", "news_queue", "sentiment_queue")}


# --- happy path ------------------------------------------------------------


def test_message_is_dispatched_to_its_queues_handler(shutdown):
    record = []
    client = FakeRedis([("prices_queue", json.dumps({"symbol": "BTC"}))])

    counts = consume(client, _handlers(record), shutdown, max_messages=1)

    assert record == [("prices_queue", {"symbol": "BTC"})]
    assert counts["prices_queue"] == 1


def test_each_queue_reaches_its_own_handler(shutdown):
    record = []
    client = FakeRedis(
        [
            ("prices_queue", json.dumps({"a": 1})),
            ("news_queue", json.dumps({"b": 2})),
            ("sentiment_queue", json.dumps({"c": 3})),
        ]
    )

    counts = consume(client, _handlers(record), shutdown, max_messages=3)

    assert [q for q, _ in record] == ["prices_queue", "news_queue", "sentiment_queue"]
    assert counts == {"prices_queue": 1, "news_queue": 1, "sentiment_queue": 1}


def test_timeout_does_not_count_as_a_message(shutdown):
    """An idle BLPOP returns None; the loop must simply go round again."""
    shutdown.requested = False

    # max_messages=0 would skip the loop, so bound by flipping the flag after
    # one idle pass instead.
    class OneIdlePass(FakeRedis):
        def blpop(self, queues, timeout=None):
            result = super().blpop(queues, timeout)
            shutdown.requested = True
            return result

    counts = consume(OneIdlePass([]), _handlers([]), shutdown)
    assert sum(counts.values()) == 0


# --- error isolation -------------------------------------------------------


def test_failing_handler_does_not_stop_the_loop(shutdown):
    """The whole point: one bad message must not end the drain."""
    record = []
    client = FakeRedis(
        [
            ("news_queue", json.dumps({"bad": True})),
            ("prices_queue", json.dumps({"good": True})),
        ]
    )

    counts = consume(client, _handlers(record, failing=("news_queue",)), shutdown, max_messages=2)

    assert record == [("prices_queue", {"good": True})], "good message after a failure was lost"
    assert counts["news_queue"] == 0
    assert counts["prices_queue"] == 1


def test_unparseable_message_is_dropped_not_requeued(shutdown):
    """Pushing it back would spin the loop on the same payload forever."""
    record = []
    client = FakeRedis(
        [
            ("news_queue", "{not json at all"),
            ("news_queue", json.dumps({"ok": True})),
        ]
    )

    counts = consume(client, _handlers(record), shutdown, max_messages=2)

    assert record == [("news_queue", {"ok": True})]
    assert counts["news_queue"] == 1


@pytest.mark.parametrize(
    "raw",
    ["[1, 2, 3]", '"a string"', "42", "null"],
    ids=["list", "string", "number", "null"],
)
def test_non_object_message_is_dropped(shutdown, raw):
    """Handlers expect a dict; a bare list would fail deep inside one."""
    record = []
    client = FakeRedis([("prices_queue", raw)])

    counts = consume(client, _handlers(record), shutdown, max_messages=1)

    assert record == []
    assert counts["prices_queue"] == 0


# --- shutdown --------------------------------------------------------------


def test_shutdown_flag_stops_the_loop_before_the_next_message(shutdown):
    record = []
    client = FakeRedis(
        [
            ("prices_queue", json.dumps({"n": 1})),
            ("prices_queue", json.dumps({"n": 2})),
        ]
    )

    handlers = _handlers(record)
    original = handlers["prices_queue"]

    def stop_after_first(payload):
        original(payload)
        shutdown.requested = True

    handlers["prices_queue"] = stop_after_first

    consume(client, handlers, shutdown)

    assert len(record) == 1, "loop continued past a shutdown request"


def test_sigterm_sets_the_flag_rather_than_raising(shutdown):
    """Raising from a signal handler would abort a transaction mid-write."""
    shutdown.install()
    assert shutdown.requested is False

    signal.raise_signal(signal.SIGTERM)

    assert shutdown.requested is True


def test_second_signal_exits_immediately(shutdown):
    shutdown.install()
    signal.raise_signal(signal.SIGTERM)

    with pytest.raises(SystemExit):
        signal.raise_signal(signal.SIGTERM)


def test_loop_does_not_start_when_shutdown_already_requested(shutdown):
    record = []
    shutdown.requested = True
    client = FakeRedis([("prices_queue", json.dumps({"n": 1}))])

    consume(client, _handlers(record), shutdown)

    assert record == []
    assert client.blpop_calls == 0


# --- resilience ------------------------------------------------------------


def test_blpop_retries_on_connection_error(monkeypatch, shutdown):
    """A Redis restart must not end the worker."""
    attempts = {"n": 0}

    class Flaky:
        def blpop(self, _queues, timeout=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise redis.ConnectionError("redis went away")
            return ("prices_queue", json.dumps({"recovered": True}))

    # Collapse the backoff so the test does not actually wait.
    monkeypatch.setattr(queue_consumer._blpop.retry, "wait", lambda *_a, **_k: 0)

    record = []
    counts = consume(Flaky(), _handlers(record), shutdown, max_messages=1)

    assert attempts["n"] == 3
    assert record == [("prices_queue", {"recovered": True})]
    assert counts["prices_queue"] == 1


def test_socket_timeout_exceeds_blpop_timeout():
    """Otherwise the socket aborts the blocking read before the server replies."""
    client = queue_consumer.get_client()
    assert client.connection_pool.connection_kwargs["socket_timeout"] > queue_consumer.BLPOP_TIMEOUT


# --- one-shot drain --------------------------------------------------------


def test_exit_when_empty_returns_on_first_idle_timeout(shutdown):
    """A one-shot drain must end by itself once the backlog is gone."""
    record = []
    client = FakeRedis([("prices_queue", json.dumps({"n": 1}))])

    counts = consume(client, _handlers(record), shutdown, exit_when_empty=True)

    assert counts["prices_queue"] == 1
    assert len(record) == 1


def test_exit_when_empty_is_off_by_default(shutdown):
    """The long-running service must treat an empty queue as normal waiting.

    Without this distinction the service would exit the first quiet moment and
    compose would restart-loop it.
    """
    polls = {"n": 0}

    class Idle:
        def blpop(self, _queues, timeout=None):
            polls["n"] += 1
            if polls["n"] >= 3:
                shutdown.requested = True
            return

    consume(Idle(), _handlers([]), shutdown)

    assert polls["n"] >= 3, "loop exited on an idle timeout with exit_when_empty=False"


def test_max_messages_larger_than_backlog_would_hang_without_the_flag(shutdown):
    """Documents the trap this flag exists for.

    max_messages counts messages, not polls, so a limit above the backlog never
    completes on its own — exactly what stalled a real 2204-message drain.
    """
    record = []
    client = FakeRedis([("prices_queue", json.dumps({"n": 1}))])

    counts = consume(client, _handlers(record), shutdown, max_messages=5000, exit_when_empty=True)

    assert counts["prices_queue"] == 1, "drain did not finish despite an empty queue"
