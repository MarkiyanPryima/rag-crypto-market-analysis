"""get_forecast: the latest stored forecast with date <= as_of, as [f…] metrics.

A forecast for date D is produced at D 00:00 UTC from data up to that moment,
so it is visible to any as_of >= D 00:00.
"""

from datetime import datetime

from sqlalchemy import Engine, text

from crypto_rag.tools.base import IdAllocator, Metric, metric

DESCRIPTIONS = {
    "buy_signal": "Model probability that {s} rises more than {thr}% over the next 7 days (forecast for {d})",
    "volatility": "Forecast realised volatility of {s} for {d} (daily, from hourly log returns)",
}


def get_forecast(
    engine: Engine, symbol: str, as_of: datetime, ids: IdAllocator | None = None
) -> list[Metric]:
    from crypto_rag.config import get_settings

    ids = ids or IdAllocator()
    thr = get_settings().buy_threshold_pct
    out = []
    with engine.connect() as conn:
        for kind in ("buy_signal", "volatility"):
            row = conn.execute(
                text(
                    "SELECT date, value, contributions, model_version FROM forecasts "
                    "WHERE symbol = :s AND kind = :k AND date <= CAST(:as_of AS timestamptz)::date "
                    "ORDER BY date DESC, created_at DESC LIMIT 1"
                ),
                {"s": symbol, "k": kind, "as_of": as_of},
            ).one_or_none()
            if row is None:
                continue
            top = sorted(row.contributions.items(), key=lambda kv: -abs(kv[1]))[:5]
            value, unit, decimals = (
                (row.value * 100, "%", 1) if kind == "buy_signal" else (row.value * 100, "%", 2)
            )
            out.append(
                metric(
                    ids, kind, value, unit, decimals,
                    DESCRIPTIONS[kind].format(s=symbol, thr=thr, d=row.date.isoformat()),
                    prefix="f", date=row.date.isoformat(), model_version=row.model_version,
                    top_contributions=dict(top),
                )
            )  # fmt: skip
    return out
