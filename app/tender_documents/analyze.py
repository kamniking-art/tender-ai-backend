from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.risk.service import compute_risk_flags, compute_risk_score_v1
from app.relevance.service import compute_relevance_v1
from app.document_module.service import (
    DocumentModuleConflictError,
    DocumentModuleNotFoundError,
    DocumentModuleValidationError,
    generate_package_for_tender,
)
from app.tender_analysis.model import TenderAnalysis
from app.tender_analysis.service import AnalysisConflictError, ScopedNotFoundError
from app.tender_documents.service import (
    DocumentStorageError,
    SourceFetchError,
    create_document_from_bytes,
    fetch_source_documents,
    list_documents_for_tender,
)
from app.tenders.service import get_tender_by_id_scoped

logger = logging.getLogger(__name__)


def _step(status: str, **kwargs: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"status": status}
    data.update(kwargs)
    return data


async def fetch_and_store_source_documents(
    db: AsyncSession,
    *,
    company_id: UUID,
    user_id: UUID,
    tender_id: UUID,
    source_url: str,
) -> dict[str, Any]:
    existing_docs = await list_documents_for_tender(db, company_id=company_id, tender_id=tender_id)
    existing_signatures = {(item.file_name.lower(), item.file_size or -1) for item in existing_docs}

    try:
        fetch_result = await fetch_source_documents(source_url, max_docs=20)
    except SourceFetchError as exc:
        blocked_by_source = exc.source_status == "blocked"
        message = str(exc)
        if blocked_by_source and exc.http_status == 434:
            message = "ЕИС временно блокирует запросы (HTTP 434), попробуйте позже"
        return {
            "source_status": exc.source_status,
            "blocked_by_source": blocked_by_source,
            "message": message,
            "attempted_pages": exc.attempted_pages,
            "found_links_count": exc.found_links_count,
            "http_status": exc.http_status,
            "downloaded_count": 0,
            "saved_files": [],
            "skipped_duplicates": 0,
            "errors_sample": exc.errors_sample[:3],
        }

    downloaded_count = 0
    skipped_duplicates = 0
    saved_files: list[str] = []
    errors_sample = list(fetch_result.errors_sample[:3])

    for file_item in fetch_result.files:
        signature = (file_item.file_name.lower(), len(file_item.content))
        if signature in existing_signatures:
            skipped_duplicates += 1
            continue
        try:
            created = await create_document_from_bytes(
                db,
                company_id=company_id,
                tender_id=tender_id,
                uploaded_by=user_id,
                file_name=file_item.file_name,
                content=file_item.content,
                content_type=file_item.content_type,
                doc_type="source_import",
            )
        except (ScopedNotFoundError, DocumentStorageError):
            errors_sample.append(f"{file_item.file_name}: save_failed")
            continue
        existing_signatures.add(signature)
        downloaded_count += 1
        saved_files.append(created.file_name)

    return {
        "source_status": fetch_result.source_status,
        "blocked_by_source": False,
        "message": (
            "Документы загружены"
            if downloaded_count > 0
            else (
                "Все найденные файлы уже загружены"
                if fetch_result.found_links_count > 0
                else "На карточке ЕИС документы не найдены"
            )
        ),
        "attempted_pages": fetch_result.attempted_pages,
        "found_links_count": fetch_result.found_links_count,
        "http_status": fetch_result.http_status,
        "downloaded_count": downloaded_count,
        "saved_files": saved_files,
        "skipped_duplicates": skipped_duplicates,
        "errors_sample": errors_sample[:3],
    }


async def analyze_from_source(
    db: AsyncSession,
    *,
    company_id: UUID,
    user_id: UUID,
    tender_id: UUID,
) -> dict[str, Any]:
    started_at = time.monotonic()
    result: dict[str, Any] = {"status": "partial", "steps": {}, "next_step": "Проверьте данные тендера"}

    tender = await get_tender_by_id_scoped(db, company_id, tender_id)
    if tender is None:
        return {
            "status": "error",
            "steps": {"fetch_documents": _step("error", message="Тендер не найден")},
            "next_step": "Откройте корректную карточку тендера",
        }
    if not tender.source_url:
        return {
            "status": "partial",
            "steps": {"fetch_documents": _step("error", message="У тендера нет source_url")},
            "next_step": "Проверьте источник тендера",
        }

    fetch_payload = await fetch_and_store_source_documents(
        db,
        company_id=company_id,
        user_id=user_id,
        tender_id=tender_id,
        source_url=tender.source_url,
    )
    fetch_ok = (fetch_payload.get("downloaded_count") or 0) > 0
    result["steps"]["fetch_documents"] = _step(
        "ok" if fetch_ok else ("blocked_by_source" if fetch_payload.get("blocked_by_source") else "error"),
        downloaded_count=fetch_payload.get("downloaded_count", 0),
        found_links_count=fetch_payload.get("found_links_count", 0),
        attempted_pages=fetch_payload.get("attempted_pages", 0),
        source_status=fetch_payload.get("source_status"),
        http_status=fetch_payload.get("http_status"),
        blocked_by_source=bool(fetch_payload.get("blocked_by_source")),
        message=fetch_payload.get("message"),
    )
    if fetch_payload.get("errors_sample"):
        result["steps"]["fetch_documents"]["errors_sample"] = fetch_payload["errors_sample"]

    # If source is blocked but documents were uploaded earlier, continue with analysis.
    docs_after_fetch = await list_documents_for_tender(db, company_id=company_id, tender_id=tender_id)
    docs_available = len(docs_after_fetch) > 0
    fetch_ok = fetch_ok or docs_available
    if fetch_payload.get("blocked_by_source") and docs_available:
        result["steps"]["fetch_documents"]["status"] = "ok"
        result["steps"]["fetch_documents"]["message"] = "Источник временно блокирует доступ, использованы ранее загруженные документы"

    if not fetch_ok:
        if fetch_payload.get("blocked_by_source"):
            result["next_step"] = "ЕИС временно блокирует доступ к документам, попробуйте позже"
            result["analysis_stage"] = "blocked_by_source"
        else:
            result["next_step"] = "Документы на ЕИС не найдены"
            result["analysis_stage"] = "documents_missing"
        result["steps"]["extract"] = _step("skipped", message="Нет документов для извлечения")
        result["steps"]["analysis"] = _step("skipped", message="Анализ не запущен")
        result["steps"]["package"] = _step("skipped", message="Пакет не сформирован")
        logger.info(
            "analyze_from_source done tender_id=%s external_id=%s stage=fetch status=partial downloaded=0 source_status=%s http_status=%s duration_ms=%s",
            tender_id,
            tender.external_id,
            fetch_payload.get("source_status"),
            fetch_payload.get("http_status"),
            int((time.monotonic() - started_at) * 1000),
        )
        return result

    try:
        from app.ai_extraction.interfaces import ExtractionProviderError
        from app.ai_extraction.service import ExtractionBadRequestError, run_extraction
        from app.ai_extraction.text_extract import NoExtractableTextError

        analysis, extracted = await run_extraction(
            db,
            company_id=company_id,
            user_id=user_id,
            tender_id=tender_id,
            document_ids=None,
        )
        result["steps"]["extract"] = _step("ok", analysis_status=analysis.status)
    except (
        ScopedNotFoundError,
        ExtractionBadRequestError,
        NoExtractableTextError,
        AnalysisConflictError,
        ExtractionProviderError,
    ) as exc:
        result["steps"]["extract"] = _step("error", message=str(exc))
        result["next_step"] = "Проверьте документы вручную"
        logger.info(
            "analyze_from_source done tender_id=%s external_id=%s stage=extract status=partial error=%s duration_ms=%s",
            tender_id,
            tender.external_id,
            str(exc),
            int((time.monotonic() - started_at) * 1000),
        )
        return result

    relevance_payload = compute_relevance_v1(tender=tender, analysis=analysis, extracted=extracted)
    result["steps"]["relevance"] = _step(
        "ok",
        relevance_score=relevance_payload.get("score"),
        relevance_label=relevance_payload.get("label"),
        category=relevance_payload.get("category"),
        matched_keywords=relevance_payload.get("matched_keywords", []),
        is_relevant=relevance_payload.get("is_relevant"),
    )

    try:
        risk_flags = compute_risk_flags(extracted, tender)
        risk_v1 = compute_risk_score_v1(extracted, tender)
        req = dict(analysis.requirements or {})
        req["risk_v1"] = risk_v1
        analysis.requirements = req
        analysis.risk_flags = risk_flags
        analysis.updated_by = user_id
        if analysis.status == "draft":
            analysis.status = "ready"
        await db.commit()
        risk_score = risk_v1.get("score_auto")
        result["steps"]["risk"] = _step("ok", risk_score=risk_score)
    except Exception as exc:  # noqa: BLE001
        result["steps"]["risk"] = _step("error", message="Не удалось рассчитать риск")
        result["steps"]["analysis"] = _step("error", message="Не удалось собрать анализ")
        result["steps"]["package"] = _step("skipped", message="Пакет не сформирован")
        result["next_step"] = "Проверьте извлечённые требования"
        logger.warning("analyze_from_source risk error tender_id=%s err=%s", tender_id, exc)
        return result

    try:
        from app.decision_engine.service import (
            DecisionEngineBadRequestError,
            ManualRecommendationConflictError,
            recompute_decision_engine_v1,
        )

        decision, _ = await recompute_decision_engine_v1(
            db,
            company_id=company_id,
            tender_id=tender_id,
            user_id=user_id,
            force=True,
        )
        result["steps"]["recompute_engine"] = _step("ok", recommendation=decision.recommendation)
        result["steps"]["analysis"] = _step(
            "ok",
            risk_score=risk_score,
            recommendation=decision.recommendation,
        )
    except (ManualRecommendationConflictError, DecisionEngineBadRequestError) as exc:
        result["steps"]["recompute_engine"] = _step("error", message=str(exc))
        result["steps"]["analysis"] = _step("error", message="Не удалось завершить анализ")
        result["steps"]["package"] = _step("skipped", message="Пакет не сформирован")
        result["next_step"] = "Проверьте извлечённые требования"
        return result

    # Auto-generate package when recommendation allows it.
    if decision.recommendation in {"go", "strong_go"}:
        try:
            generated_files, _ = await generate_package_for_tender(
                db,
                company_id=company_id,
                tender_id=tender_id,
                user_id=user_id,
                force=True,
            )
            result["steps"]["package"] = _step(
                "ok",
                generated_files_count=len(generated_files),
                message="Пакет документов сформирован",
            )
            result["status"] = "ok"
            result["analysis_stage"] = "decision_done"
            result["next_step"] = "Пайплайн завершён"
        except DocumentModuleValidationError as exc:
            result["steps"]["package"] = _step(
                "error",
                message="Не удалось сформировать пакет: профиль компании заполнен не полностью",
                missing_fields=exc.missing_fields,
            )
            result["status"] = "partial"
            result["analysis_stage"] = "decision_done"
            result["next_step"] = "Заполните профиль компании и повторите формирование пакета"
        except DocumentModuleConflictError:
            result["steps"]["package"] = _step("error", message="Пакет не удалось сформировать из-за конфликта")
            result["status"] = "partial"
            result["analysis_stage"] = "decision_done"
            result["next_step"] = "Проверьте условия формирования пакета"
        except DocumentModuleNotFoundError:
            result["steps"]["package"] = _step("error", message="Пакет не удалось сформировать: тендер или компания не найдены")
            result["status"] = "partial"
            result["analysis_stage"] = "decision_done"
            result["next_step"] = "Проверьте данные тендера и компании"
    else:
        result["steps"]["package"] = _step(
            "skipped",
            message="Пакет доступен только при решении «Участвовать»",
            recommendation=decision.recommendation,
        )
        result["status"] = "ok"
        result["analysis_stage"] = "decision_done"
        result["next_step"] = "Заполните финансовые параметры для итогового решения"

    logger.info(
        "analyze_from_source done tender_id=%s external_id=%s stage=done status=%s duration_ms=%s",
        tender_id,
        tender.external_id,
        result.get("status"),
        int((time.monotonic() - started_at) * 1000),
    )
    return result
