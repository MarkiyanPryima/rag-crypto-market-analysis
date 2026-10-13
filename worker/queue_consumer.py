"""Redis queue consumer.

Blocks on all three queues at once and dispatches each message to a handler.

Two properties matter more than throughput here:

* **Per-message error isolation.** A handler that raises loses that one
  message, not the batch and not the loop. The database connection is wrapped
  in a transaction per message, so a failure leaves no partial row.
* **Graceful shutdown.** SIGTERM sets a flag; the loop finishes the message in
  flight, closes the pool and exits. `docker compose restart` therefore cannot
  interrupt a half-written transaction.
"""

import json
import logging
import signal
from collections.abc import Callable
from types import FrameType
from typing import Any

import redis
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from worker.config import BLPOP_TIMEOUT, REDIS_HOST, REDIS_PORT, REDIS_SOCKET_TIMEOUT

logger = logging.getLogger(__name__)

# A handler takes the decoded payload and does its own database work.
Handler = Callable[[dict[str, Any]], None]

_RETRYABLE = (redis.ConnectionError, redis.TimeoutError)


class Shutdown:
    """Tracks whether a termination signal has arrived.

    A flag rather than raising from the signal handler: an exception thrown
    mid-transaction would abort it at an arbitrary point, which is exactly what
    graceful shutdown is meant to avoid.
    """

    def __init__(self) -> None:
        self.requested = False

    def install(self) -> None:
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        name = signal.Signals(signum).name
        if self.requested:
            # Second signal means someone is impatient; honour it.
            logger.warning("%s received again — exiting immediately", name)
            raise SystemExit(1)
        logger.info("%s received — finishing current message, then exiting", name)
        self.requested = True


def get_client() -> redis.Redis:
    """Redis client with timeouts set.

    socket_timeout must exceed BLPOP_TIMEOUT, or the blocking read would be
    cut short by the socket before the server ever returns an empty result.
    """
    socket_timeout = max(REDIS_SOCKET_TIMEOUT, BLPOP_TIMEOUT + 5)
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
        socket_timeout=socket_timeout,
        socket_connect_timeout=REDIS_SOCKET_TIMEOUT,
    )


@retry(
    retry=retry_if_exception_type(_RETRYABLE),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _blpop(client: redis.Redis, queues: list[str]) -> tuple[str, str] | None:
    """Pop from whichever queue has data first, or None on timeout.

    Retried on connection loss so a Redis restart does not end the worker.
    """
    return client.blpop(queues, timeout=BLPOP_TIMEOUT)


def consume(
    client: redis.Redis,
    handlers: dict[str, Handler],
    shutdown: Shutdown,
    max_messages: int | None = None,
    exit_when_empty: bool = False,
) -> dict[str, int]:
    """Drain the queues until shutdown, returning per-queue success counts.

    max_messages bounds the loop for tests and partial drains; None means run
    until a signal arrives.

    exit_when_empty returns as soon as a blocking read times out with nothing
    left. Needed because max_messages alone cannot end a one-shot drain: idle
    timeouts are not messages, so a limit larger than the queue leaves the loop
    blocking forever. The long-running service leaves this False — an empty
    queue there is normal and simply means waiting.
    """
    queues = list(handlers)
    processed = dict.fromkeys(queues, 0)
    failed = dict.fromkeys(queues, 0)
    seen = 0

    logger.info(
        "consuming %s (blpop timeout %ds, exit_when_empty=%s)",
        ", ".join(queues),
        BLPOP_TIMEOUT,
        exit_when_empty,
    )

    while not shutdown.requested:
        if max_messages is not None and seen >= max_messages:
            break

        item = _blpop(client, queues)
        if item is None:
            if exit_when_empty:
                logger.info("queues empty — exiting as requested")
                break
            continue  # idle timeout — loop round to re-check the shutdown flag

        queue, raw = item
        seen += 1

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # Unparseable message: log the body and drop it. Pushing it back
            # would spin the loop on the same bad payload forever.
            failed[queue] += 1
            logger.exception("%s: dropping unparseable message: %r", queue, raw[:200])
            continue

        if not isinstance(payload, dict):
            failed[queue] += 1
            logger.error("%s: dropping non-object message: %r", queue, payload)
            continue

        try:
            handlers[queue](payload)
            processed[queue] += 1
        except Exception:
            failed[queue] += 1
            # The handler's transaction has already rolled back, so nothing
            # partial was written. Carry on with the next message.
            logger.exception("%s: handler failed, message dropped", queue)

    total_ok = sum(processed.values())
    total_bad = sum(failed.values())
    logger.info(
        "consumer stopped: %d processed, %d failed (%s)",
        total_ok,
        total_bad,
        ", ".join(f"{q}={processed[q]}" for q in queues),
    )
    return processed
