"""ORM models. The schema is created by Alembic migrations, not by create_all().

Raw market values are stored as NUMERIC so Binance strings are kept exactly
(rule 3: raw data is never altered). Analysis code casts to float when reading.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = 1536
EPISODE_LEN = 48

TOPICS = (
    "regulation",
    "hacks_and_failures",
    "institutional",
    "technology",
    "macro",
    "market",
    "other",
)
TONES = ("negative", "neutral", "positive")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        list[str]: ARRAY(Text),
    }


class Candle(Base):
    """Hourly spot candle; `ts` is the open time. TimescaleDB hypertable on ts."""

    __tablename__ = "candles"

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric)
    high: Mapped[Decimal] = mapped_column(Numeric)
    low: Mapped[Decimal] = mapped_column(Numeric)
    close: Mapped[Decimal] = mapped_column(Numeric)
    volume: Mapped[Decimal] = mapped_column(Numeric)
    quote_volume: Mapped[Decimal] = mapped_column(Numeric)
    trades: Mapped[int] = mapped_column(Integer)
    taker_buy_volume: Mapped[Decimal] = mapped_column(Numeric)
    taker_buy_quote_volume: Mapped[Decimal] = mapped_column(Numeric)


class FundingRate(Base):
    __tablename__ = "funding_rates"

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    rate: Mapped[Decimal] = mapped_column(Numeric)


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        # source = publisher short name (guardian, coindesk, decrypt, …); channel = how it arrived.
        CheckConstraint("source <> ''", name="articles_source_check"),
        CheckConstraint(_in("channel", ("api", "rss", "hf")), name="articles_channel_check"),
        Index("ix_articles_published_at", "published_at"),
    )

    # "<source>:<external id>", e.g. "guardian:technology/2025/jan/21/..."
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    standfirst: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime]
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())
    section: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(server_default="{}")
    is_liveblog: Mapped[bool] = mapped_column(Boolean, server_default="false")
    ai_assisted: Mapped[bool] = mapped_column(Boolean, server_default="false")
    raw: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    # Hash of title + standfirst + body; chunking reprocesses only when it changes.
    content_hash: Mapped[str | None] = mapped_column(Text)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("article_id", "chunk_index", name="chunks_article_chunk_key"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_chunks_published_at", "published_at"),
        Index("ix_chunks_assets", "assets", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    article_id: Mapped[str] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"))
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime]
    assets: Mapped[list[str]] = mapped_column(server_default="{}")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(Text)
    # Explicit regconfig cast: to_tsvector(text) alone is not IMMUTABLE and is
    # rejected in a generated column.
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english'::regconfig, text)", persisted=True)
    )
    content_hash: Mapped[str] = mapped_column(Text)


class ArticleLabel(Base):
    __tablename__ = "article_labels"
    __table_args__ = (
        CheckConstraint(_in("topic", TOPICS), name="article_labels_topic_check"),
        CheckConstraint(_in("tone", TONES), name="article_labels_tone_check"),
    )

    article_id: Mapped[str] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True
    )
    topic: Mapped[str] = mapped_column(Text)
    tone: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    labeled_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Episode(Base):
    """48-hour window starting at start_ts; shape = close / first close - 1."""

    __tablename__ = "episodes"

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    start_ts: Mapped[datetime] = mapped_column(primary_key=True)
    end_ts: Mapped[datetime]
    return_pct: Mapped[float] = mapped_column(Float)
    max_drawdown: Mapped[float] = mapped_column(Float)
    shape: Mapped[list[float]] = mapped_column(Vector(EPISODE_LEN))
    # Return from end_ts to end_ts + 7 days; NULL until that time has passed.
    return_7d_after_pct: Mapped[float | None] = mapped_column(Float)


class Feature(Base):
    __tablename__ = "features"

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    features: Mapped[dict[str, Any]]


class Target(Base):
    """Kept apart from features so a target can never leak into a feature row."""

    __tablename__ = "targets"

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    return_7d_pct: Mapped[float | None] = mapped_column(Float)
    realized_vol_next_day: Mapped[float | None] = mapped_column(Float)


class Forecast(Base):
    __tablename__ = "forecasts"
    __table_args__ = (
        CheckConstraint(_in("kind", ("buy_signal", "volatility")), name="forecasts_kind_check"),
        UniqueConstraint("symbol", "date", "kind", "model_version", name="forecasts_unique_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    symbol: Mapped[str] = mapped_column(Text)
    date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(Text)
    value: Mapped[float] = mapped_column(Float)
    contributions: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    model_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class QueryLog(Base):
    __tablename__ = "query_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    asset: Mapped[str | None] = mapped_column(Text)
    as_of: Mapped[datetime]
    intent: Mapped[str | None] = mapped_column(Text)
    tool_calls: Mapped[dict[str, Any]] = mapped_column(server_default="[]")
    retrieved: Mapped[dict[str, Any]] = mapped_column(server_default="[]")
    answer: Mapped[str | None] = mapped_column(Text)
    verification: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    trace: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


ANNOTATION_TASKS = ("relevance", "claims", "labels")


class Annotation(Base):
    """An item the author judges by hand (stage 17); `label` is NULL until then."""

    __tablename__ = "annotations"
    __table_args__ = (
        CheckConstraint(_in("task", ANNOTATION_TASKS), name="annotations_task_check"),
    )

    task: Mapped[str] = mapped_column(Text, primary_key=True)
    item_id: Mapped[str] = mapped_column(Text, primary_key=True)
    payload: Mapped[dict[str, Any]]
    label: Mapped[dict[str, Any] | None]
    labelled_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LlmUsage(Base):
    """One row per OpenAI call (rule 7: costs are logged)."""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(server_default=func.now())
    purpose: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cost_usd: Mapped[float | None] = mapped_column(Float)
