"""tender nmck_enrichment_status column

Revision ID: 20260525_24
Revises: 20260524_23
Create Date: 2026-05-25 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


def _column_exists(table, column):
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name=:t AND column_name=:c"
    ), {"t": table, "c": column})
    return result.fetchone() is not None


def upgrade() -> None:
    if not _column_exists("tenders", "nmck_enrichment_status"):
        op.add_column(
            "tenders",
            sa.Column(
                "nmck_enrichment_status",
                sa.String(30),
                nullable=True,
                comment="pending | running | done | failed | skipped"
            ),
        )


def downgrade() -> None:
    op.drop_column("tenders", "nmck_enrichment_status")
