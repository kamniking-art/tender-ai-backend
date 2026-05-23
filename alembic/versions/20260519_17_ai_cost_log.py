"""add ai_cost_log table

Revision ID: 20260519_17
Revises: 20260328_16
Create Date: 2026-05-19 09:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "20260519_17"
down_revision: Union[str, None] = "20260328_16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_cost_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tender_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("chars_sent", sa.Integer(), server_default="0", nullable=False),
        sa.Column("estimated_cost", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ai_cost_log_tender_id"), "ai_cost_log", ["tender_id"], unique=False)
    op.create_index(
        "idx_ai_cost_log_company_tender_created",
        "ai_cost_log",
        ["company_id", "tender_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_ai_cost_log_company_tender_created", table_name="ai_cost_log")
    op.drop_index(op.f("ix_ai_cost_log_tender_id"), table_name="ai_cost_log")
    op.drop_table("ai_cost_log")

