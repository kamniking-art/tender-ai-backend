from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import User
from app.tender_documents.schemas import TenderDocumentRead
from app.tender_documents.analyze import analyze_from_source, fetch_and_store_source_documents
from app.tender_documents.service import (
    DocumentStorageError,
    ScopedNotFoundError,
    enforce_source_fetch_rate_limit,
    create_document_for_tender,
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

    return await fetch_and_store_source_documents(
        db,
        company_id=current_user.company_id,
        user_id=current_user.id,
        tender_id=tender_id,
        source_url=tender.source_url,
    )


@router.post("/tenders/{tender_id}/analyze-from-source")
async def analyze_tender_from_source(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    guard_key = f"{current_user.company_id}:{tender_id}:analyze"
    retry_after = await enforce_source_fetch_rate_limit(guard_key, cooldown_seconds=10 * 60)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Повторный запуск временно ограничен. Повторите через {retry_after} сек.",
        )
    return await analyze_from_source(
        db,
        company_id=current_user.company_id,
        user_id=current_user.id,
        tender_id=tender_id,
    )
