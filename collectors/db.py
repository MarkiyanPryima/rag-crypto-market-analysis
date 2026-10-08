"""Read-only database access for the collectors.

The worker is the only component that writes to Postgres. Collectors read one
thing — the tracked token list — so the set of coins lives in the database
instead of being hardcoded. The reference project duplicates that list in three
separate modules, which can silently drift apart.

Connections are opened read-only, so the single-writer invariant is enforced by
Postgres rather than relying on nobody adding an INSERT here later.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from collectors.config import postgres_conninfo

logger = logging.getLogger(__name__)


@contextmanager
def read_only_connection() -> Iterator[psycopg.Connection]:
    """Yield a read-only connection, closing it on the way out."""
    conn = psycopg.connect(postgres_conninfo())
    try:
        # Must be set before a transaction begins. Any INSERT/UPDATE/DELETE on
        # this connection now fails with a read-only transaction error.
        conn.read_only = True
        yield conn
    finally:
        conn.close()


@retry(
    retry=retry_if_exception_type(psycopg.OperationalError),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def fetch_tracked_coin_ids() -> list[str]:
    """Return the CoinGecko id of every tracked coin, from tokens.slug.

    Retries on OperationalError so a collector starting while Postgres is still
    coming up recovers instead of crash-looping.
    """
    with read_only_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT slug FROM tokens ORDER BY symbol")
        coin_ids = [row[0] for row in cur.fetchall()]

    logger.info("tracking %d coins: %s", len(coin_ids), ", ".join(coin_ids))
    return coin_ids
