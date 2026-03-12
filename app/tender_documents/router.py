from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import User
from app.tender_documents.schemas import TenderDocumentRead
from app.tender_documents.service import (
    DocumentStorageError,
    ScopedNotFoundError,
    SourceFetchError,
    SourceFetchResult,
    create_document_from_bytes,
    enforce_source_fetch_rate_limit,
    create_document_for_tender,
    fetch_source_documents,
    delete_document_scoped,
    get_document_scoped,
    list_documents_for_tender,
)
from app.core.config import settings
from app.tenders.service import get_tender_by_id_scoped

router = APIRouter(tags=["tender-documents"])


@router.post("/tenders/{tender_id}/documents", response_model=TenderDocumentRead, status_code=status.HTTP_201_CREATED)
async def upload_tender_document(
    tender_id: UUID,
    file: UploadFile = File(...),
    doc_type: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderDocumentRead:
    try:
        document = await create_document_for_tender(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
            uploaded_by=current_user.id,
            file=file,
            doc_type=doc_type,
        )
    except ScopedNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tender not found")
    except DocumentStorageError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return TenderDocumentRead.model_validate(document)


@router.get("/tenders/{tender_id}/documents", response_model=list[TenderDocumentRead])
async def list_tender_documents(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TenderDocumentRead]:
    try:
        documents = await list_documents_for_tender(db, company_id=current_user.company_id, tender_id=tender_id)
    except ScopedNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tender not found")

    return [TenderDocumentRead.model_validate(item) for item in documents]


@router.get("/tender-documents/{document_id}", response_model=TenderDocumentRead)
async def get_tender_document(
    document_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderDocumentRead:
    document = await get_document_scoped(db, company_id=current_user.company_id, document_id=document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return TenderDocumentRead.model_validate(document)


@router.get("/tender-documents/{document_id}/download")
async def download_tender_document(
    document_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FileResponse:
    document = await get_document_scoped(db, company_id=current_user.company_id, document_id=document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Документ не найден")

    if not document.storage_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл не найден на сервере")

    file_path = Path(settings.storage_root) / document.storage_path
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл не найден на сервере")

    return FileResponse(
        path=file_path,
        filename=document.file_name,
        media_type=document.content_type or "application/octet-stream",
    )


@router.delete("/tender-documents/{document_id}")
async def delete_tender_document(
    document_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, bool]:
    try:
        await delete_document_scoped(db, company_id=current_user.company_id, document_id=document_id)
    except ScopedNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    except DocumentStorageError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

    return {"ok": True}


@router.post("/tenders/{tender_id}/documents/fetch-from-source")
async def fetch_tender_documents_from_source(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    tender = await get_tender_by_id_scoped(db, current_user.company_id, tender_id)
    if tender is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тендер не найден")
    if not tender.source_url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="У тендера нет source_url")

    guard_key = f"{current_user.company_id}:{tender_id}"
    retry_after = await enforce_source_fetch_rate_limit(guard_key, cooldown_seconds=30 * 60)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Повторная загрузка доступна через {retry_after} сек.",
        )

    documents = await list_documents_for_tender(db, company_id=current_user.company_id, tender_id=tender_id)
    existing_signatures = {(item.file_name.lower(), item.file_size or -1) for item in documents}

    try:
        result: SourceFetchResult = await fetch_source_documents(tender.source_url, max_docs=20)
    except SourceFetchError as exc:
        return {
            "source_status": exc.source_status,
            "message": str(exc),
            "attempted_pages": exc.attempted_pages,
            "found_links_count": exc.found_links_count,
            "downloaded_count": 0,
            "saved_files": [],
            "skipped_duplicates": 0,
            "errors_sample": exc.errors_sample[:3],
        }

    downloaded_count = 0
    skipped_duplicates = 0
    saved_files: list[str] = []
    for file_item in result.files:
        signature = (file_item.file_name.lower(), len(file_item.content))
        if signature in existing_signatures:
            skipped_duplicates += 1
            continue
        try:
            created = await create_document_from_bytes(
                db,
                company_id=current_user.company_id,
                tender_id=tender_id,
                uploaded_by=current_user.id,
                file_name=file_item.file_name,
                content=file_item.content,
                content_type=file_item.content_type,
                doc_type="source_import",
            )
        except (ScopedNotFoundError, DocumentStorageError):
            result.errors_sample.append(f"{file_item.file_name}: save_failed")
            continue
        existing_signatures.add(signature)
        downloaded_count += 1
        saved_files.append(created.file_name)

    return {
        "source_status": result.source_status,
        "message": (
            "Документы загружены"
            if downloaded_count > 0
            else ("Все найденные файлы уже загружены" if result.found_links_count > 0 else "На карточке ЕИС документы не найдены")
        ),
        "attempted_pages": result.attempted_pages,
        "found_links_count": result.found_links_count,
        "downloaded_count": downloaded_count,
        "saved_files": saved_files,
        "skipped_duplicates": skipped_duplicates,
        "errors_sample": result.errors_sample[:3],
    }
