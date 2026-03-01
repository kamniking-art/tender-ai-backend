from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token, verify_password
from app.models import User
from app.tender_alerts.schemas import AlertCategory
from app.tender_alerts.service import ack_alert, build_alert_digest, ensure_tender_scoped
from app.tender_analysis.service import get_analysis_scoped
from app.tender_decisions.service import get_decision_scoped
from app.document_module.service import (
    DocumentModuleConflictError,
    DocumentModuleNotFoundError,
    DocumentModuleValidationError,
    get_package_for_tender,
    generate_package_for_tender,
)
from app.tender_documents.service import get_document_scoped, list_documents_for_tender
from app.tender_tasks.service import list_tasks
from app.tenders.schemas import SortField, SortOrder, TenderStatus
from app.tenders.service import get_tender_by_id_scoped, list_tenders
from app.web.deps import ACCESS_COOKIE_NAME, get_current_user_from_cookie

templates = Jinja2Templates(directory="app/web/templates")

router = APIRouter(prefix="/web", tags=["web"])


def _get_migrations_head() -> str:
    try:
        script = ScriptDirectory.from_config(Config("alembic.ini"))
        return script.get_current_head() or "unknown"
    except Exception:
        return "unknown"


def _version_info() -> dict[str, str]:
    return {
        "version": settings.app_version,
        "built_at": settings.app_built_at,
        "migrations_head": _get_migrations_head(),
    }


def _template_context(request: Request, current_user: User | None, **kwargs):
    context = {
        "request": request,
        "current_user": current_user,
        "version_info": _version_info(),
    }
    context.update(kwargs)
    return context


def _parse_optional_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid datetime: {value}") from exc

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@router.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", _template_context(request, None, error=None))


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await db.scalar(select(User).where(User.email == email))
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            _template_context(request, None, error="Invalid email or password"),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    expires = timedelta(minutes=settings.access_token_expire_minutes)
    token = create_access_token(user.id, expires)

    response = RedirectResponse(url="/web", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=ACCESS_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.web_cookie_secure,
        max_age=int(expires.total_seconds()),
    )
    return response


@router.post("/logout")
async def logout_submit():
    response = RedirectResponse(url="/web/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key=ACCESS_COOKIE_NAME)
    return response


@router.get("")
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    digest = await build_alert_digest(
        db,
        company_id=current_user.company_id,
        user_id=current_user.id,
        since=None,
        include_acknowledged=False,
        categories=None,
        limit=20,
    )
    return templates.TemplateResponse(
        "dashboard.html",
        _template_context(request, current_user, counts=digest.counts, items=digest.items),
    )


@router.post("/alerts/{tender_id}/ack")
async def web_ack_alert(
    request: Request,
    tender_id: UUID,
    category_query: AlertCategory | None = Query(default=None, alias="category"),
    category_form: AlertCategory | None = Form(default=None, alias="category"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    category = category_query or category_form
    if category is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Category is required")

    if not await ensure_tender_scoped(db, current_user.company_id, tender_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tender not found")

    await ack_alert(
        db,
        company_id=current_user.company_id,
        user_id=current_user.id,
        tender_id=tender_id,
        category=category,
    )

    return RedirectResponse(url=request.headers.get("referer", "/web"), status_code=status.HTTP_303_SEE_OTHER)


@router.get("/tenders")
async def tenders_page(
    request: Request,
    q: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    deadline_from: str | None = Query(default=None),
    deadline_to: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    parsed_status = None
    if status_filter:
        try:
            parsed_status = TenderStatus(status_filter)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid status: {status_filter}") from exc

    parsed_deadline_from = _parse_optional_datetime(deadline_from)
    parsed_deadline_to = _parse_optional_datetime(deadline_to)

    items, total = await list_tenders(
        db,
        company_id=current_user.company_id,
        status=parsed_status,
        deadline_from=parsed_deadline_from,
        deadline_to=parsed_deadline_to,
        q=q,
        sort=SortField.DEADLINE,
        order=SortOrder.ASC,
        limit=50,
        offset=0,
    )

    return templates.TemplateResponse(
        "tenders.html",
        _template_context(
            request,
            current_user,
            tenders=items,
            total=total,
            filters={
                "q": q or "",
                "status": status_filter or "",
                "deadline_from": deadline_from or "",
                "deadline_to": deadline_to or "",
            },
            statuses=[status.value for status in TenderStatus],
        ),
    )


@router.get("/tenders/{tender_id}")
async def tender_detail_page(
    request: Request,
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    tender = await get_tender_by_id_scoped(db, current_user.company_id, tender_id)
    if tender is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tender not found")

    decision = await get_decision_scoped(db, current_user.company_id, tender_id)
    analysis = await get_analysis_scoped(db, current_user.company_id, tender_id)
    tasks = await list_tasks(db, current_user.company_id, tender_id, order_by="due_at asc")
    documents = await list_documents_for_tender(db, company_id=current_user.company_id, tender_id=tender_id)
    package = await get_package_for_tender(db, company_id=current_user.company_id, tender_id=tender_id)

    return templates.TemplateResponse(
        "tender_detail.html",
        _template_context(
            request,
            current_user,
            tender=tender,
            decision=decision,
            analysis=analysis,
            tasks=tasks,
            documents=documents,
            package=package,
        ),
    )


@router.post("/tenders/{tender_id}/documents/generate")
async def web_generate_tender_package(
    request: Request,
    tender_id: UUID,
    force: bool = Form(default=False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    try:
        await generate_package_for_tender(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
            user_id=current_user.id,
            force=force,
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

    return RedirectResponse(url=request.headers.get("referer", f"/web/tenders/{tender_id}"), status_code=status.HTTP_303_SEE_OTHER)


@router.get("/tender-documents/{document_id}/download")
async def web_download_tender_document(
    document_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    document = await get_document_scoped(db, company_id=current_user.company_id, document_id=document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    file_path = Path(settings.storage_root) / document.storage_path
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    return FileResponse(
        path=file_path,
        filename=document.file_name,
        media_type=document.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{document.file_name}"'},
    )
