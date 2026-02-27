"""add tender decisions core

Revision ID: 20260227_05
Revises: 20260227_04
Create Date: 2026-02-27 14:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260227_05"
down_revision: Union[str, None] = "20260227_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tender_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tender_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recommendation", sa.Text(), server_default="unsure", nullable=False),
        sa.Column("rationale", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("assumptions", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("nmck", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("expected_revenue", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("cogs", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("logistics_cost", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("other_costs", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("expected_margin_value", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("expected_margin_pct", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("risk_score", sa.Integer(), server_default="0", nullable=False),
        sa.Column("risk_flags", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("need_bid_security", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("bid_security_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("need_contract_security", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("contract_security_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "tender_id", name="uq_tender_decisions_company_tender"),
    )

    op.create_index(op.f("ix_tender_decisions_tender_id"), "tender_decisions", ["tender_id"], unique=False)
    op.create_index(
        "idx_tender_decisions_company_recommendation",
        "tender_decisions",
        ["company_id", "recommendation"],
        unique=False,
    )
    op.create_index("idx_tender_decisions_company_risk_score", "tender_decisions", ["company_id", "risk_score"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_tender_decisions_company_risk_score", table_name="tender_decisions")
    op.drop_index("idx_tender_decisions_company_recommendation", table_name="tender_decisions")
    op.drop_index(op.f("ix_tender_decisions_tender_id"), table_name="tender_decisions")
    op.drop_table("tender_decisions")
