"""add tenders core table

Revision ID: 20260227_02
Revises: 20260227_01
Create Date: 2026-02-27 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260227_02"
down_revision: Union[str, None] = "20260227_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("customer_name", sa.Text(), nullable=True),
        sa.Column("region", sa.Text(), nullable=True),
        sa.Column("procurement_type", sa.Text(), nullable=True),
        sa.Column("nmck", sa.Numeric(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submission_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), server_default="new", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "source", "external_id", name="uq_tenders_company_source_external"),
    )

    op.create_index(op.f("ix_tenders_company_id"), "tenders", ["company_id"], unique=False)
    op.create_index(op.f("ix_tenders_region"), "tenders", ["region"], unique=False)
    op.create_index(op.f("ix_tenders_procurement_type"), "tenders", ["procurement_type"], unique=False)
    op.create_index(op.f("ix_tenders_nmck"), "tenders", ["nmck"], unique=False)
    op.create_index(op.f("ix_tenders_published_at"), "tenders", ["published_at"], unique=False)
    op.create_index(op.f("ix_tenders_submission_deadline"), "tenders", ["submission_deadline"], unique=False)
    op.create_index(op.f("ix_tenders_status"), "tenders", ["status"], unique=False)

    op.create_index("idx_tenders_company_status", "tenders", ["company_id", "status"], unique=False)
    op.create_index("idx_tenders_company_deadline", "tenders", ["company_id", "submission_deadline"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_tenders_company_deadline", table_name="tenders")
    op.drop_index("idx_tenders_company_status", table_name="tenders")
    op.drop_index(op.f("ix_tenders_status"), table_name="tenders")
    op.drop_index(op.f("ix_tenders_submission_deadline"), table_name="tenders")
    op.drop_index(op.f("ix_tenders_published_at"), table_name="tenders")
    op.drop_index(op.f("ix_tenders_nmck"), table_name="tenders")
    op.drop_index(op.f("ix_tenders_procurement_type"), table_name="tenders")
    op.drop_index(op.f("ix_tenders_region"), table_name="tenders")
    op.drop_index(op.f("ix_tenders_company_id"), table_name="tenders")
    op.drop_table("tenders")
