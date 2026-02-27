from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import CompanyRead
from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Company, User

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("/me", response_model=CompanyRead)
async def get_my_company(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CompanyRead:
    company = await db.scalar(select(Company).where(Company.id == current_user.company_id))
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return CompanyRead.model_validate(company)
