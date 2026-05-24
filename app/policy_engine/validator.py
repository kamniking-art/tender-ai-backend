from __future__ import annotations

import logging

from pydantic import ValidationError

from app.policy_engine.schema import PolicySchema

logger = logging.getLogger(__name__)


class PolicyValidator:
    """Validates a raw policy dict against PolicySchema.

    Returns a PolicySchema on success, None on any validation error.
    Never raises — invalid policies are logged and skipped.
    """

    def validate(self, row: dict) -> PolicySchema | None:
        try:
            return PolicySchema.model_validate(row)
        except (ValidationError, Exception) as exc:
            policy_id = row.get("policy_id", "unknown")
            logger.warning("Policy %s failed validation and will be skipped: %s", policy_id, exc)
            return None
