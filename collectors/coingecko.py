"""CoinGecko markets client.

Fetches current prices for a list of CoinGecko coin ids and normalises them into
the shape the worker inserts into price_snapshots.

The client takes the coin ids as an argument and performs no database access,
which keeps it pure and unit-testable. Deciding which coins to track is the
caller's job.
"""

import logging
from typing import Any

import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from collectors.config import COINGECKO_API_KEY, HTTP_TIMEOUT

logger = logging.getLogger(__name__)

MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"

# change_24h is NUMERIC(8,4) in the schema, so the magnitude cannot exceed this
# without the insert failing. Unreachable for major assets, but a microcap added
# to the tracked list could produce a four-digit percentage move.
_MAX_CHANGE_PCT = 9999.9999

# Transport-level failures are worth retrying. An HTTP 4xx is not: a bad coin id
# or a rejected API key will fail identically on every attempt.
_RETRYABLE = (requests.ConnectionError, requests.Timeout)


class CoinGeckoError(RuntimeError):
    """CoinGecko returned something unusable."""


def _headers() -> dict[str, str]:
    """Demo-plan key goes in a header. Absent is fine — it only raises limits."""
    if COINGECKO_API_KEY:
        return {"x-cg-demo-api-key": COINGECKO_API_KEY}
    return {}


@retry(
    retry=retry_if_exception_type(_RETRYABLE),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def fetch_markets(coin_ids: list[str]) -> list[dict[str, Any]]:
    """Return the raw markets payload for `coin_ids`.

    One request covers every coin, so a cycle costs a single call regardless of
    how many assets are tracked — which matters on the free tier's rate limit.
    """
    if not coin_ids:
        return []

    response = requests.get(
        MARKETS_URL,
        params={
            "vs_currency": "usd",
            "ids": ",".join(coin_ids),
            "order": "market_cap_desc",
            "per_page": len(coin_ids),
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "24h",
        },
        headers=_headers(),
        timeout=HTTP_TIMEOUT,
    )
    if response.status_code in (401, 403) and COINGECKO_API_KEY:
        # Easy to misdiagnose: without a key these endpoints are public, so a
        # rejected key is strictly worse than no key. Say so plainly.
        raise CoinGeckoError(
            f"CoinGecko rejected the request with {response.status_code}; "
            "COINGECKO_API_KEY appears to be invalid. Unset it to use the free tier."
        )
    response.raise_for_status()
    payload = response.json()

    # A rate-limit response is JSON with a "status" object rather than a list,
    # so this would otherwise surface as a confusing AttributeError later.
    if not isinstance(payload, list):
        raise CoinGeckoError(f"expected a list of markets, got {type(payload).__name__}: {payload}")
    return payload


def normalise(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Convert one markets entry into a price payload, or None if unusable.

    Validation happens here rather than in the database: a non-positive price
    would violate price_snapshots_price_positive inside the worker's
    transaction and abort the whole batch. Dropping one coin with a warning
    costs that coin's data point; letting it through costs every coin's.
    """
    slug = entry.get("id")
    symbol = entry.get("symbol")
    price = entry.get("current_price")

    if not slug or not symbol:
        logger.warning("dropping entry with no id/symbol: %r", entry)
        return None

    if price is None:
        logger.warning("dropping %s: current_price is null", slug)
        return None

    try:
        price = float(price)
    except (TypeError, ValueError):
        logger.warning("dropping %s: current_price not numeric: %r", slug, price)
        return None

    if price <= 0:
        logger.warning("dropping %s: non-positive price %r", slug, price)
        return None

    change = entry.get("price_change_percentage_24h")
    if change is not None:
        change = float(change)
        if abs(change) > _MAX_CHANGE_PCT:
            # Keep the row; the price is the valuable part. Only the percentage
            # would overflow NUMERIC(8,4).
            logger.warning("%s: 24h change %r exceeds column range, storing null", slug, change)
            change = None

    return {
        # Uppercased because tokens.symbol carries a CHECK (symbol = upper(symbol))
        # and CoinGecko returns lowercase symbols.
        "symbol": symbol.upper(),
        "slug": slug,
        "price_usd": price,
        "market_cap": entry.get("market_cap"),
        # CoinGecko calls this total_volume, not volume_24h.
        "volume_24h": entry.get("total_volume"),
        "change_24h": change,
        "source": "coingecko",
    }


def fetch_prices(coin_ids: list[str]) -> list[dict[str, Any]]:
    """Fetch and normalise prices, skipping any entry that fails validation."""
    raw = fetch_markets(coin_ids)
    prices = [payload for entry in raw if (payload := normalise(entry)) is not None]

    missing = set(coin_ids) - {p["slug"] for p in prices}
    if missing:
        logger.warning("no usable price for: %s", ", ".join(sorted(missing)))

    logger.info("normalised %d/%d coins", len(prices), len(coin_ids))
    return prices
