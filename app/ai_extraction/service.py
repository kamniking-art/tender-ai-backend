from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_extraction.client import PARSER_VERSION, CHUNKING_VERSION, pipeline_versions, get_extractor_provider
from app.ai_extraction.interfaces import ExtractionProviderError, ExtractionProviderResult
from app.ai_extraction.model import AICostLog, ExtractionEvidence, ExtractionSnapshot
from app.ai_extraction.schemas import ExtractedTenderV1
from app.ai_extraction.text_extract import (
    MAX_SEMANTIC_CHUNK_CHARS,
    NoExtractableTextError,
    build_normalized_text,
    build_semantic_chunks,
    extract_nmck_from_file,
)
from app.core.config import settings
from app.risk.service import compute_risk_flags, compute_risk_score_v1
from app.tender_analysis.model import TenderAnalysis
from app.tender_analysis.service import AnalysisConflictError, ScopedNotFoundError
from app.tender_documents.model import TenderDocument
from app.tenders.nmck import get_sane_nmck
from app.tenders.service import get_tender_by_id_scoped

if settings.feature_agent_actions:
    from app.agent_actions.service import (  # noqa: F401
        SYSTEM_AGENT_ID,
        complete_action,
        create_action,
        fail_action,
    )

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


async def _upsert_extraction_evidence(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    extracted: ExtractedTenderV1,
    extract_meta: dict,
) -> None:
    """Upsert one row per extracted field into extraction_evidence.

    Uses PostgreSQL INSERT … ON CONFLICT DO UPDATE so that re-running
    extraction always reflects the latest values without creating duplicates.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from datetime import datetime, timezone

    provider = str(extract_meta.get("provider") or "unknown")
    parser_version = str(extract_meta.get("parser_version") or PARSER_VERSION)
    now = datetime.now(timezone.utc)

    # model_dump(mode="json") converts Decimal→float, datetime→ISO string, etc.
    # Confidence/evidence keys match schema field names exactly (same dict used
    # when storing extracted_v1 in requirements JSONB).
    extracted_dict = extracted.model_dump(mode="json")
    confidence_map: dict[str, float] = extracted_dict.get("confidence") or {}
    evidence_map: dict[str, str | None] = extracted_dict.get("evidence") or {}

    # All content fields except schema meta and the maps themselves
    _SKIP = {"schema_version", "confidence", "evidence"}
    field_values = {k: v for k, v in extracted_dict.items() if k not in _SKIP}

    for field_name, value_json in field_values.items():
        # value_json is already JSON-native (None / bool / int / float / str / list)
        # Use provider confidence if available; fall back to synthetic:
        # non-null value → 0.8 (field was extracted), null → 0.0 (not found).
        conf = confidence_map.get(field_name)
        if conf is None:
            conf = 0.8 if value_json is not None else 0.0
        insert_stmt = pg_insert(ExtractionEvidence).values(
            company_id=company_id,
            tender_id=tender_id,
            field_name=field_name,
            value=value_json,
            extraction_completeness=conf,
            evidence=evidence_map.get(field_name),
            provider=provider,
            parser_version=parser_version,
            extracted_at=now,
        )
        upsert_stmt = insert_stmt.on_conflict_do_update(
            constraint="uq_extraction_evidence_company_tender_field",
            set_={
                "value": insert_stmt.excluded.value,
                "extraction_completeness": insert_stmt.excluded.extraction_completeness,
                "evidence": insert_stmt.excluded.evidence,
                "provider": insert_stmt.excluded.provider,
                "parser_version": insert_stmt.excluded.parser_version,
                "extracted_at": insert_stmt.excluded.extracted_at,
            },
        )
        await db.execute(upsert_stmt)


async def _save_extraction_snapshot(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    extracted: ExtractedTenderV1,
    extract_meta: dict,
    doc_signature: str,
) -> None:
    """Append-only snapshot of every successful extraction run.

    Never updates existing rows — each rerun inserts a new row so the
    full history is preserved for re-score and re-debug without repeating
    the AI call.
    """
    snapshot = ExtractionSnapshot(
        company_id=company_id,
        tender_id=tender_id,
        extracted_v1=extracted.model_dump(mode="json"),
        extract_meta_v1=extract_meta,
        pipeline_versions=extract_meta.get("pipeline_versions") or pipeline_versions(),
        doc_signature=doc_signature,
        provider=str(extract_meta.get("provider") or "unknown"),
        model=str(extract_meta.get("model") or "unknown"),
    )
    db.add(snapshot)
    try:
        await db.commit()
    except Exception:
        logger.exception("Failed to save extraction snapshot: tender_id=%s", tender_id)
        await db.rollback()


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

    semantic_chunks = build_semantic_chunks(
        documents=documents,
        storage_root=settings.storage_root,
        max_chars_per_chunk=min(
            MAX_SEMANTIC_CHUNK_CHARS,
            max(2000, int(settings.ai_max_input_chars or settings.ai_extractor_max_chars)),
        ),
        max_files=settings.ai_max_files,
        max_pages=settings.ai_max_pages,
    )
    if semantic_chunks:
        logger.info(
            "Extraction semantic chunks: tender_id=%s domains=%s chunk_sizes=%s",
            tender_id,
            list(semantic_chunks.keys()),
            {domain: len(chunk) for domain, chunk in semantic_chunks.items()},
        )

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
            try:
                cached_meta = dict(cached_requirements.get("extract_meta_v1") or {}) if isinstance(cached_requirements.get("extract_meta_v1"), dict) else {}
                if semantic_chunks and "chunking_version" not in cached_meta:
                    logger.info(
                        "Extraction cache stale: tender_id=%s missing_chunking_meta=True",
                        tender_id,
                    )
                else:
                    if "parser_version" not in cached_meta:
                        cached_meta["parser_version"] = PARSER_VERSION
                        cached_requirements["extract_meta_v1"] = cached_meta
                        analysis.requirements = cached_requirements
                        await db.commit()
                    logger.info("Extraction cache hit: tender_id=%s", tender_id)
                    cached_extracted_obj = ExtractedTenderV1.model_validate(cached_extracted)
                    try:
                        await _upsert_extraction_evidence(
                            db,
                            company_id=company_id,
                            tender_id=tender_id,
                            extracted=cached_extracted_obj,
                            extract_meta=cached_meta,
                        )
                    except Exception:
                        logger.exception("Failed to upsert evidence on cache hit: tender_id=%s", tender_id)
                    return analysis, cached_extracted_obj
            except Exception:
                logger.exception("Failed to sync cached parser_version for tender_id=%s", tender_id)

    # Create action record for this extraction run (best-effort — never blocks extraction).
    _action_record = None
    if settings.feature_agent_actions:
        try:
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

    # Use pre-built semantic chunks if available — avoids double document read
    if semantic_chunks:
        merged_text = "\n\n".join(semantic_chunks.values())
    else:
        merged_text = build_normalized_text(
            documents=documents,
            storage_root=settings.storage_root,
            max_chars=settings.ai_max_input_chars or settings.ai_extractor_max_chars,
            max_files=settings.ai_max_files,
            max_pages=settings.ai_max_pages,
        )

    _det_nmck: Decimal | None = None
    for _doc in documents:
        _fname = (_doc.file_name or "").lower()
        if _fname.endswith(".xlsx") or _fname.endswith(".xlsx.zip"):
            _fpath = Path(settings.storage_root) / _doc.storage_path
            if _fpath.exists():
                _det_nmck = extract_nmck_from_file(_fpath)
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
            chunks=semantic_chunks or None,
        )
    except ExtractionProviderError as exc:
        if exc.code == "PROVIDER_TIMEOUT" and _det_nmck is not None:
            logger.warning(
                "Extraction provider timeout fallback: tender_id=%s document_nmck=%s",
                tender_id,
                _det_nmck,
            )
            provider_result = ExtractionProviderResult(
                extracted=ExtractedTenderV1(
                    subject=getattr(tender, "title", None),
                    nmck=_det_nmck,
                    currency="RUB",
                    confidence={"nmck": 0.99, "overall": 0.4},
                    evidence={"nmck": "Deterministic NMCK extracted from XLSX/XLSX.ZIP document on RU after provider timeout"},
                ),
                extract_meta={
                    "provider": "deterministic_fallback",
                    "model": "ru-document-nmck-fallback",
                    "latency_ms": None,
                    "doc_coverage": 1.0,
                    "confidence": 0.99,
                    "parser_version": PARSER_VERSION,
                    "warnings": ["provider_timeout_fallback"],
                    "sources": [str(tender_id)],
                    "estimated_cost": None,
                    "chars_sent": len(merged_text),
                },
            )
        else:
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
            if settings.feature_agent_actions and _action_record is not None:
                try:
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
    extract_meta = dict(provider_result.extract_meta or {}) if isinstance(provider_result.extract_meta, dict) else {}
    if "parser_version" not in extract_meta:
        extract_meta["parser_version"] = PARSER_VERSION
    if semantic_chunks:
        extract_meta["chunking_version"] = CHUNKING_VERSION
        extract_meta["domains_extracted"] = list(semantic_chunks.keys())
        extract_meta["chunk_sizes"] = {domain: len(chunk) for domain, chunk in semantic_chunks.items()}
    # Unified pipeline version snapshot — all components in one place.
    extract_meta["pipeline_versions"] = pipeline_versions()

    # Upsert per-field evidence rows. ON CONFLICT DO UPDATE ensures re-extraction
    # overwrites stale rows rather than inserting duplicates.
    await _upsert_extraction_evidence(
        db,
        company_id=company_id,
        tender_id=tender_id,
        extracted=extracted,
        extract_meta=extract_meta,
    )

    if analysis is None:
        analysis = TenderAnalysis(
            company_id=company_id,
            tender_id=tender_id,
            status="ready",
            requirements={
                "extracted_v1": extracted_payload,
                "risk_v1": risk_v1,
                "extract_meta_v1": extract_meta,
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
        merged_requirements["extract_meta_v1"] = extract_meta
        merged_requirements["extract_doc_signature_v1"] = document_signature
        merged_requirements.pop("extract_error_v1", None)
        analysis.requirements = merged_requirements
        analysis.risk_flags = risk_flags
        analysis.summary = summary
        if analysis.status != "approved":
            analysis.status = "ready"
        analysis.updated_by = user_id

    cost_log = AICostLog(
        company_id=company_id,
        tender_id=tender_id,
        model=str(extract_meta.get("model") or "unknown"),
        operation_type=str(extract_meta.get("operation_type") or "extraction"),
        provider=str(extract_meta.get("provider") or "unknown"),
        chars_sent=int(extract_meta.get("chars_sent") or len(merged_text)),
        estimated_cost=_to_decimal_or_none(extract_meta.get("estimated_cost")),
        duration_ms=int(extract_meta.get("latency_ms")) if extract_meta.get("latency_ms") is not None else None,
        status="ok",
        error_code=None,
    )
    db.add(cost_log)

    await db.commit()
    await db.refresh(analysis)

    # Append immutable snapshot for re-score / re-debug without AI call.
    await _save_extraction_snapshot(
        db,
        company_id=company_id,
        tender_id=tender_id,
        extracted=extracted,
        extract_meta=extract_meta,
        doc_signature=document_signature,
    )

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
    _checklist_action = None
    try:
        from app.requirements.normalizer import RequirementNormalizer
        from app.requirements.service import upsert_checklist
        if settings.feature_agent_actions:
            _checklist_action = await create_action(
                db,
                company_id=company_id,
                agent_id=SYSTEM_AGENT_ID,
                action_type="build_checklist",
                target=str(tender_id),
                payload={},
            )
        _normalizer = RequirementNormalizer()
        _reqs = _normalizer.normalize(extracted)
        await upsert_checklist(db, tender_id, company_id, _reqs)
        if settings.feature_agent_actions and _checklist_action is not None:
            await complete_action(
                db, _checklist_action.action_id, result={"reqs_count": len(_reqs)}
            )
    except Exception:
        logger.exception(
            "Failed to build requirements checklist for tender_id=%s", tender_id
        )
        _reqs = []  # ensure _reqs is always defined for fit_score step below

    # Calculate company fit score using the same extracted data.
    # Lazy import + try/except — fit score failure never breaks extraction.
    _fit_action = None
    try:
        from app.fit_score.scorer import FitScorer
        from app.fit_score.service import upsert_fit_score
        from app.models import Company
        from app.requirements.normalizer import RequirementNormalizer
        if settings.feature_agent_actions:
            _fit_action = await create_action(
                db,
                company_id=company_id,
                agent_id=SYSTEM_AGENT_ID,
                action_type="calculate_fit_score",
                target=str(tender_id),
                payload={},
            )
        _company = await db.scalar(select(Company).where(Company.id == company_id))
        _profile: dict = _company.profile if _company and isinstance(_company.profile, dict) else {}
        _checklist = _reqs if _reqs else RequirementNormalizer().normalize(extracted)
        _fit_result = FitScorer().score(_profile, _checklist, extracted)
        await upsert_fit_score(db, tender_id, company_id, _fit_result)
        if settings.feature_agent_actions and _fit_action is not None:
            await complete_action(
                db, _fit_action.action_id,
                result={"score": float(_fit_result.score) if hasattr(_fit_result, "score") else None},
            )
    except Exception:
        logger.exception(
            "Failed to calculate fit score for tender_id=%s", tender_id
        )

    # Mark the extraction action as completed (best-effort).
    if settings.feature_agent_actions and _action_record is not None:
        try:
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
