"""Ingest price snapshots from prices_queue.

One message is one coin's observation. The insert is a single statement in a
single transaction, so a failure leaves nothing partial behind and the consumer
can drop that message and carry on.
"""

import logging
from typing import Any

from worker import db, tokens

logger = logging.getLogger(__name__)

INSERT_SNAPSHOT = """
    INSERT INTO price_snapshots
        (token_id, price_usd, market_cap, volume_24h, change_24h, scraped_at, source)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


class InvalidPricePayload(ValueError):
    """The message cannot be turned into a row."""


def _require(payload: dict[str, Any], field: str) -> Any:
    value = payload.get(field)
    if value is None:
        raise InvalidPricePayload(f"missing required field {field!r}")
    return value


def handle(payload: dict[str, Any]) -> None:
    """Insert one price snapshot.

    Raises on an unusable payload so the consumer logs and drops the message.
    A coin that is simply not tracked is skipped quietly instead — that is a
    configuration difference, not a corrupt message.
    """
    symbol = str(_require(payload, "symbol")).upper()
    price = _require(payload, "price_usd")

    # scraped_at comes from the payload, never from the column default. The
    # queue can be hours behind — it was over a day behind when this handler
    # was written — and now() at insert time would stamp every backlogged
    # observation with the drain time, collapsing the price history into one
    # instant.
    scraped_at = _require(payload, "scraped_at")

    with db.get_connection() as conn:
        token_id = tokens.resolve(conn, symbol)
        if token_id is None:
            logger.warning("skipping price for untracked symbol %s", symbol)
            return

        with conn.cursor() as cur:
            cur.execute(
                INSERT_SNAPSHOT,
                (
                    token_id,
                    price,
                    payload.get("market_cap"),
                    payload.get("volume_24h"),
                    payload.get("change_24h"),
                    scraped_at,
                    payload.get("source", "coingecko"),
                ),
            )

    logger.debug("stored %s at %s (observed %s)", symbol, price, scraped_at)
