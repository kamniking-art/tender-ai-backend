from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import logging
from pathlib import Path
import re
from uuid import UUID
from urllib.parse import urlencode

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Integer, and_, case, cast, func, or_, select
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
from app.ingestion.eis_browser.service import run_eis_browser_once_for_company
from app.ingestion.eis_site.service import run_eis_site_bulk_for_company, run_eis_site_once_for_company
from app.models import Company, User
from app.monitoring.schemas import MonitoringSettings, MonitoringSettingsPatch
from app.monitoring.service import get_monitoring_notifications, get_monitoring_settings, patch_monitoring_settings, run_monitoring_cycle
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
    SourceFetchError,
    SourceFetchResult,
    create_document_from_bytes,
    create_document_for_tender,
    enforce_source_fetch_rate_limit,
    fetch_source_documents,
    get_document_scoped,
    is_blacklisted_source_document,
    list_documents_for_tender,
)
from app.tender_documents.analyze import analyze_from_source
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
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web", tags=["web"])

ANALYSIS_STATUS_RU = {
    "none": "нет",
    "draft": "черновик",
    "ready": "готово",
    "approved": "утверждено",
}

DECISION_STATUS_RU = {
    "none": "нет",
    "strong_go": "точно смотреть",
    "go": "идём",
    "review": "быстро проверить",
    "weak": "сомнительно",
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
    "eis_browser": "ЕИС (браузер)",
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

PRIORITY_LABEL_RU = {
    "urgent": "срочно",
    "high": "высокий",
    "medium": "средний",
    "low": "низкий",
}

RELEVANCE_CATEGORIES = [
    "камень / гранит / памятники",
    "благоустройство / строительство",
    "строительные материалы",
    "нерелевантно / прочее",
]
MAX_UI_NMCK = Decimal("1000000000000")


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
    if currency and str(currency).upper() == "RUB":
        return f"{formatted} ₽"
    if currency:
        return f"{formatted} {str(currency).upper()}"
    return formatted


def _format_tender_nmck_ru(
    value: Decimal | float | int | str | None,
    currency: str = "RUB",
    tender_id: UUID | str | None = None,
    external_id: str | None = None,
) -> str:
    if value is None or value == "":
        return "Сумма не указана"
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "Сумма не указана"
    if parsed <= 0 or parsed > MAX_UI_NMCK:
        logger.warning(
            "invalid nmck detected, value=%s tender_id=%s external_id=%s source=ui_sanity",
            str(parsed),
            str(tender_id or "-"),
            external_id or "-",
        )
        return "Сумма не указана"
    return _format_money_ru(parsed, currency)


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
        "source_docs": "скачивание документов с ЕИС",
        "analyze_source": "автоанализ с ЕИС",
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
    return normalized in {"strong_go", "go", "review", "weak", "no_go", "unsure"} or normalized.startswith("recommendation")


def _has_finance_values(finance) -> bool:
    if not finance:
        return False
    return any(
        getattr(finance, field, None) is not None
        for field in ("cost_estimate", "participation_cost", "win_probability")
    )


def _is_valid_tender_document_for_auto_analysis(doc) -> bool:
    doc_type = str(getattr(doc, "doc_type", "") or "").lower()
    file_name = str(getattr(doc, "file_name", "") or "")
    if doc_type == "source_import":
        return False
    if is_blacklisted_source_document(file_name=file_name):
        return False
    return True


def _build_detail_flow(
    *,
    documents_count: int,
    valid_documents_count: int,
    has_source_link: bool,
    analysis,
    risk_score: int | None,
    finance,
    decision,
    package,
) -> tuple[list[dict[str, str]], dict[str, bool], dict[str, str], str | None]:
    has_documents = valid_documents_count > 0
    analysis_status = analysis.status if analysis else "none"
    requirements = analysis.requirements if analysis and isinstance(analysis.requirements, dict) else {}
    has_requirements = bool(analysis and (requirements or analysis_status in {"ready", "approved"}))
    has_risk = risk_score is not None
    has_finance = _has_finance_values(finance)
    recommendation = decision.recommendation if decision else None
    has_recommendation = recommendation in {"strong_go", "go", "review", "weak", "no_go", "unsure"}
    is_go = recommendation in {"strong_go", "go"}
    has_package = bool(package and package.files)

    can_extract = has_documents
    can_risk = has_requirements
    can_recompute = has_finance
    can_package = has_documents and has_requirements and is_go
    can_analyze_source = has_source_link

    reasons = {
        "analyze_source": "" if can_analyze_source else "У тендера отсутствует ссылка на источник",
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
        "can_analyze_source": can_analyze_source,
        "can_extract": can_extract,
        "can_risk": can_risk,
        "can_recompute": can_recompute,
        "can_package": can_package,
    }
    return steps, actions, reasons, next_step


def _pipeline_status_label(
    *,
    documents_state: str,
    has_requirements: bool,
    has_risk: bool,
    has_recommendation: bool,
    has_package: bool,
) -> str:
    if has_package:
        return "Анализ завершён"
    if has_recommendation and has_risk and has_requirements:
        return "Анализ частичный"
    if documents_state in {"не найдены", "только служебные файлы ЕИС"}:
        return "Нет входных документов"
    return "В процессе"


def _what_happened_steps(
    *,
    valid_documents_count: int,
    documents_total_count: int,
    has_requirements: bool,
    has_recommendation: bool,
    has_package: bool,
) -> tuple[list[dict[str, str]], str]:
    if valid_documents_count > 0:
        documents_state = "найдены"
    elif documents_total_count > 0:
        documents_state = "только служебные файлы ЕИС"
    else:
        documents_state = "не найдены"

    requirements_state = "извлечены" if has_requirements else ("частично" if documents_total_count > 0 else "нет")
    analysis_state = "готов" if has_recommendation else ("частичный" if has_requirements else "частичный")
    package_state = "доступен" if has_package else "недоступен"

    steps = [
        {"name": "Документы", "state": documents_state},
        {"name": "Требования", "state": requirements_state},
        {"name": "Анализ", "state": analysis_state},
        {"name": "Пакет", "state": package_state},
    ]
    return steps, documents_state


def _recommendation_factors(
    *,
    recommendation: str | None,
    recommendation_reason: str | None,
    risk_score: int | None,
    relevance_meta: dict[str, object] | None,
    documents_state: str,
    has_requirements: bool,
) -> list[str]:
    factors: list[str] = []
    recommendation_norm = (recommendation or "none").lower()

    if documents_state == "только служебные файлы ЕИС":
        factors.append("Найдены только служебные файлы ЕИС, документов тендера нет")
    elif documents_state == "не найдены":
        factors.append("Документы тендера отсутствуют")
    else:
        factors.append("Документы тендера доступны")

    if has_requirements:
        factors.append("Требования извлечены и учтены в оценке")
    else:
        factors.append("Требования не извлечены, анализ ограничен карточкой закупки")

    score = relevance_meta.get("score") if relevance_meta else None
    if isinstance(score, (int, float)):
        if score >= 70:
            factors.append("Профиль закупки хорошо совпадает с целевой нишей")
        elif score >= 45:
            factors.append("Совпадение с нишей частичное, нужен ручной просмотр")
        else:
            factors.append("Релевантность низкая")

    if risk_score is None:
        factors.append("Риск не рассчитан")
    elif risk_score >= 60:
        factors.append("Риск повышен")
    elif risk_score >= 35:
        factors.append("Риск умеренный")
    else:
        factors.append("Риск низкий")

    if recommendation_norm in {"go", "strong_go"} and documents_state == "найдены":
        factors.append("Есть достаточно данных для перехода к подготовке пакета")

    if recommendation_reason:
        factors.append(recommendation_reason.strip())

    compact: list[str] = []
    for item in factors:
        if item and item not in compact:
            compact.append(item)
        if len(compact) >= 5:
            break
    return compact


def _next_action_items(
    *,
    documents_state: str,
    recommendation: str | None,
    has_source_link: bool,
    can_package: bool,
    next_step: str | None,
) -> list[str]:
    recommendation_norm = (recommendation or "none").lower()
    actions: list[str] = []

    if documents_state in {"не найдены", "только служебные файлы ЕИС"}:
        if has_source_link:
            actions.append("Открыть ЕИС и проверить карточку закупки")
        actions.append("Скачать документы вручную")
        actions.append("Повторить анализ")
    elif recommendation_norm in {"review", "weak", "unsure"}:
        actions.append("Проверить требования вручную")
        actions.append("Уточнить объём и материалы")
        actions.append("Пересчитать рекомендацию после уточнений")
    elif recommendation_norm in {"go", "strong_go"}:
        if can_package:
            actions.append("Сформировать пакет документов")
        actions.append("Проверить сроки и подготовить подачу")
    elif next_step:
        actions.append(next_step)

    if next_step and next_step not in actions:
        actions.append(next_step)

    return actions[:4]


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
templates.env.filters["priority_label_ru"] = lambda value: _translate(value, PRIORITY_LABEL_RU)
templates.env.filters["tender_money_ru"] = _format_tender_nmck_ru


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


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _minutes_until(value: datetime | None) -> int | None:
    if value is None:
        return None
    delta = value - datetime.now(UTC)
    if delta.total_seconds() <= 0:
        return 0
    return int(delta.total_seconds() // 60) + 1


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


def _parse_optional_decimal(value: str | None, *, field: str, ge: Decimal | None = None) -> Decimal | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized == "":
        return None
    try:
        parsed = Decimal(normalized.replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid decimal for {field}: {value}",
        ) from exc
    if ge is not None and parsed < ge:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field} must be >= {ge}",
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
    if analysis and isinstance(analysis.requirements, dict):
        risk = analysis.requirements.get("risk_v1")
        if isinstance(risk, dict):
            score = risk.get("score_auto")
            if isinstance(score, int):
                return score
            if isinstance(score, float):
                return int(score)
    if decision is not None and decision.risk_score is not None:
        return int(decision.risk_score)
    return None


def _extract_relevance(decision: TenderDecision | None) -> dict[str, object] | None:
    if decision is None or not isinstance(decision.engine_meta, dict):
        return None
    payload = decision.engine_meta.get("relevance")
    if not isinstance(payload, dict):
        return None
    return payload


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
    monitor_status: str | None = Query(default=None),
    monitor_message: str | None = Query(default=None),
    monitor_details: str | None = Query(default=None),
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
    company = await db.scalar(select(Company).where(Company.id == current_user.company_id))
    monitoring_settings = get_monitoring_settings(company) if company else MonitoringSettings()
    monitoring_notifications = get_monitoring_notifications(company, limit=20) if company else []
    monitoring_last_result = {}
    if company and isinstance(company.profile, dict):
        state = company.profile.get("monitoring_state")
        if isinstance(state, dict) and isinstance(state.get("last_result"), dict):
            monitoring_last_result = state.get("last_result") or {}

    return templates.TemplateResponse(
        "dashboard.html",
        _template_context(
            request,
            current_user,
            counts=digest.counts,
            items=dashboard_items,
            monitoring_settings=monitoring_settings,
            monitoring_notifications=monitoring_notifications,
            monitoring_last_result=monitoring_last_result,
            monitor_result={
                "status": monitor_status,
                "message": monitor_message,
                "details": monitor_details,
            },
        ),
    )


@router.post("/monitoring/settings")
async def web_save_monitoring_settings(
    enabled: bool = Form(default=False),
    queries_text: str | None = Form(default=None),
    pages_per_query: int = Form(default=5),
    page_size: int = Form(default=20),
    relevance_min: int = Form(default=45),
    notify_only_new: bool = Form(default=False),
    deep_analysis_enabled: bool = Form(default=False),
    deep_analysis_limit_per_run: int = Form(default=5),
    deep_analysis_only_new: bool = Form(default=False),
    deep_analysis_timeout_seconds: int = Form(default=180),
    interval_minutes: int = Form(default=360),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    company = await db.scalar(select(Company).where(Company.id == current_user.company_id))
    if company is None:
        return RedirectResponse(
            url="/web?monitor_status=error&monitor_message=Компания не найдена",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    queries = [line.strip() for line in (queries_text or "").splitlines() if line.strip()]
    patch = MonitoringSettingsPatch(
        enabled=bool(enabled),
        queries=queries,
        pages_per_query=max(1, min(50, int(pages_per_query or 5))),
        page_size=max(10, min(50, int(page_size or 20))),
        relevance_min=max(0, min(100, int(relevance_min or 45))),
        notify_only_new=bool(notify_only_new),
        deep_analysis_enabled=bool(deep_analysis_enabled),
        deep_analysis_limit_per_run=max(0, min(20, int(deep_analysis_limit_per_run or 5))),
        deep_analysis_only_new=bool(deep_analysis_only_new),
        deep_analysis_timeout_seconds=max(30, min(900, int(deep_analysis_timeout_seconds or 180))),
        interval_minutes=max(30, min(24 * 60, int(interval_minutes or 360))),
    )
    settings_payload = patch_monitoring_settings(company, patch)
    await db.commit()
    await db.refresh(company)

    msg = (
        f"Сохранено: запросов={len(settings_payload.queries)}, "
        f"pages={settings_payload.pages_per_query}, page_size={settings_payload.page_size}, "
        f"relevance_min={settings_payload.relevance_min}, "
        f"deep={settings_payload.deep_analysis_enabled}, deep_limit={settings_payload.deep_analysis_limit_per_run}"
    )
    return RedirectResponse(
        url=f"/web?{urlencode({'monitor_status': 'ok', 'monitor_message': msg})}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/monitoring/run-once")
async def web_monitoring_run_once(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    company = await db.scalar(select(Company).where(Company.id == current_user.company_id))
    if company is None:
        return RedirectResponse(
            url="/web?monitor_status=error&monitor_message=Компания не найдена",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    result = await run_monitoring_cycle(db, company=company, actor_user_id=current_user.id)
    summary = (
        f"Запросов: {result.queries_total}, импортировано: {result.imported_total}, "
        f"новых: {result.new_tenders}, релевантных: {result.relevant_found}, "
        f"deep: {result.deep_analysis_attempted}/{result.deep_analysis_completed}/{result.deep_analysis_partial}, "
        f"уведомлений: {result.notifications_sent}"
    )
    return RedirectResponse(
        url=f"/web?{urlencode({'monitor_status': 'ok', 'monitor_message': summary})}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/ingestion/eis-site/run-once")
async def web_run_eis_site_once(
    ingest_query: str | None = Form(default=None),
    q: str | None = Form(default=None),
    limit: int = Form(default=1000),
    pages: int = Form(default=50),
    page_size: int = Form(default=20),
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

    query_value = (ingest_query or q or None)
    stats = await run_eis_site_once_for_company(
        db,
        company,
        query=query_value,
        limit=max(1, min(5000, int(limit or 1000))),
        pages=max(1, min(200, int(pages or 50))),
        page_size=max(10, min(50, int(page_size or 20))),
        region=region or None,
    )
    if stats.source_status == "ok":
        message = f"Добавлено: {stats.inserted}, обновлено: {stats.updated}, пропущено: {stats.skipped}"
        details_parts = []
        if stats.region_filter_applied:
            details_parts.append(
                f"region_filter={stats.region_filter}, candidates_before_region_filter={stats.candidates_before_region_filter}, candidates_after_region_filter={stats.candidates_after_region_filter}"
            )
        qs_payload = {"ingest_status": "ok", "ingest_message": message}
        if details_parts:
            qs_payload["ingest_details"] = ", ".join(details_parts)
        if region:
            qs_payload["region"] = region
        qs = urlencode(qs_payload)
        return RedirectResponse(url=f"/web/tenders?{qs}", status_code=status.HTTP_303_SEE_OTHER)

    reason = stats.reason or "неизвестно"
    retry_minutes = _minutes_until(stats.cooldown_until)
    if stats.source_status == "blocked" and stats.http_status == 434:
        message = "Источник временно блокирует доступ (ЕИС HTTP 434)"
        details_parts = [
            "blocked_by_source",
            f"source_status={stats.source_status}",
            f"http_status={stats.http_status}",
        ]
        if retry_minutes is not None:
            details_parts.append(f"retry_in_minutes={retry_minutes}")
            message = f"{message}. Повторная попытка через {retry_minutes} мин."
    elif stats.source_status == "cooldown":
        message = "Источник временно блокирует доступ"
        details_parts = [
            "blocked_by_source",
            f"source_status={stats.source_status}",
            "cooldown_active",
        ]
        if retry_minutes is not None:
            details_parts.append(f"retry_in_minutes={retry_minutes}")
            message = f"{message}. Повторная попытка через {retry_minutes} мин."
    else:
        message = f"Источник недоступен: {stats.source_status} ({reason})"
        details_parts = [
            f"source_status={stats.source_status}",
            f"http_status={stats.http_status or '-'}",
            f"reason={reason}",
        ]
    browser_fallback_note = None
    if stats.source_status in {"blocked", "cooldown"}:
        browser_stats = await run_eis_browser_once_for_company(
            db,
            company,
            query=query_value,
            pages=3,
            page_size=20,
            limit=50,
            region=region or None,
        )
        browser_fallback_note = (
            f"fallback:eis_browser inserted={browser_stats.inserted}, "
            f"updated={browser_stats.updated}, found={browser_stats.candidates}, status={browser_stats.source_status}"
        )

    if stats.errors_sample:
        details_parts.append("; ".join(stats.errors_sample[:3]))
    if browser_fallback_note:
        details_parts.append(browser_fallback_note)
    details = ", ".join(details_parts)
    qs_payload = {"ingest_status": "error", "ingest_message": message, "ingest_details": details}
    if region:
        qs_payload["region"] = region
    qs = urlencode(qs_payload)
    return RedirectResponse(url=f"/web/tenders?{qs}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ingestion/eis-browser/run-once")
async def web_run_eis_browser_once(
    ingest_query: str | None = Form(default=None),
    q: str | None = Form(default=None),
    pages: int = Form(default=3),
    page_size: int = Form(default=20),
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

    query_value = (ingest_query or q or None)
    stats = await run_eis_browser_once_for_company(
        db,
        company,
        query=query_value,
        pages=max(1, min(5, int(pages or 3))),
        page_size=max(10, min(50, int(page_size or 20))),
        limit=max(1, min(50, int(limit or 50))),
        region=region or None,
    )
    if stats.source_status != "error":
        message = f"Browser import: найдено {stats.candidates}, добавлено {stats.inserted}, обновлено {stats.updated}"
        details = f"stage={stats.stage}, source_status={stats.source_status}"
        qs = urlencode({"ingest_status": "ok", "ingest_message": message, "ingest_details": details})
        return RedirectResponse(url=f"/web/tenders?{qs}", status_code=status.HTTP_303_SEE_OTHER)

    details = ", ".join(stats.errors_sample[:3]) if stats.errors_sample else f"stage={stats.stage}"
    qs = urlencode({"ingest_status": "error", "ingest_message": "Browser import завершился с ошибкой", "ingest_details": details})
    return RedirectResponse(url=f"/web/tenders?{qs}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ingestion/eis-site/run-bulk")
async def web_run_eis_site_bulk(
    queries_text: str | None = Form(default=None),
    pages_per_query: int = Form(default=10),
    page_size: int = Form(default=20),
    dedupe_mode: str = Form(default="update"),
    stop_if_blocked: bool = Form(default=True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    company = await db.scalar(select(Company).where(Company.id == current_user.company_id))
    if company is None:
        return RedirectResponse(
            url="/web/tenders?ingest_status=error&ingest_message=Компания не найдена",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    custom_queries = [line.strip() for line in (queries_text or "").splitlines() if line.strip()]
    queries = custom_queries or settings.eis_site_queries_list
    bulk = await run_eis_site_bulk_for_company(
        db,
        company,
        queries=queries,
        pages_per_query=max(1, min(200, int(pages_per_query or 10))),
        page_size=max(10, min(50, int(page_size or 20))),
        dedupe_mode=dedupe_mode or "update",
        stop_if_blocked=_parse_bool(stop_if_blocked, default=True),
    )

    summary = (
        f"Кандидаты: {bulk.totals.candidates}, добавлено: {bulk.totals.inserted}, "
        f"обновлено: {bulk.totals.updated}, пропущено: {bulk.totals.skipped}"
    )
    details = "; ".join(
        f"{item.query}: +{item.inserted}/~{item.updated}/={item.skipped} ({item.source_status})" for item in bulk.breakdown
    )
    status_value = "ok" if bulk.totals.source_status == "ok" else "error"
    qs = urlencode({"ingest_status": status_value, "ingest_message": summary, "ingest_details": details[:2000]})
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
    search: str | None = Query(default=None, alias="search"),
    status_filter: str | None = Query(default=None, alias="status"),
    analysis_status: str | None = Query(default=None),
    decision_filter: str | None = Query(default=None, alias="decision"),
    source_filter: str | None = Query(default=None, alias="source"),
    region_filter: str | None = Query(default=None, alias="region"),
    price_min: str | None = Query(default=None),
    price_max: str | None = Query(default=None),
    risk_min: str | None = Query(default=None),
    risk_max: str | None = Query(default=None),
    priority_min: str | None = Query(default=None),
    priority_label: str | None = Query(default=None),
    relevance_min: str | None = Query(default=None),
    relevance_category: str | None = Query(default=None),
    relevant_only: str | None = Query(default=None),
    fresh_only: str | None = Query(default="true"),
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
    sort_by: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    parsed_risk_min = _parse_optional_int(risk_min, field="risk_min", ge=0, le=100)
    parsed_risk_max = _parse_optional_int(risk_max, field="risk_max", ge=0, le=100)
    parsed_priority_min = _parse_optional_int(priority_min, field="priority_min", ge=0, le=100)
    parsed_relevance_min = _parse_optional_int(relevance_min, field="relevance_min", ge=0, le=100)
    parsed_price_min = _parse_optional_decimal(price_min, field="price_min", ge=Decimal("0"))
    parsed_price_max = _parse_optional_decimal(price_max, field="price_max", ge=Decimal("0"))
    parsed_deadline_from = _parse_optional_datetime(deadline_from)
    parsed_deadline_to = _parse_optional_datetime(deadline_to)
    parsed_published_from = _parse_optional_datetime(published_from)
    parsed_published_to = _parse_optional_datetime(published_to)
    parsed_created_from = _parse_optional_datetime(created_from)
    parsed_created_to = _parse_optional_datetime(created_to)

    if page_size not in {20, 50, 100}:
        page_size = 50

    query_text = (q or search or "").strip()
    logger.info("web.tenders search query_text=%r q=%r search=%r", query_text, q, search)

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

    if query_text:
        pattern = f"%{query_text}%"
        cond = or_(
            Tender.title.ilike(pattern),
            Tender.customer_name.ilike(pattern),
            Tender.place_text.ilike(pattern),
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    fresh_enabled = _parse_bool(fresh_only, default=True)
    if fresh_enabled:
        cutoff = datetime.now(UTC) - timedelta(days=60)
        stmt = stmt.where(Tender.published_at.is_not(None), Tender.published_at >= cutoff)
        count_stmt = count_stmt.where(Tender.published_at.is_not(None), Tender.published_at >= cutoff)

    if status_filter:
        stmt = stmt.where(Tender.status == status_filter)
        count_stmt = count_stmt.where(Tender.status == status_filter)

    if source_filter:
        stmt = stmt.where(Tender.source == source_filter)
        count_stmt = count_stmt.where(Tender.source == source_filter)

    if region_filter:
        pattern = f"%{region_filter.strip()}%"
        region_cond = or_(
            Tender.region.ilike(pattern),
            Tender.place_text.ilike(pattern),
            Tender.customer_name.ilike(pattern),
            Tender.title.ilike(pattern),
        )
        stmt = stmt.where(region_cond)
        count_stmt = count_stmt.where(region_cond)

    valid_nmck_expr = and_(Tender.nmck.is_not(None), Tender.nmck > 0, Tender.nmck <= MAX_UI_NMCK)
    sane_nmck_sort_expr = case((valid_nmck_expr, Tender.nmck), else_=None)

    if parsed_price_min is not None:
        stmt = stmt.where(valid_nmck_expr, Tender.nmck >= parsed_price_min)
        count_stmt = count_stmt.where(valid_nmck_expr, Tender.nmck >= parsed_price_min)

    if parsed_price_max is not None:
        stmt = stmt.where(valid_nmck_expr, Tender.nmck <= parsed_price_max)
        count_stmt = count_stmt.where(valid_nmck_expr, Tender.nmck <= parsed_price_max)

    if analysis_status:
        if analysis_status == "none":
            stmt = stmt.where(TenderAnalysis.id.is_(None))
            count_stmt = count_stmt.where(TenderAnalysis.id.is_(None))
        else:
            stmt = stmt.where(TenderAnalysis.status == analysis_status)
            count_stmt = count_stmt.where(TenderAnalysis.status == analysis_status)

    decision_values: list[str] = []
    if decision_filter:
        decision_values = [part.strip() for part in decision_filter.split(",") if part.strip()]
    if decision_values:
        if len(decision_values) == 1 and decision_values[0] == "none":
            stmt = stmt.where(TenderDecision.id.is_(None))
            count_stmt = count_stmt.where(TenderDecision.id.is_(None))
        else:
            stmt = stmt.where(TenderDecision.recommendation.in_(decision_values))
            count_stmt = count_stmt.where(TenderDecision.recommendation.in_(decision_values))

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
    effective_risk_score = func.coalesce(auto_risk_score, TenderDecision.risk_score)
    relevance_score = cast(TenderDecision.engine_meta["relevance"]["score"].astext, Integer)
    relevance_category_expr = TenderDecision.engine_meta["relevance"]["category"].astext

    if _parse_bool(risky_only):
        stmt = stmt.where(effective_risk_score >= 70)
        count_stmt = count_stmt.where(effective_risk_score >= 70)

    if parsed_risk_min is not None:
        stmt = stmt.where(effective_risk_score >= parsed_risk_min)
        count_stmt = count_stmt.where(effective_risk_score >= parsed_risk_min)

    if parsed_risk_max is not None:
        stmt = stmt.where(effective_risk_score <= parsed_risk_max)
        count_stmt = count_stmt.where(effective_risk_score <= parsed_risk_max)

    if parsed_priority_min is not None:
        stmt = stmt.where(TenderDecision.priority_score >= parsed_priority_min)
        count_stmt = count_stmt.where(TenderDecision.priority_score >= parsed_priority_min)

    if priority_label:
        stmt = stmt.where(TenderDecision.priority_label == priority_label)
        count_stmt = count_stmt.where(TenderDecision.priority_label == priority_label)

    if parsed_relevance_min is not None:
        stmt = stmt.where(relevance_score >= parsed_relevance_min)
        count_stmt = count_stmt.where(relevance_score >= parsed_relevance_min)
    if _parse_bool(relevant_only):
        stmt = stmt.where(relevance_score >= 45)
        count_stmt = count_stmt.where(relevance_score >= 45)
    if relevance_category:
        stmt = stmt.where(relevance_category_expr == relevance_category)
        count_stmt = count_stmt.where(relevance_category_expr == relevance_category)

    total = int((await db.execute(count_stmt)).scalar_one() or 0)
    offset = (page - 1) * page_size
    if offset >= total and total > 0:
        page = max(1, ((total - 1) // page_size) + 1)
        offset = (page - 1) * page_size

    sort_mode = (sort_by or "published_desc").strip().lower()
    if sort_mode == "published_asc":
        stmt = stmt.order_by(Tender.published_at.asc().nulls_last(), Tender.created_at.asc())
    elif sort_mode == "deadline_asc":
        stmt = stmt.order_by(Tender.submission_deadline.asc().nulls_last(), Tender.published_at.desc().nulls_last())
    elif sort_mode == "deadline_desc":
        stmt = stmt.order_by(Tender.submission_deadline.desc().nulls_last(), Tender.published_at.desc().nulls_last())
    elif sort_mode == "nmck_desc":
        stmt = stmt.order_by(sane_nmck_sort_expr.desc().nulls_last(), Tender.published_at.desc().nulls_last())
    elif sort_mode == "nmck_asc":
        stmt = stmt.order_by(sane_nmck_sort_expr.asc().nulls_last(), Tender.published_at.desc().nulls_last())
    elif sort_mode == "priority_desc":
        stmt = stmt.order_by(
            TenderDecision.priority_score.desc().nulls_last(),
            Tender.submission_deadline.asc().nulls_last(),
            Tender.created_at.desc(),
        )
    else:  # published_desc
        sort_mode = "published_desc"
        stmt = stmt.order_by(Tender.published_at.desc().nulls_last(), Tender.created_at.desc())
    stmt = stmt.offset(offset).limit(page_size)
    tenders = list((await db.scalars(stmt)).all())

    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    base_filters = {
        "search": query_text,
        "status": status_filter or "",
        "analysis_status": analysis_status or "",
        "decision": decision_filter or "",
        "source": source_filter or "",
        "region": region_filter or "",
        "price_min": str(parsed_price_min) if parsed_price_min is not None else "",
        "price_max": str(parsed_price_max) if parsed_price_max is not None else "",
        "risk_min": parsed_risk_min if parsed_risk_min is not None else "",
        "risk_max": parsed_risk_max if parsed_risk_max is not None else "",
        "priority_min": parsed_priority_min if parsed_priority_min is not None else "",
        "priority_label": priority_label or "",
        "relevance_min": parsed_relevance_min if parsed_relevance_min is not None else "",
        "relevance_category": relevance_category or "",
        "relevant_only": "true" if _parse_bool(relevant_only) else "",
        "fresh_only": "true" if fresh_enabled else "",
        "risky_only": "true" if _parse_bool(risky_only) else "",
        "deadline_from": deadline_from or "",
        "deadline_to": deadline_to or "",
        "published_from": published_from or "",
        "published_to": published_to or "",
        "created_from": created_from or "",
        "created_to": created_to or "",
        "page_size": page_size,
        "sort_by": sort_mode,
    }

    now_utc = datetime.now(UTC)
    urgent_deadline_from = now_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    urgent_deadline_to = (now_utc + timedelta(days=14)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    new_from = (now_utc - timedelta(days=3)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    quick_pick_urls = {
        "new": f"/web/tenders?{_query_string({'published_from': new_from, 'sort_by': 'published_desc', 'page_size': page_size})}",
        "new_and_large": f"/web/tenders?{_query_string({'published_from': new_from, 'sort_by': 'published_desc', 'price_min': '3000000', 'page_size': page_size})}",
        "urgent": f"/web/tenders?{_query_string({'deadline_from': urgent_deadline_from, 'deadline_to': urgent_deadline_to, 'sort_by': 'deadline_asc', 'page_size': page_size})}",
        "large": f"/web/tenders?{_query_string({'price_min': '3000000', 'sort_by': 'nmck_desc', 'page_size': page_size})}",
        "review": f"/web/tenders?{_query_string({'decision': 'review', 'sort_by': 'published_desc', 'page_size': page_size})}",
        "promising": f"/web/tenders?{_query_string({'decision': 'go,strong_go', 'sort_by': 'published_desc', 'page_size': page_size})}",
        "all": "/web/tenders",
    }

    prev_qs = _query_string({**base_filters, "page": page - 1}) if page > 1 else ""
    next_qs = _query_string({**base_filters, "page": page + 1}) if page < total_pages else ""

    source_values = ["eis_site", "eis_browser", "manual", "eis_opendata", "fallback", "eis_public", "eis", "other"]
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

    tender_relevance: dict[str, dict[str, object]] = {}
    tender_decisions: dict[str, dict[str, object]] = {}
    for tender in tenders:
        decision = await get_decision_scoped(db, current_user.company_id, tender.id)
        rel = _extract_relevance(decision)
        if rel is not None:
            tender_relevance[str(tender.id)] = rel
        if decision is not None:
            tender_decisions[str(tender.id)] = {
                "recommendation": decision.recommendation,
                "decision_score": decision.decision_score,
                "recommendation_reason": decision.recommendation_reason,
                "priority_score": decision.priority_score,
                "priority_label": decision.priority_label,
                "priority_reason": decision.priority_reason,
            }

    company = await db.scalar(select(Company).where(Company.id == current_user.company_id))
    settings_payload = company.ingestion_settings if company and isinstance(company.ingestion_settings, dict) else {}
    eis_browser = settings_payload.get("eis_browser") if isinstance(settings_payload.get("eis_browser"), dict) else {}
    eis_browser_state = eis_browser.get("state") if isinstance(eis_browser.get("state"), dict) else {}

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
            decision_statuses=["none", "strong_go", "go", "review", "weak", "no_go", "unsure"],
            priority_labels=["urgent", "high", "medium", "low"],
            source_values=source_values,
            analysis_status_labels=ANALYSIS_STATUS_RU,
            decision_status_labels=DECISION_STATUS_RU,
            tender_status_labels=TENDER_STATUS_RU,
            source_labels=SOURCE_RU,
            priority_label_labels=PRIORITY_LABEL_RU,
            tender_relevance=tender_relevance,
            tender_decisions=tender_decisions,
            relevance_categories=RELEVANCE_CATEGORIES,
            quick_pick_urls=quick_pick_urls,
            ingest_result={
                "status": ingest_status,
                "message": ingest_message,
                "details": ingest_details,
            },
            eis_browser_status=eis_browser_state,
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
    valid_documents = [doc for doc in documents if _is_valid_tender_document_for_auto_analysis(doc)]
    package = await get_package_for_tender(db, company_id=current_user.company_id, tender_id=tender_id)
    finance = await get_finance_scoped(db, company_id=current_user.company_id, tender_id=tender_id)

    risk_score = _extract_risk_score(analysis, decision)
    relevance_meta = _extract_relevance(decision)
    risk_flags_top = _top_risk_flags(analysis)

    recommendation_value = decision.recommendation if decision else None
    analysis_status = analysis.status if analysis else "none"
    requirements = analysis.requirements if analysis and isinstance(analysis.requirements, dict) else {}
    has_requirements = bool(analysis and (requirements or analysis_status in {"ready", "approved"}))
    has_risk = risk_score is not None
    has_recommendation = recommendation_value in {"strong_go", "go", "review", "weak", "no_go", "unsure"}
    has_package = bool(package and package.files)
    happened_steps, documents_state = _what_happened_steps(
        valid_documents_count=len(valid_documents),
        documents_total_count=len(documents),
        has_requirements=has_requirements,
        has_recommendation=has_recommendation,
        has_package=has_package,
    )
    tender_flow_steps, action_availability, disabled_reasons, next_step = _build_detail_flow(
        documents_count=len(documents),
        valid_documents_count=len(valid_documents),
        has_source_link=bool(tender.source_url),
        analysis=analysis,
        risk_score=risk_score,
        finance=finance,
        decision=decision,
        package=package,
    )
    recommendation_factors = _recommendation_factors(
        recommendation=recommendation_value,
        recommendation_reason=decision.recommendation_reason if decision else None,
        risk_score=risk_score,
        relevance_meta=relevance_meta,
        documents_state=documents_state,
        has_requirements=has_requirements,
    )
    next_action_items = _next_action_items(
        documents_state=documents_state,
        recommendation=recommendation_value,
        has_source_link=bool(tender.source_url),
        can_package=action_availability.get("can_package", False),
        next_step=next_step,
    )
    pipeline_status = _pipeline_status_label(
        documents_state=documents_state,
        has_requirements=has_requirements,
        has_risk=has_risk,
        has_recommendation=has_recommendation,
        has_package=has_package,
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
                "decision_score": decision.decision_score if decision else None,
                "recommendation_reason": decision.recommendation_reason if decision else None,
                "priority_score": decision.priority_score if decision else None,
                "priority_label": decision.priority_label if decision else None,
                "priority_reason": decision.priority_reason if decision else None,
                "margin_pct": decision.expected_margin_pct if decision else None,
                "relevance_score": relevance_meta.get("score") if relevance_meta else None,
                "relevance_label": relevance_meta.get("label") if relevance_meta else None,
                "documents_count": len(valid_documents),
                "documents_total_count": len(documents),
                "package_generated": bool(package.files),
                "ingestion": _ingestion_status(company) if company else {},
            },
            relevance_meta=relevance_meta,
            finance_meta=decision.engine_meta.get("finance") if decision and isinstance(decision.engine_meta, dict) else None,
            tender_flow_steps=tender_flow_steps,
            action_availability=action_availability,
            disabled_reasons=disabled_reasons,
            next_step=next_step,
            happened_steps=happened_steps,
            documents_state=documents_state,
            recommendation_factors=recommendation_factors,
            next_action_items=next_action_items,
            mvp_summary={
                "pipeline_status": pipeline_status,
                "recommendation": recommendation_value,
                "amount": _format_tender_nmck_ru(
                    tender.nmck,
                    currency="RUB",
                    tender_id=tender.id,
                    external_id=tender.external_id,
                ),
                "next_action": next_action_items[0] if next_action_items else next_step or "-",
            },
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


@router.post("/tenders/{tender_id}/analyze-from-source")
async def web_analyze_from_source(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    guard_key = f"{current_user.company_id}:{tender_id}:analyze"
    retry_after = await enforce_source_fetch_rate_limit(guard_key, cooldown_seconds=10 * 60)
    if retry_after is not None:
        return _redirect_with_action(
            tender_id,
            "analyze_source",
            False,
            "Повторный запуск временно ограничен",
            f"Повторите через {retry_after} сек.",
        )

    data = await analyze_from_source(
        db,
        company_id=current_user.company_id,
        user_id=current_user.id,
        tender_id=tender_id,
    )
    status_ok = data.get("status") == "ok"
    steps = data.get("steps", {})
    fetch = steps.get("fetch_documents", {})
    extract = steps.get("extract", {})
    analysis_step = steps.get("analysis", {})
    relevance = steps.get("relevance", {})
    risk = steps.get("risk", {})
    engine = steps.get("recompute_engine", {})
    package = steps.get("package", {})

    details_lines = [
        f"downloading docs: {fetch.get('status', '-')}",
        f"Документы: {fetch.get('downloaded_count', 0)} загружено, найдено ссылок: {fetch.get('found_links_count', 0)}",
        f"parsing requirements: {extract.get('status', '-')}",
        f"building analysis: {analysis_step.get('status', '-')}",
        f"Релевантность: {relevance.get('relevance_score', '-')}/100 ({relevance.get('relevance_label', '-')})",
        f"Риск: {risk.get('risk_score', '-')}",
        f"Рекомендация: {engine.get('recommendation', '-')}",
        f"building package: {package.get('status', '-')}",
        f"Пакет: {package.get('message', '-')}",
    ]
    if package.get("generated_files_count") is not None:
        details_lines.append(f"Файлов в пакете: {package.get('generated_files_count')}")
    if data.get("status") == "ok":
        details_lines.append("done: yes")
    else:
        details_lines.append("done: partial")
    if fetch.get("source_status"):
        details_lines.append(f"source_status={fetch.get('source_status')}")
    if fetch.get("http_status"):
        details_lines.append(f"http_status={fetch.get('http_status')}")
    if fetch.get("message"):
        details_lines.append(f"Сообщение источника: {fetch.get('message')}")
    if data.get("next_step"):
        details_lines.append(f"Следующий шаг: {data.get('next_step')}")

    message = "Автопайплайн завершён" if status_ok else "Автопайплайн выполнен частично"
    if fetch.get("source_status") == "blocked":
        message = "ЕИС временно блокирует доступ к документам"
    return _redirect_with_action(
        tender_id,
        "analyze_source",
        status_ok,
        message,
        "\n".join(details_lines),
    )


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


@router.post("/tenders/{tender_id}/source-documents/import")
async def web_import_source_documents(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
):
    tender = await get_tender_by_id_scoped(db, current_user.company_id, tender_id)
    if tender is None:
        return _redirect_with_action(tender_id, "source_docs", False, "Тендер не найден")
    if not tender.source_url:
        return _redirect_with_action(tender_id, "source_docs", False, "У тендера отсутствует ссылка на источник")

    guard_key = f"{current_user.company_id}:{tender_id}"
    retry_after = await enforce_source_fetch_rate_limit(guard_key, cooldown_seconds=30 * 60)
    if retry_after is not None:
        return _redirect_with_action(
            tender_id,
            "source_docs",
            False,
            "Слишком частый запуск загрузки документов",
            f"Повторите через {retry_after} сек.",
        )

    try:
        existing_documents = await list_documents_for_tender(db, company_id=current_user.company_id, tender_id=tender_id)
    except DocumentScopedNotFoundError:
        return _redirect_with_action(tender_id, "source_docs", False, "Тендер не найден")

    existing_signatures = {
        (doc.file_name.lower(), doc.file_size or -1)
        for doc in existing_documents
        if str(doc.doc_type or "").lower() != "source_import"
    }
    try:
        result: SourceFetchResult = await fetch_source_documents(tender.source_url, max_docs=20)
    except SourceFetchError as exc:
        if exc.source_status == "blocked" and exc.http_status == 434:
            message = "ЕИС временно блокирует доступ к документам (HTTP 434)"
        else:
            message = str(exc)
        details = (
            f"Страниц проверено: {exc.attempted_pages}, ссылок найдено: {exc.found_links_count}, "
            f"source_status={exc.source_status}, http_status={exc.http_status or '-'}"
        )
        if exc.errors_sample:
            details = f"{details}. Ошибки: {'; '.join(exc.errors_sample[:3])}"
        return _redirect_with_action(tender_id, "source_docs", False, message, details)

    created = 0
    skipped_duplicates = 0
    for file_item in result.files:
        signature = (file_item.file_name.lower(), len(file_item.content))
        if signature in existing_signatures:
            skipped_duplicates += 1
            continue
        try:
            await create_document_from_bytes(
                db,
                company_id=current_user.company_id,
                tender_id=tender_id,
                uploaded_by=current_user.id,
                file_name=file_item.file_name,
                content=file_item.content,
                content_type=file_item.content_type,
                doc_type="tender_source",
            )
            existing_signatures.add(signature)
            created += 1
        except (DocumentScopedNotFoundError, DocumentStorageError):
            skipped_duplicates += 1

    if created == 0 and skipped_duplicates == 0:
        return _redirect_with_action(tender_id, "source_docs", False, "Документы не найдены на странице источника")

    details = (
        f"Страниц проверено: {result.attempted_pages}, ссылок найдено: {result.found_links_count}, "
        f"скачано: {created}, дубликатов: {skipped_duplicates}"
    )
    if result.errors_sample:
        details = f"{details}. Ошибки: {'; '.join(result.errors_sample[:3])}"

    return _redirect_with_action(
        tender_id,
        "source_docs",
        True,
        f"Импорт документов завершен: добавлено {created}, пропущено {skipped_duplicates}",
        details,
    )


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
