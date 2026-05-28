"""ai_cost_log v2: add operation_type, provider, status, error_code

Revision ID: 20260528_30
Revises: 20260528_29
Create Date: 2026-05-28 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260528_30"
down_revision: Union[str, None] = "20260528_29"
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
    if not _column_exists("ai_cost_log", "operation_type"):
        op.add_column(
            "ai_cost_log",
            sa.Column(
                "operation_type",
                sa.String(50),
                nullable=True,
                comment="extraction | fallback | cache_hit",
            ),
        )
    if not _column_exists("ai_cost_log", "provider"):
        op.add_column(
            "ai_cost_log",
            sa.Column("provider", sa.String(50), nullable=True),
        )
    if not _column_exists("ai_cost_log", "status"):
        op.add_column(
            "ai_cost_log",
            sa.Column(
                "status",
                sa.String(20),
                nullable=True,
                comment="ok | error | timeout",
            ),
        )
    if not _column_exists("ai_cost_log", "error_code"):
        op.add_column(
            "ai_cost_log",
            sa.Column("error_code", sa.String(50), nullable=True),
        )


def downgrade() -> None:
    for col in ("error_code", "status", "provider", "operation_type"):
        if _column_exists("ai_cost_log", col):
            op.drop_column("ai_cost_log", col)
