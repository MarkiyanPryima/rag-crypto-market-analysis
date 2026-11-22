"""Daily features at 00:00 UTC (13.1) and targets (13.2).

Features for day D use only information available at D 00:00 UTC:
  * candles that CLOSED by D 00:00 (open time <= D - 1h),
  * funding prints with ts <= D 00:00,
  * articles published before D 00:00.
News features come only from the sources in settings.feature_news_sources (The
Guardian by default): it is the one source with the same coverage in the training,
tuning and test periods, so a "with news / without news" comparison is not skewed by
a source that appears or disappears between periods.
Targets look forward and are stored in a separate table.

Usage:
    python -m crypto_rag.forecasting.features build
"""

import argparse
import logging
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text
from sqlalchemy.dialects.postgresql import insert

from crypto_rag.config import get_settings, setup_logging
from crypto_rag.db.models import TOPICS, Feature, Target
from crypto_rag.db.session import get_engine
from crypto_rag.processing.assets import SYMBOL_ASSET, detect_assets

logger = logging.getLogger(__name__)

PRICE_FEATURES = [
    "ret_1d", "ret_7d", "ret_30d", "rv_1d", "rv_7d", "rv_30d",
    "volume_ratio_30d", "taker_buy_share_1d", "funding_last", "funding_change_1d",
    "funding_mean_7d", "dow",
]  # fmt: skip
BTC_FEATURES = ["btc_ret_1d", "btc_ret_7d"]
NEWS_FEATURES = [
    "news_count_1d", "news_count_7d", "tone_mean_1d", "tone_mean_7d",
    *[f"topic_share_7d_{t}" for t in TOPICS],
]  # fmt: skip
TONE_VALUE = {"negative": -1.0, "neutral": 0.0, "positive": 1.0}
MIN_HISTORY = timedelta(days=30)


def load_candles_df(engine: Engine, symbol: str) -> pd.DataFrame:
    with engine.connect() as conn:
        df = pd.read_sql(
            text(
                "SELECT ts, close::float8, volume::float8, taker_buy_volume::float8 "
                "FROM candles WHERE symbol = :s ORDER BY ts"
            ),
            conn,
            params={"s": symbol},
        )
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts")


def load_funding_df(engine: Engine, symbol: str) -> pd.Series:
    with engine.connect() as conn:
        df = pd.read_sql(
            text("SELECT ts, rate::float8 FROM funding_rates WHERE symbol = :s ORDER BY ts"),
            conn,
            params={"s": symbol},
        )
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts").rate


def load_news_df(engine: Engine) -> pd.DataFrame:
    with engine.connect() as conn:
        df = pd.read_sql(
            text(
                "SELECT a.published_at, a.title, coalesce(a.standfirst, '') AS standfirst, "
                "left(coalesce(a.body_text, ''), 2000) AS body, l.topic, l.tone "
                "FROM articles a LEFT JOIN article_labels l ON l.article_id = a.id "
                "WHERE NOT a.is_liveblog AND a.source = ANY(:sources)"
            ),
            conn,
            params={"sources": get_settings().feature_news_sources},
        )
    df["published_at"] = pd.to_datetime(df["published_at"], utc=True)
    df["assets"] = (df.title + " " + df.standfirst + " " + df.body).map(detect_assets)
    return df.drop(columns=["title", "standfirst", "body"])


def _price_features(
    candles: pd.DataFrame, funding: pd.Series, days: pd.DatetimeIndex
) -> pd.DataFrame:
    """Vectorised: rolling stats over hourly rows, sampled at the candle that closes at D."""
    hourly = candles.asfreq("1h")
    close = hourly.close
    lr = np.log(close / close.shift(1))
    sq = lr.pow(2)
    # The candle opening at D-1h is the last one closed at D 00:00.
    at = days - pd.Timedelta(hours=1)
    out = pd.DataFrame(index=days)
    c = close.reindex(at).to_numpy()
    for n in (1, 7, 30):
        out[f"ret_{n}d"] = c / close.reindex(at - pd.Timedelta(days=n)).to_numpy() - 1
        out[f"rv_{n}d"] = np.sqrt(
            sq.rolling(24 * n, min_periods=24 * n).sum().reindex(at).to_numpy() / n
        )
    vol_24 = hourly.volume.rolling(24, min_periods=24).sum()
    vol_30d = hourly.volume.rolling(24 * 30, min_periods=24 * 30).sum() / 30
    out["volume_ratio_30d"] = (vol_24 / vol_30d).reindex(at).to_numpy()
    taker_24 = hourly.taker_buy_volume.rolling(24, min_periods=24).sum()
    out["taker_buy_share_1d"] = (taker_24 / vol_24).reindex(at).to_numpy()
    # Funding: last print with ts <= D (asof join).
    f = funding.sort_index()
    last = f.reindex(days, method="ffill") if not f.empty else pd.Series(np.nan, index=days)
    prev = f.reindex(days - pd.Timedelta(days=1), method="ffill") if not f.empty else last
    out["funding_last"] = last.to_numpy()
    out["funding_change_1d"] = last.to_numpy() - prev.to_numpy()
    # BTC's own frame is built with an empty funding series when used as ETH's reference.
    out["funding_mean_7d"] = (
        [f[(f.index > d - pd.Timedelta(days=7)) & (f.index <= d)].mean() for d in days]
        if not f.empty
        else np.nan
    )
    out["dow"] = days.dayofweek
    return out


def _news_features(news: pd.DataFrame, asset: str, days: pd.DatetimeIndex) -> pd.DataFrame:
    mine = news[news.assets.map(lambda a: asset in a)].sort_values("published_at")
    out = pd.DataFrame(index=days)
    rows = []
    for d in days:
        last1 = mine[(mine.published_at < d) & (mine.published_at >= d - pd.Timedelta(days=1))]
        last7 = mine[(mine.published_at < d) & (mine.published_at >= d - pd.Timedelta(days=7))]
        row = {
            "news_count_1d": float(len(last1)),
            "news_count_7d": float(len(last7)),
            "tone_mean_1d": last1.tone.map(TONE_VALUE).mean(),
            "tone_mean_7d": last7.tone.map(TONE_VALUE).mean(),
        }
        labelled = last7.topic.dropna()
        for t in TOPICS:
            row[f"topic_share_7d_{t}"] = (labelled == t).mean() if len(labelled) else np.nan
        rows.append(row)
    return pd.DataFrame(rows, index=out.index)


def compute_features(
    candles: pd.DataFrame,
    funding: pd.Series,
    news: pd.DataFrame,
    asset: str,
    days: pd.DatetimeIndex,
    btc_candles: pd.DataFrame | None = None,
) -> pd.DataFrame:
    feats = _price_features(candles, funding, days)
    if btc_candles is not None:
        btc = _price_features(btc_candles, pd.Series(dtype=float), days)
        feats["btc_ret_1d"] = btc.ret_1d
        feats["btc_ret_7d"] = btc.ret_7d
    return feats.join(_news_features(news, asset, days))


def compute_targets(candles: pd.DataFrame, days: pd.DatetimeIndex) -> pd.DataFrame:
    hourly = candles.asfreq("1h")
    close = hourly.close
    lr = np.log(close / close.shift(1))
    at = days - pd.Timedelta(hours=1)
    c0 = close.reindex(at).to_numpy()
    c7 = close.reindex(at + pd.Timedelta(days=7)).to_numpy()
    # Next-day RV: hourly log returns of the 24 candles opening D..D+23h.
    rv_next = np.sqrt(
        lr.pow(2).rolling(24, min_periods=24).sum().shift(-23).reindex(days).to_numpy()
    )
    return pd.DataFrame(
        {"return_7d_pct": (c7 / c0 - 1) * 100, "realized_vol_next_day": rv_next}, index=days
    )


def feature_days(candles: pd.DataFrame, start: datetime, end: datetime) -> pd.DatetimeIndex:
    first = max(pd.Timestamp(start) + MIN_HISTORY, candles.index.min().ceil("D") + MIN_HISTORY)
    last_closed = candles.index.max() + pd.Timedelta(hours=1)
    return pd.date_range(
        first.ceil("D"), min(pd.Timestamp(end), last_closed).floor("D"), freq="D", tz="UTC"
    )


def _clean(v: float) -> float | None:
    return None if v is None or (isinstance(v, float) and not np.isfinite(v)) else float(v)


def build(engine: Engine, symbols: list[str] | None = None, end: datetime | None = None) -> int:
    s = get_settings()
    symbols = symbols or s.symbols
    news = load_news_df(engine)
    btc = load_candles_df(engine, "BTCUSDT")
    total = 0
    for symbol in symbols:
        candles = load_candles_df(engine, symbol) if symbol != "BTCUSDT" else btc
        days = feature_days(candles, s.start_date, end or pd.Timestamp.now(tz="UTC"))
        feats = compute_features(
            candles, load_funding_df(engine, symbol), news, SYMBOL_ASSET[symbol], days,
            btc_candles=btc if symbol != "BTCUSDT" else None,
        )  # fmt: skip
        targets = compute_targets(candles, days)
        f_rows = [
            {"symbol": symbol, "date": d.date(), "features": {k: _clean(v) for k, v in row.items()}}
            for d, row in feats.iterrows()
        ]
        t_rows = [
            {"symbol": symbol, "date": d.date(), "return_7d_pct": _clean(r.return_7d_pct),
             "realized_vol_next_day": _clean(r.realized_vol_next_day)}
            for d, r in targets.iterrows()
        ]  # fmt: skip
        with engine.begin() as conn:
            st = insert(Feature)
            conn.execute(st.on_conflict_do_update(index_elements=["symbol", "date"],
                                                  set_={"features": st.excluded.features}), f_rows)  # fmt: skip
            tt = insert(Target)
            conn.execute(tt.on_conflict_do_update(
                index_elements=["symbol", "date"],
                set_={"return_7d_pct": tt.excluded.return_7d_pct,
                      "realized_vol_next_day": tt.excluded.realized_vol_next_day}), t_rows)  # fmt: skip
        logger.info(
            "%s: %d feature days (%s .. %s)", symbol, len(f_rows), days[0].date(), days[-1].date()
        )
        total += len(f_rows)
    return total


def load_dataset(engine: Engine, symbol: str) -> pd.DataFrame:
    """Features joined with targets, indexed by date."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT f.date, f.features, t.return_7d_pct, t.realized_vol_next_day "
                "FROM features f LEFT JOIN targets t USING (symbol, date) "
                "WHERE f.symbol = :s ORDER BY f.date"
            ),
            {"s": symbol},
        ).all()
    df = pd.DataFrame([{**r.features, "date": r.date, "return_7d_pct": r.return_7d_pct,
                        "realized_vol_next_day": r.realized_vol_next_day} for r in rows])  # fmt: skip
    return df.set_index("date") if not df.empty else df


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Feature table")
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args()
    end = datetime.combine(args.end, datetime.min.time()) if args.end else None
    build(get_engine(), end=pd.Timestamp(end, tz="UTC") if end else None)


if __name__ == "__main__":
    main()
