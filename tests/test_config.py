from crypto_rag.config import Settings


def test_defaults_cover_both_assets_and_period_start():
    s = Settings(_env_file=None)
    assert s.symbols == ["BTCUSDT", "ETHUSDT"]
    assert s.start_date.isoformat() == "2025-01-20T00:00:00+00:00"
    assert s.embedding_dimensions == 1536
