"""48-hour price episodes and similarity search over their shapes.

An episode starting at T covers the 48 hourly candles opening at T..T+47h and
ends at end_ts = T+48h (when its last candle closes).
shape = close_i / close_0 - 1, so episodes are compared by form, not level.

Usage:
    python -m crypto_rag.tools.episodes build
"""

import argparse
import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text
from sqlalchemy.dialects.postgresql import insert

from crypto_rag.config import get_settings, setup_logging
from crypto_rag.db.models import EPISODE_LEN, Episode
from crypto_rag.db.session import get_engine
from crypto_rag.processing.assets import SYMBOL_ASSET
from crypto_rag.tools.base import IdAllocator, Metric, load_candles, metric

logger = logging.getLogger(__name__)

HORIZON = timedelta(days=7)
WINDOW = timedelta(hours=EPISODE_LEN)


def compute_episodes(candles: pd.DataFrame) -> pd.DataFrame:
    """All 48h windows with 1h step from an hourly candle frame (ts, close)."""
    closes = candles.set_index("ts").close.asfreq("1h")
    values = closes.to_numpy()
    n = len(values) - EPISODE_LEN + 1
    if n <= 0:
        return pd.DataFrame()
    windows = np.lib.stride_tricks.sliding_window_view(values, EPISODE_LEN)
    valid = ~np.isnan(windows).any(axis=1)
    shapes = windows / windows[:, :1] - 1
    running_max = np.maximum.accumulate(windows, axis=1)
    max_dd = (windows / running_max - 1).min(axis=1) * 100
    starts = closes.index[:n]
    ends = starts + WINDOW
    # Close 7 days after the episode ends (NaN if beyond the data).
    after = closes.reindex(ends - timedelta(hours=1) + HORIZON).to_numpy()
    last = windows[:, -1]
    df = pd.DataFrame(
        {
            "start_ts": starts,
            "end_ts": ends,
            "return_pct": shapes[:, -1] * 100,
            "max_drawdown": max_dd,
            "shape": list(shapes),
            "return_7d_after_pct": (after / last - 1) * 100,
        }
    )
    return df[valid].reset_index(drop=True)


def build_episodes(engine: Engine, symbol: str) -> int:
    far = datetime.max.replace(tzinfo=get_settings().start_date.tzinfo)
    candles = load_candles(engine, symbol, get_settings().start_date, far, far)
    df = compute_episodes(candles)
    rows = [
        {
            "symbol": symbol,
            "start_ts": r.start_ts.to_pydatetime(),
            "end_ts": r.end_ts.to_pydatetime(),
            "return_pct": float(r.return_pct),
            "max_drawdown": float(r.max_drawdown),
            "shape": [float(x) for x in r.shape],
            "return_7d_after_pct": None
            if np.isnan(r.return_7d_after_pct)
            else float(r.return_7d_after_pct),
        }
        for r in df.itertuples()
    ]
    if not rows:
        return 0
    stmt = insert(Episode)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Episode.symbol, Episode.start_ts],
        set_={
            c: stmt.excluded[c]
            for c in ("end_ts", "return_pct", "max_drawdown", "shape", "return_7d_after_pct")
        },
    )
    with engine.begin() as conn:
        for i in range(0, len(rows), 2000):
            conn.execute(stmt, rows[i : i + 2000])
    logger.info("%s: %d episodes", symbol, len(rows))
    return len(rows)


def current_shape(
    engine: Engine, symbol: str, as_of: datetime
) -> tuple[datetime, list[float]] | None:
    candles = load_candles(engine, symbol, as_of - WINDOW - timedelta(hours=1), as_of, as_of)
    if len(candles) < EPISODE_LEN:
        return None
    closes = candles.close.to_numpy()[-EPISODE_LEN:]
    return candles.ts.iloc[-EPISODE_LEN].to_pydatetime(), list(closes / closes[0] - 1)


def find_similar_episodes(
    engine: Engine, symbol: str, as_of: datetime, k: int = 3, ids: IdAllocator | None = None
) -> list[dict]:
    """k most similar past episodes (cosine distance on shape).

    Candidates must END at least 48 h before as_of, so they never overlap the
    current window. Results are at least 48 h apart from each other, otherwise
    the top-k would be the same event shifted by an hour. The 7-day outcome is
    shown only if end_ts + 7 days <= as_of.
    """
    ids = ids or IdAllocator()
    cur = current_shape(engine, symbol, as_of)
    if cur is None:
        return []
    cur_start, shape = cur
    vec = "[" + ",".join(f"{x:.8f}" for x in shape) + "]"
    with engine.connect() as conn:
        candidates = conn.execute(
            text(
                "SELECT start_ts, end_ts, return_pct, max_drawdown, return_7d_after_pct, "
                "shape <=> CAST(:v AS vector) AS distance FROM episodes "
                "WHERE symbol = :s AND end_ts <= :cutoff ORDER BY distance LIMIT 300"
            ),
            {"v": vec, "s": symbol, "cutoff": cur_start},
        ).all()
        picked = []
        for c in candidates:
            if all(
                abs((c.start_ts - p.start_ts).total_seconds()) >= WINDOW.total_seconds()
                for p in picked
            ):
                picked.append(c)
            if len(picked) == k:
                break
        asset = SYMBOL_ASSET.get(symbol)
        results = []
        for c in picked:
            after_known = c.end_ts + HORIZON <= as_of
            news = conn.execute(
                text(
                    "SELECT DISTINCT ON (a.id) a.id, a.title, a.published_at, a.url FROM articles a "
                    "JOIN chunks ch ON ch.article_id = a.id "
                    "WHERE a.published_at BETWEEN :f AND :t AND a.published_at <= :as_of "
                    "AND NOT a.is_liveblog AND (CAST(:asset AS text) IS NULL OR CAST(:asset AS text) = ANY(ch.assets)) "
                    "ORDER BY a.id, a.published_at LIMIT 2"
                ),
                {"f": c.start_ts, "t": c.end_ts, "as_of": as_of, "asset": asset},
            ).mappings().all()  # fmt: skip
            period = f"{c.start_ts:%Y-%m-%d %H:%M}–{c.end_ts:%Y-%m-%d %H:%M} UTC"
            metrics: list[Metric] = [
                metric(ids, "episode_return_pct", c.return_pct, "%", 2, f"{symbol} return over {period}"),
                metric(ids, "episode_max_drawdown_pct", c.max_drawdown, "%", 2, f"Max drawdown over {period}"),
                metric(ids, "episode_return_7d_after_pct",
                       c.return_7d_after_pct if after_known else None, "%", 2,
                       f"{symbol} return in the 7 days after {c.end_ts:%Y-%m-%d %H:%M} UTC"),
                metric(ids, "episode_similarity", 1 - c.distance, "", 3, f"Cosine similarity of {period} to the current 48 h"),
            ]  # fmt: skip
            results.append(
                {
                    "start_ts": c.start_ts.isoformat(),
                    "end_ts": c.end_ts.isoformat(),
                    "metrics": [m.to_dict() for m in metrics],
                    "news": [{**n, "published_at": n["published_at"].isoformat()} for n in news],
                }
            )
    return results


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Episodes")
    parser.add_argument("command", choices=["build"])
    parser.parse_args()
    engine = get_engine()
    for symbol in get_settings().symbols:
        build_episodes(engine, symbol)


if __name__ == "__main__":
    main()
