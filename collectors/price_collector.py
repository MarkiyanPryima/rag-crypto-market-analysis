"""Price collector.

Reads the tracked coin list from Postgres, fetches current prices from
CoinGecko and pushes one payload per coin onto prices_queue. The worker drains
the queue and performs the inserts.

    python -m collectors.price_collector
"""

import logging
import time

import redis

from collectors import redis_queue
from collectors.coingecko import fetch_prices
from collectors.config import PRICE_FETCH_INTERVAL, PRICES_QUEUE, RUN_ONCE
from collectors.db import fetch_tracked_coin_ids
from collectors.logging_setup import configure

logger = logging.getLogger(__name__)


def run_cycle(client: redis.Redis) -> int:
    """Run one fetch-and-push cycle, returning the number of coins queued."""
    coin_ids = fetch_tracked_coin_ids()
    if not coin_ids:
        # An empty tokens table is a setup problem, not a transient failure, so
        # it is worth saying loudly rather than reporting a successful cycle
        # that collected nothing.
        logger.warning("no tracked tokens in the database — did bdd/seed.sql run?")
        return 0

    prices = fetch_prices(coin_ids)
    if not prices:
        logger.warning("no usable prices this cycle")
        return 0

    redis_queue.push_many(client, PRICES_QUEUE, prices)
    return len(prices)


def main() -> None:
    configure("price_collector")
    client = redis_queue.get_client()

    logger.info(
        "starting (interval=%ds, run_once=%s)",
        PRICE_FETCH_INTERVAL,
        RUN_ONCE,
    )

    while True:
        try:
            queued = run_cycle(client)
            logger.info("cycle complete: %d coins queued", queued)
        except Exception:
            # Error isolation: a failed cycle must not take the collector down.
            # Whatever broke — CoinGecko, Redis, Postgres — the next interval
            # starts from scratch, and the worker is unaffected because nothing
            # partial was written.
            logger.exception("cycle failed; retrying next interval")

        if RUN_ONCE:
            logger.info("RUN_ONCE set, exiting after one cycle")
            return

        time.sleep(PRICE_FETCH_INTERVAL)


if __name__ == "__main__":
    main()
