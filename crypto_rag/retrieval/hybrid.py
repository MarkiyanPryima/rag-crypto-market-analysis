"""search_news: hybrid (vector + full-text) news retrieval bounded by as_of.

1. Hard filters: as_of - window <= published_at <= as_of, asset in chunk.assets
   (if given), no live blogs.
2. Candidates: top-50 by cosine distance and top-50 by ts_rank
   (websearch_to_tsquery, 'english').
3. Reciprocal rank fusion: 1/(k + r_vec) + 1/(k + r_fts), k = 60.
4. Recency decay: score = rrf * exp(-λ·Δt_hours), λ = ln 2 / T½.
5. At most 2 chunks per article; top-k (default 6).

The filtered set is MATERIALIZED and ranked exactly. An HNSW index scan with
these filters would apply them after the index lookup and could silently
return fewer than 50 candidates.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict
from sqlalchemy import Engine, text

from crypto_rag.config import get_settings
from crypto_rag.processing.assets import ASSET_SYMBOL
from crypto_rag.tools.base import IdAllocator
from crypto_rag.tools.news_reaction import news_price_reaction

QueryEmbedder = Callable[[str], list[float]]

SEARCH_SQL = """
WITH filtered AS MATERIALIZED (
    SELECT c.id, c.article_id, c.text, c.published_at, c.embedding, c.tsv
    FROM chunks c JOIN articles a ON a.id = c.article_id
    WHERE c.published_at <= :as_of
      AND c.published_at >= :since
      AND NOT a.is_liveblog
      AND (CAST(:asset AS text) IS NULL OR CAST(:asset AS text) = ANY(c.assets))
),
vec AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> CAST(:qvec AS vector)) AS r
    FROM filtered
    WHERE embedding IS NOT NULL AND CAST(:qvec AS text) IS NOT NULL
    ORDER BY embedding <=> CAST(:qvec AS vector)
    LIMIT :n
),
q AS (SELECT websearch_to_tsquery('english', :query) AS q),
fts AS (
    SELECT f.id, row_number() OVER (ORDER BY ts_rank(f.tsv, q.q) DESC, f.id) AS r
    FROM filtered f, q
    WHERE f.tsv @@ q.q
    ORDER BY ts_rank(f.tsv, q.q) DESC, f.id
    LIMIT :n
)
SELECT f.id, f.article_id, f.text, f.published_at, a.title, a.source, a.url,
       vec.r AS rank_vec, fts.r AS rank_fts
FROM filtered f
JOIN articles a ON a.id = f.article_id
LEFT JOIN vec ON vec.id = f.id
LEFT JOIN fts ON fts.id = f.id
WHERE vec.id IS NOT NULL OR fts.id IS NOT NULL
"""


@dataclass
class NewsHit:
    id: str
    chunk_id: int
    article_id: str
    title: str
    source: str
    published_at: str
    url: str
    text: str
    rank_vec: int | None
    rank_fts: int | None
    rrf: float
    decay: float
    score: float
    price_reaction: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decay_factor(published_at: datetime, as_of: datetime, half_life_hours: float) -> float:
    dt_hours = max((as_of - published_at).total_seconds() / 3600, 0.0)
    return math.exp(-math.log(2) / half_life_hours * dt_hours)


def search_news(
    engine: Engine,
    query: str,
    as_of: datetime,
    asset: str | None = None,
    window_hours: float = 72,
    k: int | None = None,
    embed_query: QueryEmbedder | None = None,
    half_life_hours: float | None = None,
    with_reaction: bool = True,
    ids: IdAllocator | None = None,
) -> list[NewsHit]:
    s = get_settings()
    k = k or s.retrieval_top_k
    half_life = half_life_hours or s.decay_half_life_hours
    ids = ids or IdAllocator()
    qvec = None
    if embed_query is not None:
        qvec = "[" + ",".join(f"{x:.7f}" for x in embed_query(query)) + "]"
    params = {
        "as_of": as_of,
        "since": as_of - timedelta(hours=window_hours),
        "asset": asset,
        "qvec": qvec,
        "query": query,
        "n": s.retrieval_candidates,
    }
    with engine.connect() as conn:
        rows = conn.execute(text(SEARCH_SQL), params).mappings().all()
    scored = []
    for r in rows:
        rrf = sum(1 / (s.rrf_k + rank) for rank in (r["rank_vec"], r["rank_fts"]) if rank)
        decay = decay_factor(r["published_at"], as_of, half_life)
        scored.append((rrf * decay, rrf, decay, r))
    scored.sort(key=lambda x: (-x[0], x[3]["id"]))
    per_article: dict[str, int] = {}
    hits: list[NewsHit] = []
    for score, rrf, decay, r in scored:
        if per_article.get(r["article_id"], 0) >= s.max_chunks_per_article:
            continue
        per_article[r["article_id"]] = per_article.get(r["article_id"], 0) + 1
        hits.append(
            NewsHit(
                id=ids.next("c_"),
                chunk_id=r["id"],
                article_id=r["article_id"],
                title=r["title"],
                source=r["source"],
                published_at=r["published_at"].isoformat(),
                url=r["url"],
                text=r["text"],
                rank_vec=r["rank_vec"],
                rank_fts=r["rank_fts"],
                rrf=round(rrf, 6),
                decay=round(decay, 4),
                score=round(score, 6),
            )
        )
        if len(hits) == k:
            break
    if with_reaction:
        symbol = ASSET_SYMBOL.get(asset) if asset else None
        for h in hits:
            h.price_reaction = [
                m.to_dict()
                for m in news_price_reaction(
                    engine, h.article_id, as_of, symbol=symbol or "BTCUSDT", ids=ids
                )
            ]
    return hits


def search_news_around(
    engine: Engine,
    at: datetime,
    as_of: datetime,
    asset: str | None = None,
    hours: float = 6,
    limit: int = 6,
    ids: IdAllocator | None = None,
) -> list[dict[str, Any]]:
    """Articles published within ±hours of a moment (e.g. the sharpest move), never after as_of."""
    ids = ids or IdAllocator()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT ON (a.id) a.id AS article_id, a.title, a.source, a.url, a.published_at, "
                "c.id AS chunk_id, c.text FROM articles a JOIN chunks c ON c.article_id = a.id "
                "WHERE a.published_at BETWEEN :f AND :t AND a.published_at <= :as_of "
                "AND NOT a.is_liveblog "
                "AND (CAST(:asset AS text) IS NULL OR CAST(:asset AS text) = ANY(c.assets)) "
                "ORDER BY a.id, c.chunk_index"
            ),
            {"f": at - timedelta(hours=hours), "t": at + timedelta(hours=hours), "as_of": as_of, "asset": asset},
        ).mappings().all()  # fmt: skip
    rows = sorted(rows, key=lambda r: abs((r["published_at"] - at).total_seconds()))[:limit]
    return [
        {
            "id": ids.next("c_"),
            **{k: v for k, v in r.items() if k != "published_at"},
            "published_at": r["published_at"].isoformat(),
            "offset_hours": round((r["published_at"] - at).total_seconds() / 3600, 2),
        }
        for r in rows
    ]


class HybridNewsRetriever(BaseRetriever):
    """LangChain wrapper. as_of is fixed at construction, never taken from the query."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    engine: Engine
    as_of: datetime
    asset: str | None = None
    window_hours: float = 72
    k: int = 6
    embed_query: QueryEmbedder | None = None

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        hits = search_news(
            self.engine, query, self.as_of, self.asset, self.window_hours, self.k, self.embed_query
        )
        return [
            Document(
                page_content=h.text, metadata={k: v for k, v in h.to_dict().items() if k != "text"}
            )
            for h in hits
        ]


def openai_query_embedder(engine: Engine) -> QueryEmbedder:
    from crypto_rag.processing.embed import embed_texts

    def _embed(query: str) -> list[float]:
        return embed_texts(engine, [query], purpose="embed_query")[0]

    return _embed


def hits_to_dicts(hits: Sequence[NewsHit]) -> list[dict[str, Any]]:
    return [h.to_dict() for h in hits]
