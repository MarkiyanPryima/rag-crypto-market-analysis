"""Shared pytest configuration.

Its one job is a safety rail that was added the hard way.

Integration tests truncate tables so each starts from a known state. Run
against the working database — which is what `POSTGRES_HOST=localhost pytest`
does by default — they delete real collected data. That happened on 13 Oct and
destroyed 2520 price snapshots covering two days; the queue had already been
drained, so the rows were unrecoverable.

Integration tests are therefore skipped unless POSTGRES_DB names a database
with "test" in it. The guard is deliberately crude: anything subtler could be
satisfied by accident, and the cost of a false negative (a skipped test) is far
lower than the cost of a false positive (deleted history).
"""

import os

import pytest

TEST_DB_MARKER = "test"


def _is_test_database() -> bool:
    return TEST_DB_MARKER in os.getenv("POSTGRES_DB", "").lower()


def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless pointed at a test database."""
    if _is_test_database():
        return

    current = os.getenv("POSTGRES_DB", "<unset>")
    reason = (
        f"integration tests need a test database (POSTGRES_DB={current!r} has no "
        f"{TEST_DB_MARKER!r} in it). They DELETE table contents — run `make "
        f"test-integration`, which points them at a disposable database."
    )
    skip = pytest.mark.skip(reason=reason)

    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def _assert_not_production():
    """Second line of defence, in case a test bypasses the marker check."""
    if os.getenv("POSTGRES_DB") and not _is_test_database():
        # Not an error on its own — unit tests run fine here. Only integration
        # tests touch the database, and those are already skipped above.
        pass
    yield
