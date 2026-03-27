from __future__ import annotations

import asyncio
import copy
import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.decision_engine.service import DecisionEngineBadRequestError, recompute_decision_engine_v1
from app.ingestion.eis_site.service import run_eis_site_bulk_for_company
from app.models import Company, User
from app.monitoring.schemas import MonitoringNotification, MonitoringRunResponse, MonitoringSettings, MonitoringSettingsPatch
from app.tender_analysis.model import TenderAnalysis
from app.tender_decisions.model import TenderDecision
from app.tender_documents.analyze import analyze_from_source
from app.tenders.model import Tender

logger = logging.getLogger("uvicorn.error")


def _ensure_profile(company: Company) -> dict:
    profile = company.profile if isinstance(company.profile, dict) else {}
    if not isinstance(profile, dict):
        profile = {}
    return copy.deepcopy(profile)


def get_monitoring_settings(company: Company) -> MonitoringSettings:
    return MonitoringSettings.from_profile(_ensure_profile(company))


def patch_monitoring_settings(company: Company, patch: MonitoringSettingsPatch) -> MonitoringSettings:
    profile = _ensure_profile(company)
    current = MonitoringSettings.from_profile(profile).model_dump()
    updates = patch.model_dump(exclude_none=True)
    if "queries" in updates:
        updates["queries"] = [str(item).strip() for item in updates["queries"] if str(item).strip()]
    current.update(updates)
    profile["monitoring"] = MonitoringSettings.model_validate(current).model_dump()
    company.profile = profile
    return MonitoringSettings.model_validate(profile["monitoring"])


def _state(profile: dict) -> dict:
    state = profile.get("monitoring_state")
    if not isinstance(state, dict):
        state = {}
    if not isinstance(state.get("sent_tenders"), dict):
        state["sent_tenders"] = {}
    if not isinstance(state.get("notifications"), list):
        state["notifications"] = []
    return state


async def _resolve_actor_user_id(db: AsyncSession, company_id: UUID, actor_user_id: UUID | None) -> UUID | None:
    if actor_user_id is not None:
        return actor_user_id
    admin = await db.scalar(
        select(User).where(User.company_id == company_id).order_by(desc(User.role == "admin"), User.created_at.asc())
    )
    return admin.id if admin else None


async def _query_new_tenders(db: AsyncSession, company_id: UUID, started_at: datetime) -> list[Tender]:
    stmt = (
        select(Tender)
        .where(
            Tender.company_id == company_id,
            Tender.source == "eis_site",
            Tender.created_at >= started_at,
        )
        .order_by(Tender.created_at.desc())
    )
    return list((await db.scalars(stmt)).all())


async def _extract_relevance_payload(db: AsyncSession, company_id: UUID, tender_id: UUID) -> tuple[int | None, bool, dict]:
    decision = await db.scalar(
        select(TenderDecision).where(TenderDecision.company_id == company_id, TenderDecision.tender_id == tender_id)
    )
    payload = decision.engine_meta if decision and isinstance(decision.engine_meta, dict) else {}
    relevance = payload.get("relevance") if isinstance(payload.get("relevance"), dict) else {}
    score = relevance.get("score")
    is_relevant = bool(relevance.get("is_relevant"))
    return (int(score) if isinstance(score, int) else None, is_relevant, relevance)


async def _extract_risk_score(db: AsyncSession, company_id: UUID, tender_id: UUID) -> int | None:
    analysis = await db.scalar(
        select(TenderAnalysis).where(TenderAnalysis.company_id == company_id, TenderAnalysis.tender_id == tender_id)
    )
    if analysis and isinstance(analysis.requirements, dict):
        risk = analysis.requirements.get("risk_v1")
        if isinstance(risk, dict) and isinstance(risk.get("score_auto"), int):
            return int(risk["score_auto"])

    decision = await db.scalar(
        select(TenderDecision).where(TenderDecision.company_id == company_id, TenderDecision.tender_id == tender_id)
    )
    if decision and decision.risk_score is not None:
        return int(decision.risk_score)
    return None


def _trim_state(state: dict) -> None:
    sent_tenders = state.get("sent_tenders", {})
    if isinstance(sent_tenders, dict) and len(sent_tenders) > 2000:
        ordered = sorted(sent_tenders.items(), key=lambda item: item[1], reverse=True)[:1500]
        state["sent_tenders"] = {k: v for k, v in ordered}
    notifications = state.get("notifications", [])
    if isinstance(notifications, list) and len(notifications) > 200:
        state["notifications"] = notifications[:200]


def _format_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _summary_reason(*, category: str | None, relevance_label: str | None, relevance_reason: str | None, matched_keywords: list[str], risk_score: int | None, recommendation: str | None) -> str:
    category_text = category or "без категории"
    rel_text = relevance_label or "не определена"
    risk_text = str(risk_score) if risk_score is not None else "не рассчитан"
    rec_text = recommendation or "не определена"
    keywords_text = ", ".join(matched_keywords[:4]) if matched_keywords else "ключевые признаки не выделены"
    reason = relevance_reason.strip() if relevance_reason else ""
    if reason:
        return (
            f"Категория: {category_text}. Релевантность: {rel_text}. "
            f"Совпадения: {keywords_text}. Риск: {risk_text}. Рекомендация: {rec_text}. "
            f"Почему показан: {reason}"
        )
    return (
        f"Категория: {category_text}. Релевантность: {rel_text}. "
        f"Совпадения: {keywords_text}. Риск: {risk_text}. Рекомендация: {rec_text}."
    )


def _analysis_snapshot(data: dict | None) -> tuple[str, int, str, str, str]:
    if not isinstance(data, dict):
        return "imported", 0, "not_run", "not_run", "not_run"
    steps = data.get("steps") if isinstance(data.get("steps"), dict) else {}
    fetch = steps.get("fetch_documents") if isinstance(steps.get("fetch_documents"), dict) else {}
    extract = steps.get("extract") if isinstance(steps.get("extract"), dict) else {}
    risk = steps.get("risk") if isinstance(steps.get("risk"), dict) else {}
    decision = steps.get("recompute_engine") if isinstance(steps.get("recompute_engine"), dict) else {}

    docs = int(fetch.get("downloaded_count") or 0)
    extract_status = str(extract.get("status") or "not_run")
    risk_status = str(risk.get("status") or "not_run")
    decision_status = str(decision.get("status") or "not_run")

    if docs <= 0:
        stage = "documents_missing"
    elif extract_status == "error":
        stage = "extract_failed"
    elif risk_status == "error":
        stage = "risk_failed"
    elif decision_status == "ok":
        stage = "decision_done"
    elif docs > 0:
        stage = "documents_downloaded"
    else:
        stage = "imported"

    return stage, docs, extract_status, risk_status, decision_status


async def run_monitoring_cycle(
    db: AsyncSession,
    *,
    company: Company,
    actor_user_id: UUID | None = None,
) -> MonitoringRunResponse:
    profile = _ensure_profile(company)
    monitoring = MonitoringSettings.from_profile(profile)
    if not monitoring.enabled:
        return MonitoringRunResponse(
            status="disabled",
            queries_total=len(monitoring.queries),
            imported_total=0,
            new_tenders=0,
            relevance_checked=0,
            relevant_found=0,
            deep_analysis_attempted=0,
            deep_analysis_completed=0,
            deep_analysis_partial=0,
            notifications_sent=0,
            details={"reason": "monitoring_disabled"},
        )

    started_at = datetime.now(UTC)
    bulk = await run_eis_site_bulk_for_company(
        db,
        company,
        queries=monitoring.queries,
        pages_per_query=monitoring.pages_per_query,
        page_size=monitoring.page_size,
        dedupe_mode="update",
        stop_if_blocked=True,
    )

    new_tenders = await _query_new_tenders(db, company.id, started_at)
    actor_id = await _resolve_actor_user_id(db, company.id, actor_user_id)
    relevance_checked = 0
    relevant_tenders: list[tuple[Tender, dict, int | None]] = []

    for tender in new_tenders:
        if actor_id is not None:
            try:
                await recompute_decision_engine_v1(
                    db,
                    company_id=company.id,
                    tender_id=tender.id,
                    user_id=actor_id,
                    force=True,
                )
            except DecisionEngineBadRequestError:
                logger.warning("monitoring recompute skipped: company_id=%s tender_id=%s", company.id, tender.id)
            except Exception:
                logger.exception("monitoring recompute failed: company_id=%s tender_id=%s", company.id, tender.id)

        score, is_relevant, payload = await _extract_relevance_payload(db, company.id, tender.id)
        relevance_checked += 1
        if score is None:
            continue
        if is_relevant and score >= monitoring.relevance_min:
            risk_score = await _extract_risk_score(db, company.id, tender.id)
            relevant_tenders.append((tender, payload, risk_score))

    state = _state(profile)
    sent = state.get("sent_tenders", {})
    notifications = state.get("notifications", [])
    notifications_sent = 0
    deep_analysis_attempted = 0
    deep_analysis_completed = 0
    deep_analysis_partial = 0
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    for tender, rel_payload, risk_score in relevant_tenders:
        key = str(tender.id)
        already_sent = key in sent
        if monitoring.notify_only_new and already_sent:
            continue

        deep_result: dict | None = None
        run_deep = (
            monitoring.deep_analysis_enabled
            and actor_id is not None
            and deep_analysis_attempted < monitoring.deep_analysis_limit_per_run
            and (not monitoring.deep_analysis_only_new or not already_sent)
        )
        if run_deep:
            deep_analysis_attempted += 1
            try:
                deep_result = await asyncio.wait_for(
                    analyze_from_source(
                        db,
                        company_id=company.id,
                        user_id=actor_id,
                        tender_id=tender.id,
                    ),
                    timeout=monitoring.deep_analysis_timeout_seconds,
                )
            except TimeoutError:
                deep_result = {"status": "partial", "steps": {"fetch_documents": {"status": "error", "message": "timeout"}}}
            except Exception:
                logger.exception("monitoring deep analysis failed: company_id=%s tender_id=%s", company.id, tender.id)
                deep_result = {"status": "partial", "steps": {"fetch_documents": {"status": "error", "message": "deep_analysis_failed"}}}

        stage, docs_downloaded, extract_status, risk_status, decision_status = _analysis_snapshot(deep_result)
        if deep_result is not None:
            if stage == "decision_done":
                deep_analysis_completed += 1
            else:
                deep_analysis_partial += 1

        decision = await db.scalar(
            select(TenderDecision).where(TenderDecision.company_id == company.id, TenderDecision.tender_id == tender.id)
        )
        relevance_label = str(rel_payload.get("label")) if rel_payload.get("label") else None
        category = str(rel_payload.get("category")) if rel_payload.get("category") else None
        relevance_reason = str(rel_payload.get("reason")) if rel_payload.get("reason") else None
        matched_keywords = [str(item) for item in rel_payload.get("matched_keywords", []) if isinstance(item, str)]
        tender_ai_url = f"{settings.public_base_url.rstrip('/')}/web/tenders/{tender.id}"
        recommendation = decision.recommendation if decision else None
        decision_score = decision.decision_score if decision else None
        recommendation_reason = decision.recommendation_reason if decision else None
        priority_score = decision.priority_score if decision else None
        priority_label = decision.priority_label if decision else None
        priority_reason = decision.priority_reason if decision else None
        notification = MonitoringNotification(
            tender_id=tender.id,
            title=tender.title,
            external_id=tender.external_id,
            relevance_score=int(rel_payload.get("score")) if isinstance(rel_payload.get("score"), int) else None,
            relevance_label=relevance_label,
            category=category,
            summary_reason=_summary_reason(
                category=category,
                relevance_label=relevance_label,
                relevance_reason=relevance_reason,
                matched_keywords=matched_keywords,
                risk_score=risk_score,
                recommendation=recommendation,
            ),
            matched_keywords=matched_keywords[:10],
            risk_score=risk_score,
            recommendation=recommendation,
            decision_score=decision_score,
            recommendation_reason=recommendation_reason,
            priority_score=priority_score,
            priority_label=priority_label,
            priority_reason=priority_reason,
            analysis_stage=stage,
            documents_downloaded_count=docs_downloaded,
            extract_status=extract_status,
            risk_status=risk_status,
            decision_status=decision_status,
            nmck=float(tender.nmck) if tender.nmck is not None else None,
            published_at=_format_dt(tender.published_at),
            deadline=_format_dt(tender.submission_deadline),
            tender_ai_url=tender_ai_url,
            tender_url=tender_ai_url,
            source_url=tender.source_url,
            sent_at=now_iso,
        )
        notifications.insert(0, notification.model_dump(mode="json"))
        sent[key] = now_iso
        notifications_sent += 1

    state["sent_tenders"] = sent
    state["notifications"] = notifications
    state["last_run_at"] = now_iso
    state["last_result"] = {
        "queries_total": len(monitoring.queries),
        "imported_total": bulk.totals.inserted + bulk.totals.updated,
        "new_tenders": len(new_tenders),
        "relevance_checked": relevance_checked,
        "relevant_found": len(relevant_tenders),
        "deep_analysis_attempted": deep_analysis_attempted,
        "deep_analysis_completed": deep_analysis_completed,
        "deep_analysis_partial": deep_analysis_partial,
        "notifications_sent": notifications_sent,
        "source_status": bulk.totals.source_status,
    }
    _trim_state(state)

    profile["monitoring"] = monitoring.model_dump(mode="json")
    profile["monitoring_state"] = state
    company.profile = profile
    await db.commit()
    await db.refresh(company)

    return MonitoringRunResponse(
        status="ok",
        queries_total=len(monitoring.queries),
        imported_total=bulk.totals.inserted + bulk.totals.updated,
        new_tenders=len(new_tenders),
        relevance_checked=relevance_checked,
        relevant_found=len(relevant_tenders),
        deep_analysis_attempted=deep_analysis_attempted,
        deep_analysis_completed=deep_analysis_completed,
        deep_analysis_partial=deep_analysis_partial,
        notifications_sent=notifications_sent,
        details={
            "breakdown": [item.__dict__ for item in bulk.breakdown],
            "blocked_count": bulk.blocked_count,
            "maintenance_count": bulk.maintenance_count,
            "source_status": bulk.totals.source_status,
        },
    )


def get_monitoring_notifications(company: Company, limit: int = 20) -> list[dict]:
    profile = _ensure_profile(company)
    state = _state(profile)
    notifications = state.get("notifications", [])
    if not isinstance(notifications, list):
        return []
    ordered = sorted(
        notifications,
        key=lambda item: (
            int(item.get("priority_score", -1)) if isinstance(item, dict) and item.get("priority_score") is not None else -1,
            str(item.get("sent_at", "")) if isinstance(item, dict) else "",
        ),
        reverse=True,
    )
    return ordered[: max(1, min(100, limit))]
