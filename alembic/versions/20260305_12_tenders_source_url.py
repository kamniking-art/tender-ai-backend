"""add source_url to tenders

Revision ID: 20260305_12
Revises: 20260301_11
Create Date: 2026-03-05 13:40:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260305_12"
down_revision: Union[str, Sequence[str], None] = "20260301_11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tenders", sa.Column("source_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenders", "source_url")
