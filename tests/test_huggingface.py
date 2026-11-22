from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from crypto_rag.ingestion import huggingface as hf

MF = hf.DATASETS["maryamfakhari"]
KL = hf.DATASETS["katelabs"]


def _maryamfakhari_df():
    return pd.DataFrame({
        "id": [1, 2, 3],
        "published_on": ["2024-03-05 14:00:00.000000", "2024-03-05 15:00:00.000000", "2019-12-31 23:00:00.000000"],
        "title": ["Bitcoin ETF sees record inflows", "Solana upgrade ships", "Ether falls before new year"],
        "body": ["Spot funds bought BTC...", "Nothing about majors here", "ETH slid 3%"],
        "url": ["https://www.coindesk.com/a/?utm=x", "https://decrypt.co/b", "https://decrypt.co/c"],
        "tags": ["Bitcoin|ETF", "Solana", "Ethereum"],
        "categories": ["BTC|MARKET", "SOL", "ETH"],
        "source": ["coindesk", "decrypt", "decrypt"],
        "upvotes": [0, 0, 0],
        "downvotes": [0, 0, 0],
    })  # fmt: skip


def test_publisher_key():
    assert hf.publisher_key("AMBCrypto") == "ambcrypto"
    assert hf.publisher_key("Crypto Economy") == "crypto-economy"
    assert hf.publisher_key("") == "unknown"


def test_maryamfakhari_rows_keep_btc_eth_in_utc():
    rows = list(hf.rows_maryamfakhari(_maryamfakhari_df(), MF, "sha"))
    assert [r["id"] for r in rows] == ["hf:maryamfakhari:1", "hf:maryamfakhari:3"]  # Solana dropped
    first = rows[0]
    assert first["published_at"] == datetime(2024, 3, 5, 14, tzinfo=UTC)
    assert first["url"] == "https://www.coindesk.com/a"
    assert first["source"] == "coindesk" and first["channel"] == "hf"
    assert first["raw"]["dataset"] == MF.repo and first["raw"]["hf_id"] == 1


def test_katelabs_rows_use_coins_or_dictionary():
    df = pd.DataFrame({
        "id": ["a", "b", "c"],
        "published_at": pd.to_datetime(["2026-02-17 10:00", "2026-02-17 11:00", "2026-02-17 12:00"], utc=True),
        "ingested_at": pd.to_datetime(["2026-02-17 10:15"] * 3, utc=True),
        "source": ["AMBCrypto", "NewsBTC", "UToday"],
        "domain": ["ambcrypto.com", "newsbtc.com", "u.today"],
        "url": ["https://ambcrypto.com/x", "https://newsbtc.com/y", "https://u.today/z"],
        "headline": ["Market wrap: majors steady", "XRP rallies", "Ethereum gas fees fall"],
        "coins": [np.array(["BTC"]), np.array(["XRP"]), np.array([], dtype=object)],
        "news_type": ["reactive"] * 3,
        "score": [0.4, 0.1, 0.2],
    })  # fmt: skip
    rows = list(hf.rows_katelabs(df, KL, "sha"))
    assert [r["source"] for r in rows] == ["ambcrypto", "utoday"]  # BTC by coins, ETH by headline
    assert rows[0]["tags"] == ["BTC"] and rows[0]["body_text"] is None


@pytest.mark.integration
def test_import_is_idempotent_and_skips_known_urls(engine, tmp_path, monkeypatch):
    path = tmp_path / "mf.parquet"
    _maryamfakhari_df().to_parquet(path)
    monkeypatch.setattr(hf, "download", lambda ds: (path, "sha"))
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM articles WHERE channel = 'hf' OR url = 'https://www.coindesk.com/a'")
        )
    stats = hf.import_dataset(engine, "maryamfakhari")
    assert stats["inserted"] == 1 and stats["before_start"] == 1
    assert hf.import_dataset(engine, "maryamfakhari")["inserted"] == 0
    with engine.connect() as conn:
        channel = conn.execute(
            text("SELECT channel FROM articles WHERE url = 'https://www.coindesk.com/a'")
        ).scalar_one()
    assert channel == "hf"
