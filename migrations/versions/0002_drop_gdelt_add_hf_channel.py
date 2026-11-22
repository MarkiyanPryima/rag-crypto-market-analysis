"""Drop GDELT, add the Hugging Face channel, let `source` name any publisher.

GDELT was removed as a source (its time is when GDELT saw an article, not when it
was published). Hugging Face datasets aggregate many publishers, so
`articles.source` now holds the publisher's short name and `channel` says how the
article reached the system: 'api' (The Guardian), 'rss' (CoinDesk), 'hf'.

Downgrade restores the old constraints after deleting 'hf' rows; the GDELT rows
deleted here cannot be restored (re-collect them with the v0-era module if needed).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Chunks and labels cascade with their articles.
    op.execute("DELETE FROM articles WHERE channel = 'gdelt'")
    op.drop_constraint("articles_source_check", "articles", type_="check")
    op.drop_constraint("articles_channel_check", "articles", type_="check")
    op.create_check_constraint("articles_channel_check", "articles", "channel IN ('api', 'rss', 'hf')")
    op.create_check_constraint("articles_source_check", "articles", "source <> ''")


def downgrade() -> None:
    op.execute("DELETE FROM articles WHERE channel = 'hf'")
    op.drop_constraint("articles_source_check", "articles", type_="check")
    op.drop_constraint("articles_channel_check", "articles", type_="check")
    op.create_check_constraint(
        "articles_channel_check", "articles", "channel IN ('api', 'rss', 'gdelt')"
    )
    op.create_check_constraint(
        "articles_source_check", "articles", "source IN ('guardian', 'coindesk')"
    )
