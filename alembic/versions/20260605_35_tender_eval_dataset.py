"""add tender_eval_dataset table

Revision ID: 20260605_35
Revises: 20260604_34
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "20260605_35"
down_revision = "20260604_34"
branch_labels = None
depends_on = None


def _table_exists(name):
    from sqlalchemy import inspect
    bind = op.get_bind()
    return inspect(bind).has_table(name)


def upgrade():
    if _table_exists("tender_eval_dataset"):
        return
    op.create_table(
        "tender_eval_dataset",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tender_id", UUID(as_uuid=True), sa.ForeignKey("tenders.id"), nullable=False),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=True),
        sa.Column("expected_decision", sa.Text, nullable=False),  # go/no_go/review
        sa.Column("expected_risks", JSONB, nullable=True),        # список ожидаемых рисков
        sa.Column("expected_reason", sa.Text, nullable=True),     # обоснование
        sa.Column("verified_by", sa.Text, nullable=True),         # кто верифицировал
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("tender_id", "company_id", name="uq_eval_dataset_tender_company"),
    )
    op.create_index("ix_eval_dataset_company_id", "tender_eval_dataset", ["company_id"])
    op.create_index("ix_eval_dataset_expected_decision", "tender_eval_dataset", ["expected_decision"])


def downgrade():
    op.drop_table("tender_eval_dataset")
