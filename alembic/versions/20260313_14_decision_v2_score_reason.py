"""add decision v2 score and reason

Revision ID: 20260313_14
Revises: 20260312_13
Create Date: 2026-03-13 09:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260313_14"
down_revision = "20260312_13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tender_decisions", sa.Column("decision_score", sa.Integer(), nullable=True))
    op.add_column("tender_decisions", sa.Column("recommendation_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tender_decisions", "recommendation_reason")
    op.drop_column("tender_decisions", "decision_score")

