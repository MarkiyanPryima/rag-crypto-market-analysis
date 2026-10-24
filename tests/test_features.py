import numpy as np
import pandas as pd

from crypto_rag.forecasting.features import compute_features, compute_targets

START = pd.Timestamp("2025-01-01", tz="UTC")


def _frames(n_hours: int, seed: int = 0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(START, periods=n_hours, freq="1h")
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n_hours)))
    candles = pd.DataFrame({"close": close, "volume": rng.uniform(5, 15, n_hours)}, index=idx)
    candles["taker_buy_volume"] = candles.volume * 0.5
    funding = pd.Series(rng.normal(0.0001, 0.00005, n_hours // 8), index=idx[::8][: n_hours // 8])
    news = pd.DataFrame({
        "published_at": idx[::5], "topic": "market", "tone": "positive",
        "assets": [["BTC"]] * len(idx[::5]),
    })  # fmt: skip
    return candles, funding, news


def test_data_after_day_does_not_change_features():
    candles, funding, news = _frames(24 * 60)
    day = pd.DatetimeIndex([START + pd.Timedelta(days=40)])
    full = compute_features(candles, funding, news, "BTC", day)
    cut = day[0]
    truncated = compute_features(
        candles[candles.index < cut],
        funding[funding.index <= cut],
        news[news.published_at < cut],
        "BTC",
        day,
    )
    # Scramble everything after D: features of D must not move.
    noisy = candles.copy()
    noisy.loc[noisy.index >= cut, ["close", "volume"]] *= 7.0
    scrambled = compute_features(noisy, funding, news, "BTC", day)
    pd.testing.assert_frame_equal(full, truncated)
    pd.testing.assert_frame_equal(full, scrambled)


def test_targets_look_forward_only_from_day():
    candles, _, _ = _frames(24 * 60)
    day = pd.DatetimeIndex([START + pd.Timedelta(days=40)])
    t = compute_targets(candles, day)
    c0 = candles.close[day[0] - pd.Timedelta(hours=1)]
    c7 = candles.close[day[0] + pd.Timedelta(days=7) - pd.Timedelta(hours=1)]
    assert t.return_7d_pct.iloc[0] == (c7 / c0 - 1) * 100
    lr = np.log(candles.close / candles.close.shift(1))
    window = lr[(lr.index >= day[0]) & (lr.index < day[0] + pd.Timedelta(days=1))]
    assert np.isclose(t.realized_vol_next_day.iloc[0], np.sqrt((window**2).sum()))
