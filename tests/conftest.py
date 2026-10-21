"""Shared fixtures.

Integration tests rebuild the schema, so they run ONLY against
TEST_DATABASE_URL, and only if its database name contains "test". The v0
project lost two days of collected prices by running tests against the
working database; this guard exists so that cannot happen again.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from crypto_rag.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _test_url() -> str:
    url = get_settings().test_database_url
    if "test" not in (make_url(url).database or ""):
        pytest.exit(f"TEST_DATABASE_URL must name a *test* database, got {url!r}", returncode=2)
    return url


def _alembic(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = _test_url()
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"test database unavailable: {exc}")
    cfg = _alembic(url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    """Session whose writes are rolled back after each test."""
    conn = engine.connect()
    trans = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()
