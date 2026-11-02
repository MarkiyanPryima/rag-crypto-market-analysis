from crypto_rag.config import Settings


def test_defaults_cover_both_assets_and_period_start():
    s = Settings(_env_file=None)
    assert s.symbols == ["BTCUSDT", "ETHUSDT"]
    assert s.start_date.isoformat() == "2025-01-20T00:00:00+00:00"
    assert s.embedding_dimensions == 1536


def test_guardian_tags_parse_from_any_env_form(monkeypatch):
    expected = ["technology/cryptocurrencies", "technology/bitcoin"]
    for raw in (
        "technology/cryptocurrencies,technology/bitcoin",
        '["technology/cryptocurrencies","technology/bitcoin"]',
        "[technology/cryptocurrencies,technology/bitcoin]",  # JSON after a shell stripped quotes
    ):
        monkeypatch.setenv("GUARDIAN_TAGS", raw)
        assert Settings(_env_file=None).guardian_tags == expected
