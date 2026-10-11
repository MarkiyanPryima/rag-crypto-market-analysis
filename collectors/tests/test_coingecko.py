"""Tests for the CoinGecko client.

The fixture is a real response recorded from
/api/v3/coins/markets on 7 Oct 2026, so these assert against the API's actual
shape rather than an invented one. If CoinGecko changes a field name, the
fixture goes stale and should be re-recorded — but a test written against a
guessed payload would never have caught the change at all.
"""

import json
import pathlib
from datetime import UTC, datetime

import pytest

from collectors import coingecko

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "coingecko_markets.json"
TRACKED = [
    "bitcoin",
    "ethereum",
    "solana",
    "binancecoin",
    "ripple",
    "cardano",
    "dogecoin",
    "polkadot",
]


@pytest.fixture
def markets() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def test_fixture_covers_every_tracked_coin(markets):
    assert {e["id"] for e in markets} == set(TRACKED)


def test_normalise_maps_every_column(markets):
    btc = next(e for e in markets if e["id"] == "bitcoin")

    result = coingecko.normalise(btc)
    scraped_at = result.pop("scraped_at")

    assert result == {
        "symbol": "BTC",
        "slug": "bitcoin",
        "price_usd": btc["current_price"],
        "market_cap": btc["market_cap"],
        "volume_24h": btc["total_volume"],
        "change_24h": btc["price_change_percentage_24h"],
        "source": "coingecko",
    }
    assert scraped_at


def test_scraped_at_is_stamped_at_fetch_and_timezone_aware(markets):
    """The worker must not fall back to the column default.

    scraped_at defaults to now() at INSERT time, so a backlogged queue would
    stamp every message with the drain time and flatten the price history into
    one instant. Stamping at fetch keeps the observation time truthful however
    long the message waits.
    """
    result = coingecko.normalise(markets[0])
    stamped = datetime.fromisoformat(result["scraped_at"])

    assert stamped.tzinfo is not None, "naive timestamp would be misread as local time"
    assert abs((datetime.now(UTC) - stamped).total_seconds()) < 60


def test_symbol_is_uppercased(markets):
    """tokens.symbol has CHECK (symbol = upper(symbol)); CoinGecko sends lowercase."""
    for entry in markets:
        assert entry["symbol"].islower()
        assert coingecko.normalise(entry)["symbol"] == entry["symbol"].upper()


def test_slug_is_the_coingecko_id_not_the_symbol(markets):
    """BNB's id is 'binancecoin' — deriving the slug from the symbol would break."""
    bnb = next(e for e in markets if e["symbol"] == "bnb")
    assert coingecko.normalise(bnb)["slug"] == "binancecoin"


def test_volume_comes_from_total_volume(markets):
    """The field is total_volume; there is no volume_24h in the payload."""
    entry = markets[0]
    assert "volume_24h" not in entry
    assert coingecko.normalise(entry)["volume_24h"] == entry["total_volume"]


def test_all_fixture_entries_normalise(markets):
    assert all(coingecko.normalise(e) is not None for e in markets)


@pytest.mark.parametrize(
    "price",
    [0, -1, 0.0, "not a number", None],
    ids=["zero", "negative", "zero-float", "non-numeric", "null"],
)
def test_unusable_price_is_dropped(markets, price):
    """Validated here so the worker's batch insert is never aborted by a CHECK."""
    entry = dict(markets[0], current_price=price)
    assert coingecko.normalise(entry) is None


def test_missing_identity_is_dropped(markets):
    assert coingecko.normalise(dict(markets[0], id=None)) is None
    assert coingecko.normalise(dict(markets[0], symbol=None)) is None


def test_null_change_is_preserved_not_dropped(markets):
    """change_24h is nullable; a missing percentage must not cost us the price."""
    result = coingecko.normalise(dict(markets[0], price_change_percentage_24h=None))
    assert result is not None
    assert result["change_24h"] is None


def test_out_of_range_change_is_nulled_but_row_kept(markets):
    """NUMERIC(8,4) cannot hold a five-digit percentage; the price still can."""
    result = coingecko.normalise(dict(markets[0], price_change_percentage_24h=15000.0))
    assert result is not None
    assert result["change_24h"] is None
    assert result["price_usd"] == markets[0]["current_price"]


def test_fetch_prices_skips_bad_entries(monkeypatch, markets):
    poisoned = [dict(markets[0], current_price=0), *markets[1:]]
    monkeypatch.setattr(coingecko, "fetch_markets", lambda _ids: poisoned)

    prices = coingecko.fetch_prices(TRACKED)

    assert len(prices) == len(markets) - 1
    assert markets[0]["id"] not in {p["slug"] for p in prices}


def test_fetch_markets_short_circuits_on_empty_input(monkeypatch):
    """No coins means no HTTP call at all."""

    def explode(*_args, **_kwargs):
        raise AssertionError("should not issue a request")

    monkeypatch.setattr(coingecko.requests, "get", explode)
    assert coingecko.fetch_markets([]) == []


def test_rejected_key_gives_an_actionable_error(monkeypatch):
    """A bad key 401s on an endpoint that is public without one — say so."""

    class Response:
        status_code = 401

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(coingecko, "COINGECKO_API_KEY", "CG-invalid")
    monkeypatch.setattr(coingecko.requests, "get", lambda *_a, **_k: Response())

    with pytest.raises(coingecko.CoinGeckoError, match="Unset it to use the free tier"):
        coingecko.fetch_markets(["bitcoin"])


def test_401_without_a_key_is_not_misreported(monkeypatch):
    """With no key configured, a 401 is some other problem — let it surface as HTTP."""

    class Response:
        status_code = 401

        @staticmethod
        def raise_for_status():
            raise coingecko.requests.HTTPError("401 Unauthorized")

    monkeypatch.setattr(coingecko, "COINGECKO_API_KEY", None)
    monkeypatch.setattr(coingecko.requests, "get", lambda *_a, **_k: Response())

    with pytest.raises(coingecko.requests.HTTPError):
        coingecko.fetch_markets(["bitcoin"])


def test_non_list_payload_raises(monkeypatch):
    """A rate-limit body is a dict; without this it becomes a confusing TypeError."""

    class Response:
        status_code = 429

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"status": {"error_code": 429, "error_message": "rate limited"}}

    monkeypatch.setattr(coingecko.requests, "get", lambda *_a, **_k: Response())

    with pytest.raises(coingecko.CoinGeckoError, match="expected a list"):
        coingecko.fetch_markets(["bitcoin"])
