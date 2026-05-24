"""agent_evaluation table + supporting enums

Revision ID: 20260522_22
Revises: 20260522_21
Create Date: 2026-05-22 22:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "20260522_22"
down_revision: Union[str, None] = "20260522_21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── Idempotency helpers ────────────────────────────────────────────────────────


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    result = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :name"
        ),
        {"name": table_name},
    ).fetchone()
    return result is not None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def _index_exists(index_name: str) -> bool:
    bind = op.get_bind()
    result = bind.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
        {"name": index_name},
    ).fetchone()
    return result is not None


# ── Upgrade ────────────────────────────────────────────────────────────────────


def upgrade() -> None:
    # Create enums (idempotent via DO block)
    op.execute("""
        DO $$
        BEGIN
            CREATE TYPE agent_recommendation_enum AS ENUM ('go', 'no_go', 'needs_review');
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$;
    """)
    op.execute("COMMIT")

    op.execute("""
        DO $$
        BEGIN
            CREATE TYPE human_decision_enum AS ENUM ('participate', 'skip', 'deferred');
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$;
    """)
    op.execute("COMMIT")

    op.execute("""
        DO $$
        BEGIN
            CREATE TYPE actual_result_enum AS ENUM ('won', 'lost', 'cancelled', 'not_submitted');
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$;
    """)
    op.execute("COMMIT")

    # Create agent_evaluation table (idempotent)
    if not _table_exists("agent_evaluation"):
        op.create_table(
            "agent_evaluation",
            sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "company_id",
                PG_UUID(as_uuid=True),
                sa.ForeignKey("companies.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "tender_id",
                PG_UUID(as_uuid=True),
                sa.ForeignKey("tenders.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "agent_recommendation",
                sa.Enum(
                    "go", "no_go", "needs_review",
                    name="agent_recommendation_enum",
                    create_type=False,
                ),
                nullable=True,
            ),
            sa.Column(
                "human_decision",
                sa.Enum(
                    "participate", "skip", "deferred",
                    name="human_decision_enum",
                    create_type=False,
                ),
                nullable=True,
            ),
            sa.Column(
                "actual_result",
                sa.Enum(
                    "won", "lost", "cancelled", "not_submitted",
                    name="actual_result_enum",
                    create_type=False,
                ),
                nullable=True,
            ),
            sa.Column("was_right", sa.Boolean, nullable=True),
            sa.Column("notes", sa.Text, nullable=True),
            sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )

    # Indexes
    if not _index_exists("idx_agent_evaluation_company_id"):
        op.create_index(
            "idx_agent_evaluation_company_id",
            "agent_evaluation",
            ["company_id"],
        )

    if not _index_exists("idx_agent_evaluation_tender_id"):
        op.create_index(
            "idx_agent_evaluation_tender_id",
            "agent_evaluation",
            ["tender_id"],
        )

    # Unique constraint: one evaluation per (company_id, tender_id)
    if not _index_exists("uq_agent_evaluation_company_tender"):
        op.create_index(
            "uq_agent_evaluation_company_tender",
            "agent_evaluation",
            ["company_id", "tender_id"],
            unique=True,
        )


# ── Downgrade ──────────────────────────────────────────────────────────────────


def downgrade() -> None:
    if _index_exists("uq_agent_evaluation_company_tender"):
        op.drop_index("uq_agent_evaluation_company_tender", table_name="agent_evaluation")

    if _index_exists("idx_agent_evaluation_tender_id"):
        op.drop_index("idx_agent_evaluation_tender_id", table_name="agent_evaluation")

    if _index_exists("idx_agent_evaluation_company_id"):
        op.drop_index("idx_agent_evaluation_company_id", table_name="agent_evaluation")

    if _table_exists("agent_evaluation"):
        op.drop_table("agent_evaluation")

    op.execute("DROP TYPE IF EXISTS actual_result_enum")
    op.execute("DROP TYPE IF EXISTS human_decision_enum")
    op.execute("DROP TYPE IF EXISTS agent_recommendation_enum")
