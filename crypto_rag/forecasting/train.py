"""Forecasting models, walk-forward evaluation and the daily forecast (13.3–13.5).

Targets (13.2): (a) the 7-day return exceeds settings.buy_threshold_pct; (b) next-day
realised volatility. Models: LightGBM classifier for (a) and regressor for (b, on
log volatility), each with price features only and with price + news features.
Parameters are fixed in advance (no search), seed from settings.

Periods (by feature date): training 2020–2023, tuning 2024, test 2025-01-01 → end.
Walk-forward: at the first day of every tuning/test month the models are refitted
on all rows whose target was already known (date + 7 days <= month start), which
also purges the last 7 days before each period boundary. The probability threshold
of the signal and the production variant are chosen on the tuning period only.

Usage:
    python -m crypto_rag.forecasting.train evaluate   # results into evaluation/results
    python -m crypto_rag.forecasting.train forecast   # today's forecast into `forecasts`
"""

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier, LGBMRegressor
from sklearn.metrics import brier_score_loss, roc_auc_score
from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert

from crypto_rag.config import get_settings, setup_logging
from crypto_rag.db.models import Forecast
from crypto_rag.db.session import get_engine
from crypto_rag.forecasting import backtest
from crypto_rag.forecasting.features import (
    BTC_FEATURES,
    NEWS_FEATURES,
    PRICE_FEATURES,
    load_candles_df,
    load_dataset,
)
from crypto_rag.forecasting.garch import garch_daily_vol

logger = logging.getLogger(__name__)

TUNE_START, TEST_START = date(2024, 1, 1), date(2025, 1, 1)
LABEL_DAYS = 7  # the 7-day return of day D is known at D + 7 days 00:00
VARIANTS = ("price", "price+news")
RESULTS = Path("evaluation/results")
THRESHOLDS = np.round(np.arange(0.30, 0.81, 0.01), 2)
MIN_TUNE_TRADES = 5

PARAMS = {
    "n_estimators": 300, "learning_rate": 0.03, "num_leaves": 15, "min_child_samples": 30,
    "subsample": 0.8, "subsample_freq": 1, "colsample_bytree": 0.8, "verbose": -1,
}  # fmt: skip


def columns(symbol: str, variant: str) -> list[str]:
    cols = PRICE_FEATURES + (BTC_FEATURES if symbol != "BTCUSDT" else [])
    return cols + (NEWS_FEATURES if variant == "price+news" else [])


def labels(df: pd.DataFrame) -> pd.Series:
    return (
        (df.return_7d_pct > get_settings().buy_threshold_pct)
        .astype(float)
        .where(df.return_7d_pct.notna())
    )


def _clf() -> LGBMClassifier:
    return LGBMClassifier(**PARAMS, random_state=get_settings().random_seed)


def _reg() -> LGBMRegressor:
    return LGBMRegressor(**PARAMS, random_state=get_settings().random_seed)


def known_by(df: pd.DataFrame, moment: date) -> pd.DataFrame:
    """Rows whose 7-day target is known at `moment` 00:00 UTC."""
    return df[(df.index <= moment - timedelta(days=LABEL_DAYS)) & df.return_7d_pct.notna()]


@dataclass
class Fitted:
    clf: LGBMClassifier
    reg: LGBMRegressor
    cols: list[str]


def fit(train: pd.DataFrame, cols: list[str]) -> Fitted:
    x = train[cols].astype(float)
    vol = train.realized_vol_next_day
    ok = vol.notna() & (vol > 0)
    return Fitted(
        clf=_clf().fit(x, labels(train)),
        reg=_reg().fit(x[ok], np.log(vol[ok])),
        cols=cols,
    )


def predict(m: Fitted, rows: pd.DataFrame) -> pd.DataFrame:
    x = rows[m.cols].astype(float)
    return pd.DataFrame(
        {"prob": m.clf.predict_proba(x)[:, 1], "vol": np.exp(m.reg.predict(x))}, index=rows.index
    )


def walk_forward(df: pd.DataFrame, cols: list[str], start: date, end: date) -> pd.DataFrame:
    """Out-of-sample predictions for start..end, refitting at every month start."""
    out = []
    for month in pd.date_range(start.replace(day=1), end, freq="MS").date:
        nxt = (pd.Timestamp(month) + pd.offsets.MonthBegin(1)).date()
        rows = df[(df.index >= max(month, start)) & (df.index < nxt) & (df.index <= end)]
        if rows.empty:
            continue
        out.append(predict(fit(known_by(df, month), cols), rows))
    return pd.concat(out)


def qlike(true_vol: pd.Series, pred_vol: pd.Series) -> float:
    ratio = true_vol.pow(2) / pred_vol.pow(2)
    return float((ratio - np.log(ratio) - 1).mean())


def classification_metrics(y: pd.Series, prob: pd.Series) -> dict[str, float]:
    ok = y.notna()
    return {
        "auc": float(roc_auc_score(y[ok], prob[ok])),
        "brier": float(brier_score_loss(y[ok], prob[ok])),
        "base_rate": float(y[ok].mean()),
    }


def volatility_metrics(true_vol: pd.Series, pred_vol: pd.Series) -> dict[str, float]:
    ok = true_vol.notna() & pred_vol.notna() & (true_vol > 0)
    return {
        "mae_pct": float((true_vol[ok] - pred_vol[ok]).abs().mean() * 100),
        "qlike": qlike(true_vol[ok], pred_vol[ok]),
    }


def _ts_index(index: pd.Index) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(index)).tz_localize(UTC)


def choose_threshold(prob: pd.Series, close: pd.Series) -> float:
    """Probability threshold with the best tuning-period Sharpe (>= MIN_TUNE_TRADES trades)."""
    best, best_sharpe = 0.5, -np.inf
    for thr in THRESHOLDS:
        m = backtest.metrics(backtest.run(prob >= thr, close))
        if m["n_trades"] >= MIN_TUNE_TRADES and m["sharpe"] > best_sharpe:
            best, best_sharpe = float(thr), m["sharpe"]
    return best


@dataclass
class Selection:
    clf_variant: str
    reg_variant: str
    threshold: float
    tune: dict


def _symbol_data(engine: Engine, symbol: str) -> tuple[pd.DataFrame, pd.Series]:
    df = load_dataset(engine, symbol)
    close = backtest.daily_close(load_candles_df(engine, symbol))
    return df, close


def select(df: pd.DataFrame, symbol: str, close: pd.Series) -> Selection:
    """Production variant and threshold, from the tuning period only."""
    tune_end = TEST_START - timedelta(days=1)
    tune = df[(df.index >= TUNE_START) & (df.index <= tune_end)]
    stats, preds = {}, {}
    for v in VARIANTS:
        p = walk_forward(df, columns(symbol, v), TUNE_START, tune_end)
        preds[v] = p
        stats[v] = classification_metrics(labels(tune), p.prob) | volatility_metrics(
            tune.realized_vol_next_day, p.vol
        )
    clf_v = max(VARIANTS, key=lambda v: stats[v]["auc"])
    reg_v = min(VARIANTS, key=lambda v: stats[v]["qlike"])
    prob = preds[clf_v].prob
    prob.index = _ts_index(prob.index)
    return Selection(clf_v, reg_v, choose_threshold(prob, close), stats)


@lru_cache
def _selection(symbol: str) -> Selection:
    df, close = _symbol_data(get_engine(), symbol)
    return select(df, symbol, close)


def _contributions(model, x: pd.DataFrame) -> dict[str, float]:
    contrib = model.predict(x, pred_contrib=True)[0]
    return {c: round(float(v), 4) for c, v in zip(x.columns, contrib[:-1], strict=True) if v}


def forecast_rows(
    symbol: str, sel: Selection, models: dict[str, Fitted], rows: pd.DataFrame, version: str
) -> list[dict]:
    out = []
    for d in rows.index:
        row = rows.loc[[d]]
        clf, reg = models[sel.clf_variant], models[sel.reg_variant]
        x_c, x_r = row[clf.cols].astype(float), row[reg.cols].astype(float)
        out += [
            {"symbol": symbol, "date": d, "kind": "buy_signal",
             "value": float(clf.clf.predict_proba(x_c)[0, 1]),
             "contributions": _contributions(clf.clf, x_c),
             "model_version": f"{version}:{sel.clf_variant}:thr{sel.threshold:.2f}"},
            {"symbol": symbol, "date": d, "kind": "volatility",
             "value": float(np.exp(reg.reg.predict(x_r)[0])),
             "contributions": _contributions(reg.reg, x_r),
             "model_version": f"{version}:{sel.reg_variant}"},
        ]  # fmt: skip
    return out


def store(engine: Engine, rows: list[dict]) -> int:
    if not rows:
        return 0
    stmt = insert(Forecast).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="forecasts_unique_key",
        set_={"value": stmt.excluded.value, "contributions": stmt.excluded.contributions},
    )
    with engine.begin() as conn:
        conn.execute(stmt)
    return len(rows)


def daily_forecast(engine: Engine, day: date | None = None) -> int:
    """13.5: forecast for `day` (default today) from models fitted on data known at day 00:00."""
    day = day or datetime.now(UTC).date()
    n = 0
    for symbol in get_settings().symbols:
        df = load_dataset(engine, symbol)
        if day not in df.index:
            logger.warning("%s: no feature row for %s", symbol, day)
            continue
        sel = _selection(symbol)
        train = known_by(df, day)
        models = {v: fit(train, columns(symbol, v)) for v in {sel.clf_variant, sel.reg_variant}}
        n += store(engine, forecast_rows(symbol, sel, models, df.loc[[day]], "lgbm-daily"))
        logger.info("%s %s: forecast stored (%s)", symbol, day, sel)
    return n


def retrain(engine: Engine) -> None:
    """Weekly: recompute the tuning-period selection (models themselves refit daily)."""
    _selection.cache_clear()
    for symbol in get_settings().symbols:
        logger.info("%s selection: %s", symbol, _selection(symbol))


def evaluate(engine: Engine, store_forecasts: bool = True) -> pd.DataFrame:
    """Test-period table for all models and baselines; equity plot; walk-forward forecasts."""
    seed = get_settings().random_seed
    RESULTS.mkdir(parents=True, exist_ok=True)
    table, curves, summary = [], {}, {}
    for symbol in get_settings().symbols:
        df, close = _symbol_data(engine, symbol)
        sel = select(df, symbol, close)
        end = df.index.max()
        test = df[df.index >= TEST_START]
        y, true_vol = labels(test), test.realized_vol_next_day
        days = _ts_index(test.index)
        test_close = close.reindex(days).dropna()
        preds = {v: walk_forward(df, columns(symbol, v), TEST_START, end) for v in VARIANTS}

        for v, p in preds.items():
            prob = pd.Series(p.prob.to_numpy(), index=days)
            bt = backtest.run(prob >= sel.threshold, test_close)
            curves[(symbol, v)] = bt["equity"]
            table.append({"symbol": symbol, "target": "buy_signal", "model": f"LightGBM {v}",
                          **classification_metrics(y, p.prob), **backtest.metrics(bt)})  # fmt: skip
            table.append({"symbol": symbol, "target": "volatility", "model": f"LightGBM {v}",
                          **volatility_metrics(true_vol, p.vol)})  # fmt: skip
        bh = backtest.buy_and_hold(test_close)
        curves[(symbol, "buy&hold")] = bh["equity"]
        table.append({"symbol": symbol, "target": "buy_signal", "model": "buy & hold",
                      **backtest.metrics(bh)})  # fmt: skip
        rate = float((pd.Series(preds[sel.clf_variant].prob.to_numpy()) >= sel.threshold).mean())
        table.append({"symbol": symbol, "target": "buy_signal",
                      "model": f"random entries (rate {rate:.2f})",
                      **backtest.random_entries(test_close.index, rate, test_close, seed)})  # fmt: skip
        hourly = load_candles_df(engine, symbol).close
        garch = garch_daily_vol(hourly, days)
        table.append({"symbol": symbol, "target": "volatility", "model": "GARCH(1,1)",
                      **volatility_metrics(true_vol, pd.Series(garch.to_numpy(), index=test.index))})  # fmt: skip
        table.append({"symbol": symbol, "target": "volatility", "model": "mean of week",
                      **volatility_metrics(true_vol, test.rv_7d)})  # fmt: skip
        summary[symbol] = {"selection": sel.__dict__, "test": [str(test.index.min()), str(end)]}

        if store_forecasts:
            rows = []
            for month in pd.date_range(TEST_START, end, freq="MS").date:
                nxt = (pd.Timestamp(month) + pd.offsets.MonthBegin(1)).date()
                part = test[(test.index >= month) & (test.index < nxt)]
                train = known_by(df, month)
                models = {v: fit(train, columns(symbol, v)) for v in VARIANTS}
                rows += forecast_rows(symbol, sel, models, part, "lgbm-walkforward")
            store(engine, rows)

    result = pd.DataFrame(table)
    result.to_csv(RESULTS / "forecast_test_metrics.csv", index=False, float_format="%.4f")
    (RESULTS / "forecast_summary.json").write_text(
        json.dumps(summary, indent=2, default=str, ensure_ascii=False) + "\n"
    )
    pd.DataFrame({f"{sym} {name}": eq for (sym, name), eq in curves.items()}).to_csv(
        RESULTS / "forecast_equity.csv", float_format="%.6f"
    )
    _plot(curves)
    return result


def _plot(curves: dict) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    symbols = sorted({s for s, _ in curves})
    fig, axes = plt.subplots(len(symbols), 1, figsize=(10, 4 * len(symbols)), sharex=True)
    for ax, symbol in zip(np.atleast_1d(axes), symbols, strict=True):
        for (s, name), eq in curves.items():
            if s == symbol:
                ax.plot(eq.index, eq.to_numpy(), label=name)
        ax.set_title(f"{symbol}: equity, test period")
        ax.axhline(1.0, color="grey", lw=0.5)
        ax.legend()
    fig.tight_layout()
    fig.savefig(RESULTS / "forecast_equity.png", dpi=150)
    plt.close(fig)


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["evaluate", "forecast"])
    parser.add_argument("--day", type=date.fromisoformat)
    args = parser.parse_args()
    engine = get_engine()
    if args.command == "evaluate":
        logger.info(
            "test period:\n%s", evaluate(engine).to_string(float_format=lambda v: f"{v:.4f}")
        )
    else:
        daily_forecast(engine, args.day)


if __name__ == "__main__":
    main()
