from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.config import DEFAULT_EIS_SITE_QUERIES


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class MonitoringSettings(BaseModel):
    enabled: bool = True
    queries: list[str] = Field(default_factory=lambda: list(DEFAULT_EIS_SITE_QUERIES))
    pages_per_query: int = 5
    page_size: int = 20
    relevance_min: int = 45
    notify_only_new: bool = True
    interval_minutes: int = 360

    @classmethod
    def from_profile(cls, profile: dict[str, Any] | None) -> "MonitoringSettings":
        raw = profile.get("monitoring") if isinstance(profile, dict) and isinstance(profile.get("monitoring"), dict) else {}
        payload = cls().model_dump()
        payload.update(raw)
        payload["queries"] = [str(item).strip() for item in payload.get("queries", []) if str(item).strip()]
        if not payload["queries"]:
            payload["queries"] = list(DEFAULT_EIS_SITE_QUERIES)
        payload["pages_per_query"] = max(1, min(50, int(payload.get("pages_per_query", 5))))
        payload["page_size"] = max(10, min(50, int(payload.get("page_size", 20))))
        payload["relevance_min"] = max(0, min(100, int(payload.get("relevance_min", 45))))
        payload["interval_minutes"] = max(30, min(24 * 60, int(payload.get("interval_minutes", 360))))
        payload["enabled"] = bool(payload.get("enabled", True))
        payload["notify_only_new"] = bool(payload.get("notify_only_new", True))
        return cls.model_validate(payload)


class MonitoringSettingsPatch(BaseModel):
    enabled: bool | None = None
    queries: list[str] | None = None
    pages_per_query: int | None = None
    page_size: int | None = None
    relevance_min: int | None = None
    notify_only_new: bool | None = None
    interval_minutes: int | None = None


class MonitoringNotification(BaseModel):
    tender_id: UUID
    title: str | None = None
    external_id: str | None = None
    relevance_score: int | None = None
    relevance_label: str | None = None
    category: str | None = None
    summary_reason: str | None = None
    matched_keywords: list[str] = Field(default_factory=list)
    risk_score: int | None = None
    recommendation: str | None = None
    decision_score: int | None = None
    recommendation_reason: str | None = None
    nmck: float | None = None
    published_at: str | None = None
    deadline: str | None = None
    tender_ai_url: str | None = None
    tender_url: str | None = None
    source_url: str | None = None
    sent_at: str = Field(default_factory=_now_iso)


class MonitoringRunResponse(BaseModel):
    status: str
    queries_total: int
    imported_total: int
    new_tenders: int
    relevance_checked: int
    relevant_found: int
    notifications_sent: int
    details: dict[str, Any] = Field(default_factory=dict)
