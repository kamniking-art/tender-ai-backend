from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_extraction.client import AIExtractorClient
from app.ai_extraction.schemas import ExtractedTenderV1
from app.ai_extraction.text_extract import NoExtractableTextError, build_normalized_text
from app.core.config import settings
from app.tender_analysis.model import TenderAnalysis
from app.tender_analysis.service import AnalysisConflictError, ScopedNotFoundError
from app.tender_documents.model import TenderDocument
from app.tenders.model import Tender
from app.tenders.service import get_tender_by_id_scoped


class ExtractionBadRequestError(ValueError):
    pass


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _line_or_dash(value: object | None) -> str:
    return str(value) if value is not None else "-"


def build_summary(extracted: ExtractedTenderV1) -> str:
    lines = [
        f"Subject: {_line_or_dash(extracted.subject)}",
        f"NMCK: {_line_or_dash(extracted.nmck)} {_line_or_dash(extracted.currency)}",
        f"Submission deadline: {_line_or_dash(extracted.submission_deadline_at)}",
        f"Bid security: {_line_or_dash(extracted.bid_security_amount)} ({_line_or_dash(extracted.bid_security_pct)}%)",
        f"Contract security: {_line_or_dash(extracted.contract_security_amount)} ({_line_or_dash(extracted.contract_security_pct)}%)",
    ]
    return "\n".join(lines)


def _severity_for_security(pct: Decimal | None) -> str:
    if pct is None:
        return "medium"
    return "high" if pct >= Decimal("10") else "medium"


def compute_risk_flags(extracted: ExtractedTenderV1, tender: Tender) -> list[dict]:
    flags: list[dict] = []
    now = _now_utc()

    nmck = extracted.nmck or tender.nmck
    deadline = extracted.submission_deadline_at or tender.submission_deadline

    if deadline is not None and deadline <= now + timedelta(days=3):
        flags.append(
            {
                "code": "short_deadline",
                "title": "Короткий срок подачи",
                "severity": "high",
                "note": f"Deadline is {deadline.isoformat()}",
            }
        )

    bid_pct = extracted.bid_security_pct
    if (
        bid_pct is not None
        and bid_pct >= Decimal("5")
    ) or (
        extracted.bid_security_amount is not None
        and nmck is not None
        and extracted.bid_security_amount >= nmck * Decimal("0.05")
    ):
        flags.append(
            {
                "code": "high_bid_security",
                "title": "Высокое обеспечение заявки",
                "severity": _severity_for_security(bid_pct),
                "note": "Bid security looks high for this tender.",
            }
        )

    contract_pct = extracted.contract_security_pct
    if (
        contract_pct is not None
        and contract_pct >= Decimal("5")
    ) or (
        extracted.contract_security_amount is not None
        and nmck is not None
        and extracted.contract_security_amount >= nmck * Decimal("0.05")
    ):
        flags.append(
            {
                "code": "high_contract_security",
                "title": "Высокое обеспечение контракта",
                "severity": _severity_for_security(contract_pct),
                "note": "Contract security looks high for this tender.",
            }
        )

    penalties_text = " ".join(extracted.penalties).lower()
    if re.search(r"неустойк|штраф|пени|0,1%", penalties_text, flags=re.IGNORECASE):
        flags.append(
            {
                "code": "harsh_penalties",
                "title": "Жесткие штрафные условия",
                "severity": "medium",
                "note": "Penalty clauses detected in extracted terms.",
            }
        )

    req_text = " ".join(extracted.qualification_requirements).lower()
    if len(extracted.qualification_requirements) >= 8 or re.search(r"сро|опыт выполнения|аналогичных контрактов", req_text):
        flags.append(
            {
                "code": "excessive_requirements",
                "title": "Повышенные квалификационные требования",
                "severity": "medium",
                "note": "Qualification requirements may be restrictive.",
            }
        )

    return flags


async def _resolve_documents(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    document_ids: list[UUID] | None,
) -> list[TenderDocument]:
    base_stmt = select(TenderDocument).where(
        TenderDocument.company_id == company_id,
        TenderDocument.tender_id == tender_id,
    )

    if document_ids is None:
        docs = list((await db.scalars(base_stmt.order_by(TenderDocument.uploaded_at.desc()))).all())
        if not docs:
            raise ExtractionBadRequestError("No documents found for tender")
        return docs

    if not document_ids:
        raise ExtractionBadRequestError("document_ids is empty")

    docs = list(
        (
            await db.scalars(
                base_stmt.where(TenderDocument.id.in_(document_ids)).order_by(TenderDocument.uploaded_at.desc())
            )
        ).all()
    )
    if len(docs) != len(set(document_ids)):
        raise ScopedNotFoundError("One or more documents not found in this tender")

    return docs


async def run_extraction(
    db: AsyncSession,
    *,
    company_id: UUID,
    user_id: UUID,
    tender_id: UUID,
    document_ids: list[UUID] | None,
) -> tuple[TenderAnalysis, ExtractedTenderV1]:
    tender = await get_tender_by_id_scoped(db, company_id, tender_id)
    if tender is None:
        raise ScopedNotFoundError("Tender not found")

    documents = await _resolve_documents(
        db,
        company_id=company_id,
        tender_id=tender_id,
        document_ids=document_ids,
    )

    merged_text = build_normalized_text(
        documents=documents,
        storage_root=settings.storage_root,
        max_chars=settings.ai_extractor_max_chars,
    )

    client = AIExtractorClient()
    extracted = await client.extract(tender_id=tender_id, text=merged_text)
    risk_flags = compute_risk_flags(extracted, tender)
    summary = build_summary(extracted)

    analysis = await db.scalar(
        select(TenderAnalysis).where(
            TenderAnalysis.company_id == company_id,
            TenderAnalysis.tender_id == tender_id,
        )
    )

    if analysis is not None and analysis.status == "approved":
        raise AnalysisConflictError("Approved analysis cannot be overwritten")

    extracted_payload = extracted.model_dump(mode="json")

    if analysis is None:
        analysis = TenderAnalysis(
            company_id=company_id,
            tender_id=tender_id,
            status="ready",
            requirements={"extracted_v1": extracted_payload},
            missing_docs=[],
            risk_flags=risk_flags,
            summary=summary,
            created_by=user_id,
            updated_by=user_id,
        )
        db.add(analysis)
    else:
        merged_requirements = dict(analysis.requirements or {})
        merged_requirements["extracted_v1"] = extracted_payload
        analysis.requirements = merged_requirements
        analysis.risk_flags = risk_flags
        analysis.summary = summary
        if analysis.status != "approved":
            analysis.status = "ready"
        analysis.updated_by = user_id

    await db.commit()
    await db.refresh(analysis)
    return analysis, extracted


async def get_extracted_v1(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
) -> ExtractedTenderV1 | None:
    tender = await get_tender_by_id_scoped(db, company_id, tender_id)
    if tender is None:
        raise ScopedNotFoundError("Tender not found")

    analysis = await db.scalar(
        select(TenderAnalysis).where(
            TenderAnalysis.company_id == company_id,
            TenderAnalysis.tender_id == tender_id,
        )
    )
    if analysis is None:
        return None

    extracted = (analysis.requirements or {}).get("extracted_v1")
    if extracted is None:
        return None

    return ExtractedTenderV1.model_validate(extracted)
