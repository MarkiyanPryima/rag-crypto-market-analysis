"""News collector.

Reads symbol aliases from Postgres, fetches the RSS feeds, and pushes one
payload per article onto news_queue. The worker drains the queue, embeds each
article and classifies its sentiment.

    python -m collectors.news_collector
"""

import logging
import time

import redis

from collectors import redis_queue, rss
from collectors.config import NEWS_FETCH_INTERVAL, NEWS_QUEUE, RUN_ONCE
from collectors.db import fetch_symbol_aliases
from collectors.logging_setup import configure

logger = logging.getLogger(__name__)


def run_cycle(client: redis.Redis) -> int:
    """Run one fetch-and-push cycle, returning the number of articles queued."""
    # Guard on the database result, not the merged dict: build_aliases() folds
    # in the curated extras, so the merged version is never empty and would
    # mask an unseeded tokens table. The collector would then run happily while
    # tagging only the two hardcoded aliases.
    db_aliases = fetch_symbol_aliases()
    if not db_aliases:
        logger.warning("no tokens in the database — did bdd/seed.sql run?")
        return 0

    aliases = rss.build_aliases(db_aliases)

    articles = rss.fetch_articles(aliases)
    if not articles:
        logger.warning("no articles this cycle")
        return 0

    # One message per article, unlike the reference project which pushes the
    # whole batch as a single JSON blob. Per-article messages mean one
    # malformed story fails alone instead of discarding the other 92, and the
    # worker's per-message error isolation has something to isolate.
    redis_queue.push_many(client, NEWS_QUEUE, articles)
    return len(articles)


def main() -> None:
    configure("news_collector")
    client = redis_queue.get_client()

    logger.info("starting (interval=%ds, run_once=%s)", NEWS_FETCH_INTERVAL, RUN_ONCE)

    while True:
        try:
            queued = run_cycle(client)
            logger.info("cycle complete: %d articles queued", queued)
        except Exception:
            # Error isolation: a failed cycle must not end the collector. The
            # next interval starts clean, and nothing partial was written.
            logger.exception("cycle failed; retrying next interval")

        if RUN_ONCE:
            logger.info("RUN_ONCE set, exiting after one cycle")
            return

        time.sleep(NEWS_FETCH_INTERVAL)


if __name__ == "__main__":
    main()
