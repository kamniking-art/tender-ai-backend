from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class CompanyRead(BaseModel):
    id: UUID
    name: str
    inn: str | None
    ogrn: str | None
    legal_address: str | None
    bank_details: dict | None
    scoring_settings: dict | None
    finance_settings: dict | None
    ingestion_settings: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class UserRead(BaseModel):
    id: UUID
    company_id: UUID
    email: EmailStr
    role: str
    created_at: datetime

    model_config = {"from_attributes": True}


class RegisterRequest(BaseModel):
    company_name: str = Field(min_length=1, max_length=255)
    inn: str | None = None
    ogrn: str | None = None
    legal_address: str | None = None
    bank_details: dict | None = None
    scoring_settings: dict | None = None
    finance_settings: dict | None = None

    admin_email: EmailStr
    admin_password: str = Field(min_length=8, max_length=72)


class RegisterResponse(BaseModel):
    company: CompanyRead
    user: UserRead


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
