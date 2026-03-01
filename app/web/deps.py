from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import TokenPayloadError, decode_access_token
from app.models import User

ACCESS_COOKIE_NAME = "access_token"


def redirect_to_login_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        detail="Authentication required",
        headers={"Location": "/web/login"},
    )


def extract_token_from_cookie(raw_cookie: str | None) -> str | None:
    if not raw_cookie:
        return None
    if raw_cookie.startswith("Bearer "):
        return raw_cookie.split(" ", 1)[1].strip()
    return raw_cookie.strip()


async def get_current_user_from_cookie(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    raw_cookie = request.cookies.get(ACCESS_COOKIE_NAME)
    token = extract_token_from_cookie(raw_cookie)
    if not token:
        raise redirect_to_login_exception()

    try:
        user_id = decode_access_token(token)
    except TokenPayloadError:
        raise redirect_to_login_exception()

    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise redirect_to_login_exception()

    return user
