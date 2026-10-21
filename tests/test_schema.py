import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


def test_extensions_installed(db):
    names = set(db.execute(text("SELECT extname FROM pg_extension")).scalars())
    assert {"timescaledb", "vector"} <= names


def test_candles_is_hypertable(db):
    rows = db.execute(
        text("SELECT hypertable_name FROM timescaledb_information.hypertables")
    ).scalars()
    assert "candles" in set(rows)


def test_chunk_tsv_is_generated(db):
    db.execute(
        text(
            "INSERT INTO articles (id, source, channel, url, title, published_at) "
            "VALUES ('t:1', 'coindesk', 'rss', 'https://x/1', 'T', now())"
        )
    )
    db.execute(
        text(
            "INSERT INTO chunks (article_id, chunk_index, text, published_at, content_hash) "
            "VALUES ('t:1', 0, 'Bitcoin rallies after ETF approval', now(), 'h')"
        )
    )
    tsv = db.execute(text("SELECT tsv::text FROM chunks WHERE article_id = 't:1'")).scalar_one()
    assert "'bitcoin'" in tsv
    assert "'ralli'" in tsv
