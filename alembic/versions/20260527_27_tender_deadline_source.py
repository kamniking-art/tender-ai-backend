"""tender deadline_source, deadline_confidence, deadline_updated_at columns

Revision ID: 20260527_27
Revises: 20260527_26
Create Date: 2026-05-27 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260527_27"
down_revision: Union[str, None] = "20260527_26"
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
    if not _column_exists("tenders", "deadline_source"):
        op.add_column(
            "tenders",
            sa.Column(
                "deadline_source",
                sa.String(50),
                nullable=True,
                comment="eis_ingestion | document_extraction | manual_override",
            ),
        )
    if not _column_exists("tenders", "deadline_confidence"):
        op.add_column(
            "tenders",
            sa.Column("deadline_confidence", sa.Numeric(4, 3), nullable=True),
        )
    if not _column_exists("tenders", "deadline_updated_at"):
        op.add_column(
            "tenders",
            sa.Column("deadline_updated_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    for col in ("deadline_updated_at", "deadline_confidence", "deadline_source"):
        if _column_exists("tenders", col):
            op.drop_column("tenders", col)
