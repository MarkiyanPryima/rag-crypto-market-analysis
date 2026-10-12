"""Tests for the worker's database pool.

The pool tests are marked `integration` because they need a live Postgres.
Run the fast subset with `make test-unit`.
"""

import pytest

from worker import db
from worker.config import ConfigError, _int_env, postgres_conninfo


@pytest.fixture(autouse=True)
def _close_pool():
    """Each test starts and ends without a pool, so they cannot affect each other."""
    db.close_pool()
    yield
    db.close_pool()


# --- configuration ---------------------------------------------------------


def test_conninfo_contains_every_required_field():
    info = postgres_conninfo()
    for field in ("host=", "port=", "dbname=", "user=", "password=", "connect_timeout="):
        assert field in info


def test_conninfo_sets_a_connect_timeout():
    """Without one, a worker started before Postgres blocks forever instead of retrying."""
    assert "connect_timeout=" in postgres_conninfo()


@pytest.mark.parametrize("raw", ["5m", "abc", "1.5"], ids=["suffix", "words", "float"])
def test_malformed_int_setting_raises(monkeypatch, raw):
    monkeypatch.setenv("SOME_POOL_SIZE", raw)
    with pytest.raises(ConfigError, match="must be an integer"):
        _int_env("SOME_POOL_SIZE", 1)


# --- pool lifecycle --------------------------------------------------------


def test_close_pool_is_safe_when_never_opened():
    """Shutdown runs even if startup failed before the pool existed."""
    db.close_pool()  # must not raise


@pytest.mark.integration
def test_pool_opens_and_reuses_one_connection():
    pool = db.get_pool()
    assert pool is db.get_pool(), "get_pool must return the same pool, not open a second"


@pytest.mark.integration
def test_connection_executes_a_query():
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)


@pytest.mark.integration
def test_check_connection_reports_true_when_reachable():
    assert db.check_connection() is True


@pytest.mark.integration
def test_worker_connection_can_write():
    """Unlike the collectors, this connection is NOT read-only.

    The collectors' read_only flag is what enforces the single-writer
    invariant; this asserts the writer side actually has the access it needs,
    so the invariant is proven from both directions.
    """
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("CREATE TEMPORARY TABLE write_probe (n int)")
        cur.execute("INSERT INTO write_probe (n) VALUES (1)")
        cur.execute("SELECT count(*) FROM write_probe")
        assert cur.fetchone() == (1,)


@pytest.mark.integration
def test_exception_inside_the_block_rolls_back():
    """Per-message error isolation depends on this: a failed message leaves no row.

    Table name is written literally rather than interpolated — an f-string in a
    query is the pattern ruff's S608 flags, and it should not appear even in
    tests where the value happens to be a constant.
    """
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS rollback_probe (n int)")
        cur.execute("DELETE FROM rollback_probe")

    with pytest.raises(RuntimeError), db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO rollback_probe (n) VALUES (99)")
        raise RuntimeError("simulated handler failure")

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM rollback_probe")
        assert cur.fetchone() == (0,), "row survived a failed block — rollback did not happen"
        cur.execute("DROP TABLE rollback_probe")


@pytest.mark.integration
def test_pool_survives_a_returned_connection():
    """Connections go back to the pool rather than being closed."""
    for _ in range(3):
        with db.get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()

    stats = db.get_pool().get_stats()
    assert stats["connections_num"] <= 3, f"pool grew unexpectedly: {stats}"
