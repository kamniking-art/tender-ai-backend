from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_extraction.interfaces import ExtractionProviderError
from app.ai_extraction.schemas import ExtractedReadResponse, ExtractionRequest, ExtractionResponse
from app.ai_extraction.service import ExtractionBadRequestError, get_extracted_v1, run_extraction
from app.ai_extraction.text_extract import NoExtractableTextError
from app.core.database import get_db
from app.core.security import get_current_user
from app.models import User
from app.tender_analysis.service import AnalysisConflictError, ScopedNotFoundError

router = APIRouter(prefix="/tenders/{tender_id}/analysis", tags=["ai-extraction"])


@router.post("/extract", response_model=ExtractionResponse)
async def extract_analysis(
    tender_id: UUID,
    payload: ExtractionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExtractionResponse:
    try:
        analysis, extracted = await run_extraction(
            db,
            company_id=current_user.company_id,
            user_id=current_user.id,
            tender_id=tender_id,
            document_ids=payload.document_ids,
        )
    except ScopedNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (ExtractionBadRequestError, NoExtractableTextError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except AnalysisConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ExtractionProviderError as exc:
        if exc.code in {"NO_DOCS", "UNSUPPORTED_FORMAT"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        if exc.code == "PROVIDER_TIMEOUT":
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        if exc.code in {"VALIDATION_ERROR"}:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    return ExtractionResponse(
        analysis_status=analysis.status,
        risk_flags=analysis.risk_flags,
        extracted=extracted,
        summary=analysis.summary,
    )


@router.get("/extracted", response_model=ExtractedReadResponse)
async def get_extracted_analysis(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExtractedReadResponse:
    try:
        extracted = await get_extracted_v1(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
        )
    except ScopedNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return ExtractedReadResponse(extracted=extracted)
