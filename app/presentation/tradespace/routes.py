from __future__ import annotations

import os
from uuid import UUID

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.session import realm_cabinet_path, realm_login_path, request_auth_realm
from app.db.session import get_db
from app.presentation.tradespace.merchant import load_merchant_page
from app.presentation.tradespace.navigation import mobile_navigation_for_role, mobile_overflow_for_role, navigation_for_role, profile_for_role, section_for_role
from app.presentation.tradespace.trader import load_trader_page
from app.presentation.tradespace.teamlead import load_teamlead_page
from app.presentation.tradespace.staff import load_staff_page
from app.presentation.tradespace.staff.access import STAFF_ROLES
from app.presentation.tradespace.wallet import load_wallet


router = APIRouter(tags=["tradespace-presentation"])
TRADER_ROLES = frozenset({"operator", "trader"})

_TRADER_ALIASES = {
    "deposits": "work",
    "appeals": "disputes",
    "balance": "finance",
    "account": "security",
}
_MERCHANT_ALIASES = {
    "deposits": "operations",
    "payouts": "settlements",
    "wallet": "finance",
    "rolling": "finance",
    "Пополнения": "finance",
    "appeals": "disputes",
    "merchants": "integration",
    "webhook": "integration",
    "account": "security",
}
_TRADER_TEMPLATES = {
    "work": "tradespace/trader/workbench.html",
    "history": "tradespace/trader/history.html",
    "requisites": "tradespace/trader/requisites.html",
    "finance": "tradespace/trader/finance.html",
    "disputes": "tradespace/trader/disputes.html",
    "analytics": "tradespace/trader/analytics.html",
    "notifications": "tradespace/trader/workbench.html",
}
_MERCHANT_TEMPLATES = {
    "overview": "tradespace/merchant/overview.html",
    "operations": "tradespace/merchant/operations.html",
    "finance": "tradespace/merchant/finance.html",
    "settlements": "tradespace/merchant/settlements.html",
    "integration": "tradespace/merchant/integration.html",
    "disputes": "tradespace/merchant/disputes.html",
    "analytics": "tradespace/merchant/analytics.html",
    "notifications": "tradespace/merchant/overview.html",
}


def _tradespace_enabled(request: Request) -> bool:
    # Disabling the UI exposes a neutral product screen, never another cabinet.
    return os.getenv("TRADESPACE_UI_ENABLED", "true").strip().lower() not in {
        "0", "false", "no", "off"
    }


def _base_context(request: Request, user, *, active_section: str):
    from app.web import routes as legacy

    realm = request_auth_realm(request)
    profile = profile_for_role(user.role)
    cabinet_base = realm_cabinet_path(realm) if realm else "/cabinet"
    return {
        "request": request,
        "product_name": "TradeSpace",
        "user": user,
        "role": user.role,
        "realm": realm,
        "profile": profile,
        "navigation": navigation_for_role(user.role),
        "mobile_navigation": mobile_navigation_for_role(user.role),
        "mobile_overflow": mobile_overflow_for_role(user.role),
        "active_section": active_section,
        "cabinet_base": cabinet_base,
        "logout_url": f"/{realm}/logout" if realm else "/logout",
        "security_url": f"{cabinet_base}/tradespace/security",
        "ui_message": legacy.resolve_ui_message(request.query_params),
        "session_view": legacy.session_view_fingerprint(request.session),
    }


async def _authenticated_user(request: Request, db: AsyncSession):
    from app.web import routes as legacy

    if await legacy._session_identity_changed(request):
        return None, legacy._session_changed_page(request)
    user = await legacy.get_current_web_user(request, db)
    if user:
        if not _tradespace_enabled(request):
            return None, legacy.templates.TemplateResponse(
                request=request, name="tradespace/pages/maintenance.html",
                context={**_base_context(request, user, active_section=""),
                         "page_title": "Кабинет временно недоступен"},
                headers={"Cache-Control": "no-store, max-age=0"},
            )
        return user, None
    realm = request_auth_realm(request)
    target = realm_login_path(realm) if realm else "/login"
    return None, RedirectResponse(target, status_code=303)


def _render_security(request: Request, user, context: dict):
    from app.web import routes as legacy

    context = {
        **context,
        "active_section": "security",
        "page_title": "Аккаунт",
        "twofa_required": legacy.staff_2fa_setup_required(user),
        "security_qr": (
            legacy.totp_qr_data_uri(user.email, legacy.decrypt_secret(user.twofa_secret))
            if user.twofa_secret else None
        ),
    }
    response = legacy.templates.TemplateResponse(
        request=request,
        name="tradespace/pages/security.html",
        context=context,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def _forbidden(request: Request, user, context: dict, section_slug: str, *, status_code: int = 403):
    from app.web import routes as legacy

    return legacy.templates.TemplateResponse(
        request=request,
        name="tradespace/states/forbidden.html",
        context={
            **context,
            "requested_role": user.role,
            "requested_section": section_slug,
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


async def _render_trader(
    request: Request,
    user,
    db: AsyncSession,
    *,
    section_slug: str,
    selected_operation_id: UUID | None = None,
):
    from app.web import routes as legacy

    section = section_for_role(user.role, section_slug)
    context = _base_context(request, user, active_section=section_slug)
    if section_slug == "security":
        return _render_security(request, user, context)
    if not section or section_slug not in _TRADER_TEMPLATES:
        return _forbidden(request, user, context, section_slug)
    data = await load_trader_page(
        db,
        user,
        cabinet_base=context["cabinet_base"],
        query_params=request.query_params,
        section=section_slug,
        selected_operation_id=selected_operation_id,
    )
    if selected_operation_id is not None and data.selected_operation is None:
        return _forbidden(request, user, context, "operation", status_code=404)
    page_title = "Требуют внимания" if section_slug == "notifications" else section.label
    response = legacy.templates.TemplateResponse(
        request=request,
        name=_TRADER_TEMPLATES[section_slug],
        context={
            **context,
            "page_title": page_title,
            "section": section,
            "trader": data,
            "attention_only": section_slug == "notifications",
            "platform_wallet_view": await load_wallet(request,db,user,context["cabinet_base"]) if section_slug == "finance" else None,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


async def _render_merchant(
    request: Request,
    user,
    db: AsyncSession,
    *,
    section_slug: str,
    selected_operation_id: UUID | None = None,
):
    from app.web import routes as legacy

    section = section_for_role(user.role, section_slug)
    context = _base_context(request, user, active_section=section_slug)
    if section_slug == "security":
        return _render_security(request, user, context)
    if not section or section_slug not in _MERCHANT_TEMPLATES:
        return _forbidden(request, user, context, section_slug)
    data = await load_merchant_page(
        db,
        user,
        cabinet_base=context["cabinet_base"],
        query_params=request.query_params,
        section=section_slug,
        selected_operation_id=selected_operation_id,
    )
    if data is None:
        return legacy.templates.TemplateResponse(
            request=request,
            name="tradespace/pages/shell.html",
            context={
                **context,
                "page_title": section.label,
                "section": section,
                "is_landing": section_slug == "overview",
            },
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    if selected_operation_id is not None and data.selected_operation is None:
        return _forbidden(request, user, context, "operation", status_code=404)
    template = (
        "tradespace/merchant/operation.html"
        if selected_operation_id is not None
        else _MERCHANT_TEMPLATES[section_slug]
    )
    page_title = "Требует внимания" if section_slug == "notifications" else section.label
    response = legacy.templates.TemplateResponse(
        request=request,
        name=template,
        context={
            **context,
            "page_title": page_title,
            "section": section,
            "merchant": data,
            "attention_only": section_slug == "notifications",
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


_TEAMLEAD_ALIASES = {
    "teamlead": "settlements", "traders": "team", "operations": "accruals",
    "economics": "accruals", "account": "security", "безопасность": "security",
}


async def _render_teamlead(request, user, db, *, section_slug):
    from app.web import routes as legacy

    context = _base_context(request, user, active_section=section_slug)
    if legacy.staff_2fa_setup_required(user) or section_slug == "security":
        return _render_security(request, user, context)
    section = section_for_role(user.role, section_slug)
    if section_slug not in {"overview", "team", "accruals", "settlements", "notifications"} or not section:
        return _forbidden(request, user, context, section_slug)
    data = await load_teamlead_page(
        db, user, cabinet_base=context["cabinet_base"],
        query_params=request.query_params, section=section_slug,
    )
    if data.member_requested and data.selected_member is None:
        return _forbidden(request, user, context, "member", status_code=404)
    template = "overview" if section_slug == "notifications" else section_slug
    return legacy.templates.TemplateResponse(
        request=request, name=f"tradespace/teamlead/{template}.html",
        context={**context, "page_title": section.label, "section": section, "lead": data},
        headers={"Cache-Control": "no-store, max-age=0"},
    )


# Return destinations used by the frozen commands are mapped to presentation tabs.
_STAFF_RETURNS = {
    "deposits": ("operations", "deposits"), "payouts": ("finance", "settlements"),
    "appeals": ("operations", "appeals"), "disputes": ("operations", "appeals"),
    "traders": ("network", "traders"), "merchants": ("network", "merchants"),
    "requisites": ("network", "requisites"), "teamlead": ("network", "teamleads"),
    "aggregators": ("network", "aggregators"), "participants": ("network", "traders"),
    "rolling": ("finance", "rolling"), "wallet": ("finance", "wallet"),
    "fees": ("finance", "fees"), "tariffs": ("finance", "fees"),
    "webhook": ("integrations", "webhooks"), "audit": ("control", "audit"),
    "users": ("control", "users"), "create": ("network", "traders"),
    "antiscam": ("control", "antiscam"), "ai-office": ("control", "ai"),
    "risk": ("control", "risk"), "history": ("operations", "appeals"),
    "income": ("finance", "income"), "overview": ("center", ""),
}

async def _render_staff(request, user, db, *, section_slug, selected_operation_id=None):
    from app.web import routes as legacy
    from app.presentation.tradespace.staff.view_models import Page, status_map
    from app.presentation.tradespace.staff.forms import METHODS

    original_section = section_slug
    section_slug, default_tab = _STAFF_RETURNS.get(section_slug, (section_slug, ""))
    context = _base_context(request, user, active_section=section_slug)
    if legacy.staff_2fa_setup_required(user) or section_slug == "security":
        return _render_security(request, user, context)
    section = section_for_role(user.role, section_slug)
    if not section:
        return _forbidden(request, user, context, original_section)
    params = dict(request.query_params)
    if default_tab and not params.get("tab"):
        params["tab"] = default_tab
    try:
        data = await load_staff_page(db, user, cabinet_base=context["cabinet_base"],
            query_params=params, section=section_slug, selected_operation_id=selected_operation_id)
        if user.role == "superadmin" and data.tab == "traders":
            from urllib.parse import quote_plus
            for row in data.rows:
                token = legacy.issue_preview_context_token(
                    request.session, preview_role="trader", subject_id=row["id"],
                )
                if token:
                    row["preview_url"] = context["cabinet_base"] + "/trader?preview_context=" + quote_plus(token)
        status_code = 200
    except HTTPException as exc:
        if exc.status_code in {403,404}:
            return _forbidden(request, user, context, original_section, status_code=exc.status_code)
        if exc.status_code != 400: raise
        data = Page(section_slug, title="Проверьте параметры поиска", note=str(exc.detail))
        data.links = [dict(title="Сбросить фильтры", text="Вернуться к разделу", href=context["cabinet_base"]+"/tradespace/"+section_slug)]
        status_code = 400
    return legacy.templates.TemplateResponse(request=request,
        name="tradespace/staff/page.html", context={**context, "page_title":data.title,
            "section":section, "staff":data, "staff_statuses":status_map(data.tab),"staff_methods":METHODS,
            "aggregator_secret_pending": user.role in {"admin","superadmin"} and bool(request.session.get(legacy.AGGREGATOR_SECRET_FLASH_SESSION_KEY)),
            "platform_wallet_view": await load_wallet(request,db,user,context["cabinet_base"]) if data.tab == "wallet" else None},
        status_code=status_code, headers={"Cache-Control":"no-store, max-age=0"})


@router.get("/cabinet", response_class=HTMLResponse, name="tradespace_cabinet")
async def tradespace_cabinet(request: Request, db: AsyncSession = Depends(get_db)):
    user, response = await _authenticated_user(request, db)
    if response:
        return response
    from app.web import routes as legacy

    legacy._clear_preview_context(request)
    profile = profile_for_role(user.role)
    requested = str(request.query_params.get("section") or "").strip()
    if user.role in TRADER_ROLES:
        requested = _TRADER_ALIASES.get(requested.lower(), requested.lower())
        if requested == "work" and request.query_params.get("view") == "history":
            requested = "history"
    elif user.role == "merchant":
        requested = _MERCHANT_ALIASES.get(requested, _MERCHANT_ALIASES.get(requested.lower(), requested.lower()))
    if user.role == "teamlead":
        requested = _TEAMLEAD_ALIASES.get(requested.lower(), requested.lower())
    if legacy.staff_2fa_setup_required(user):
        return _render_security(request, user, _base_context(request, user, active_section="security"))
    if user.role in STAFF_ROLES:
        return await _render_staff(request, user, db, section_slug=requested or profile.landing)
    active = requested if section_for_role(user.role, requested) else profile.landing
    if user.role in TRADER_ROLES:
        return await _render_trader(request, user, db, section_slug=active)
    if user.role == "merchant":
        return await _render_merchant(request, user, db, section_slug=active)
    if user.role == "teamlead":
        return await _render_teamlead(request, user, db, section_slug=active)
    if active == "security":
        return _render_security(request, user, _base_context(request, user, active_section="security"))
    section = section_for_role(user.role, active)
    context = {
        **_base_context(request, user, active_section=active),
        "page_title": section.label if section else profile.title,
        "section": section,
        "is_landing": active == profile.landing,
    }
    return legacy.templates.TemplateResponse(
        request=request,
        name="tradespace/pages/shell.html",
        context=context,
    )


@router.get("/cabinet/tradespace", name="tradespace_root")
async def tradespace_root(request: Request):
    realm = request_auth_realm(request)
    target = realm_cabinet_path(realm) if realm else "/cabinet"
    return RedirectResponse(target, status_code=303)


async def trader_preview(request, user, subject, db):
    """Render the existing signed, server-scoped preview in the product shell."""
    from app.web import routes as commands
    _, response = await _authenticated_user(request, db)
    if response:
        return response
    if user.role != "superadmin" or commands.staff_2fa_setup_required(user):
        return _forbidden(request, user, _base_context(request,user,active_section="network"), "preview")
    filters = commands.normalized_deposit_filters(
        request.query_params, valid_bank_codes=(bank.code for bank in commands.get_enabled_banks()),
    )
    data = await commands._load_trader_deposit_view(db, subject_trader_id=subject.id, filters=filters)
    return commands.templates.TemplateResponse(
        request=request, name="tradespace/staff/trader_preview.html",
        context={**_base_context(request,user,active_section="network"),
                 "page_title":"Просмотр трейдера", "subject":subject, "preview":data, "filters":filters},
        headers={"Cache-Control":"no-store, max-age=0"},
    )


@router.get("/cabinet/tradespace/secrets/aggregator", name="tradespace_aggregator_secret")
async def tradespace_aggregator_secret(request: Request, db: AsyncSession = Depends(get_db)):
    from app.web import routes as commands
    user, response = await _authenticated_user(request, db)
    if response:
        return response
    context = _base_context(request,user,active_section="network")
    if user.role not in {"admin","superadmin"}:
        return _forbidden(request,user,context,"credentials")
    if commands.staff_2fa_setup_required(user):
        return _render_security(request,user,context)
    credentials = await commands._consume_aggregator_secret_flash(request,user)
    return commands.templates.TemplateResponse(
        request=request, name="tradespace/auth/aggregator_secret.html",
        context={**context,"page_title":"Данные доступа агрегатора","credentials":credentials},
        headers={"Cache-Control":"no-store, max-age=0","Pragma":"no-cache","Referrer-Policy":"no-referrer"},
    )


@router.get(
    "/cabinet/tradespace/operations/{operation_id}",
    response_class=HTMLResponse,
    name="tradespace_trader_operation",
)
async def tradespace_trader_operation(
    request: Request,
    operation_id: str,
    db: AsyncSession = Depends(get_db),
):
    user, response = await _authenticated_user(request, db)
    if response:
        return response
    if user.role in STAFF_ROLES:
        try:
            parsed = UUID(operation_id)
        except ValueError:
            return _forbidden(request, user, _base_context(request,user,active_section="operations"), "operation", status_code=404)
        return await _render_staff(request,user,db,section_slug="operations",selected_operation_id=parsed)
    active = "operations" if user.role == "merchant" else "work"
    context = _base_context(request, user, active_section=active)
    if user.role not in TRADER_ROLES and user.role != "merchant":
        return _forbidden(request, user, context, "operation")
    try:
        parsed = UUID(operation_id)
    except ValueError:
        return _forbidden(request, user, context, "operation", status_code=404)
    if user.role == "merchant":
        return await _render_merchant(
            request, user, db, section_slug="operations", selected_operation_id=parsed
        )
    return await _render_trader(
        request,
        user,
        db,
        section_slug="work",
        selected_operation_id=parsed,
    )


@router.get(
    "/cabinet/tradespace/{section_slug}",
    response_class=HTMLResponse,
    name="tradespace_section",
)
async def tradespace_section(
    request: Request,
    section_slug: str,
    db: AsyncSession = Depends(get_db),
):
    user, response = await _authenticated_user(request, db)
    if response:
        return response
    if user.role in STAFF_ROLES:
        return await _render_staff(request,user,db,section_slug=section_slug)
    if user.role == "teamlead":
        section_slug = _TEAMLEAD_ALIASES.get(section_slug.lower(), section_slug)
        return await _render_teamlead(request, user, db, section_slug=section_slug)
    context = _base_context(request, user, active_section=section_slug)
    if section_slug == "security":
        return _render_security(request, user, context)
    section = section_for_role(user.role, section_slug)
    if section is None:
        return _forbidden(request, user, context, section_slug)
    if user.role in TRADER_ROLES:
        return await _render_trader(request, user, db, section_slug=section_slug)
    if user.role == "merchant":
        return await _render_merchant(request, user, db, section_slug=section_slug)
    from app.web import routes as legacy

    return legacy.templates.TemplateResponse(
        request=request,
        name="tradespace/pages/placeholder.html",
        context={
            **context,
            "page_title": section.label,
            "section": section,
        },
    )


@router.get("/cabinet/legacy", name="tradespace_legacy_fallback")
async def tradespace_legacy_fallback(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user, response = await _authenticated_user(request, db)
    if response:
        return response
    realm = request_auth_realm(request)
    base = realm_cabinet_path(realm) if realm else "/cabinet"
    return RedirectResponse(base, status_code=303)
