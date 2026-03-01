from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_extraction.client import get_extractor_provider
from app.ai_extraction.interfaces import ExtractionProviderError
from app.ai_extraction.schemas import ExtractedTenderV1
from app.ai_extraction.text_extract import NoExtractableTextError, build_normalized_text
from app.core.config import settings
from app.risk.service import compute_risk_flags, compute_risk_score_v1
from app.tender_analysis.model import TenderAnalysis
from app.tender_analysis.service import AnalysisConflictError, ScopedNotFoundError
from app.tender_documents.model import TenderDocument
from app.tenders.service import get_tender_by_id_scoped

logger = logging.getLogger(__name__)


class ExtractionBadRequestError(ValueError):
    pass


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
            raise ExtractionBadRequestError("Сначала загрузите документы тендера")
        return docs

    if not document_ids:
        raise ExtractionBadRequestError("Список document_ids пуст")

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

    docs_paths = [
        str((Path(settings.storage_root) / doc.storage_path)) if doc.storage_path else "-"
        for doc in documents
    ]
    logger.info("Extraction docs: tender_id=%s count=%s paths=%s", tender_id, len(documents), docs_paths)

    missing_paths = [
        str(Path(settings.storage_root) / doc.storage_path)
        for doc in documents
        if not doc.storage_path or not (Path(settings.storage_root) / doc.storage_path).is_file()
    ]
    if missing_paths:
        logger.warning("Extraction missing files: tender_id=%s paths=%s", tender_id, missing_paths)
        raise ExtractionBadRequestError("Документ не найден на сервере")

    supported_suffixes = {".pdf", ".docx", ".txt"}
    has_supported = any((doc.file_name or "").lower().endswith(tuple(supported_suffixes)) for doc in documents)
    if not has_supported:
        raise ExtractionProviderError("UNSUPPORTED_FORMAT", "No supported document formats (.pdf/.docx/.txt)")

    merged_text = build_normalized_text(
        documents=documents,
        storage_root=settings.storage_root,
        max_chars=settings.ai_extractor_max_chars,
    )

    provider = get_extractor_provider()
    try:
        provider_result = await provider.extract(
            tender_id=tender_id,
            company_id=company_id,
            tender_context={
                "title": getattr(tender, "title", None),
                "external_id": getattr(tender, "external_id", None),
                "source": getattr(tender, "source", None),
                "nmck": str(getattr(tender, "nmck", None)) if getattr(tender, "nmck", None) is not None else None,
                "published_at": getattr(tender, "published_at", None).isoformat() if getattr(tender, "published_at", None) else None,
                "submission_deadline": getattr(tender, "submission_deadline", None).isoformat() if getattr(tender, "submission_deadline", None) else None,
            },
            text=merged_text,
        )
    except ExtractionProviderError:
        # Best-effort persist of extraction error without schema changes.
        analysis_err = await db.scalar(
            select(TenderAnalysis).where(
                TenderAnalysis.company_id == company_id,
                TenderAnalysis.tender_id == tender_id,
            )
        )
        if analysis_err and analysis_err.status != "approved":
            req_err = dict(analysis_err.requirements or {})
            req_err["extract_error_v1"] = {
                "status": "failed",
                "error": "provider_error",
            }
            analysis_err.requirements = req_err
            analysis_err.updated_by = user_id
            await db.commit()
        raise

    extracted = provider_result.extracted
    risk_flags = compute_risk_flags(extracted, tender)
    risk_v1 = compute_risk_score_v1(extracted, tender)
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
            requirements={"extracted_v1": extracted_payload, "risk_v1": risk_v1, "extract_meta_v1": provider_result.extract_meta},
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
        merged_requirements["risk_v1"] = risk_v1
        merged_requirements["extract_meta_v1"] = provider_result.extract_meta
        merged_requirements.pop("extract_error_v1", None)
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
