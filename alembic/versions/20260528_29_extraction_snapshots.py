"""extraction_snapshots table

Revision ID: 20260528_29
Revises: 20260528_28
Create Date: 2026-05-28 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260528_29"
down_revision: Union[str, None] = "20260528_28"
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
    if _table_exists("extraction_snapshots"):
        return

    op.create_table(
        "extraction_snapshots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tender_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "snapshot_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("extracted_v1", postgresql.JSONB(), nullable=True),
        sa.Column("extract_meta_v1", postgresql.JSONB(), nullable=True),
        sa.Column("pipeline_versions", postgresql.JSONB(), nullable=True),
        sa.Column("doc_signature", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(50), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
    )
    op.create_index(
        "idx_extraction_snapshots_company_tender",
        "extraction_snapshots",
        ["company_id", "tender_id"],
    )


def downgrade() -> None:
    if not _table_exists("extraction_snapshots"):
        return
    op.drop_index("idx_extraction_snapshots_company_tender", table_name="extraction_snapshots")
    op.drop_table("extraction_snapshots")
