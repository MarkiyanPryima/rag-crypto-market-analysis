from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from crypto_rag.forecasting import backtest, train
from crypto_rag.forecasting.garch import garch_daily_vol

DAYS = pd.date_range("2025-01-01", periods=30, freq="D", tz="UTC")


def test_trades_do_not_overlap_and_pay_costs():
    close = pd.Series(np.linspace(100, 129, 30), index=DAYS)
    signal = pd.Series(True, index=DAYS)
    out = backtest.run(signal, close, cost=0.001)
    trades = out["trades"]
    assert len(trades) == 4  # days 0, 7, 14, 21; a trade from day 28 would end after the data
    assert (trades.entry.iloc[1:].to_numpy() >= trades.exit.iloc[:-1].to_numpy()).all()
    first = trades.iloc[0]
    assert first["return"] == pytest.approx(close.iloc[7] / close.iloc[0] * 0.999**2 - 1)
    assert out["equity"].iloc[7] == pytest.approx(1 + first["return"])


def test_equity_on_entry_day_is_the_cash_before_the_trade():
    close = pd.Series([100.0] * 10 + [50.0] * 20, index=DAYS)
    signal = pd.Series([False] * 10 + [True] + [False] * 19, index=DAYS)
    eq = backtest.run(signal, close, cost=0.0)["equity"]
    assert eq.iloc[10] == 1.0 and eq.iloc[17] == pytest.approx(1.0)
    loss = pd.Series([100.0] * 5 + [50.0] * 25, index=DAYS)
    m = backtest.metrics(backtest.run(pd.Series([True] + [False] * 29, index=DAYS), loss, cost=0.0))
    assert m["total_return"] == pytest.approx(-0.5) and m["sharpe"] < 0


def test_no_signal_keeps_cash_and_metrics_are_flat():
    close = pd.Series(np.linspace(100, 50, 30), index=DAYS)
    m = backtest.metrics(backtest.run(pd.Series(False, index=DAYS), close))
    assert m["total_return"] == 0 and m["max_drawdown"] == 0 and m["n_trades"] == 0


def test_daily_close_is_the_candle_closing_at_midnight():
    idx = pd.date_range("2025-01-01", periods=72, freq="1h", tz="UTC")
    candles = pd.DataFrame({"close": np.arange(72.0)}, index=idx)
    close = backtest.daily_close(candles)
    assert close[pd.Timestamp("2025-01-02", tz="UTC")] == 23.0  # opened 01-01 23:00


def test_qlike_is_zero_for_a_perfect_forecast():
    v = pd.Series([0.01, 0.02, 0.03])
    assert train.qlike(v, v) == pytest.approx(0)
    assert train.qlike(v, v * 2) > 0


def _dataset(n_days: int, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = [date(2023, 1, 1) + timedelta(days=i) for i in range(n_days)]
    df = pd.DataFrame(rng.normal(size=(n_days, len(train.columns("ETHUSDT", "price+news")))),
                      columns=train.columns("ETHUSDT", "price+news"), index=idx)  # fmt: skip
    df["return_7d_pct"] = rng.normal(0, 5, n_days)
    df["realized_vol_next_day"] = rng.uniform(0.01, 0.05, n_days)
    return df


def test_walk_forward_trains_only_on_known_targets(monkeypatch):
    df = _dataset(500)
    seen = []
    real_fit = train.fit
    monkeypatch.setattr(
        train, "fit", lambda t, cols: seen.append(t.index.max()) or real_fit(t, cols)
    )
    preds = train.walk_forward(
        df, train.columns("ETHUSDT", "price"), date(2024, 1, 1), date(2024, 3, 31)
    )
    assert seen == [date(2023, 12, 25), date(2024, 1, 25), date(2024, 2, 23)]  # 2024 is a leap year
    assert preds.index.min() == date(2024, 1, 1) and preds.index.max() == date(2024, 3, 31)
    assert preds.prob.between(0, 1).all() and (preds.vol > 0).all()


def test_garch_forecast_uses_only_past_returns():
    rng = np.random.default_rng(1)
    idx = pd.date_range("2024-01-01", periods=24 * 150, freq="1h", tz="UTC")
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.005, len(idx)))), index=idx)
    days = pd.date_range("2024-05-01", "2024-05-10", freq="D", tz="UTC")
    base = garch_daily_vol(close, days)
    assert base.notna().all() and base.between(0.005, 0.1).all()
    later = close.copy()
    later[later.index >= days[5]] *= np.exp(
        np.cumsum(rng.normal(0, 0.05, (later.index >= days[5]).sum()))
    )
    changed = garch_daily_vol(later, days)
    pd.testing.assert_series_equal(base[:6], changed[:6])
