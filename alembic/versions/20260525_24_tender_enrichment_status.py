"""tender nmck_enrichment_status column

Revision ID: 20260525_24
Revises: 20260524_23
Create Date: 2026-05-25 00:00:00.000000
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "20260525_24"
down_revision: Union[str, None] = "20260524_23"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
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
    if _column_exists("tenders", "nmck_enrichment_status"):
        op.drop_column("tenders", "nmck_enrichment_status")
