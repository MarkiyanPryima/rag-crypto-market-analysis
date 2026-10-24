"""Synthetic market data with known answers."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, text

T0 = datetime(2025, 6, 1, tzinfo=UTC)


def insert_candles(engine: Engine, symbol: str, closes: list[float], start: datetime = T0,
                   volumes: list[float] | None = None, taker_share: float = 0.6) -> None:  # fmt: skip
    rows = []
    prev = closes[0]
    for i, c in enumerate(closes):
        v = volumes[i] if volumes else 10.0
        rows.append(
            {"s": symbol, "ts": start + timedelta(hours=i), "o": prev, "h": max(prev, c), "l": min(prev, c),
             "c": c, "v": v, "qv": v * c, "n": 100, "tb": v * taker_share, "tbq": v * taker_share * c}
        )  # fmt: skip
        prev = c
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO candles VALUES (:s, :ts, :o, :h, :l, :c, :v, :qv, :n, :tb, :tbq) "
                "ON CONFLICT (symbol, ts) DO UPDATE SET open = excluded.open, high = excluded.high, "
                "low = excluded.low, close = excluded.close, volume = excluded.volume, "
                "taker_buy_volume = excluded.taker_buy_volume"
            ),
            rows,
        )


def reset(engine: Engine, symbol: str) -> None:
    with engine.begin() as conn:
        for t in ("candles", "funding_rates", "episodes"):
            conn.execute(text(f"DELETE FROM {t} WHERE symbol = :s"), {"s": symbol})  # noqa: S608
