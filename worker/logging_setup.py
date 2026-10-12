"""Logging configuration for the worker.

Mirrors collectors/logging_setup.py so both services produce identically
shaped log lines — `docker compose logs` interleaves them, and a differing
format makes that output far harder to read.
"""

import logging
import sys

from worker.config import LOG_LEVEL


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
