"""Fear & Greed Index client (alternative.me).

Market-wide sentiment: one integer 0-100 plus a classification, published once
a day. No API key required.

Verified against a live response on 11 Oct 2026. Two details the design doc
glosses over:

  * `value` and `timestamp` arrive as **strings**, not numbers.
  * the envelope carries `metadata.error`, which is null on success — an error
    response still returns HTTP 200, so the status code alone is not enough.

Readings land at midnight UTC, so polling every 6 hours sees the same reading
up to four times. The timestamp is therefore carried through as `value_at`,
which fear_greed_index declares UNIQUE, letting the worker discard repeats with
ON CONFLICT DO NOTHING rather than storing four copies of each day.
"""

import logging
from datetime import UTC, datetime
from typing import Any

import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from collectors.config import FEAR_GREED_API_URL, HTTP_TIMEOUT

logger = logging.getLogger(__name__)

# fear_greed_index.value carries CHECK (value BETWEEN 0 AND 100).
_MIN_VALUE = 0
_MAX_VALUE = 100

# fear_greed_index.classification is VARCHAR(20). The documented labels are
# "Extreme Fear" through "Extreme Greed", so 20 is ample — but a feed change
# must fail here rather than as a database error inside the worker.
_MAX_CLASSIFICATION = 20

_RETRYABLE = (requests.ConnectionError, requests.Timeout)


class FearGreedError(RuntimeError):
    """alternative.me returned something unusable."""


@retry(
    retry=retry_if_exception_type(_RETRYABLE),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def fetch_raw(limit: int = 1) -> list[dict[str, Any]]:
    """Return the raw `data` entries, newest first."""
    response = requests.get(
        FEAR_GREED_API_URL,
        params={"limit": limit},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict):
        raise FearGreedError(f"expected a JSON object, got {type(payload).__name__}")

    # An error is reported in the body with HTTP 200, so this must be checked
    # explicitly or a failure would look like an empty result.
    error = (payload.get("metadata") or {}).get("error")
    if error:
        raise FearGreedError(f"API reported an error: {error}")

    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise FearGreedError(f"no data in response: {payload}")

    return data


def normalise(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Convert one reading into a payload, or None if unusable.

    Validated here rather than in the database: every CHECK this would violate
    lives inside the worker's transaction, so a bad reading would abort the
    message instead of being skipped with a warning.
    """
    raw_value = entry.get("value")
    classification = (entry.get("value_classification") or "").strip()
    raw_timestamp = entry.get("timestamp")

    try:
        # Arrives as a string; int() also rejects "61.5" and "".
        value = int(raw_value)
    except (TypeError, ValueError):
        logger.warning("dropping reading: value not an integer: %r", raw_value)
        return None

    if not _MIN_VALUE <= value <= _MAX_VALUE:
        logger.warning("dropping reading: value %d outside 0-100", value)
        return None

    if not classification:
        logger.warning("dropping reading: no classification")
        return None

    if len(classification) > _MAX_CLASSIFICATION:
        logger.warning(
            "dropping reading: classification %r exceeds %d characters",
            classification,
            _MAX_CLASSIFICATION,
        )
        return None

    try:
        value_at = datetime.fromtimestamp(int(raw_timestamp), UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        logger.warning("dropping reading: bad timestamp %r", raw_timestamp)
        return None

    return {
        "value": value,
        "classification": classification,
        # The reading's own publication time. UNIQUE in the schema, so the
        # worker uses it to ignore the repeats a 6-hourly poll produces.
        "value_at": value_at.isoformat(),
        # When we observed it, kept distinct from value_at.
        "fetched_at": datetime.now(UTC).isoformat(),
    }


def fetch_current() -> dict[str, Any] | None:
    """Fetch the latest reading, or None if it fails validation."""
    entries = fetch_raw(limit=1)
    reading = normalise(entries[0])

    if reading is None:
        logger.warning("latest reading was unusable")
        return None

    logger.info(
        "fear & greed: %d (%s) published %s",
        reading["value"],
        reading["classification"],
        reading["value_at"],
    )
    return reading
