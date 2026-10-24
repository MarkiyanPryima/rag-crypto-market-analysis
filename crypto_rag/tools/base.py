"""Common result type for numeric tools.

Every number the language model may quote is a `Metric` with a stable id
([m1], [f1], ...). The model only references ids; verify.py checks that each
number in the answer matches a metric at the metric's rounding.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy import Engine, text


@dataclass
class Metric:
    id: str
    name: str
    value: float | None
    unit: str
    decimals: int
    description: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def display(self) -> str:
        if self.value is None:
            return "n/a"
        return f"{self.value:,.{self.decimals}f}{self.unit if self.unit == '%' else ' ' + self.unit if self.unit else ''}"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "display": self.display}


class IdAllocator:
    """Hands out ids per prefix so ids stay unique across tool calls in one query."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def next(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}{self._counters[prefix]}"


def metric(
    ids: IdAllocator,
    name: str,
    value: float | None,
    unit: str,
    decimals: int,
    description: str,
    prefix: str = "m",
    **extra: Any,
) -> Metric:
    rounded = None if value is None or pd.isna(value) else round(float(value), decimals)
    return Metric(ids.next(prefix), name, rounded, unit, decimals, description, extra)


def load_candles(
    engine: Engine, symbol: str, from_ts: datetime, to_ts: datetime, as_of: datetime
) -> pd.DataFrame:
    """Candles that had CLOSED by `as_of` (ts + 1h <= as_of) and open within [from_ts, to_ts)."""
    sql = text(
        "SELECT ts, open::float8, high::float8, low::float8, close::float8, volume::float8, "
        "quote_volume::float8, trades, taker_buy_volume::float8 "
        "FROM candles WHERE symbol = :s AND ts >= :f AND ts < :t "
        "AND ts + interval '1 hour' <= :as_of ORDER BY ts"
    )
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"s": symbol, "f": from_ts, "t": to_ts, "as_of": as_of})
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def price_at(engine: Engine, symbol: str, at: datetime, as_of: datetime) -> float | None:
    """Close of the last candle that closed at or before `at` (and before as_of)."""
    limit = min(at, as_of)
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT close::float8 FROM candles WHERE symbol = :s "
                "AND ts + interval '1 hour' <= :t ORDER BY ts DESC LIMIT 1"
            ),
            {"s": symbol, "t": limit},
        ).scalar()
