from fastapi import APIRouter, Depends

from app.auth.schemas import UserRead
from app.core.security import get_current_user
from app.models import User

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserRead)
async def get_me(current_user: User = Depends(get_current_user)) -> UserRead:
    return UserRead.model_validate(current_user)
