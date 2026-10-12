"""Configuration for the worker service.

Deliberately separate from collectors/config.py rather than shared: the two
services need different things. Collectors read the token list and write to
Redis; the worker reads Redis and writes to Postgres, and will also need an
OpenAI key the collectors never touch.

What is duplicated here is the reading of environment variables, not business
logic — a wrong value fails immediately on connect. The one exception is the
queue names, where a silent divergence would create a queue nobody drains, so
tests/test_queue_names.py asserts they match the collectors' names exactly.
"""

import os

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """A required environment variable is missing or malformed."""


def _int_env(name: str, default: int) -> int:
    """Read an integer setting, failing loudly rather than silently defaulting."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


# --- Postgres (read-write) --------------------------------------------------
# No read_only flag here, unlike the collectors: this is the writer.
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = _int_env("POSTGRES_PORT", 5432)
POSTGRES_DB = os.getenv("POSTGRES_DB", "crypto_rag")
POSTGRES_USER = os.getenv("POSTGRES_USER", "crypto")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
POSTGRES_CONNECT_TIMEOUT = _int_env("POSTGRES_CONNECT_TIMEOUT", 10)

# Connection pool bounds. The worker is single-threaded, so one connection is
# enough to do the work; the second is headroom so a stale connection can be
# replaced without the pool going empty.
POOL_MIN_SIZE = _int_env("WORKER_POOL_MIN_SIZE", 1)
POOL_MAX_SIZE = _int_env("WORKER_POOL_MAX_SIZE", 3)

# How long to wait for Postgres when opening the pool. The worker starts after
# a compose health check, but a cold boot can still race the first connection.
POOL_OPEN_TIMEOUT = _int_env("WORKER_POOL_OPEN_TIMEOUT", 30)


def postgres_conninfo() -> str:
    """Build the connection string.

    A function rather than a module constant so importing config never requires
    database credentials — unit tests and CI import this module freely.
    """
    return (
        f"host={POSTGRES_HOST} port={POSTGRES_PORT} dbname={POSTGRES_DB} "
        f"user={POSTGRES_USER} password={POSTGRES_PASSWORD} "
        f"connect_timeout={POSTGRES_CONNECT_TIMEOUT}"
    )


# --- Redis ------------------------------------------------------------------
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = _int_env("REDIS_PORT", 6379)
REDIS_SOCKET_TIMEOUT = _int_env("REDIS_SOCKET_TIMEOUT", 10)

# MUST match collectors/config.py. Asserted by tests/test_queue_names.py —
# a typo here would leave a queue filling up with nothing to drain it.
PRICES_QUEUE = "prices_queue"
NEWS_QUEUE = "news_queue"
SENTIMENT_QUEUE = "sentiment_queue"

# Seconds BLPOP blocks before returning empty. Not zero (block forever): the
# loop needs to come up for air periodically to notice a shutdown signal.
BLPOP_TIMEOUT = _int_env("WORKER_BLPOP_TIMEOUT", 5)

# --- Logging ----------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
