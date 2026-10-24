"""Process new articles: chunk -> embed -> label. Idempotent; safe to run after every fetch.

Usage:
    python -m crypto_rag.processing.pipeline [--skip-llm]
"""

import argparse
import logging

from crypto_rag.config import setup_logging
from crypto_rag.db.session import get_engine
from crypto_rag.processing.chunk import chunk_pending
from crypto_rag.processing.embed import embed_pending
from crypto_rag.processing.label import label_pending

logger = logging.getLogger(__name__)


def run(skip_llm: bool = False) -> None:
    engine = get_engine()
    chunk_pending(engine)
    if skip_llm:
        logger.info("--skip-llm: embeddings and labels left for a later run")
        return
    embed_pending(engine)
    label_pending(engine)


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-llm", action="store_true")
    run(parser.parse_args().skip_llm)


if __name__ == "__main__":
    main()
