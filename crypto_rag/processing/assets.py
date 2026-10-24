"""Asset dictionary: which of the tracked coins a piece of text is about."""

import re

ASSET_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("bitcoin", "bitcoins", "btc", "xbt"),
    "ETH": ("ethereum", "ether", "eth"),
}

# Symbol used in candles / forecasts for each asset.
ASSET_SYMBOL = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
SYMBOL_ASSET = {v: k for k, v in ASSET_SYMBOL.items()}

_PATTERNS = {
    asset: re.compile(r"\b(?:" + "|".join(map(re.escape, aliases)) + r")(?:'s)?\b", re.IGNORECASE)
    for asset, aliases in ASSET_ALIASES.items()
}


def detect_assets(text: str) -> list[str]:
    """Sorted list of assets mentioned; word boundaries keep 'whether' from matching 'ether'."""
    return sorted(asset for asset, pattern in _PATTERNS.items() if pattern.search(text or ""))
