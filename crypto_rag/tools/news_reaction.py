"""news_price_reaction: price move in the 2 h before and 6 h after a publication."""

from datetime import datetime, timedelta

from sqlalchemy import Engine, text

from crypto_rag.processing.assets import ASSET_SYMBOL
from crypto_rag.tools.base import IdAllocator, Metric, metric, price_at

BEFORE = timedelta(hours=2)
AFTER = timedelta(hours=6)


def news_price_reaction(
    engine: Engine,
    article_id: str,
    as_of: datetime,
    symbol: str | None = None,
    ids: IdAllocator | None = None,
) -> list[Metric]:
    ids = ids or IdAllocator()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT a.published_at, coalesce(array_agg(DISTINCT x) FILTER (WHERE x IS NOT NULL), '{}') AS assets "
                "FROM articles a LEFT JOIN chunks c ON c.article_id = a.id "
                "LEFT JOIN LATERAL unnest(c.assets) x ON true "
                "WHERE a.id = :id AND a.published_at <= :as_of GROUP BY a.published_at"
            ),
            {"id": article_id, "as_of": as_of},
        ).one_or_none()
    if row is None:
        return []
    published = row.published_at
    symbols = [symbol] if symbol else [ASSET_SYMBOL[a] for a in row.assets] or ["BTCUSDT"]
    out = []
    for sym in symbols:
        p_before = price_at(engine, sym, published - BEFORE, as_of)
        p_pub = price_at(engine, sym, published, as_of)
        # The "after" leg exists only once the 6 h have passed by as_of.
        p_after = (
            price_at(engine, sym, published + AFTER, as_of) if published + AFTER <= as_of else None
        )
        pre = None if not (p_before and p_pub) else (p_pub / p_before - 1) * 100
        post = None if not (p_pub and p_after) else (p_after / p_pub - 1) * 100
        out.append(
            metric(
                ids,
                "move_2h_before_pct",
                pre,
                "%",
                2,
                f"{sym} change in the 2 h before publication",
                article_id=article_id,
            )
        )
        out.append(metric(ids, "move_6h_after_pct", post, "%", 2,
                          f"{sym} change in the 6 h after publication", article_id=article_id))  # fmt: skip
    return out
