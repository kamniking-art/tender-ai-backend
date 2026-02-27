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

    document_id = uuid.uuid4()
    relative_path, absolute_path = build_storage_path(company_id, tender_id, document_id, file.filename)

    try:
        content = await file.read()
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.write_bytes(content)
    except OSError as exc:
        raise DocumentStorageError("Failed to store uploaded file") from exc
    finally:
        await file.close()

    document = TenderDocument(
        id=document_id,
        company_id=company_id,
        tender_id=tender_id,
        file_name=file.filename or "file",
        storage_path=relative_path,
        content_type=file.content_type,
        doc_type=doc_type,
        file_size=len(content),
        uploaded_by=uploaded_by,
    )

    db.add(document)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        if absolute_path.exists():
            absolute_path.unlink()
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
