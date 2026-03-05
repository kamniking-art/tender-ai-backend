from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID
from urllib.parse import urlencode

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
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
from app.ingestion.eis_site.service import run_eis_site_once_for_company
from app.models import Company, User
from app.risk.service import compute_risk_flags, compute_risk_score_v1
from app.tender_alerts.schemas import AlertCategory
from app.tender_alerts.service import ack_alert, build_alert_digest, ensure_tender_scoped
from app.tender_analysis.model import TenderAnalysis
from app.tender_analysis.service import AnalysisConflictError, ScopedNotFoundError, get_analysis_scoped
from app.tender_decisions.model import TenderDecision
from app.tender_decisions.service import get_decision_scoped
from app.tender_documents.service import (
    DocumentStorageError,
    ScopedNotFoundError as DocumentScopedNotFoundError,
    create_document_for_tender,
    get_document_scoped,
    list_documents_for_tender,
)
from app.tender_finance.schemas import TenderFinanceUpsert
from app.tender_finance.service import (
    ScopedNotFoundError as FinanceScopedNotFoundError,
    get_finance_scoped,
    upsert_finance,
)
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

ANALYSIS_STATUS_RU = {
    "none": "нет",
    "draft": "черновик",
    "ready": "готово",
    "approved": "утверждено",
}

DECISION_STATUS_RU = {
    "none": "нет",
    "go": "идём",
    "no_go": "не идём",
    "unsure": "сомнительно",
}

INGESTION_STATE_RU = {
    "ok": "работает",
    "cooldown": "пауза (cooldown)",
    "maintenance": "техработы",
    "unknown": "неизвестно",
    "disabled": "выключено",
}

TENDER_STATUS_RU = {
    "new": "новый",
    "analyzing": "анализ",
    "approved": "утвержден",
    "rejected": "отклонен",
    "submitted": "подан",
    "won": "выигран",
    "lost": "проигран",
}

RISK_FLAG_RU = {
    "short_deadline": "короткие сроки",
    "high_penalty": "высокие штрафы",
    "harsh_penalties": "жесткие штрафы",
    "missing_docs": "не хватает документов",
    "price_anomaly": "аномальная цена",
    "customer_risk": "риск заказчика",
    "high_bid_security": "высокое обеспечение заявки",
    "high_contract_security": "высокое обеспечение контракта",
    "excessive_requirements": "завышенные требования",
}

ALERT_CATEGORY_RU = {
    "new": "новые",
    "deadline_soon": "дедлайн скоро",
    "risky": "высокий риск",
    "go": "рекомендация: идём",
    "no_go": "рекомендация: не идём",
    "overdue_task": "просроченные задачи",
}

TASK_STATUS_RU = {
    "pending": "в работе",
    "done": "выполнено",
    "overdue": "просрочено",
}

TASK_TYPE_RU = {
    "clarification_deadline": "дедлайн разъяснений",
    "submission_deadline": "дедлайн подачи",
    "bid_security_deadline": "дедлайн обеспечения заявки",
    "contract_security_deadline": "дедлайн обеспечения контракта",
    "contract_signing_deadline": "дедлайн подписания контракта",
    "other": "прочее",
}

SOURCE_RU = {
    "eis": "ЕИС",
    "eis_site": "ЕИС (сайт)",
    "eis_public": "ЕИС (публичный поиск)",
    "eis_opendata": "ЕИС (открытые данные)",
    "fallback": "fallback (тестовые CSV)",
    "manual": "вручную",
    "other": "другое",
}

FINANCE_RECOMMENDATION_RU = {
    "go": "участвовать",
    "no_go": "не участвовать",
    "requires_analysis": "требует анализа",
}


def _translate(value: str | None, mapping: dict[str, str], fallback: str = "-") -> str:
    if not value:
        return fallback
    return mapping.get(value, value)


def _format_datetime_ru(value: datetime | str | None) -> str:
    if value is None:
        return "-"

    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return value

    if parsed is None:
        return str(value)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    if parsed.hour == 0 and parsed.minute == 0:
        return parsed.strftime("%d.%m.%Y")
    return parsed.strftime("%d.%m.%Y %H:%M")


def _format_money_ru(value: Decimal | float | int | str | None, currency: str | None = None) -> str:
    if value is None or value == "":
        return "-"
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)

    formatted = f"{decimal_value:,.2f}".replace(",", " ").replace(".", ",")
    if currency == "RUB":
        return f"{formatted} ₽"
    return formatted


def _humanize_risk_flag(flag: str) -> str:
    normalized = flag.strip()
    if not normalized:
        return ""
    translated = RISK_FLAG_RU.get(normalized)
    if translated:
        return translated
    return normalized.replace("_", " ")


def _translate_action_name(action: str | None) -> str:
    action_map = {
        "upload": "загрузка документа",
        "extract": "извлечение требований",
        "risk": "расчет риска",
        "engine": "пересчет рекомендации",
        "package": "формирование пакета",
        "finance": "финансовая оценка",
    }
    return _translate(action, action_map)


def _is_recommendation_category(category: str | None) -> bool:
    if not category:
        return False
    normalized = str(category)
    return normalized in {"go", "no_go", "unsure"} or normalized.startswith("recommendation")


def _has_finance_values(finance) -> bool:
    if not finance:
        return False
    return any(
        getattr(finance, field, None) is not None
        for field in ("cost_estimate", "participation_cost", "win_probability")
    )


def _build_detail_flow(
    *,
    documents_count: int,
    analysis,
    risk_score: int | None,
    finance,
    decision,
    package,
) -> tuple[list[dict[str, str]], dict[str, bool], dict[str, str], str | None]:
    has_documents = documents_count > 0
    analysis_status = analysis.status if analysis else "none"
    requirements = analysis.requirements if analysis and isinstance(analysis.requirements, dict) else {}
    has_requirements = bool(analysis and (requirements or analysis_status in {"ready", "approved"}))
    has_risk = risk_score is not None
    has_finance = _has_finance_values(finance)
    recommendation = decision.recommendation if decision else None
    has_recommendation = recommendation in {"go", "no_go", "unsure"}
    is_go = recommendation == "go"
    has_package = bool(package and package.files)

    can_extract = has_documents
    can_risk = has_requirements
    can_recompute = has_finance
    can_package = has_documents and has_requirements and is_go

    reasons = {
        "extract": "" if can_extract else "Сначала загрузите документы тендера",
        "risk": "" if can_risk else "Сначала извлеките требования",
        "recompute": "" if can_recompute else "Заполните финансовые параметры",
        "package": "",
    }
    if not can_package:
        if not has_documents:
            reasons["package"] = "Загрузите документы"
        elif not has_requirements:
            reasons["package"] = "Сначала извлеките требования"
        else:
            reasons["package"] = "Пакет доступен только при решении «Участвовать»"

    steps = [
        {"name": "Документы", "state": "готово" if has_documents else "нужно"},
        {"name": "Требования", "state": "готово" if has_requirements else "нужно"},
        {"name": "Риск", "state": "готово" if has_risk else "нужно"},
        {"name": "Финансовые параметры", "state": "готово" if has_finance else "нужно"},
        {"name": "Рекомендация", "state": "готово" if has_recommendation else "нужно"},
        {"name": "Пакет документов", "state": "готово" if has_package else "нужно"},
    ]

    next_step = None
    if not has_documents:
        next_step = "Загрузите документы тендера"
    elif not has_requirements:
        next_step = "Извлеките требования"
    elif not has_risk:
        next_step = "Рассчитайте риск"
    elif not has_finance:
        next_step = "Заполните финансовые параметры"
    elif not has_recommendation:
        next_step = "Пересчитайте рекомендацию"
    elif not has_package and is_go:
        next_step = "Сформируйте пакет документов"

    actions = {
        "can_extract": can_extract,
        "can_risk": can_risk,
        "can_recompute": can_recompute,
        "can_package": can_package,
    }
    return steps, actions, reasons, next_step


def _friendly_extract_error(exc: Exception) -> tuple[str, str]:
    text = str(exc)
    normalized = text.lower()
    if "no documents" in normalized or "сначала загрузите документы" in normalized:
        return "Загрузите документы тендера (шаг 1)", "Следующий шаг: загрузите хотя бы один документ"
    if "документ не найден на сервере" in normalized:
        return "Документ не найден на сервере", "Следующий шаг: перезагрузите документ и повторите извлечение"
    if "no extractable text" in normalized:
        return "Не удалось извлечь текст из документов", "Следующий шаг: загрузите документ в формате PDF/DOCX/TXT"
    return "Извлечение не выполнено", text


templates.env.filters["analysis_status_ru"] = lambda value: _translate(value, ANALYSIS_STATUS_RU)
templates.env.filters["decision_status_ru"] = lambda value: _translate(value, DECISION_STATUS_RU)
templates.env.filters["tender_status_ru"] = lambda value: _translate(value, TENDER_STATUS_RU)
templates.env.filters["ingestion_state_ru"] = lambda value: _translate(value, INGESTION_STATE_RU)
templates.env.filters["risk_flag_ru"] = _humanize_risk_flag
templates.env.filters["source_ru"] = lambda value: _translate(value, SOURCE_RU)
templates.env.filters["alert_category_ru"] = lambda value: _translate(value, ALERT_CATEGORY_RU)
templates.env.filters["task_status_ru"] = lambda value: _translate(value, TASK_STATUS_RU)
templates.env.filters["task_type_ru"] = lambda value: _translate(value, TASK_TYPE_RU)
templates.env.filters["ru_dt"] = _format_datetime_ru
templates.env.filters["ru_money"] = _format_money_ru
templates.env.filters["finance_recommendation_ru"] = lambda value: _translate(value, FINANCE_RECOMMENDATION_RU)


def _get_migrations_head() -> str:
    try:
        script = ScriptDirectory.from_config(Config("alembic.ini"))
        return script.get_current_head() or "неизвестно"
    except Exception:
        return "неизвестно"


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


def _parse_optional_int(value: str | None, *, field: str, ge: int | None = None, le: int | None = None) -> int | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized == "":
        return None
    try:
        parsed = int(normalized)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid integer for {field}: {value}",
        ) from exc
    if ge is not None and parsed < ge:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field} must be >= {ge}",
        )
    if le is not None and parsed > le:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field} must be <= {le}",
        )
    return parsed


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
                flags.append(_humanize_risk_flag(str(title)))
        elif isinstance(item, str):
            flags.append(_humanize_risk_flag(item))
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
        "eis_public": _translate(public_status, INGESTION_STATE_RU),
        "eis_public_cooldown_until": str(eis_public_state.get("cooldown_until") or "-"),
        "eis_opendata": _translate(opendata_status, INGESTION_STATE_RU),
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
            _template_context(request, None, error="Неверный email или пароль"),
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
    dashboard_items: list[dict[str, object]] = []
    for item in digest.items:
        category_value = str(item.category)
        recommendation_display = item.recommendation if _is_recommendation_category(category_value) else None
        dashboard_items.append(
            {
                "tender_id": item.tender_id,
                "title": item.title,
                "category": category_value,
                "deadline_at": item.deadline_at,
                "risk_score": item.risk_score,
                "recommendation_display": recommendation_display,
            }
        )
    return templates.TemplateResponse(
        "dashboard.html",
        _template_context(request, current_user, counts=digest.counts, items=dashboard_items),
    )


@router.post("/ingestion/eis-site/run-once")
async def web_run_eis_site_once(
    q: str | None = Form(default=None),
    limit: int = Form(default=50),
    region: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    company = await db.scalar(select(Company).where(Company.id == current_user.company_id))
    if company is None:
        return RedirectResponse(
            url="/web/tenders?ingest_status=error&ingest_message=Компания не найдена",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    stats = await run_eis_site_once_for_company(
        db,
        company,
        query=q or None,
        limit=max(1, min(200, int(limit or 50))),
        region=region or None,
    )
    if stats.source_status == "ok":
        message = f"Добавлено: {stats.inserted}, обновлено: {stats.updated}, пропущено: {stats.skipped}"
        qs = urlencode({"ingest_status": "ok", "ingest_message": message})
        return RedirectResponse(url=f"/web/tenders?{qs}", status_code=status.HTTP_303_SEE_OTHER)

    reason = stats.reason or "неизвестно"
    message = f"Источник недоступен: {stats.source_status} ({reason})"
    details = ",".join(stats.errors_sample) if stats.errors_sample else ""
    qs = urlencode({"ingest_status": "error", "ingest_message": message, "ingest_details": details})
    return RedirectResponse(url=f"/web/tenders?{qs}", status_code=status.HTTP_303_SEE_OTHER)


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
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Нужно указать категорию")

    if not await ensure_tender_scoped(db, current_user.company_id, tender_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тендер не найден")

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
    risk_min: str | None = Query(default=None),
    risk_max: str | None = Query(default=None),
    risky_only: str | None = Query(default=None),
    deadline_from: str | None = Query(default=None),
    deadline_to: str | None = Query(default=None),
    published_from: str | None = Query(default=None),
    published_to: str | None = Query(default=None),
    created_from: str | None = Query(default=None),
    created_to: str | None = Query(default=None),
    ingest_status: str | None = Query(default=None),
    ingest_message: str | None = Query(default=None),
    ingest_details: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    parsed_risk_min = _parse_optional_int(risk_min, field="risk_min", ge=0, le=100)
    parsed_risk_max = _parse_optional_int(risk_max, field="risk_max", ge=0, le=100)
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

    if parsed_risk_min is not None:
        stmt = stmt.where(effective_risk_score >= parsed_risk_min)
        count_stmt = count_stmt.where(effective_risk_score >= parsed_risk_min)

    if parsed_risk_max is not None:
        stmt = stmt.where(effective_risk_score <= parsed_risk_max)
        count_stmt = count_stmt.where(effective_risk_score <= parsed_risk_max)

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
        "risk_min": parsed_risk_min if parsed_risk_min is not None else "",
        "risk_max": parsed_risk_max if parsed_risk_max is not None else "",
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

    source_values = ["eis_site", "manual", "eis_opendata", "fallback", "eis_public", "eis", "other"]
    distinct_sources = list(
        (
            await db.scalars(
                select(Tender.source).where(Tender.company_id == current_user.company_id).distinct()
            )
        ).all()
    )
    for src in sorted({s for s in distinct_sources if s}):
        if src not in source_values:
            source_values.append(src)
    if "fallback" not in {s for s in distinct_sources if s}:
        source_values = [s for s in source_values if s != "fallback"]

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
            source_values=source_values,
            analysis_status_labels=ANALYSIS_STATUS_RU,
            decision_status_labels=DECISION_STATUS_RU,
            tender_status_labels=TENDER_STATUS_RU,
            source_labels=SOURCE_RU,
            ingest_result={
                "status": ingest_status,
                "message": ingest_message,
                "details": ingest_details,
            },
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тендер не найден")

    company = await db.scalar(select(Company).where(Company.id == current_user.company_id))
    decision = await get_decision_scoped(db, current_user.company_id, tender_id)
    analysis = await get_analysis_scoped(db, current_user.company_id, tender_id)
    tasks = await list_tasks(db, current_user.company_id, tender_id, order_by="due_at asc")
    documents = await list_documents_for_tender(db, company_id=current_user.company_id, tender_id=tender_id)
    package = await get_package_for_tender(db, company_id=current_user.company_id, tender_id=tender_id)
    finance = await get_finance_scoped(db, company_id=current_user.company_id, tender_id=tender_id)

    risk_score = _extract_risk_score(analysis, decision)
    risk_flags_top = _top_risk_flags(analysis)
    tender_flow_steps, action_availability, disabled_reasons, next_step = _build_detail_flow(
        documents_count=len(documents),
        analysis=analysis,
        risk_score=risk_score,
        finance=finance,
        decision=decision,
        package=package,
    )

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
            finance=finance,
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
            finance_meta=decision.engine_meta.get("finance") if decision and isinstance(decision.engine_meta, dict) else None,
            tender_flow_steps=tender_flow_steps,
            action_availability=action_availability,
            disabled_reasons=disabled_reasons,
            next_step=next_step,
            action_result={
                "action": _translate_action_name(action),
                "status": "успешно" if action_status == "ok" else ("ошибка" if action_status == "error" else "-"),
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
        documents = await list_documents_for_tender(db, company_id=current_user.company_id, tender_id=tender_id)
    except DocumentScopedNotFoundError:
        return _redirect_with_action(tender_id, "extract", False, "Тендер не найден")
    if not documents:
        return _redirect_with_action(
            tender_id,
            "extract",
            False,
            "Загрузите документы тендера (шаг 1)",
            "Следующий шаг: загрузите хотя бы один документ и повторите извлечение",
        )

    try:
        analysis, _ = await run_extraction(
            db,
            company_id=current_user.company_id,
            user_id=current_user.id,
            tender_id=tender_id,
            document_ids=None,
        )
        analysis_label = _translate(analysis.status, ANALYSIS_STATUS_RU)
        return _redirect_with_action(
            tender_id,
            "extract",
            True,
            f"Извлечение завершено. Статус анализа: {analysis_label}",
            "Следующий шаг: рассчитайте риск (шаг 3)",
        )
    except (ScopedNotFoundError,):
        return _redirect_with_action(tender_id, "extract", False, "Тендер не найден")
    except (ExtractionBadRequestError, NoExtractableTextError) as exc:
        message, details = _friendly_extract_error(exc)
        return _redirect_with_action(tender_id, "extract", False, message, details)
    except AnalysisConflictError as exc:
        return _redirect_with_action(tender_id, "extract", False, "Извлечение заблокировано", str(exc))
    except ExtractionProviderError as exc:
        return _redirect_with_action(tender_id, "extract", False, f"Ошибка сервиса извлечения: {exc.code}", str(exc))
    except Exception as exc:
        return _redirect_with_action(tender_id, "extract", False, "Непредвиденная ошибка извлечения", str(exc))


@router.post("/tenders/{tender_id}/risk/recompute")
async def web_recompute_risk(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    tender = await get_tender_by_id_scoped(db, current_user.company_id, tender_id)
    if tender is None:
        return _redirect_with_action(tender_id, "risk", False, "Тендер не найден")

    analysis = await db.scalar(
        select(TenderAnalysis).where(
            TenderAnalysis.company_id == current_user.company_id,
            TenderAnalysis.tender_id == tender_id,
        )
    )
    if analysis is None:
        return _redirect_with_action(
            tender_id,
            "risk",
            False,
            "Сначала извлеките требования (шаг 2)",
            "Следующий шаг: нажмите «Извлечь требования»",
        )

    if analysis.status == "approved":
        return _redirect_with_action(tender_id, "risk", False, "Нельзя изменять утвержденный анализ")

    extracted_payload = (analysis.requirements or {}).get("extracted_v1")
    if extracted_payload is None:
        return _redirect_with_action(
            tender_id,
            "risk",
            False,
            "Сначала извлеките требования (шаг 2)",
            "Следующий шаг: запустите извлечение требований",
        )

    try:
        from app.ai_extraction.schemas import ExtractedTenderV1

        extracted = ExtractedTenderV1.model_validate(extracted_payload)
    except Exception:
        return _redirect_with_action(
            tender_id,
            "risk",
            False,
            "Не удалось прочитать данные извлечения",
            "Следующий шаг: повторите извлечение требований",
        )

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
    return _redirect_with_action(tender_id, "risk", True, f"Риск пересчитан: score={risk_v1.get('score_auto')}")


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
            f"Рекомендация пересчитана: {_translate(decision.recommendation, DECISION_STATUS_RU)}, score={engine.get('score')}",
        )
    except ManualRecommendationConflictError as exc:
        return _redirect_with_action(tender_id, "engine", False, "Рекомендация задана вручную", str(exc))
    except DecisionEngineBadRequestError as exc:
        return _redirect_with_action(tender_id, "engine", False, "Не удалось пересчитать рекомендацию", str(exc))


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
            f"Пакет сформирован ({len(generated_files)} файлов)",
        )
    except DocumentModuleNotFoundError as exc:
        return _redirect_with_action(
            tender_id,
            "package",
            False,
            "Не удалось сформировать пакет",
            "Следующий шаг: проверьте документы и извлечение требований",
        )
    except DocumentModuleConflictError as exc:
        detail = str(exc)
        if "decision is not go" in detail.lower():
            return _redirect_with_action(
                tender_id,
                "package",
                False,
                "Сначала получите решение «Участвовать» (шаг 5)",
                "Следующий шаг: заполните финпараметры и пересчитайте рекомендацию",
            )
        return _redirect_with_action(tender_id, "package", False, "Формирование пакета заблокировано", detail)
    except DocumentModuleValidationError as exc:
        return _redirect_with_action(tender_id, "package", False, "Профиль компании заполнен не полностью", str(exc))


@router.post("/tenders/{tender_id}/finance")
async def web_upsert_finance(
    tender_id: UUID,
    cost_estimate: Decimal | None = Form(default=None),
    participation_cost: Decimal | None = Form(default=None),
    win_probability: Decimal | None = Form(default=None),
    notes: str | None = Form(default=None),
    auto_recompute: bool = Form(default=True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    try:
        payload = TenderFinanceUpsert(
            cost_estimate=cost_estimate,
            participation_cost=participation_cost,
            win_probability=win_probability,
            notes=notes,
        )
        await upsert_finance(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
            payload=payload,
        )
    except FinanceScopedNotFoundError:
        return _redirect_with_action(tender_id, "finance", False, "Тендер не найден")
    except Exception as exc:
        return _redirect_with_action(tender_id, "finance", False, "Не удалось сохранить финансовые параметры", str(exc))

    if auto_recompute:
        try:
            decision, engine = await recompute_decision_engine_v1(
                db,
                company_id=current_user.company_id,
                tender_id=tender_id,
                user_id=current_user.id,
                force=True,
            )
            return _redirect_with_action(
                tender_id,
                "finance",
                True,
                f"Финансовые параметры сохранены. Рекомендация: {_translate(decision.recommendation, DECISION_STATUS_RU)}",
                f"EV={engine.get('finance', {}).get('expected_value')}",
            )
        except Exception as exc:
            return _redirect_with_action(
                tender_id,
                "finance",
                False,
                "Финансовые параметры сохранены, но пересчет рекомендации не выполнен",
                str(exc),
            )

    return _redirect_with_action(tender_id, "finance", True, "Финансовые параметры сохранены")


@router.post("/tenders/{tender_id}/documents/upload")
async def web_upload_tender_document(
    tender_id: UUID,
    file: UploadFile = File(...),
    doc_type: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    try:
        document = await create_document_for_tender(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
            uploaded_by=current_user.id,
            file=file,
            doc_type=doc_type,
        )
        return _redirect_with_action(
            tender_id,
            "upload",
            True,
            f"Документ загружен: {document.file_name}",
            "Следующий шаг: извлеките требования (шаг 2)",
        )
    except DocumentScopedNotFoundError:
        return _redirect_with_action(tender_id, "upload", False, "Тендер не найден")
    except DocumentStorageError as exc:
        return _redirect_with_action(tender_id, "upload", False, "Не удалось сохранить файл", str(exc))


@router.get("/tender-documents/{document_id}/download")
async def web_download_tender_document(
    document_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    document = await get_document_scoped(db, company_id=current_user.company_id, document_id=document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Документ не найден")

    if not document.storage_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл не найден на сервере")

    file_path = Path(settings.storage_root) / document.storage_path
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл не найден на сервере")

    return FileResponse(
        path=file_path,
        filename=document.file_name,
        media_type=document.content_type or "application/octet-stream",
    )
