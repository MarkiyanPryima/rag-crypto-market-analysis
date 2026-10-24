"""Shared HTTP client with polite pacing and exponential-backoff retries."""

import logging
import time

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

USER_AGENT = "crypto-rag-thesis/0.1 (research; contact via repository)"


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 425, 429, 500, 502, 503, 504}
    return isinstance(exc, httpx.TransportError)


class PoliteClient:
    """httpx.Client wrapper that sleeps `min_interval` seconds between requests."""

    def __init__(self, min_interval: float = 0.2, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )
        self._min_interval = min_interval
        self._last = 0.0

    def _pace(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    @retry(
        retry=retry_if_exception(_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(6),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def get(self, url: str, **kwargs) -> httpx.Response:
        self._pace()
        response = self._client.get(url, **kwargs)
        response.raise_for_status()
        return response

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
