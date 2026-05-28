"""extraction_evidence: rename confidence to extraction_completeness

Revision ID: 20260528_28
Revises: 20260527_27
Create Date: 2026-05-28 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260528_28"
down_revision: Union[str, None] = "20260527_27"
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
    if _column_exists("extraction_evidence", "confidence") and \
            not _column_exists("extraction_evidence", "extraction_completeness"):
        op.alter_column(
            "extraction_evidence",
            "confidence",
            new_column_name="extraction_completeness",
        )


def downgrade() -> None:
    if _column_exists("extraction_evidence", "extraction_completeness") and \
            not _column_exists("extraction_evidence", "confidence"):
        op.alter_column(
            "extraction_evidence",
            "extraction_completeness",
            new_column_name="confidence",
        )
