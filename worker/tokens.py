"""Symbol-to-token-id resolution with an in-process cache.

Every price message and every tagged article needs the UUID of its token. The
queue holds thousands of messages for the same eight coins, so looking the
symbol up per message would mean thousands of identical queries.

The cache is safe because `tokens` is a curated list, seeded once and changed
only deliberately. A symbol missing from the cache is re-checked against the
database before being reported unknown, so a coin added while the worker runs
is still picked up.
"""

import logging

import psycopg

logger = logging.getLogger(__name__)

_cache: dict[str, str] = {}


def resolve(conn: psycopg.Connection, symbol: str) -> str | None:
    """Return the token id for `symbol`, or None if it is not tracked.

    None is not an error: the collectors read their list from this same table,
    but a feed can mention a coin we do not track, and an article tagged with
    one simply gets a NULL token_id.
    """
    key = symbol.upper()

    if key in _cache:
        return _cache[key]

    with conn.cursor() as cur:
        cur.execute("SELECT id FROM tokens WHERE symbol = %s", (key,))
        row = cur.fetchone()

    if row is None:
        # Not cached — a negative result is deliberately not cached, so adding
        # a token to the database takes effect without restarting the worker.
        logger.warning("symbol %r is not in the tokens table", key)
        return None

    token_id = str(row[0])
    _cache[key] = token_id
    return token_id


def clear_cache() -> None:
    """Drop the cache. Used by tests and after a token list change."""
    _cache.clear()


def cache_size() -> int:
    return len(_cache)
