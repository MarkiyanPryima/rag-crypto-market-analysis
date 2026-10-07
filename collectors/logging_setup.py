"""Shared logging configuration.

Replaces the reference project's print(..., flush=True) calls. Log records carry
a timestamp, level and service name, the level is controlled by LOG_LEVEL, and
output goes to stdout so `docker compose logs` picks it up.
"""

import logging
import sys

from collectors.config import LOG_LEVEL


def configure(service: str) -> logging.Logger:
    """Configure root logging and return the logger for one service.

    basicConfig is a no-op once the root logger has handlers, so calling this
    from several modules in the same process is safe.
    """
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
    )
    return logging.getLogger(service)
