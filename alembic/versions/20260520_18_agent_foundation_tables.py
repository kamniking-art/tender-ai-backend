"""add agent foundation tables

Revision ID: 20260520_18
Revises: 20260519_17
Create Date: 2026-05-20 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "20260520_18"
down_revision: Union[str, None] = "20260519_17"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SANDBOX_MODE_ENUM = postgresql.ENUM(
    "simulate",
    "dry-run",
    "production",
    name="sandbox_mode_enum",
    create_type=False,
)
ACTION_STATUS_ENUM = postgresql.ENUM(
    "pending",
    "running",
    "completed",
    "failed",
    "rolled_back",
    name="action_status_enum",
    create_type=False,
)
ESCALATION_STATUS_ENUM = postgresql.ENUM(
    "pending",
    "approved",
    "rejected",
    "timeout",
    name="escalation_status_enum",
    create_type=False,
)
DEADLINE_STATUS_ENUM = postgresql.ENUM(
    "safe",
    "warning",
    "urgent",
    "expired",
    name="deadline_status_enum",
    create_type=False,
)
REQUIREMENT_STATUS_ENUM = postgresql.ENUM(
    "ok",
    "missing",
    "unknown",
    "risk",
    name="requirement_status_enum",
    create_type=False,
)
TENDER_STAGE_ENUM = postgresql.ENUM(
    "found",
    "documents_loaded",
    "analyzed",
    "needs_review",
    "approved_to_prepare",
    "package_ready",
    "submitted_manually",
    "result_tracked",
    "closed",
    name="tender_stage_enum",
    create_type=False,
)
CLARIFICATION_STATUS_ENUM = postgresql.ENUM(
    "draft",
    "approved",
    "sent",
    "answered",
    "timeout",
    name="clarification_status_enum",
    create_type=False,
)


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names(schema="public")


def _create_enums() -> None:
    bind = op.get_bind()
    for enum_type in (
        SANDBOX_MODE_ENUM,
        ACTION_STATUS_ENUM,
        ESCALATION_STATUS_ENUM,
        DEADLINE_STATUS_ENUM,
        REQUIREMENT_STATUS_ENUM,
        TENDER_STAGE_ENUM,
        CLARIFICATION_STATUS_ENUM,
    ):
        enum_type.create(bind, checkfirst=True)


def _drop_enums() -> None:
    bind = op.get_bind()
    for enum_type in (
        CLARIFICATION_STATUS_ENUM,
        TENDER_STAGE_ENUM,
        REQUIREMENT_STATUS_ENUM,
        DEADLINE_STATUS_ENUM,
        ESCALATION_STATUS_ENUM,
        ACTION_STATUS_ENUM,
        SANDBOX_MODE_ENUM,
    ):
        enum_type.drop(bind, checkfirst=True)


def upgrade() -> None:
    _create_enums()

    if not _table_exists("agents"):
        op.create_table(
            "agents",
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("role", sa.Text(), nullable=False),
            sa.Column("specialization", sa.Text(), nullable=True),
            sa.Column("authority_level", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("tool_permissions", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("sandbox_mode", SANDBOX_MODE_ENUM, nullable=False, server_default="simulate"),
            sa.Column("persona", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.PrimaryKeyConstraint("agent_id"),
        )
        op.create_index("idx_agents_company_role", "agents", ["company_id", "role"], unique=False)

    if not _table_exists("tasks"):
        op.create_table(
            "tasks",
            sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tender_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("task_type", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
            sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"]),
            sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"]),
            sa.PrimaryKeyConstraint("task_id"),
        )
        op.create_index("idx_tasks_company_status_priority", "tasks", ["company_id", "status", "priority"], unique=False)
        op.create_index("idx_tasks_tender_id", "tasks", ["tender_id"], unique=False)

    if not _table_exists("actions"):
        op.create_table(
            "actions",
            sa.Column("action_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("action_type", sa.Text(), nullable=False),
            sa.Column("target", sa.Text(), nullable=True),
            sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("status", ACTION_STATUS_ENUM, nullable=False, server_default="pending"),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("rollback_possible", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"]),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"]),
            sa.PrimaryKeyConstraint("action_id"),
            sa.CheckConstraint(
                "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
                name="ck_actions_confidence_range",
            ),
        )
        op.create_index("idx_actions_company_status", "actions", ["company_id", "status"], unique=False)
        op.create_index("idx_actions_task_id", "actions", ["task_id"], unique=False)

    if not _table_exists("events"):
        op.create_table(
            "events",
            sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("event_type", sa.Text(), nullable=False),
            sa.Column("source", sa.Text(), nullable=True),
            sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("processed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.PrimaryKeyConstraint("event_id"),
        )
        op.create_index("idx_events_processed_created", "events", ["processed", "created_at"], unique=False)
        op.create_index("idx_events_type_created", "events", ["event_type", "created_at"], unique=False)

    if not _table_exists("policies"):
        op.create_table(
            "policies",
            sa.Column("policy_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("policy_type", sa.Text(), nullable=False),
            sa.Column("condition", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("action", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.PrimaryKeyConstraint("policy_id"),
        )
        op.create_index("idx_policies_company_active_priority", "policies", ["company_id", "active", "priority"], unique=False)

    if not _table_exists("escalations"):
        op.create_table(
            "escalations",
            sa.Column("escalation_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("status", ESCALATION_STATUS_ENUM, nullable=False, server_default="pending"),
            sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("override_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"]),
            sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
            sa.PrimaryKeyConstraint("escalation_id"),
            sa.CheckConstraint(
                "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
                name="ck_escalations_confidence_range",
            ),
        )
        op.create_index("idx_escalations_company_status", "escalations", ["company_id", "status"], unique=False)

    if not _table_exists("reasoning_traces"):
        op.create_table(
            "reasoning_traces",
            sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("tender_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("decision", sa.Text(), nullable=True),
            sa.Column("factors", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("rules_fired", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("evidence_used", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"]),
            sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"]),
            sa.PrimaryKeyConstraint("trace_id"),
            sa.CheckConstraint(
                "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
                name="ck_reasoning_traces_confidence_range",
            ),
        )
        op.create_index("idx_reasoning_traces_company_tender", "reasoning_traces", ["company_id", "tender_id"], unique=False)

    # cost_log is intentionally skipped: ai_cost_log already exists in production.

    if not _table_exists("deadline_control"):
        op.create_table(
            "deadline_control",
            sa.Column("tender_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("submission_deadline", sa.DateTime(timezone=True), nullable=True),
            sa.Column("hours_remaining", sa.Integer(), nullable=True, comment="derived field, updated by scheduler job"),
            sa.Column("deadline_status", DEADLINE_STATUS_ENUM, nullable=False, server_default="safe"),
            sa.Column("can_recommend_go", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"]),
            sa.PrimaryKeyConstraint("tender_id"),
        )
        op.create_index("idx_deadline_control_company_status", "deadline_control", ["company_id", "deadline_status"], unique=False)
        op.create_index("idx_deadline_control_company_deadline", "deadline_control", ["company_id", "submission_deadline"], unique=False)

    if not _table_exists("requirements_checklist"):
        op.create_table(
            "requirements_checklist",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tender_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("requirement_type", sa.Text(), nullable=False),
            sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("status", REQUIREMENT_STATUS_ENUM, nullable=False, server_default="unknown"),
            sa.Column("evidence", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("company_id", "tender_id", "requirement_type", name="uq_requirements_checklist_company_tender_type"),
        )
        op.create_index("idx_requirements_checklist_company_status", "requirements_checklist", ["company_id", "status"], unique=False)

    if not _table_exists("company_fit_score"):
        op.create_table(
            "company_fit_score",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tender_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("okved_match", sa.Boolean(), nullable=True),
            sa.Column("sro_ok", sa.Boolean(), nullable=True),
            sa.Column("license_ok", sa.Boolean(), nullable=True),
            sa.Column("experience_ok", sa.Boolean(), nullable=True),
            sa.Column("funds_ok", sa.Boolean(), nullable=True),
            sa.Column("fit_score", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("company_id", "tender_id", name="uq_company_fit_score_company_tender"),
            sa.CheckConstraint(
                "fit_score IS NULL OR (fit_score >= 0 AND fit_score <= 100)",
                name="ck_company_fit_score_range",
            ),
        )
        op.create_index("idx_company_fit_score_company_fit", "company_fit_score", ["company_id", "fit_score"], unique=False)

    if not _table_exists("tender_stage_log"):
        op.create_table(
            "tender_stage_log",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tender_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("stage", TENDER_STAGE_ENUM, nullable=False, server_default="found"),
            sa.Column("changed_by", sa.Text(), nullable=True),
            sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("note", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("idx_tender_stage_log_company_tender_changed", "tender_stage_log", ["company_id", "tender_id", "changed_at"], unique=False)

    if not _table_exists("clarification_questions"):
        op.create_table(
            "clarification_questions",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tender_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("question_text", sa.Text(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("status", CLARIFICATION_STATUS_ENUM, nullable=False, server_default="draft"),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("answer_text", sa.Text(), nullable=True),
            sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("idx_clarification_questions_company_status", "clarification_questions", ["company_id", "status"], unique=False)

    if not _table_exists("agent_evaluation"):
        op.create_table(
            "agent_evaluation",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tender_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("agent_recommendation", sa.Text(), nullable=True),
            sa.Column("human_decision", sa.Text(), nullable=True),
            sa.Column("actual_result", sa.Text(), nullable=True),
            sa.Column("was_agent_right", sa.Boolean(), nullable=True),
            sa.Column("reason_of_mismatch", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("company_id", "tender_id", name="uq_agent_evaluation_company_tender"),
        )
        op.create_index("idx_agent_evaluation_company_right", "agent_evaluation", ["company_id", "was_agent_right"], unique=False)


def downgrade() -> None:
    for index_name, table_name in (
        ("idx_agent_evaluation_company_right", "agent_evaluation"),
        ("idx_clarification_questions_company_status", "clarification_questions"),
        ("idx_tender_stage_log_company_tender_changed", "tender_stage_log"),
        ("idx_company_fit_score_company_fit", "company_fit_score"),
        ("idx_requirements_checklist_company_status", "requirements_checklist"),
        ("idx_deadline_control_company_deadline", "deadline_control"),
        ("idx_deadline_control_company_status", "deadline_control"),
        ("idx_reasoning_traces_company_tender", "reasoning_traces"),
        ("idx_escalations_company_status", "escalations"),
        ("idx_policies_company_active_priority", "policies"),
        ("idx_events_type_created", "events"),
        ("idx_events_processed_created", "events"),
        ("idx_actions_task_id", "actions"),
        ("idx_actions_company_status", "actions"),
        ("idx_tasks_tender_id", "tasks"),
        ("idx_tasks_company_status_priority", "tasks"),
        ("idx_agents_company_role", "agents"),
    ):
        if _table_exists(table_name):
            op.drop_index(index_name, table_name=table_name)

    for table_name in (
        "agent_evaluation",
        "clarification_questions",
        "tender_stage_log",
        "company_fit_score",
        "requirements_checklist",
        "deadline_control",
        "reasoning_traces",
        "escalations",
        "policies",
        "events",
        "actions",
        "tasks",
        "agents",
    ):
        if _table_exists(table_name):
            op.drop_table(table_name)

    _drop_enums()
