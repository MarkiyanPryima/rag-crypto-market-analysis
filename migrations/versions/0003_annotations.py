"""Manual annotations for the evaluation (stage 17).

One row per item the author judges in the UI: an article's relevance to a question,
a claim the judge model extracted from an answer, or an article's topic/tone label.
`payload` is what the annotation page shows, `label` is the author's answer (NULL
until annotated). Only the author writes `label`; model outputs live in `payload`.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "annotations",
        sa.Column("task", sa.Text, primary_key=True),
        sa.Column("item_id", sa.Text, primary_key=True),
        sa.Column("payload", pg.JSONB, nullable=False),
        sa.Column("label", pg.JSONB),
        sa.Column("labelled_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "task IN ('relevance', 'claims', 'labels')", name="annotations_task_check"
        ),
    )


def downgrade() -> None:
    op.drop_table("annotations")
