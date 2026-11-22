"""Backtest of the 7-day buy signal and the baselines it is compared with (13.4).

Rules, the same for the model and every baseline:
  * a position opens at the close known at D 00:00 UTC on a signal day D and closes
    7 days later; while it is open new signals are ignored (no overlapping trades);
  * every entry and exit pays commission + slippage (settings, percent per side);
  * equity is marked to market daily; Sharpe uses daily equity returns x sqrt(365)
    (crypto trades every day) with a zero risk-free rate.
"""

import numpy as np
import pandas as pd

from crypto_rag.config import get_settings

HOLD_DAYS = 7


def cost_per_side() -> float:
    s = get_settings()
    return (s.commission_pct + s.slippage_pct) / 100


def daily_close(candles: pd.DataFrame) -> pd.Series:
    """Close of the candle that closes at D 00:00 (it opens at D-1h), indexed by D."""
    close = candles.close.asfreq("1h")
    at_midnight = close[close.index.hour == 23]
    at_midnight.index = at_midnight.index + pd.Timedelta(hours=1)
    return at_midnight.dropna()


def run(signal: pd.Series, close: pd.Series, cost: float | None = None) -> dict:
    """signal: bool by day. Returns {'equity': daily Series, 'trades': DataFrame}."""
    cost = cost_per_side() if cost is None else cost
    days = signal.index.intersection(close.index)
    signal, close = signal.reindex(days).fillna(False), close.reindex(days)
    equity = pd.Series(1.0, index=days)
    trades = []
    cash, i = 1.0, 0
    while i < len(days):
        exit_i = i + HOLD_DAYS
        if signal.iloc[i] and exit_i < len(days):
            entry = close.iloc[i]
            equity.iloc[i] = cash
            held = cash * (1 - cost)
            for j in range(i + 1, exit_i + 1):
                equity.iloc[j] = held * close.iloc[j] / entry
            cash = equity.iloc[exit_i] * (1 - cost)
            equity.iloc[exit_i] = cash
            trades.append({
                "entry": days[i], "exit": days[exit_i],
                "return": (close.iloc[exit_i] / entry) * (1 - cost) ** 2 - 1,
            })  # fmt: skip
            i = exit_i
        else:
            equity.iloc[i] = cash
            i += 1
    return {"equity": equity, "trades": pd.DataFrame(trades, columns=["entry", "exit", "return"])}


def buy_and_hold(close: pd.Series, cost: float | None = None) -> dict:
    cost = cost_per_side() if cost is None else cost
    equity = (close / close.iloc[0]) * (1 - cost)
    equity.iloc[-1] *= 1 - cost
    trade = {"entry": close.index[0], "exit": close.index[-1], "return": equity.iloc[-1] - 1}
    return {"equity": equity, "trades": pd.DataFrame([trade])}


def metrics(result: dict) -> dict[str, float]:
    equity, trades = result["equity"], result["trades"]
    daily = equity.pct_change().dropna()
    sd = daily.std()
    return {
        "total_return": float(equity.iloc[-1] / equity.iloc[0] - 1),
        "max_drawdown": float((equity / equity.cummax() - 1).min()),
        "sharpe": float(daily.mean() / sd * np.sqrt(365)) if sd > 0 else 0.0,
        "win_rate": float((trades["return"] > 0).mean()) if len(trades) else float("nan"),
        "n_trades": float(len(trades)),
    }


def random_entries(
    days: pd.DatetimeIndex, rate: float, close: pd.Series, seed: int, sims: int = 1000
) -> dict[str, float]:
    """Mean metrics of strategies that buy on random days with the model's signal rate."""
    rng = np.random.default_rng(seed)
    rows = [
        metrics(run(pd.Series(rng.random(len(days)) < rate, index=days), close))
        for _ in range(sims)
    ]
    return {k: float(np.nanmean([r[k] for r in rows])) for k in rows[0]}
