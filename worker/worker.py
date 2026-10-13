"""Worker entry point.

    python -m worker.worker

HANDLERS maps a queue name to the function that ingests its payloads. Each
later step in the build plan registers one.

**A queue with no registered handler is deliberately not consumed.** BLPOP
removes a message the moment it is read, so a placeholder handler that logged
and discarded would destroy real collected data the first time the worker ran.
Leaving the queue unlistened means its messages wait — unprocessed, but intact.
"""

import logging
import os
import sys

from worker import db, ingest_prices, queue_consumer
from worker.config import PRICES_QUEUE
from worker.logging_setup import configure

logger = logging.getLogger(__name__)

# news_queue and sentiment_queue are intentionally absent until their handlers
# exist — an unlistened queue keeps its messages, a placeholder would eat them.
HANDLERS: dict[str, queue_consumer.Handler] = {
    PRICES_QUEUE: ingest_prices.handle,
}


def main() -> int:
    configure("worker")

    if not HANDLERS:
        logger.error(
            "no queue handlers registered — refusing to start. "
            "Consuming a queue without a handler would discard collected data."
        )
        return 1

    shutdown = queue_consumer.Shutdown()
    shutdown.install()

    # Fail fast on an unreachable database rather than discovering it on the
    # first message, which would drop that message for no good reason.
    if not db.check_connection():
        logger.error("database unreachable at startup, exiting")
        return 1

    # Bounded run for integration checks, so a drain can be verified without
    # leaving a long-running process behind.
    raw_max = os.getenv("WORKER_MAX_MESSAGES", "").strip()
    max_messages = int(raw_max) if raw_max else None
    if max_messages:
        logger.info("WORKER_MAX_MESSAGES=%d — will exit after that many", max_messages)

    # One-shot drain: exit as soon as the queues run dry. Without this a limit
    # larger than the backlog blocks forever, because an idle timeout is not a
    # message and never advances the counter.
    exit_when_empty = bool(os.getenv("WORKER_EXIT_WHEN_EMPTY", "").strip())
    if exit_when_empty:
        logger.info("WORKER_EXIT_WHEN_EMPTY set — will exit once the queues are drained")

    client = queue_consumer.get_client()

    try:
        queue_consumer.consume(
            client,
            HANDLERS,
            shutdown,
            max_messages=max_messages,
            exit_when_empty=exit_when_empty,
        )
    finally:
        # Runs on both the normal path and SIGTERM, so connections are always
        # released rather than waiting on the container being killed.
        db.close_pool()

    return 0


if __name__ == "__main__":
    sys.exit(main())
