"""HTTP API: the only way the UI reaches the system.

    POST /ask            question -> verified answer, sections, sources, query id
    GET  /trace/{id}     full trace from query_log
    GET  /stats          price reaction by news topic, forecast summary, data volumes
    GET  /prices         hourly closes for the chart (bounded by as_of)
    GET  /news           article markers for the chart (bounded by as_of)
    GET  /health         database and data freshness

Run: uvicorn crypto_rag.api.main:app --reload
"""

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import Engine, text

from crypto_rag.agent.graph import AgentDeps, default_deps, run_query
from crypto_rag.config import get_settings, setup_logging
from crypto_rag.db.session import get_engine
from crypto_rag.processing.assets import ASSET_SYMBOL

logger = logging.getLogger(__name__)

Asset = Literal["BTC", "ETH"]
# Headings are requested in Ukrainian, but a model answering an English question
# sometimes writes them in English; both map to the same keys.
SECTION_KEYS = {
    "що сталося": "what_happened",
    "what happened": "what_happened",
    "ймовірні причини": "likely_causes",
    "likely causes": "likely_causes",
    "possible causes": "likely_causes",
    "схожі епізоди": "similar_episodes",
    "similar episodes": "similar_episodes",
    "прогноз": "forecast",
    "forecast": "forecast",
}


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Crypto RAG API",
    description="Explains BTC/ETH price moves from news and market data. Not financial advice.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# --- Dependencies (overridden in tests) ----------------------------------------


def engine_dep() -> Engine:
    return get_engine()


def deps_dep(engine: Annotated[Engine, Depends(engine_dep)]) -> AgentDeps:
    if not get_settings().openai_api_key.get_secret_value():
        raise HTTPException(503, "OPENAI_API_KEY is not configured; /ask is unavailable")
    return default_deps(engine)


EngineDep = Annotated[Engine, Depends(engine_dep)]


# --- Schemas -----------------------------------------------------------------


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    asset: Asset | None = None
    as_of: datetime | None = Field(default=None, description="UTC; defaults to now")
    window_hours: int | None = Field(
        default=None, ge=1, le=24 * 90, description="News/market window"
    )


class Source(BaseModel):
    id: str
    title: str
    url: str
    source: str
    published_at: str
    score: float | None = None


class Verification(BaseModel):
    ok: bool
    errors: list[dict[str, Any]] = []


class MetricOut(BaseModel):
    id: str
    name: str
    value: float | None
    display: str
    unit: str
    description: str


class AskResponse(BaseModel):
    query_id: int
    as_of: datetime
    asset: str | None
    intent: str | None
    answer: str
    sections: dict[str, str]
    sources: list[Source]
    metrics: list[MetricOut]
    verification: Verification
    refused: bool


def split_sections(answer: str) -> dict[str, str]:
    """Map '## <heading>' blocks to stable keys; unknown headings keep their text."""
    sections: dict[str, str] = {}
    current, buf = "intro", []
    for line in answer.splitlines():
        m = re.match(r"^\s*#{1,6}\s*(.+?)\s*$", line)
        if m:
            if "".join(buf).strip():
                sections[current] = "\n".join(buf).strip()
            heading = m.group(1).strip().lower()
            current = next((v for k, v in SECTION_KEYS.items() if k in heading), heading)
            buf = []
        else:
            buf.append(line)
    if "".join(buf).strip():
        sections[current] = "\n".join(buf).strip()
    return sections


def _sources(state: dict[str, Any]) -> list[Source]:
    cited = set(re.findall(r"\[(c_\d+)\]", state.get("answer") or ""))
    out: dict[str, Source] = {}
    for name in ("search_news", "news_around_move"):
        for call in state.get("tool_results", {}).get(name, []):
            for h in call:
                if h["id"] in cited and h["id"] not in out:
                    out[h["id"]] = Source(
                        id=h["id"], title=h["title"], url=h["url"], source=h["source"],
                        published_at=h["published_at"], score=h.get("score"),
                    )  # fmt: skip
    return sorted(out.values(), key=lambda s: int(s.id.split("_")[1]))


def _metrics(state: dict[str, Any]) -> list[MetricOut]:
    from crypto_rag.verification.verify import collect_metrics

    cited = list(dict.fromkeys(re.findall(r"\[((?:m|f)\d+)\]", state.get("answer") or "")))
    pool = collect_metrics(state.get("tool_results", {}))
    return [
        MetricOut(**{k: pool[i][k] for k in MetricOut.model_fields}) for i in cited if i in pool
    ]


# --- Endpoints ---------------------------------------------------------------


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest, deps: Annotated[AgentDeps, Depends(deps_dep)]) -> AskResponse:
    now = datetime.now(UTC)
    as_of = req.as_of or now
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    if as_of > now + timedelta(minutes=1):
        raise HTTPException(422, "as_of cannot be in the future")
    try:
        state = run_query(deps, req.question, req.asset, as_of, window_hours=req.window_hours)
    except Exception as exc:
        logger.exception("query failed")
        raise HTTPException(502, f"Query failed: {type(exc).__name__}: {exc}") from exc
    answer = state.get("answer") or ""
    return AskResponse(
        query_id=state["query_id"],
        as_of=as_of,
        asset=state.get("asset"),
        intent=state.get("intent"),
        answer=answer,
        sections=split_sections(answer),
        sources=_sources(state),
        metrics=_metrics(state),
        verification=Verification(
            **{
                k: v
                for k, v in state.get("verification", {"ok": False}).items()
                if k in ("ok", "errors")
            }
        ),
        refused=bool(state.get("refused")),
    )


@app.get("/trace/{query_id}")
def trace(query_id: int, engine: EngineDep) -> dict[str, Any]:
    with engine.connect() as conn:
        row = (
            conn.execute(text("SELECT * FROM query_log WHERE id = :i"), {"i": query_id})
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(404, f"query {query_id} not found")
    events = row["trace"].get("events", [])
    return {
        "id": row["id"],
        "question": row["question"],
        "asset": row["asset"],
        "as_of": row["as_of"],
        "intent": row["intent"],
        "created_at": row["created_at"],
        "routing": next((e for e in events if e["event"] == "router"), None),
        "tool_calls": row["tool_calls"],
        "retrieved": row["retrieved"],
        "attempts": [e for e in events if e["event"] in ("generate", "verify")],
        "verification": row["verification"],
        "answer": row["answer"],
        "tool_results": row["trace"].get("tool_results", {}),
    }


TOPIC_REACTION_SQL = """
WITH a AS (
    SELECT l.topic, l.tone, ar.published_at,
           CASE WHEN 'ETH' = ANY(ch.assets) AND NOT 'BTC' = ANY(ch.assets) THEN 'ETHUSDT' ELSE 'BTCUSDT' END AS symbol
    FROM article_labels l
    JOIN articles ar ON ar.id = l.article_id
    JOIN chunks ch ON ch.article_id = ar.id AND ch.chunk_index = 0
    WHERE NOT ar.is_liveblog AND ar.published_at + interval '6 hours' <= now()
),
p AS (
    SELECT a.topic, a.tone,
        (SELECT close::float8 FROM candles c WHERE c.symbol = a.symbol
           AND c.ts + interval '1 hour' <= a.published_at ORDER BY c.ts DESC LIMIT 1) AS p0,
        (SELECT close::float8 FROM candles c WHERE c.symbol = a.symbol
           AND c.ts + interval '1 hour' <= a.published_at + interval '6 hours' ORDER BY c.ts DESC LIMIT 1) AS p6,
        (SELECT stddev(ln(close::float8 / NULLIF(open::float8, 0))) FROM candles c WHERE c.symbol = a.symbol
           AND c.ts >= a.published_at AND c.ts < a.published_at + interval '6 hours') AS vol_after,
        (SELECT stddev(ln(close::float8 / NULLIF(open::float8, 0))) FROM candles c WHERE c.symbol = a.symbol
           AND c.ts >= a.published_at - interval '6 hours' AND c.ts < a.published_at) AS vol_before
    FROM a
)
SELECT topic, count(*) AS articles,
       round(avg((p6 / p0 - 1) * 100)::numeric, 3)::float8 AS avg_move_6h_pct,
       round(avg(abs(p6 / p0 - 1) * 100)::numeric, 3)::float8 AS avg_abs_move_6h_pct,
       round(avg(vol_after / NULLIF(vol_before, 0))::numeric, 3)::float8 AS vol_ratio_after_before,
       round(avg(CASE tone WHEN 'positive' THEN 1 WHEN 'negative' THEN -1 ELSE 0 END)::numeric, 3)::float8 AS avg_tone
FROM p WHERE p0 IS NOT NULL AND p6 IS NOT NULL
GROUP BY topic ORDER BY articles DESC
"""


@app.get("/stats")
def stats(engine: EngineDep) -> dict[str, Any]:
    with engine.connect() as conn:
        candles = conn.execute(text(
            "SELECT symbol, count(*) AS n, min(ts) AS first, max(ts) AS last FROM candles GROUP BY symbol ORDER BY symbol"
        )).mappings().all()  # fmt: skip
        articles = conn.execute(text(
            "SELECT to_char(date_trunc('month', published_at), 'YYYY-MM') AS month, source, channel, count(*) AS n "
            "FROM articles GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
        )).mappings().all()  # fmt: skip
        totals = conn.execute(text(
            "SELECT (SELECT count(*) FROM articles) AS articles, (SELECT count(*) FROM chunks) AS chunks, "
            "(SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) AS embedded, "
            "(SELECT count(*) FROM article_labels) AS labelled, (SELECT count(*) FROM episodes) AS episodes, "
            "(SELECT count(*) FROM query_log) AS queries"
        )).mappings().one()  # fmt: skip
        topics = conn.execute(text(TOPIC_REACTION_SQL)).mappings().all()
        forecasts = conn.execute(text(
            "SELECT symbol, kind, count(*) AS n, max(date) AS latest_date, "
            "(array_agg(value ORDER BY date DESC))[1] AS latest_value, "
            "(array_agg(model_version ORDER BY date DESC))[1] AS model_version "
            "FROM forecasts GROUP BY symbol, kind ORDER BY symbol, kind"
        )).mappings().all()  # fmt: skip
        cost = conn.execute(text(
            "SELECT purpose, model, count(*) AS calls, sum(input_tokens) AS input_tokens, "
            "sum(output_tokens) AS output_tokens, round(sum(cost_usd)::numeric, 4)::float8 AS cost_usd "
            "FROM llm_usage GROUP BY 1, 2 ORDER BY 1, 2"
        )).mappings().all()  # fmt: skip
    rows = lambda rs: [dict(r) for r in rs]  # noqa: E731
    return {
        "data": {
            "candles": rows(candles),
            "articles_by_month": rows(articles),
            "totals": dict(totals),
        },
        "topic_reaction": rows(topics),
        "forecasts": rows(forecasts),
        "llm_usage": rows(cost),
    }


@app.get("/prices")
def prices(
    engine: EngineDep,
    asset: Asset,
    start: datetime,
    end: datetime | None = None,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    as_of = as_of or datetime.now(UTC)
    end = min(end or as_of, as_of)
    if end - start > timedelta(days=120):
        raise HTTPException(422, "window too large (max 120 days)")
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT ts, open::float8, high::float8, low::float8, close::float8, volume::float8 "
                "FROM candles WHERE symbol = :s AND ts >= :f AND ts + interval '1 hour' <= :t ORDER BY ts"
            ),
            {"s": ASSET_SYMBOL[asset], "f": start, "t": end},
        ).mappings().all()  # fmt: skip
    return [dict(r) for r in rows]


@app.get("/news")
def news(
    engine: EngineDep,
    start: datetime,
    end: datetime | None = None,
    as_of: datetime | None = None,
    asset: Asset | None = None,
    limit: Annotated[int, Query(le=500)] = 200,
) -> list[dict[str, Any]]:
    as_of = as_of or datetime.now(UTC)
    end = min(end or as_of, as_of)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.id, a.title, a.url, a.source, a.channel, a.published_at, l.topic, l.tone "
                "FROM articles a LEFT JOIN article_labels l ON l.article_id = a.id "
                "WHERE a.published_at BETWEEN :f AND :t AND NOT a.is_liveblog "
                "AND (CAST(:asset AS text) IS NULL OR EXISTS (SELECT 1 FROM chunks c WHERE c.article_id = a.id "
                "     AND CAST(:asset AS text) = ANY(c.assets))) "
                "ORDER BY a.published_at DESC LIMIT :n"
            ),
            {"f": start, "t": end, "asset": asset, "n": limit},
        ).mappings().all()  # fmt: skip
    return [dict(r) for r in rows]


@app.get("/health")
def health(engine: EngineDep) -> dict[str, Any]:
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT (SELECT max(ts) FROM candles) AS last_candle, "
                "(SELECT max(published_at) FROM articles) AS last_article, "
                "(SELECT max(fetched_at) FROM articles) AS last_fetch"
            )).mappings().one()  # fmt: skip
    except Exception as exc:
        raise HTTPException(503, f"database unavailable: {type(exc).__name__}") from exc
    s = get_settings()
    return {
        "status": "ok",
        "database": "ok",
        **dict(row),
        "openai_configured": bool(s.openai_api_key.get_secret_value()),
        "guardian_configured": bool(s.guardian_api_key.get_secret_value()),
    }
