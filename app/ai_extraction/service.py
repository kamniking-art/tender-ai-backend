from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_extraction.client import get_extractor_provider
from app.ai_extraction.interfaces import ExtractionProviderError
from app.ai_extraction.model import AICostLog
from app.ai_extraction.schemas import ExtractedTenderV1
from app.ai_extraction.text_extract import NoExtractableTextError, build_normalized_text
from app.core.config import settings
from app.risk.service import compute_risk_flags, compute_risk_score_v1
from app.tender_analysis.model import TenderAnalysis
from app.tender_analysis.service import AnalysisConflictError, ScopedNotFoundError
from app.tender_documents.model import TenderDocument
from app.tenders.nmck import get_sane_nmck
from app.tenders.service import get_tender_by_id_scoped

logger = logging.getLogger(__name__)


class ExtractionBadRequestError(ValueError):
    pass


def _build_document_signature(documents: list[TenderDocument]) -> str:
    parts: list[str] = []
    for doc in sorted(documents, key=lambda d: str(d.id)):
        parts.append(f"{doc.id}:{doc.file_name}:{doc.file_size}:{doc.uploaded_at.isoformat() if doc.uploaded_at else '-'}")
    return "|".join(parts)


def _to_decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _line_or_dash(value: object | None) -> str:
    return str(value) if value is not None else "-"


def build_summary(extracted: ExtractedTenderV1, *, tender_nmck: object | None) -> str:
    nmck = get_sane_nmck(tender_nmck)
    lines = [
        f"Subject: {_line_or_dash(extracted.subject)}",
        f"NMCK: {_line_or_dash(nmck)} {'RUB' if nmck is not None else '-'}",
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
    document_signature = _build_document_signature(documents)

    docs_paths = [
        str((Path(settings.storage_root) / doc.storage_path)) if doc.storage_path else "-"
        for doc in documents
    ]
    logger.warning("Extraction docs: tender_id=%s count=%s paths=%s", tender_id, len(documents), docs_paths)

    missing_paths = [
        str(Path(settings.storage_root) / doc.storage_path)
        for doc in documents
        if not doc.storage_path or not (Path(settings.storage_root) / doc.storage_path).is_file()
    ]
    if missing_paths:
        logger.warning("Extraction missing files: tender_id=%s paths=%s", tender_id, missing_paths)
        raise ExtractionBadRequestError("Документ не найден на сервере")

    supported_suffixes = {".pdf", ".docx", ".doc", ".txt", ".xlsx", ".zip"}
    has_supported = any((doc.file_name or "").lower().endswith(tuple(supported_suffixes)) for doc in documents)
    if not has_supported:
        raise ExtractionProviderError("UNSUPPORTED_FORMAT", "No supported document formats (.pdf/.docx/.doc/.txt/.xlsx/.zip)")

    analysis = await db.scalar(
        select(TenderAnalysis).where(
            TenderAnalysis.company_id == company_id,
            TenderAnalysis.tender_id == tender_id,
        )
    )

    if analysis is not None:
        cached_requirements = dict(analysis.requirements or {})
        cached_signature = str(cached_requirements.get("extract_doc_signature_v1") or "")
        cached_extracted = cached_requirements.get("extracted_v1")
        if cached_signature and cached_signature == document_signature and isinstance(cached_extracted, dict):
            logger.info("Extraction cache hit: tender_id=%s", tender_id)
            return analysis, ExtractedTenderV1.model_validate(cached_extracted)

    # Create action record for this extraction run (best-effort — never blocks extraction).
    _action_record = None
    try:
        from app.agent_actions.service import SYSTEM_AGENT_ID, create_action
        _action_record = await create_action(
            db,
            company_id=company_id,
            agent_id=SYSTEM_AGENT_ID,
            action_type="extract_documents",
            target=str(tender_id),
            payload={"doc_count": len(documents)},
        )
    except Exception:
        logger.exception(
            "Failed to create action record for extraction tender_id=%s", tender_id
        )

    merged_text = build_normalized_text(
        documents=documents,
        storage_root=settings.storage_root,
        max_chars=settings.ai_max_input_chars or settings.ai_extractor_max_chars,
        max_files=settings.ai_max_files,
        max_pages=settings.ai_max_pages,
    )

    # Deterministic NMCK extraction — before AI
    from app.ai_extraction.text_extract import extract_nmck_from_xlsx
    from pathlib import Path
    from decimal import Decimal

    _det_nmck: Decimal | None = None
    for _doc in documents:
        _fname = (_doc.file_name or "").lower()
        if _fname.endswith(".xlsx") or _fname.endswith(".xlsx.zip"):
            _fpath = Path(settings.storage_root) / _doc.storage_path
            if _fpath.exists():
                _det_nmck = extract_nmck_from_xlsx(_fpath)
                if _det_nmck is not None:
                    break

    if _det_nmck is not None and (tender.nmck is None or tender.nmck == 0):
        tender.nmck = _det_nmck
        tender.nmck_source = "deterministic"
        tender.nmck_confidence = Decimal("0.99")
        await db.commit()
        logger.info("deterministic nmck before AI: tender_id=%s nmck=%s", tender_id, _det_nmck)

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
        if _action_record is not None:
            try:
                from app.agent_actions.service import fail_action
                await fail_action(
                    db, _action_record.action_id, result={"error": "provider_error"}
                )
            except Exception:
                pass
        raise

    extracted = provider_result.extracted
    risk_flags = compute_risk_flags(extracted, tender)
    risk_v1 = compute_risk_score_v1(extracted, tender)
    summary = build_summary(extracted, tender_nmck=tender.nmck)

    if analysis is not None and analysis.status == "approved":
        raise AnalysisConflictError("Approved analysis cannot be overwritten")

    extracted_payload = extracted.model_dump(mode="json")

    if analysis is None:
        analysis = TenderAnalysis(
            company_id=company_id,
            tender_id=tender_id,
            status="ready",
            requirements={
                "extracted_v1": extracted_payload,
                "risk_v1": risk_v1,
                "extract_meta_v1": provider_result.extract_meta,
                "extract_doc_signature_v1": document_signature,
            },
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
        merged_requirements["extract_doc_signature_v1"] = document_signature
        merged_requirements.pop("extract_error_v1", None)
        analysis.requirements = merged_requirements
        analysis.risk_flags = risk_flags
        analysis.summary = summary
        if analysis.status != "approved":
            analysis.status = "ready"
        analysis.updated_by = user_id

    extract_meta = provider_result.extract_meta if isinstance(provider_result.extract_meta, dict) else {}
    cost_log = AICostLog(
        company_id=company_id,
        tender_id=tender_id,
        model=str(extract_meta.get("model") or "unknown"),
        chars_sent=int(extract_meta.get("chars_sent") or len(merged_text)),
        estimated_cost=_to_decimal_or_none(extract_meta.get("estimated_cost")),
        duration_ms=int(extract_meta.get("latency_ms")) if extract_meta.get("latency_ms") is not None else None,
    )
    db.add(cost_log)

    await db.commit()
    await db.refresh(analysis)

    # Sync nmck from extraction to tender if tender.nmck is empty
    try:
        if extracted.nmck is not None and (tender.nmck is None or tender.nmck == 0):
            tender.nmck = extracted.nmck
            await db.commit()
            logger.info(
                "Synced nmck from extraction: tender_id=%s nmck=%s",
                tender_id, extracted.nmck
            )
    except Exception:
        logger.exception("Failed to sync nmck for tender_id=%s", tender_id)

    # Build requirements checklist deterministically from extracted data.
    # Lazy import — avoids pulling SQLAlchemy into pure test environments.
    # Wrapped in try/except so a checklist failure never breaks extraction.
    try:
        from app.requirements.normalizer import RequirementNormalizer
        from app.requirements.service import upsert_checklist
        _normalizer = RequirementNormalizer()
        _reqs = _normalizer.normalize(extracted)
        await upsert_checklist(db, tender_id, company_id, _reqs)
    except Exception:
        logger.exception(
            "Failed to build requirements checklist for tender_id=%s", tender_id
        )
        _reqs = []  # ensure _reqs is always defined for fit_score step below

    # Calculate company fit score using the same extracted data.
    # Lazy import + try/except — fit score failure never breaks extraction.
    try:
        from app.fit_score.scorer import FitScorer
        from app.fit_score.service import upsert_fit_score
        from app.models import Company
        from app.requirements.normalizer import RequirementNormalizer
        _company = await db.scalar(select(Company).where(Company.id == company_id))
        _profile: dict = _company.profile if _company and isinstance(_company.profile, dict) else {}
        _checklist = _reqs if _reqs else RequirementNormalizer().normalize(extracted)
        _fit_result = FitScorer().score(_profile, _checklist, extracted)
        await upsert_fit_score(db, tender_id, company_id, _fit_result)
    except Exception:
        logger.exception(
            "Failed to calculate fit score for tender_id=%s", tender_id
        )

    # Mark the extraction action as completed (best-effort).
    if _action_record is not None:
        try:
            from app.agent_actions.service import complete_action
            await complete_action(
                db, _action_record.action_id, result={"status": "ok"}
            )
        except Exception:
            logger.exception(
                "Failed to complete action record for tender_id=%s", tender_id
            )

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
