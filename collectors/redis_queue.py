"""Redis queue helpers shared by the collectors.

A collector's only side effect is appending a JSON payload to a queue. The
worker drains the queues and is the sole writer to Postgres, so a collector
failing mid-cycle cannot leave the database half-updated.

Named redis_queue rather than queue to avoid shadowing the standard library
module of that name.
"""

import json
import logging
from typing import Any

import redis
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from collectors.config import REDIS_HOST, REDIS_PORT, REDIS_SOCKET_TIMEOUT

logger = logging.getLogger(__name__)

# Connection drops and timeouts are worth retrying; a malformed command is not.
_RETRYABLE = (redis.ConnectionError, redis.TimeoutError)


def get_client() -> redis.Redis:
    """Return a Redis client with timeouts set.

    decode_responses keeps reads as str rather than bytes. Both timeouts are
    set because the default is None, which blocks forever if the server stops
    responding mid-operation — the collector would hang silently rather than
    fail and retry.
    """
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
        socket_timeout=REDIS_SOCKET_TIMEOUT,
        socket_connect_timeout=REDIS_SOCKET_TIMEOUT,
    )


@retry(
    retry=retry_if_exception_type(_RETRYABLE),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def push(client: redis.Redis, queue: str, payload: Any) -> int:
    """Append one JSON-encoded payload to `queue`, returning the queue length.

    default=str so datetimes serialise instead of raising TypeError and
    discarding a whole cycle's data.
    """
    body = json.dumps(payload, default=str)
    length = client.rpush(queue, body)
    logger.info("pushed to %s (queue length %d)", queue, length)
    return length


def push_many(client: redis.Redis, queue: str, payloads: list[Any]) -> int:
    """Append several payloads as individual queue entries.

    Each entry is pushed separately so the worker can fail one message without
    losing the rest of the batch.
    """
    if not payloads:
        logger.warning("nothing to push to %s", queue)
        return 0
    length = 0
    for payload in payloads:
        length = push(client, queue, payload)
    logger.info("pushed %d items to %s", len(payloads), queue)
    return length
