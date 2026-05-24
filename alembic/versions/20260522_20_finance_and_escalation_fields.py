"""finance snapshot fields + tender_id in escalations

Revision ID: 20260522_20
Revises: 20260522_19
Create Date: 2026-05-22 20:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260522_20"
down_revision: Union[str, None] = "20260522_19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── Idempotency helpers ───────────────────────────────────────────────────────


def _column_exists(table_name: str, column_name: str) -> bool:
    """Return True if *column_name* already exists in *table_name*."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def _index_exists(index_name: str) -> bool:
    """Return True if a PostgreSQL index with *index_name* already exists."""
    bind = op.get_bind()
    result = bind.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
        {"name": index_name},
    ).fetchone()
    return result is not None


# ── Upgrade ───────────────────────────────────────────────────────────────────


def upgrade() -> None:
    # ── escalations: add direct tender_id FK column ───────────────────────────
    if not _column_exists("escalations", "tender_id"):
        op.add_column(
            "escalations",
            sa.Column(
                "tender_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("tenders.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )

    if not _index_exists("idx_escalations_tender_id"):
        op.create_index(
            "idx_escalations_tender_id",
            "escalations",
            ["tender_id"],
            unique=False,
        )

    # ── tender_finance: add financial snapshot fields ─────────────────────────
    if not _column_exists("tender_finance", "profitability_status"):
        op.add_column(
            "tender_finance",
            sa.Column("profitability_status", sa.Text(), nullable=True),
        )

    if not _column_exists("tender_finance", "is_loss_leader"):
        op.add_column(
            "tender_finance",
            sa.Column(
                "is_loss_leader",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )

    if not _column_exists("tender_finance", "gross_margin"):
        op.add_column(
            "tender_finance",
            sa.Column("gross_margin", sa.Numeric(14, 2), nullable=True),
        )

    if not _column_exists("tender_finance", "gross_margin_pct"):
        op.add_column(
            "tender_finance",
            sa.Column("gross_margin_pct", sa.Numeric(7, 4), nullable=True),
        )

    if not _column_exists("tender_finance", "expected_value"):
        op.add_column(
            "tender_finance",
            sa.Column("expected_value", sa.Numeric(14, 2), nullable=True),
        )

    if not _column_exists("tender_finance", "finance_calculated_at"):
        op.add_column(
            "tender_finance",
            sa.Column(
                "finance_calculated_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )


# ── Downgrade ─────────────────────────────────────────────────────────────────


def downgrade() -> None:
    if _column_exists("tender_finance", "finance_calculated_at"):
        op.drop_column("tender_finance", "finance_calculated_at")
    if _column_exists("tender_finance", "expected_value"):
        op.drop_column("tender_finance", "expected_value")
    if _column_exists("tender_finance", "gross_margin_pct"):
        op.drop_column("tender_finance", "gross_margin_pct")
    if _column_exists("tender_finance", "gross_margin"):
        op.drop_column("tender_finance", "gross_margin")
    if _column_exists("tender_finance", "is_loss_leader"):
        op.drop_column("tender_finance", "is_loss_leader")
    if _column_exists("tender_finance", "profitability_status"):
        op.drop_column("tender_finance", "profitability_status")

    if _index_exists("idx_escalations_tender_id"):
        op.drop_index("idx_escalations_tender_id", table_name="escalations")
    if _column_exists("escalations", "tender_id"):
        op.drop_column("escalations", "tender_id")
