"""add tender_opportunity_report table

Revision ID: 20260605_37
Revises: 20260605_36
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "20260605_37"
down_revision = "20260605_36"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    from sqlalchemy import inspect
    return inspect(op.get_bind()).has_table(name)


def upgrade():
    if _table_exists("tender_opportunity_report"):
        return
    op.create_table(
        "tender_opportunity_report",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tender_id", UUID(as_uuid=True), sa.ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("strengths", JSONB, nullable=False, server_default="'[]'::jsonb"),
        sa.Column("risks", JSONB, nullable=False, server_default="'[]'::jsonb"),
        sa.Column("missing_information", JSONB, nullable=False, server_default="'[]'::jsonb"),
        sa.Column("required_documents", JSONB, nullable=False, server_default="'[]'::jsonb"),
        sa.Column("recommended_actions", JSONB, nullable=False, server_default="'[]'::jsonb"),
        sa.Column("recommendation", sa.Text, nullable=False, server_default="'unsure'"),
        sa.Column("score", sa.Integer, nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tender_id", "company_id", name="uq_opp_report_tender_company"),
    )
    op.create_index("ix_opp_report_company_id", "tender_opportunity_report", ["company_id"])


def downgrade():
    op.drop_table("tender_opportunity_report")
