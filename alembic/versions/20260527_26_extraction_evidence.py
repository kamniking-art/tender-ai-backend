"""extraction_evidence table

Revision ID: 20260527_26
Revises: 20260525_24
Create Date: 2026-05-27 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260527_26"
down_revision: Union[str, None] = "20260525_24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=:t"
    ), {"t": table})
    return result.fetchone() is not None


def upgrade() -> None:
    if _table_exists("extraction_evidence"):
        return

    op.create_table(
        "extraction_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tender_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("field_name", sa.String(100), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(50), nullable=True),
        sa.Column("parser_version", sa.String(20), nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint(
        "uq_extraction_evidence_company_tender_field",
        "extraction_evidence",
        ["company_id", "tender_id", "field_name"],
    )
    op.create_index(
        "idx_extraction_evidence_company_tender",
        "extraction_evidence",
        ["company_id", "tender_id"],
    )


def downgrade() -> None:
    if not _table_exists("extraction_evidence"):
        return
    op.drop_index("idx_extraction_evidence_company_tender", table_name="extraction_evidence")
    op.drop_constraint("uq_extraction_evidence_company_tender_field", "extraction_evidence", type_="unique")
    op.drop_table("extraction_evidence")
