"""tender alerts core

Revision ID: 20260227_08
Revises: 20260227_07
Create Date: 2026-02-27 16:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "20260227_08"
down_revision: Union[str, None] = "20260227_07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


alert_category_enum = postgresql.ENUM(
    "new",
    "deadline_soon",
    "risky",
    "go",
    "no_go",
    "overdue_task",
    name="alert_category",
    create_type=False,
)


def upgrade() -> None:
    alert_category_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "tender_alert_views",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tender_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category", alert_category_enum, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "user_id",
            "tender_id",
            "category",
            name="uq_tender_alert_views_company_user_tender_category",
        ),
    )
    op.create_index(op.f("ix_tender_alert_views_company_id"), "tender_alert_views", ["company_id"], unique=False)
    op.create_index(op.f("ix_tender_alert_views_user_id"), "tender_alert_views", ["user_id"], unique=False)
    op.create_index(op.f("ix_tender_alert_views_tender_id"), "tender_alert_views", ["tender_id"], unique=False)
    op.create_index(op.f("ix_tender_alert_views_category"), "tender_alert_views", ["category"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_tender_alert_views_category"), table_name="tender_alert_views")
    op.drop_index(op.f("ix_tender_alert_views_tender_id"), table_name="tender_alert_views")
    op.drop_index(op.f("ix_tender_alert_views_user_id"), table_name="tender_alert_views")
    op.drop_index(op.f("ix_tender_alert_views_company_id"), table_name="tender_alert_views")
    op.drop_table("tender_alert_views")
    alert_category_enum.drop(op.get_bind(), checkfirst=True)
