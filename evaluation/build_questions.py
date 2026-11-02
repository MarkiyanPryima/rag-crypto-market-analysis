"""Stage 17.1: pick BTC/ETH moves after the model's knowledge cut-off for evaluation.

For each asset, every 24-hour window is scored by its absolute return. Moves are
picked greedily (largest first) with at least 72 h between picks, so no two
questions describe the same event. The set mixes large and moderate moves,
rises and falls.

For every move the script lists candidate articles (published from 12 h before
the window to its end) so the author can mark the relevant ones by hand.

Outputs:
    evaluation/questions_draft.jsonl   one question per line (relevant_article_ids empty)
    evaluation/annotate_articles.csv   candidates to mark: relevant = 1 / 0

Usage:
    python evaluation/build_questions.py [--per-asset 25] [--since 2026-02-16]
"""

import argparse
import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--per-asset", type=int, default=25)
    parser.add_argument("--since", default="2026-02-16")
    args = parser.parse_args()
    since = pd.Timestamp(args.since, tz="UTC")
    engine = get_engine()

    questions, rows = [], []
    for asset, symbol in ASSETS.items():
        close = load_candles_df(engine, symbol).close.asfreq("1h")
        for i, (end, ret_pct) in enumerate(pick_moves(close, since, args.per_asset)):
            direction = "down" if ret_pct < 0 else "up"
            start = end - WINDOW
            as_of = (end + timedelta(hours=2)).to_pydatetime()
            day = (
                f"{start:%Y-%m-%d}"
                if start.date() == end.date()
                else f"{start:%Y-%m-%d}–{end:%Y-%m-%d}"
            )
            template = TEMPLATES[direction][i % len(TEMPLATES[direction])]
            qid = f"{asset.lower()}-{end:%Y%m%d%H}"
            questions.append({
                "id": qid,
                "asset": asset,
                "as_of": as_of.isoformat(),
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
                "move_pct": round(ret_pct, 2),
                "question": template.format(asset=asset, day=day),
                "relevant_article_ids": [],
            })  # fmt: skip
            with engine.connect() as conn:
                cands = conn.execute(
                    text(
                        "SELECT DISTINCT a.id, a.source, a.channel, a.published_at, a.title FROM articles a "
                        "JOIN chunks c ON c.article_id = a.id "
                        "WHERE a.published_at BETWEEN :f AND :t AND NOT a.is_liveblog "
                        "AND :asset = ANY(c.assets) ORDER BY a.published_at"
                    ),
                    {"f": start - timedelta(hours=12), "t": end, "asset": asset},
                ).all()
            for c in cands:
                rows.append({"question_id": qid, "question": questions[-1]["question"],
                             "move_pct": round(ret_pct, 2), "article_id": c.id, "source": c.source,
                             "channel": c.channel, "published_at": c.published_at.astimezone(UTC).isoformat(),
                             "title": c.title, "relevant": ""})  # fmt: skip

    (OUT / "questions_draft.jsonl").write_text(
        "".join(json.dumps(q, ensure_ascii=False) + "\n" for q in questions)
    )
    with (OUT / "annotate_articles.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["question_id"])
        w.writeheader()
        w.writerows(rows)
    no_news = sum(1 for q in questions if not any(r["question_id"] == q["id"] for r in rows))
    print(
        f"{len(questions)} questions, {len(rows)} candidate articles, {no_news} questions with no candidates"
    )
    print(f"generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC -> {OUT}")


if __name__ == "__main__":
    main()
