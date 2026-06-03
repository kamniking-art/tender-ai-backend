"""add customer_email to tenders

Revision ID: 20260529_32
Revises: 20260528_31
Create Date: 2026-05-29 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260529_32"
down_revision: Union[str, None] = "20260528_31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    from sqlalchemy import inspect
    bind = op.get_bind()
    return column in [c["name"] for c in inspect(bind).get_columns(table)]


def upgrade() -> None:
    if not _column_exists("tenders", "customer_email"):
        op.add_column(
            "tenders",
            sa.Column("customer_email", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if _column_exists("tenders", "customer_email"):
        op.drop_column("tenders", "customer_email")
