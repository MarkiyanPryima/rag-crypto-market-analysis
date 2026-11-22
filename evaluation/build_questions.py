"""Stage 17.1: pick BTC/ETH moves after the model's knowledge cut-off for evaluation.

The short second window holds fewer non-overlapping moves, hence a per-window maximum.
Two windows, both after the generator's knowledge cut-off (2026-02-16) and both with
news coverage beyond The Guardian:
  * 2026-02-16 .. 2026-04-30 — Hugging Face katelabs headlines;
  * 2026-10-09 .. now        — CoinDesk RSS.
For each asset and window, every 24-hour move is scored by its absolute return and
moves are picked greedily with at least 72 h between picks, half from the largest
moves and half from moderate ones (10th-50th percentile of |move|), so the set mixes
large and moderate moves, rises and falls.

Relevant articles are not listed here: they are marked by hand on the pooled results
of every configuration (see crypto_rag.evaluation.annotate).

Output: evaluation/questions.jsonl (one question per line).

Usage:
    python evaluation/build_questions.py [--per-window 13]
"""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from crypto_rag.db.session import get_engine
from crypto_rag.forecasting.features import load_candles_df

OUT = Path(__file__).resolve().parent
WINDOW = timedelta(hours=24)
SEPARATION = timedelta(hours=72)
ASSETS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}

TEMPLATES = {
    "down": [
        "Чому {asset} впав {day}?",
        "Що спричинило падіння {asset} {day}?",
        "Why did {asset} drop on {day}?",
    ],
    "up": [
        "Чому {asset} зріс {day}?",
        "Що стояло за зростанням {asset} {day}?",
        "Why did {asset} rally on {day}?",
    ],
}


def pick_moves(close: pd.Series, since: pd.Timestamp, n: int) -> list[tuple[pd.Timestamp, float]]:
    """(window_end, return_pct) of the n largest non-overlapping 24 h moves after `since`."""
    ret = (close / close.shift(24) - 1) * 100
    ret = ret[ret.index >= since + WINDOW].dropna()
    # Half the picks from the largest moves, half spread over moderate ones (rank-sampled),
    # so the set is not only crashes and squeezes.
    order = ret.abs().sort_values(ascending=False)
    picked: list[pd.Timestamp] = []

    def take(candidates: pd.Index, limit: int) -> None:
        for ts in candidates:
            if len(picked) >= limit:
                return
            if all(abs(ts - p) >= SEPARATION for p in picked):
                picked.append(ts)

    take(order.index, n // 2)
    moderate = order.iloc[
        len(order) // 10 : len(order) // 2
    ].index  # 10th-50th percentile of |move|
    rng = np.random.default_rng(42)
    take(pd.Index(rng.permutation(moderate)), n)
    return sorted((ts + pd.Timedelta(hours=1), float(ret[ts])) for ts in picked)


WINDOWS = [("2026-02-16", "2026-04-30"), ("2026-10-09", None)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--per-window", type=int, default=13, help="max questions per asset and window"
    )
    args = parser.parse_args()
    engine = get_engine()

    questions = []
    for asset, symbol in ASSETS.items():
        close = load_candles_df(engine, symbol).close.asfreq("1h")
        for since, until in WINDOWS:
            part = (
                close[: pd.Timestamp(until, tz="UTC") + pd.Timedelta(hours=23)] if until else close
            )
            moves = pick_moves(part, pd.Timestamp(since, tz="UTC"), args.per_window)
            for end, ret_pct in moves:
                i = len(questions)
                direction = "down" if ret_pct < 0 else "up"
                start = end - WINDOW
                day = (
                    f"{start:%Y-%m-%d}"
                    if start.date() == end.date()
                    else f"{start:%Y-%m-%d}–{end:%Y-%m-%d}"
                )
                template = TEMPLATES[direction][i % len(TEMPLATES[direction])]
                questions.append({
                    "id": f"{asset.lower()}-{end:%Y%m%d%H}",
                    "asset": asset,
                    "window": since,
                    "as_of": (end + timedelta(hours=2)).isoformat(),
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                    "move_pct": round(ret_pct, 2),
                    "question": template.format(asset=asset, day=day),
                })  # fmt: skip

    questions.sort(key=lambda q: q["window_end"])
    (OUT / "questions.jsonl").write_text(
        "".join(json.dumps(q, ensure_ascii=False) + "\n" for q in questions)
    )
    print(f"{len(questions)} questions; generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC -> {OUT}")


if __name__ == "__main__":
    main()
