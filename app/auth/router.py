from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import LoginRequest, RegisterRequest, RegisterResponse, TokenResponse
from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token, hash_password, verify_password
from app.models import Company, User

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)) -> RegisterResponse:
    existing = await db.scalar(select(User).where(User.email == payload.admin_email))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    company = Company(
        name=payload.company_name,
        inn=payload.inn,
        ogrn=payload.ogrn,
        legal_address=payload.legal_address,
        bank_details=payload.bank_details,
        scoring_settings=payload.scoring_settings,
        finance_settings=payload.finance_settings,
    )
    db.add(company)
    await db.flush()

    admin_user = User(
        company_id=company.id,
        email=payload.admin_email,
        password_hash=hash_password(payload.admin_password),
        role="admin",
    )
    db.add(admin_user)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Registration conflict")

    await db.refresh(company)
    await db.refresh(admin_user)
    return RegisterResponse(company=company, user=admin_user)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    user = await db.scalar(select(User).where(User.email == payload.email))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    expires = timedelta(minutes=settings.access_token_expire_minutes)
    token = create_access_token(user.id, expires)
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=int(expires.total_seconds()),
    )
