"""Market sentiment collector.

Polls the Fear & Greed Index and pushes each reading onto sentiment_queue.

Unlike the other two collectors this one needs no database access at all: there
is nothing to look up, since the index is a single market-wide number rather
than something per-coin.

    python -m collectors.sentiment_collector
"""

import logging
import time

import redis

from collectors import redis_queue
from collectors.config import RUN_ONCE, SENTIMENT_FETCH_INTERVAL, SENTIMENT_QUEUE
from collectors.fear_greed import fetch_current
from collectors.logging_setup import configure

logger = logging.getLogger(__name__)


def run_cycle(client: redis.Redis) -> int:
    """Fetch the current reading and queue it. Returns the number queued (0 or 1)."""
    reading = fetch_current()
    if reading is None:
        return 0

    redis_queue.push(client, SENTIMENT_QUEUE, reading)
    return 1


def main() -> None:
    configure("sentiment_collector")
    client = redis_queue.get_client()

    logger.info("starting (interval=%ds, run_once=%s)", SENTIMENT_FETCH_INTERVAL, RUN_ONCE)

    while True:
        try:
            queued = run_cycle(client)
            logger.info("cycle complete: %d readings queued", queued)
        except Exception:
            # Nothing else in the system depends on this being fresh, so a
            # failure is logged and retried next cycle. The index only updates
            # daily; missing one 6-hourly poll costs nothing.
            logger.exception("cycle failed; retrying next interval")

        if RUN_ONCE:
            logger.info("RUN_ONCE set, exiting after one cycle")
            return

        time.sleep(SENTIMENT_FETCH_INTERVAL)


if __name__ == "__main__":
    main()
