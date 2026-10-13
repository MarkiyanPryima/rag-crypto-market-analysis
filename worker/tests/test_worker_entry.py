"""Tests for the worker entry point."""

import contextlib

from worker import worker


def test_refuses_to_start_without_handlers(monkeypatch, caplog):
    """The safety property: an unlistened queue keeps its data.

    BLPOP removes a message as it reads it, so starting with a placeholder
    handler that discarded payloads would destroy real collected data on the
    first run. Refusing to start is the safe default.
    """
    monkeypatch.setattr(worker, "HANDLERS", {})

    assert worker.main() == 1
    assert "no queue handlers registered" in caplog.text


def test_does_not_touch_redis_without_handlers(monkeypatch):
    def explode():
        raise AssertionError("must not connect to Redis with no handlers")

    monkeypatch.setattr(worker, "HANDLERS", {})
    monkeypatch.setattr(worker.queue_consumer, "get_client", explode)

    assert worker.main() == 1


def test_exits_nonzero_when_database_unreachable(monkeypatch, caplog):
    monkeypatch.setattr(worker, "HANDLERS", {"prices_queue": lambda _p: None})
    monkeypatch.setattr(worker.db, "check_connection", lambda: False)

    assert worker.main() == 1
    assert "database unreachable" in caplog.text


def test_closes_the_pool_even_when_consume_raises(monkeypatch):
    closed = {"n": 0}

    monkeypatch.setattr(worker, "HANDLERS", {"prices_queue": lambda _p: None})
    monkeypatch.setattr(worker.db, "check_connection", lambda: True)
    monkeypatch.setattr(worker.db, "close_pool", lambda: closed.__setitem__("n", closed["n"] + 1))
    monkeypatch.setattr(worker.queue_consumer, "get_client", lambda: object())

    def explode(*_a, **_k):
        raise RuntimeError("consume blew up")

    monkeypatch.setattr(worker.queue_consumer, "consume", explode)

    with contextlib.suppress(RuntimeError):
        worker.main()

    assert closed["n"] == 1, "pool was not closed on the error path"
