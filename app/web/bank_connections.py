"""Trader draft forms using the existing realm/session/CSRF middleware."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.russian_banks import find_bank, find_bank_by_code
from app.db.session import get_db
from app.models import Requisite, TraderBankConnection
from app.services import bank_connections as drafts

router = APIRouter(tags=["bank-connection-drafts"])


async def render_drafts(request, user, db, *, values=None, error=None, status_code=200):
    from app.presentation.tradespace.routes import _base_context
    from app.web import routes as web

    drafts.require_trader(user)
    context = _base_context(request, user, active_section="bank-integrations")
    rows = (await db.scalars(select(TraderBankConnection).where(
        TraderBankConnection.trader_id == user.id,
    ).order_by(TraderBankConnection.created_at.desc(), TraderBankConnection.id))).all()
    requisites = (await db.scalars(select(Requisite).where(
        Requisite.trader_id == user.id, Requisite.method == "sbp_qr",
        Requisite.is_archived.is_(False), Requisite.status != "deleted",
    ).order_by(Requisite.created_at, Requisite.id))).all()
    choices = []
    for req in requisites:
        bank = find_bank(req.bank_code or req.bank_name)
        if bank and bank.enabled and bank.code in drafts.BANK_CODES:
            choices.append({"id": str(req.id), "bank_code": bank.code,
                            "label": f"{bank.display_name} · QR NSPK · {str(req.id)[:8]}"})
    linked = {item["id"]: item["label"] for item in choices}
    if values is None and request.query_params.get("edit"):
        row = await drafts.owned_draft(db, user, request.query_params["edit"])
        values = {"id": str(row.id), "bank_code": row.bank_code, "name": row.name,
                  "requisite_id": str(row.requisite_id) if row.requisite_id else ""}
    if values is None and request.query_params.get("new") == "1":
        values = {"id": "", "bank_code": "alfabank", "name": "", "requisite_id": ""}
    notices = {"saved": "Черновик сохранён.", "deleted": "Черновик удалён."}
    return web.templates.TemplateResponse(request=request,
        name="tradespace/trader/bank_connections.html", status_code=status_code,
        context={**context, "page_title": "Интеграция с банком", "drafts": rows,
                 "banks": [find_bank_by_code(code) for code in drafts.BANK_CODES],
                 "bank_labels": {code: find_bank_by_code(code).display_name for code in drafts.BANK_CODES},
                 "requisite_choices": choices, "linked_labels": linked, "values": values,
                 "draft_error": error, "notice": notices.get(request.query_params.get("result"))},
        headers={"Cache-Control": "no-store, max-age=0"})


async def _user(request, db):
    from app.presentation.tradespace.routes import _authenticated_user
    user, response = await _authenticated_user(request, db)
    if not response:
        drafts.require_trader(user)
    return user, response


async def _save(request, db, bank_code, name, requisite_id, draft_id=None):
    from app.web.routes import client_ip
    user, response = await _user(request, db)
    if response:
        return response
    try:
        await drafts.save_draft(db, user, bank_code=bank_code, name=name,
                                requisite_id=requisite_id, draft_id=draft_id, ip=client_ip(request))
        await db.commit()
    except drafts.DraftValidationError as exc:
        # Validation happens before any mutation; don't expire the authenticated user on render.
        return await render_drafts(request, user, db, values={"id": draft_id or "",
            "bank_code": bank_code, "name": name, "requisite_id": requisite_id},
            error=str(exc), status_code=422)
    return RedirectResponse("/cabinet/tradespace/bank-integrations?result=saved", status_code=303)


@router.post("/cabinet/bank-integrations/create", name="bank_draft_create")
async def create(request: Request, bank_code: str = Form(""), name: str = Form(""),
                 requisite_id: str = Form(""), db: AsyncSession = Depends(get_db)):
    return await _save(request, db, bank_code, name, requisite_id)


@router.post("/cabinet/bank-integrations/{draft_id}/edit", name="bank_draft_edit")
async def edit(request: Request, draft_id: str, bank_code: str = Form(""), name: str = Form(""),
               requisite_id: str = Form(""), db: AsyncSession = Depends(get_db)):
    return await _save(request, db, bank_code, name, requisite_id, draft_id)


@router.post("/cabinet/bank-integrations/{draft_id}/delete", name="bank_draft_delete")
async def delete(request: Request, draft_id: str, db: AsyncSession = Depends(get_db)):
    from app.web.routes import client_ip
    user, response = await _user(request, db)
    if response:
        return response
    await drafts.delete_draft(db, user, draft_id, ip=client_ip(request))
    await db.commit()
    return RedirectResponse("/cabinet/tradespace/bank-integrations?result=deleted", status_code=303)
