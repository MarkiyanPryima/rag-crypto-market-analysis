"""Initial schema: market data, articles, chunks, labels, episodes, forecasts, logs.

Revision ID: 0001
Revises:
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql as pg

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TOPICS = "'regulation', 'hacks_and_failures', 'institutional', 'technology', 'macro', 'market', 'other'"
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "candles",
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("ts", TS, primary_key=True),
        *(
            sa.Column(c, sa.Numeric, nullable=False)
            for c in ("open", "high", "low", "close", "volume", "quote_volume")
        ),
        sa.Column("trades", sa.Integer, nullable=False),
        sa.Column("taker_buy_volume", sa.Numeric, nullable=False),
        sa.Column("taker_buy_quote_volume", sa.Numeric, nullable=False),
    )
    op.execute("SELECT create_hypertable('candles', by_range('ts', INTERVAL '30 days'))")

    op.create_table(
        "funding_rates",
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("ts", TS, primary_key=True),
        sa.Column("rate", sa.Numeric, nullable=False),
    )

    op.create_table(
        "articles",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("standfirst", sa.Text),
        sa.Column("body_text", sa.Text),
        sa.Column("published_at", TS, nullable=False),
        sa.Column("fetched_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("section", sa.Text),
        sa.Column("tags", pg.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("is_liveblog", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("ai_assisted", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("raw", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("content_hash", sa.Text),
        sa.CheckConstraint("source IN ('guardian', 'coindesk')", name="articles_source_check"),
        sa.CheckConstraint("channel IN ('api', 'rss', 'gdelt')", name="articles_channel_check"),
    )
    op.create_index("ix_articles_published_at", "articles", ["published_at"])

    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column(
            "article_id",
            sa.Text,
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("published_at", TS, nullable=False),
        sa.Column("assets", pg.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("embedding", Vector(1536)),
        sa.Column("embedding_model", sa.Text),
        sa.Column(
            "tsv",
            pg.TSVECTOR,
            sa.Computed("to_tsvector('english'::regconfig, text)", persisted=True),
        ),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.UniqueConstraint("article_id", "chunk_index", name="chunks_article_chunk_key"),
    )
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")
    op.create_index("ix_chunks_published_at", "chunks", ["published_at"])
    op.create_index("ix_chunks_assets", "chunks", ["assets"], postgresql_using="gin")
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "article_labels",
        sa.Column(
            "article_id",
            sa.Text,
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("topic", sa.Text, nullable=False),
        sa.Column("tone", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("labeled_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(f"topic IN ({TOPICS})", name="article_labels_topic_check"),
        sa.CheckConstraint(
            "tone IN ('negative', 'neutral', 'positive')", name="article_labels_tone_check"
        ),
    )

    op.create_table(
        "episodes",
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("start_ts", TS, primary_key=True),
        sa.Column("end_ts", TS, nullable=False),
        sa.Column("return_pct", sa.Float, nullable=False),
        sa.Column("max_drawdown", sa.Float, nullable=False),
        sa.Column("shape", Vector(48), nullable=False),
        sa.Column("return_7d_after_pct", sa.Float),
    )
    op.create_index("ix_episodes_end_ts", "episodes", ["symbol", "end_ts"])

    op.create_table(
        "features",
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("features", pg.JSONB, nullable=False),
    )
    op.create_table(
        "targets",
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("return_7d_pct", sa.Float),
        sa.Column("realized_vol_next_day", sa.Float),
    )

    op.create_table(
        "forecasts",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("contributions", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("model_version", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('buy_signal', 'volatility')", name="forecasts_kind_check"),
        sa.UniqueConstraint("symbol", "date", "kind", "model_version", name="forecasts_unique_key"),
    )

    op.create_table(
        "query_log",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("asset", sa.Text),
        sa.Column("as_of", TS, nullable=False),
        sa.Column("intent", sa.Text),
        sa.Column("tool_calls", pg.JSONB, nullable=False, server_default="[]"),
        sa.Column("retrieved", pg.JSONB, nullable=False, server_default="[]"),
        sa.Column("answer", sa.Text),
        sa.Column("verification", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("trace", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "llm_usage",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("ts", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("purpose", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float),
    )


def downgrade() -> None:
    for table in (
        "llm_usage",
        "query_log",
        "forecasts",
        "targets",
        "features",
        "episodes",
        "article_labels",
        "chunks",
        "articles",
        "funding_rates",
        "candles",
    ):
        op.drop_table(table)
