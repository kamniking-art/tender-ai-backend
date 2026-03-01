"""add tender finance v2

Revision ID: 20260301_11
Revises: 20260301_10
Create Date: 2026-03-01 14:40:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260301_11"
down_revision: Union[str, Sequence[str], None] = "20260301_10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tender_finance",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tender_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cost_estimate", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("participation_cost", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("win_probability", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "tender_id", name="uq_tender_finance_company_tender"),
    )
    op.create_index(op.f("ix_tender_finance_company_id"), "tender_finance", ["company_id"], unique=False)
    op.create_index(op.f("ix_tender_finance_tender_id"), "tender_finance", ["tender_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_tender_finance_tender_id"), table_name="tender_finance")
    op.drop_index(op.f("ix_tender_finance_company_id"), table_name="tender_finance")
    op.drop_table("tender_finance")
