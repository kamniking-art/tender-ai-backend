from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.document_module.schemas import (
    DocumentPackageGenerateRequest,
    DocumentPackageGenerateResponse,
    DocumentPackageReadResponse,
    GeneratedFileRead,
    PackageFileRead,
)
from app.document_module.service import (
    DocumentModuleConflictError,
    DocumentModuleNotFoundError,
    DocumentModuleValidationError,
    generate_package_for_tender,
    get_package_for_tender,
)
from app.models import User

router = APIRouter(tags=["document-module"])


@router.post("/tenders/{tender_id}/documents/generate", response_model=DocumentPackageGenerateResponse)
async def generate_tender_package(
    tender_id: UUID,
    payload: DocumentPackageGenerateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentPackageGenerateResponse:
    try:
        generated_files, checklist = await generate_package_for_tender(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
            user_id=current_user.id,
            force=payload.force,
        )
    except DocumentModuleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DocumentModuleConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DocumentModuleValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": str(exc), "missing_fields": exc.missing_fields},
        ) from exc

    return DocumentPackageGenerateResponse(
        ok=True,
        generated_files=[GeneratedFileRead(document_id=item.document_id, filename=item.filename) for item in generated_files],
        checklist=checklist,
    )


@router.get("/tenders/{tender_id}/documents/package", response_model=DocumentPackageReadResponse)
async def get_tender_package(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentPackageReadResponse:
    try:
        package_state = await get_package_for_tender(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
        )
    except DocumentModuleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return DocumentPackageReadResponse(
        exists=bool(package_state.files),
        files=[
            PackageFileRead(
                document_id=item.id,
                filename=item.file_name,
                content_type=item.content_type,
                file_size=item.file_size,
                uploaded_at=item.uploaded_at,
            )
            for item in package_state.files
        ],
        checklist=package_state.checklist,
    )
