"""tender nmck_source and nmck_confidence columns

Revision ID: 20260524_23
Revises: 20260522_22
Create Date: 2026-05-24 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260524_23"
down_revision: Union[str, None] = "20260522_22"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── Idempotency helpers ────────────────────────────────────────────────────────


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


# ── Upgrade ────────────────────────────────────────────────────────────────────


def upgrade() -> None:
    if not _column_exists("tenders", "nmck_source"):
        op.add_column(
            "tenders",
            sa.Column("nmck_source", sa.String(50), nullable=True),
        )

    if not _column_exists("tenders", "nmck_confidence"):
        op.add_column(
            "tenders",
            sa.Column("nmck_confidence", sa.Numeric(4, 2), nullable=True),
        )


# ── Downgrade ──────────────────────────────────────────────────────────────────


def downgrade() -> None:
    if _column_exists("tenders", "nmck_confidence"):
        op.drop_column("tenders", "nmck_confidence")

    if _column_exists("tenders", "nmck_source"):
        op.drop_column("tenders", "nmck_source")
