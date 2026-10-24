"""get_market_stats: descriptive statistics for a symbol over a window.

All inputs are cut at `as_of`: a candle is visible only after it has closed,
a funding rate only after its timestamp.
"""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from crypto_rag.tools.base import IdAllocator, Metric, load_candles, metric

NORM_LOOKBACK = timedelta(days=365)


def _funding(engine: Engine, symbol: str, at: datetime) -> float | None:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT rate::float8 FROM funding_rates WHERE symbol = :s AND ts <= :t "
                "ORDER BY ts DESC LIMIT 1"
            ),
            {"s": symbol, "t": at},
        ).scalar()


def _window_returns(closes: pd.Series, hours: int) -> pd.Series:
    """Return over every `hours`-long window ending at each hour (percent)."""
    return (closes / closes.shift(hours) - 1) * 100


def get_market_stats(
    engine: Engine,
    symbol: str,
    from_ts: datetime,
    to_ts: datetime,
    as_of: datetime,
    ids: IdAllocator | None = None,
) -> list[Metric]:
    ids = ids or IdAllocator()
    to_ts = min(to_ts, as_of)
    window = load_candles(engine, symbol, from_ts, to_ts, as_of)
    if window.empty:
        return []
    hours = len(window)
    desc_window = f"{symbol} {window.ts.iloc[0]:%Y-%m-%d %H:%M}–{(window.ts.iloc[-1] + timedelta(hours=1)):%Y-%m-%d %H:%M} UTC"
    start_price, end_price = window.open.iloc[0], window.close.iloc[-1]
    change = (end_price / start_price - 1) * 100
    log_ret = np.log(window.close / window.close.shift(1).fillna(window.open.iloc[0]))
    sharp_idx = log_ret.abs().idxmax()
    sharp_pct = (np.exp(log_ret[sharp_idx]) - 1) * 100
    sharp_ts = window.ts[sharp_idx]
    vol = log_ret.std(ddof=1) * 100 if hours > 1 else None

    # Baseline: the 30 days before the window.
    base = load_candles(engine, symbol, from_ts - timedelta(days=30), from_ts, as_of)
    vol_ratio = window.volume.mean() / base.volume.mean() if not base.empty else None
    taker_share = window.taker_buy_volume.sum() / window.volume.sum() * 100

    f_end = _funding(engine, symbol, to_ts)
    f_start = _funding(engine, symbol, from_ts)

    out = [
        metric(ids, "price_start", start_price, "USDT", 2, f"Price at the start of {desc_window}"),
        metric(ids, "price_end", end_price, "USDT", 2, f"Price at the end of {desc_window}"),
        metric(ids, "price_change_pct", change, "%", 2, f"Price change over {desc_window}"),
        metric(
            ids, "sharpest_hour_pct", sharp_pct, "%", 2,
            f"Largest single-hour move, in the hour starting {sharp_ts:%Y-%m-%d %H:%M} UTC",
            ts=sharp_ts.isoformat(),
        ),
        metric(ids, "volatility_hourly_pct", vol, "%", 3,
               "Volatility: standard deviation of hourly log returns in the window"),
        metric(ids, "volume_vs_30d", vol_ratio, "x", 2,
               "Average hourly volume in the window divided by the 30-day average before it"),
        metric(ids, "taker_buy_share_pct", taker_share, "%", 1,
               "Share of volume from aggressive (taker) buys"),
        metric(ids, "funding_rate_pct", None if f_end is None else f_end * 100, "%", 4,
               "Latest perpetual funding rate at the end of the window"),
        metric(ids, "funding_change_pp",
               None if f_end is None or f_start is None else (f_end - f_start) * 100, "pp", 4,
               "Change in funding rate over the window (percentage points)"),
    ]  # fmt: skip

    # Comparison with the norm over the preceding year.
    hist = load_candles(engine, symbol, to_ts - NORM_LOOKBACK, to_ts, as_of)
    if len(hist) > hours * 2:
        closes = hist.set_index("ts").close
        rets = _window_returns(closes, hours).dropna()
        current = rets.iloc[-1]
        earlier = rets.iloc[:-1]
        if current < 0:
            worse = earlier[earlier <= current]
            days = (
                (rets.index[-1] - worse.index[-1]).days
                if not worse.empty
                else (rets.index[-1] - rets.index[0]).days
            )
            out.append(metric(
                ids, "largest_drop_days", days, "days", 0,
                f"The {hours}h drop is the largest in this many days",
            ))  # fmt: skip
        else:
            better = earlier[earlier >= current]
            days = (
                (rets.index[-1] - better.index[-1]).days
                if not better.empty
                else (rets.index[-1] - rets.index[0]).days
            )
            out.append(metric(
                ids, "largest_rise_days", days, "days", 0,
                f"The {hours}h rise is the largest in this many days",
            ))  # fmt: skip
        hist_lr = np.log(closes / closes.shift(1)).dropna()
        rolling_vol = hist_lr.rolling(max(hours, 2)).std().dropna() * 100
        if vol is not None and len(rolling_vol) > 1:
            pct = (rolling_vol < vol).mean() * 100
            out.append(metric(ids, "volatility_percentile", pct, "%", 0,
                              "Percentile of the window's volatility among same-length windows in the past year"))  # fmt: skip
    return out
