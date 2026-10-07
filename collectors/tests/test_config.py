"""Tests for the collector configuration loader.

The helpers are exercised directly rather than by reloading the module and
inspecting its constants. importlib.reload re-executes the module body, which
rebinds ConfigError to a *new* class object — so `pytest.raises(ConfigError)`
holds a stale reference and never matches the exception actually raised. Testing
the functions avoids that trap entirely, and they are where the logic lives.
"""

import pytest

from collectors.config import ConfigError, _int_env, _optional_env


@pytest.mark.parametrize("raw", ["", "   "], ids=["empty", "whitespace"])
def test_optional_blank_is_absent(monkeypatch, raw):
    monkeypatch.setenv("SOME_KEY", raw)
    assert _optional_env("SOME_KEY") is None


def test_optional_unset_is_absent(monkeypatch):
    monkeypatch.delenv("SOME_KEY", raising=False)
    assert _optional_env("SOME_KEY") is None


def test_unreplaced_template_value_is_treated_as_absent(monkeypatch):
    """.env is copied from .env.example, where optional keys read `your_...`.

    That string is truthy, so sending it as a credential earns HTTP 401 — while
    sending no key at all works on CoinGecko's free tier. An unedited
    placeholder is therefore strictly worse than nothing, and must not count as
    a key. This exact case produced a live 401 before the guard existed.
    """
    monkeypatch.setenv("COINGECKO_API_KEY", "your_coingecko_api_key")
    assert _optional_env("COINGECKO_API_KEY") is None


def test_real_key_is_kept(monkeypatch):
    monkeypatch.setenv("COINGECKO_API_KEY", "CG-abc123")
    assert _optional_env("COINGECKO_API_KEY") == "CG-abc123"


def test_key_is_stripped(monkeypatch):
    monkeypatch.setenv("COINGECKO_API_KEY", "  CG-abc123  ")
    assert _optional_env("COINGECKO_API_KEY") == "CG-abc123"


def test_int_setting_parsed(monkeypatch):
    monkeypatch.setenv("SOME_INTERVAL", "60")
    assert _int_env("SOME_INTERVAL", 300) == 60


def test_negative_int_is_parsed(monkeypatch):
    monkeypatch.setenv("SOME_INTERVAL", "-1")
    assert _int_env("SOME_INTERVAL", 300) == -1


@pytest.mark.parametrize("raw", ["", "  "], ids=["empty", "whitespace"])
def test_int_blank_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setenv("SOME_INTERVAL", raw)
    assert _int_env("SOME_INTERVAL", 300) == 300


def test_int_unset_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("SOME_INTERVAL", raising=False)
    assert _int_env("SOME_INTERVAL", 300) == 300


@pytest.mark.parametrize("raw", ["5m", "abc", "1.5"], ids=["suffix", "words", "float"])
def test_malformed_int_raises_rather_than_defaulting(monkeypatch, raw):
    """A typo must fail loudly; silently using the default looks like it applied."""
    monkeypatch.setenv("SOME_INTERVAL", raw)
    with pytest.raises(ConfigError, match="must be an integer"):
        _int_env("SOME_INTERVAL", 300)


def test_module_constants_use_the_optional_helper():
    """Guards the wiring, not just the helper: the key must be None or a real value."""
    from collectors import config

    assert config.COINGECKO_API_KEY is None or not config.COINGECKO_API_KEY.startswith("your_")
