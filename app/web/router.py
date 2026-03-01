from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID
from urllib.parse import urlencode

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Integer, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_extraction.interfaces import ExtractionProviderError
from app.ai_extraction.service import ExtractionBadRequestError, run_extraction
from app.ai_extraction.text_extract import NoExtractableTextError
from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token, verify_password
from app.document_module.service import (
    DocumentModuleConflictError,
    DocumentModuleNotFoundError,
    DocumentModuleValidationError,
    get_package_for_tender,
    generate_package_for_tender,
)
from app.models import Company, User
from app.risk.service import compute_risk_flags, compute_risk_score_v1
from app.tender_alerts.schemas import AlertCategory
from app.tender_alerts.service import ack_alert, build_alert_digest, ensure_tender_scoped
from app.tender_analysis.model import TenderAnalysis
from app.tender_analysis.service import AnalysisConflictError, ScopedNotFoundError, get_analysis_scoped
from app.tender_decisions.model import TenderDecision
from app.tender_decisions.service import get_decision_scoped
from app.tender_documents.service import get_document_scoped, list_documents_for_tender
from app.tender_tasks.service import list_tasks
from app.tenders.model import Tender
from app.tenders.schemas import TenderStatus
from app.tenders.service import get_tender_by_id_scoped
from app.decision_engine.service import (
    DecisionEngineBadRequestError,
    ManualRecommendationConflictError,
    recompute_decision_engine_v1,
)
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


def _parse_bool(value: str | None) -> bool:
    if not value:
        return False
    return value.lower() in {"1", "true", "yes", "on"}


def _query_string(params: dict[str, object]) -> str:
    filtered = {k: v for k, v in params.items() if v not in (None, "", False)}
    if not filtered:
        return ""
    return urlencode(filtered)


def _redirect_with_action(tender_id: UUID, action: str, ok: bool, message: str, details: str | None = None) -> RedirectResponse:
    params = {
        "action": action,
        "action_status": "ok" if ok else "error",
        "action_message": message,
    }
    if details:
        params["action_details"] = details[:2000]

    url = f"/web/tenders/{tender_id}?{urlencode(params)}"
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


def _extract_risk_score(analysis: TenderAnalysis | None, decision: TenderDecision | None) -> int | None:
    if decision is not None and decision.risk_score is not None:
        return int(decision.risk_score)

    if analysis and isinstance(analysis.requirements, dict):
        risk = analysis.requirements.get("risk_v1")
        if isinstance(risk, dict):
            score = risk.get("score_auto")
            if isinstance(score, int):
                return score
            if isinstance(score, float):
                return int(score)
    return None


def _top_risk_flags(analysis: TenderAnalysis | None, limit: int = 3) -> list[str]:
    if analysis is None or not isinstance(analysis.risk_flags, list):
        return []
    flags: list[str] = []
    for item in analysis.risk_flags:
        if isinstance(item, dict):
            title = item.get("title") or item.get("code")
            if title:
                flags.append(str(title))
        elif isinstance(item, str):
            flags.append(item)
        if len(flags) >= limit:
            break
    return flags


def _ingestion_status(company: Company) -> dict[str, str]:
    settings_payload = company.ingestion_settings if isinstance(company.ingestion_settings, dict) else {}

    eis_public = settings_payload.get("eis_public") if isinstance(settings_payload.get("eis_public"), dict) else {}
    eis_public_state = eis_public.get("state") if isinstance(eis_public.get("state"), dict) else {}

    eis_opendata = settings_payload.get("eis_opendata") if isinstance(settings_payload.get("eis_opendata"), dict) else {}
    od_state = eis_opendata.get("state") if isinstance(eis_opendata.get("state"), dict) else {}
    discovery = od_state.get("discovery") if isinstance(od_state.get("discovery"), dict) else {}

    public_status = "disabled"
    if eis_public.get("enabled"):
        public_status = "cooldown" if eis_public_state.get("cooldown_until") else "ok"

    opendata_status = "disabled"
    if eis_opendata.get("enabled"):
        opendata_status = str(discovery.get("status") or "unknown")

    return {
        "eis_public": public_status,
        "eis_public_cooldown_until": str(eis_public_state.get("cooldown_until") or "-"),
        "eis_opendata": opendata_status,
        "eis_opendata_last_success_at": str(discovery.get("last_success_at") or "-"),
    }


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
    analysis_status: str | None = Query(default=None),
    decision_filter: str | None = Query(default=None, alias="decision"),
    source_filter: str | None = Query(default=None, alias="source"),
    risk_min: int | None = Query(default=None, ge=0, le=100),
    risk_max: int | None = Query(default=None, ge=0, le=100),
    risky_only: str | None = Query(default=None),
    deadline_from: str | None = Query(default=None),
    deadline_to: str | None = Query(default=None),
    published_from: str | None = Query(default=None),
    published_to: str | None = Query(default=None),
    created_from: str | None = Query(default=None),
    created_to: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    parsed_deadline_from = _parse_optional_datetime(deadline_from)
    parsed_deadline_to = _parse_optional_datetime(deadline_to)
    parsed_published_from = _parse_optional_datetime(published_from)
    parsed_published_to = _parse_optional_datetime(published_to)
    parsed_created_from = _parse_optional_datetime(created_from)
    parsed_created_to = _parse_optional_datetime(created_to)

    if page_size not in {20, 50, 100}:
        page_size = 50

    stmt = (
        select(Tender)
        .outerjoin(TenderAnalysis, and_(TenderAnalysis.company_id == current_user.company_id, TenderAnalysis.tender_id == Tender.id))
        .outerjoin(TenderDecision, and_(TenderDecision.company_id == current_user.company_id, TenderDecision.tender_id == Tender.id))
        .where(Tender.company_id == current_user.company_id)
    )

    count_stmt = (
        select(func.count(func.distinct(Tender.id)))
        .select_from(Tender)
        .outerjoin(TenderAnalysis, and_(TenderAnalysis.company_id == current_user.company_id, TenderAnalysis.tender_id == Tender.id))
        .outerjoin(TenderDecision, and_(TenderDecision.company_id == current_user.company_id, TenderDecision.tender_id == Tender.id))
        .where(Tender.company_id == current_user.company_id)
    )

    if q:
        pattern = f"%{q.strip()}%"
        cond = or_(
            Tender.title.ilike(pattern),
            Tender.customer_name.ilike(pattern),
            Tender.external_id.ilike(pattern),
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    if status_filter:
        stmt = stmt.where(Tender.status == status_filter)
        count_stmt = count_stmt.where(Tender.status == status_filter)

    if source_filter:
        stmt = stmt.where(Tender.source == source_filter)
        count_stmt = count_stmt.where(Tender.source == source_filter)

    if analysis_status:
        if analysis_status == "none":
            stmt = stmt.where(TenderAnalysis.id.is_(None))
            count_stmt = count_stmt.where(TenderAnalysis.id.is_(None))
        else:
            stmt = stmt.where(TenderAnalysis.status == analysis_status)
            count_stmt = count_stmt.where(TenderAnalysis.status == analysis_status)

    if decision_filter:
        if decision_filter == "none":
            stmt = stmt.where(TenderDecision.id.is_(None))
            count_stmt = count_stmt.where(TenderDecision.id.is_(None))
        else:
            stmt = stmt.where(TenderDecision.recommendation == decision_filter)
            count_stmt = count_stmt.where(TenderDecision.recommendation == decision_filter)

    if parsed_deadline_from:
        stmt = stmt.where(Tender.submission_deadline >= parsed_deadline_from)
        count_stmt = count_stmt.where(Tender.submission_deadline >= parsed_deadline_from)
    if parsed_deadline_to:
        stmt = stmt.where(Tender.submission_deadline <= parsed_deadline_to)
        count_stmt = count_stmt.where(Tender.submission_deadline <= parsed_deadline_to)
    if parsed_published_from:
        stmt = stmt.where(Tender.published_at >= parsed_published_from)
        count_stmt = count_stmt.where(Tender.published_at >= parsed_published_from)
    if parsed_published_to:
        stmt = stmt.where(Tender.published_at <= parsed_published_to)
        count_stmt = count_stmt.where(Tender.published_at <= parsed_published_to)
    if parsed_created_from:
        stmt = stmt.where(Tender.created_at >= parsed_created_from)
        count_stmt = count_stmt.where(Tender.created_at >= parsed_created_from)
    if parsed_created_to:
        stmt = stmt.where(Tender.created_at <= parsed_created_to)
        count_stmt = count_stmt.where(Tender.created_at <= parsed_created_to)

    auto_risk_score = cast(TenderAnalysis.requirements["risk_v1"]["score_auto"].astext, Integer)
    effective_risk_score = func.coalesce(TenderDecision.risk_score, auto_risk_score)

    if _parse_bool(risky_only):
        stmt = stmt.where(effective_risk_score >= 70)
        count_stmt = count_stmt.where(effective_risk_score >= 70)

    if risk_min is not None:
        stmt = stmt.where(effective_risk_score >= risk_min)
        count_stmt = count_stmt.where(effective_risk_score >= risk_min)

    if risk_max is not None:
        stmt = stmt.where(effective_risk_score <= risk_max)
        count_stmt = count_stmt.where(effective_risk_score <= risk_max)

    total = int((await db.execute(count_stmt)).scalar_one() or 0)
    offset = (page - 1) * page_size
    if offset >= total and total > 0:
        page = max(1, ((total - 1) // page_size) + 1)
        offset = (page - 1) * page_size

    stmt = stmt.order_by(Tender.submission_deadline.asc().nulls_last(), Tender.created_at.desc()).offset(offset).limit(page_size)
    tenders = list((await db.scalars(stmt)).all())

    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    base_filters = {
        "q": q or "",
        "status": status_filter or "",
        "analysis_status": analysis_status or "",
        "decision": decision_filter or "",
        "source": source_filter or "",
        "risk_min": risk_min if risk_min is not None else "",
        "risk_max": risk_max if risk_max is not None else "",
        "risky_only": "true" if _parse_bool(risky_only) else "",
        "deadline_from": deadline_from or "",
        "deadline_to": deadline_to or "",
        "published_from": published_from or "",
        "published_to": published_to or "",
        "created_from": created_from or "",
        "created_to": created_to or "",
        "page_size": page_size,
    }

    prev_qs = _query_string({**base_filters, "page": page - 1}) if page > 1 else ""
    next_qs = _query_string({**base_filters, "page": page + 1}) if page < total_pages else ""

    return templates.TemplateResponse(
        "tenders.html",
        _template_context(
            request,
            current_user,
            tenders=tenders,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            prev_qs=prev_qs,
            next_qs=next_qs,
            filters=base_filters,
            statuses=[status.value for status in TenderStatus],
            analysis_statuses=["none", "draft", "ready", "approved"],
            decision_statuses=["none", "go", "no_go", "unsure"],
            source_values=["eis", "eis_public", "eis_opendata", "manual", "other"],
        ),
    )


@router.get("/tenders/{tender_id}")
async def tender_detail_page(
    request: Request,
    tender_id: UUID,
    action: str | None = Query(default=None),
    action_status: str | None = Query(default=None),
    action_message: str | None = Query(default=None),
    action_details: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    tender = await get_tender_by_id_scoped(db, current_user.company_id, tender_id)
    if tender is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tender not found")

    company = await db.scalar(select(Company).where(Company.id == current_user.company_id))
    decision = await get_decision_scoped(db, current_user.company_id, tender_id)
    analysis = await get_analysis_scoped(db, current_user.company_id, tender_id)
    tasks = await list_tasks(db, current_user.company_id, tender_id, order_by="due_at asc")
    documents = await list_documents_for_tender(db, company_id=current_user.company_id, tender_id=tender_id)
    package = await get_package_for_tender(db, company_id=current_user.company_id, tender_id=tender_id)

    risk_score = _extract_risk_score(analysis, decision)
    risk_flags_top = _top_risk_flags(analysis)

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
            badges={
                "analysis_status": analysis.status if analysis else "none",
                "risk_score": risk_score,
                "risk_flags": risk_flags_top,
                "decision": decision.recommendation if decision else "none",
                "margin_pct": decision.expected_margin_pct if decision else None,
                "documents_count": len(documents),
                "package_generated": bool(package.files),
                "ingestion": _ingestion_status(company) if company else {},
            },
            action_result={
                "action": action,
                "status": action_status,
                "message": action_message,
                "details": action_details,
            },
        ),
    )


@router.post("/tenders/{tender_id}/extract")
async def web_extract_tender(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    try:
        analysis, _ = await run_extraction(
            db,
            company_id=current_user.company_id,
            user_id=current_user.id,
            tender_id=tender_id,
            document_ids=None,
        )
        return _redirect_with_action(tender_id, "extract", True, f"Extraction completed. Analysis status: {analysis.status}")
    except (ScopedNotFoundError,):
        return _redirect_with_action(tender_id, "extract", False, "Tender not found")
    except (ExtractionBadRequestError, NoExtractableTextError) as exc:
        return _redirect_with_action(tender_id, "extract", False, "Extraction failed", str(exc))
    except AnalysisConflictError as exc:
        return _redirect_with_action(tender_id, "extract", False, "Extraction blocked", str(exc))
    except ExtractionProviderError as exc:
        return _redirect_with_action(tender_id, "extract", False, f"Extractor error: {exc.code}", str(exc))
    except Exception as exc:
        return _redirect_with_action(tender_id, "extract", False, "Extractor unexpected error", str(exc))


@router.post("/tenders/{tender_id}/risk/recompute")
async def web_recompute_risk(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    tender = await get_tender_by_id_scoped(db, current_user.company_id, tender_id)
    if tender is None:
        return _redirect_with_action(tender_id, "risk", False, "Tender not found")

    analysis = await db.scalar(
        select(TenderAnalysis).where(
            TenderAnalysis.company_id == current_user.company_id,
            TenderAnalysis.tender_id == tender_id,
        )
    )
    if analysis is None:
        return _redirect_with_action(tender_id, "risk", False, "Analysis not found")

    if analysis.status == "approved":
        return _redirect_with_action(tender_id, "risk", False, "Approved analysis cannot be overwritten")

    extracted_payload = (analysis.requirements or {}).get("extracted_v1")
    if extracted_payload is None:
        return _redirect_with_action(tender_id, "risk", False, "Extracted data is missing")

    try:
        from app.ai_extraction.schemas import ExtractedTenderV1

        extracted = ExtractedTenderV1.model_validate(extracted_payload)
    except Exception:
        return _redirect_with_action(tender_id, "risk", False, "Invalid extracted_v1 payload")

    risk_flags = compute_risk_flags(extracted, tender)
    risk_v1 = compute_risk_score_v1(extracted, tender)

    req = dict(analysis.requirements or {})
    req["risk_v1"] = risk_v1
    analysis.requirements = req
    analysis.risk_flags = risk_flags
    analysis.updated_by = current_user.id
    if analysis.status == "draft":
        analysis.status = "ready"

    await db.commit()
    return _redirect_with_action(tender_id, "risk", True, f"Risk recomputed: score={risk_v1.get('score_auto')}")


@router.post("/tenders/{tender_id}/engine/recompute")
async def web_recompute_engine(
    tender_id: UUID,
    force: bool = Form(default=False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    try:
        decision, engine = await recompute_decision_engine_v1(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
            user_id=current_user.id,
            force=force,
        )
        return _redirect_with_action(
            tender_id,
            "engine",
            True,
            f"Engine recomputed. Recommendation={decision.recommendation}, score={engine.get('score')}",
        )
    except ManualRecommendationConflictError as exc:
        return _redirect_with_action(tender_id, "engine", False, "Manual recommendation set", str(exc))
    except DecisionEngineBadRequestError as exc:
        return _redirect_with_action(tender_id, "engine", False, "Engine recompute failed", str(exc))


@router.post("/tenders/{tender_id}/documents/generate")
async def web_generate_tender_package(
    tender_id: UUID,
    force: bool = Form(default=False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    try:
        generated_files, _ = await generate_package_for_tender(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
            user_id=current_user.id,
            force=force,
        )
        return _redirect_with_action(
            tender_id,
            "package",
            True,
            f"Package generated ({len(generated_files)} files)",
        )
    except DocumentModuleNotFoundError as exc:
        return _redirect_with_action(tender_id, "package", False, "Package generation failed", str(exc))
    except DocumentModuleConflictError as exc:
        return _redirect_with_action(tender_id, "package", False, "Package generation blocked", str(exc))
    except DocumentModuleValidationError as exc:
        return _redirect_with_action(tender_id, "package", False, "Company profile validation failed", str(exc))


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
