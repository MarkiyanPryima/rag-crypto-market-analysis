"""Postgres access for the worker — the only writer in the system.

Uses a psycopg 3 connection pool rather than the reference project's
connection-per-call with psycopg2. Two concrete reasons:

1. **Recovery.** The pool checks a connection's health before handing it out
   and replaces dead ones. A long-lived raw connection dies permanently when
   Postgres restarts, and the worker would then fail every message until it is
   itself restarted.
2. **No manual lifecycle.** `pool.connection()` commits on a clean exit and
   rolls back on an exception, so a handler cannot leak a connection or leave
   a transaction open by forgetting a `finally`.

The pool is small on purpose: the worker is single-threaded, so one connection
does the work and the rest is headroom for replacing a stale one.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool

from worker.config import (
    POOL_MAX_SIZE,
    POOL_MIN_SIZE,
    POOL_OPEN_TIMEOUT,
    postgres_conninfo,
)

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Return the process-wide pool, opening it on first use."""
    global _pool
    if _pool is None:
        # open=False then an explicit open(): constructing with open=True is
        # deprecated in psycopg_pool 3.2+, and the explicit call lets us wait
        # with a timeout so a cold start fails with a clear error rather than
        # handing out connections that cannot yet be established.
        _pool = ConnectionPool(
            conninfo=postgres_conninfo(),
            min_size=POOL_MIN_SIZE,
            max_size=POOL_MAX_SIZE,
            open=False,
            name="worker",
        )
        _pool.open(wait=True, timeout=POOL_OPEN_TIMEOUT)
        logger.info(
            "database pool open (min=%d, max=%d)",
            POOL_MIN_SIZE,
            POOL_MAX_SIZE,
        )
    return _pool


@contextmanager
def get_connection() -> Iterator[psycopg.Connection]:
    """Yield a pooled connection wrapped in a transaction.

    Commits when the block exits cleanly, rolls back if it raises. That is what
    makes per-message error isolation safe: a failed message leaves no partial
    row behind.
    """
    with get_pool().connection() as conn:
        yield conn


def close_pool() -> None:
    """Close the pool. Called on shutdown so connections are released promptly."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
        logger.info("database pool closed")


def check_connection() -> bool:
    """Return True if a trivial query succeeds. Used by the startup check."""
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone() == (1,)
    except Exception:
        logger.exception("database connectivity check failed")
        return False
