"""Ingest Fear & Greed readings from sentiment_queue.

The index publishes once a day; the collector polls every six hours. So the
same reading arrives up to four times, and `value_at` — the reading's own
publication time, UNIQUE in the schema — is what collapses those duplicates
into one row.

Without that, the table would hold four copies of every day, the API's
"last 30 readings" would span a week instead of a month, and the daily join in
the correlation analysis would have to de-duplicate before it could compute
anything.
"""

import logging
from typing import Any

from worker import db

logger = logging.getLogger(__name__)

# DO NOTHING rather than DO UPDATE: a reading is immutable once published, so a
# repeat poll carries no new information. Updating fetched_at on every poll
# would also make it impossible to tell when a reading was first seen.
INSERT_READING = """
    INSERT INTO fear_greed_index (value, classification, value_at, fetched_at)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (value_at) DO NOTHING
    RETURNING id
"""


class InvalidSentimentPayload(ValueError):
    """The message cannot be turned into a row."""


def _require(payload: dict[str, Any], field: str) -> Any:
    value = payload.get(field)
    if value is None:
        raise InvalidSentimentPayload(f"missing required field {field!r}")
    return value


def handle(payload: dict[str, Any]) -> None:
    """Insert one market sentiment reading, ignoring repeats."""
    value = _require(payload, "value")
    classification = _require(payload, "classification")
    value_at = _require(payload, "value_at")

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            INSERT_READING,
            (value, classification, value_at, payload.get("fetched_at")),
        )
        inserted = cur.fetchone() is not None

    if inserted:
        logger.info("stored fear & greed %s (%s) for %s", value, classification, value_at)
    else:
        # Expected three times out of four, so this is debug rather than a
        # warning — it means deduplication is working.
        logger.debug("reading for %s already stored, ignoring repeat", value_at)
