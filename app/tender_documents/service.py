import re
import uuid
from pathlib import Path
from uuid import UUID

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.tender_documents.model import TenderDocument
from app.tenders.model import Tender


class ScopedNotFoundError(Exception):
    pass


class DocumentStorageError(Exception):
    pass


def sanitize_filename(filename: str | None) -> str:
    candidate = Path(filename or "").name
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", candidate).strip("._")
    return sanitized or "file"


def build_storage_path(company_id: UUID, tender_id: UUID, document_id: UUID, original_filename: str | None) -> tuple[str, Path]:
    safe_name = sanitize_filename(original_filename)
    relative_path = Path(settings.documents_subdir) / str(company_id) / str(tender_id) / f"{document_id}_{safe_name}"
    absolute_path = Path(settings.storage_root) / relative_path
    return relative_path.as_posix(), absolute_path


async def _get_tender_scoped(db: AsyncSession, company_id: UUID, tender_id: UUID) -> Tender | None:
    stmt = select(Tender).where(Tender.id == tender_id, Tender.company_id == company_id)
    return await db.scalar(stmt)


async def create_document_for_tender(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    uploaded_by: UUID,
    file: UploadFile,
    doc_type: str | None,
) -> TenderDocument:
    tender = await _get_tender_scoped(db, company_id, tender_id)
    if tender is None:
        raise ScopedNotFoundError("Tender not found")

    try:
        content = await file.read()
    finally:
        await file.close()

    return await create_document_from_bytes(
        db,
        company_id=company_id,
        tender_id=tender_id,
        uploaded_by=uploaded_by,
        file_name=file.filename or "file",
        content=content,
        content_type=file.content_type,
        doc_type=doc_type,
    )


def write_bytes_to_storage(relative_path: str, content: bytes) -> Path:
    absolute_path = Path(settings.storage_root) / relative_path
    try:
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.write_bytes(content)
    except OSError as exc:
        raise DocumentStorageError("Failed to store file") from exc
    return absolute_path


async def create_document_from_bytes(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    uploaded_by: UUID | None,
    file_name: str,
    content: bytes,
    content_type: str | None,
    doc_type: str | None,
    relative_path_override: str | None = None,
) -> TenderDocument:
    tender = await _get_tender_scoped(db, company_id, tender_id)
    if tender is None:
        raise ScopedNotFoundError("Tender not found")

    document_id = uuid.uuid4()
    if relative_path_override:
        relative_path = relative_path_override
    else:
        relative_path, _ = build_storage_path(company_id, tender_id, document_id, file_name)

    absolute_path = write_bytes_to_storage(relative_path, content)

    document = TenderDocument(
        id=document_id,
        company_id=company_id,
        tender_id=tender_id,
        file_name=file_name,
        storage_path=relative_path,
        content_type=content_type,
        doc_type=doc_type,
        file_size=len(content),
        uploaded_by=uploaded_by,
    )

    db.add(document)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        try:
            if absolute_path.exists():
                absolute_path.unlink()
        except OSError:
            pass
        raise

    await db.refresh(document)
    return document


async def list_documents_for_tender(db: AsyncSession, *, company_id: UUID, tender_id: UUID) -> list[TenderDocument]:
    tender = await _get_tender_scoped(db, company_id, tender_id)
    if tender is None:
        raise ScopedNotFoundError("Tender not found")

    stmt = (
        select(TenderDocument)
        .where(TenderDocument.company_id == company_id, TenderDocument.tender_id == tender_id)
        .order_by(TenderDocument.uploaded_at.desc())
    )
    return list((await db.scalars(stmt)).all())


async def get_document_scoped(db: AsyncSession, *, company_id: UUID, document_id: UUID) -> TenderDocument | None:
    stmt = select(TenderDocument).where(TenderDocument.id == document_id, TenderDocument.company_id == company_id)
    return await db.scalar(stmt)


async def delete_document_scoped(db: AsyncSession, *, company_id: UUID, document_id: UUID) -> bool:
    document = await get_document_scoped(db, company_id=company_id, document_id=document_id)
    if document is None:
        raise ScopedNotFoundError("Document not found")

    file_path = Path(settings.storage_root) / document.storage_path

    await db.delete(document)
    await db.commit()

    if file_path.exists():
        try:
            file_path.unlink()
        except OSError as exc:
            raise DocumentStorageError("Document deleted from DB, but failed to delete file") from exc

    return True
