"""eval_dataset: add reason column

Revision ID: 20260605_36
Revises: 20260605_35
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa

revision = "20260605_36"
down_revision = "20260605_35"
branch_labels = None
depends_on = None


def _column_exists(table, column):
    from sqlalchemy import inspect
    return column in [c["name"] for c in inspect(op.get_bind()).get_columns(table)]


def upgrade():
    if not _column_exists("tender_eval_dataset", "reason"):
        op.add_column(
            "tender_eval_dataset",
            sa.Column("reason", sa.Text(), nullable=True),
        )


def downgrade():
    if _column_exists("tender_eval_dataset", "reason"):
        op.drop_column("tender_eval_dataset", "reason")
