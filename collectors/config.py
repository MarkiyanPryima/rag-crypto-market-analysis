"""Configuration for the collector services.

Settings are read here rather than with inline os.getenv calls spread through
each collector, so the full set of knobs is visible in one place and a
malformed value fails at import instead of halfway through a collection cycle.
"""

import os

from dotenv import load_dotenv

# Populates os.environ from .env when a collector runs directly on the host for
# testing. Inside Docker the variables already exist and load_dotenv does not
# overwrite them.
load_dotenv()


class ConfigError(RuntimeError):
    """A required environment variable is missing or malformed."""


def _optional_env(name: str) -> str | None:
    """Read an optional setting, treating an unreplaced template value as unset.

    .env is copied from .env.example, whose optional entries read
    `your_coingecko_api_key`. That string is truthy, so a plain os.getenv sends
    it as a credential and the request fails 401 — strictly worse than sending
    no key at all, which the free tier accepts. Anything still carrying the
    template prefix is therefore treated as absent.
    """
    value = (os.getenv(name) or "").strip()
    if not value or value.startswith("your_"):
        return None
    return value


def _int_env(name: str, default: int) -> int:
    """Read an integer setting, failing loudly rather than silently defaulting.

    A typo such as PRICE_FETCH_INTERVAL=5m would otherwise fall back to the
    default and look like it had been applied.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


# --- Redis ------------------------------------------------------------------
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = _int_env("REDIS_PORT", 6379)

# Redis defaults to no socket timeout, which means a dropped connection hangs
# the collector indefinitely instead of raising and letting the retry fire.
REDIS_SOCKET_TIMEOUT = _int_env("REDIS_SOCKET_TIMEOUT", 10)

# Queue names are constants rather than string literals at each call site: a
# typo in one collector would otherwise create a queue nothing ever drains.
PRICES_QUEUE = "prices_queue"
NEWS_QUEUE = "news_queue"
SENTIMENT_QUEUE = "sentiment_queue"

# --- Postgres (read-only) ---------------------------------------------------
# Collectors read exactly one thing: the tracked token list. The worker remains
# the only writer; collector connections are opened read-only so that invariant
# is enforced by the database rather than merely documented.
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = _int_env("POSTGRES_PORT", 5432)
POSTGRES_DB = os.getenv("POSTGRES_DB", "crypto_rag")
POSTGRES_USER = os.getenv("POSTGRES_USER", "crypto")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")

# Seconds to wait for a connection. Without a limit, a collector started before
# Postgres is accepting connections blocks indefinitely instead of failing and
# retrying.
POSTGRES_CONNECT_TIMEOUT = _int_env("POSTGRES_CONNECT_TIMEOUT", 10)


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


# --- Collection intervals (seconds) -----------------------------------------
PRICE_FETCH_INTERVAL = _int_env("PRICE_FETCH_INTERVAL", 300)
NEWS_FETCH_INTERVAL = _int_env("NEWS_FETCH_INTERVAL", 900)
SENTIMENT_FETCH_INTERVAL = _int_env("SENTIMENT_FETCH_INTERVAL", 21600)

# When set, a collector runs one cycle and exits instead of looping. Used by the
# integration checks so a six-hour interval does not have to be waited out.
RUN_ONCE = bool(os.getenv("RUN_ONCE", "").strip())

# --- External sources -------------------------------------------------------
# Optional: raises the free-tier rate limit. Prices still work without it.
COINGECKO_API_KEY = _optional_env("COINGECKO_API_KEY")
FEAR_GREED_API_URL = os.getenv("FEAR_GREED_API_URL", "https://api.alternative.me/fng/")
SCRAPER_SERVICE_URL = os.getenv("SCRAPER_SERVICE_URL", "http://scraper:8001/scrape")

# Every outbound HTTP call passes a timeout. Without one, requests blocks
# forever on a silently dropped connection and the collector stops collecting
# without ever logging an error.
HTTP_TIMEOUT = _int_env("HTTP_TIMEOUT", 10)

# --- Logging ----------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
