"""Capability-protected, read-only checkout; never confirms or expires payments."""
import re
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from app.db.session import get_db
from app.models import Deposit
from app.services.payment_qr import image_png
from app.services.deposit_lifecycle import deposit_is_expired

router=APIRouter(include_in_schema=False)
HEADERS={'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer', 'X-Robots-Tag':'noindex, nofollow', 'X-Content-Type-Options':'nosniff'}

async def find_payment(db,token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):raise HTTPException(404,'Заявка не найдена')
    dep=await db.scalar(select(Deposit).where(Deposit.qr_token==token,Deposit.method=='sbp_qr'))
    if not dep or not dep.qr_snapshot:raise HTTPException(404,'Заявка не найдена')
    return dep

@router.get('/pay/qr/{token}',name='qr_checkout')
async def checkout(token: str, request: Request, db: AsyncSession=Depends(get_db)):
    from app.web.routes import templates
    dep=await find_payment(db,token)
    active=dep.status in {'created','pending'} and not deposit_is_expired(dep)
    return templates.TemplateResponse(request=request,name='tradespace/payment_qr.html',context={
        'payment':dep,'active':active,'qr':dep.qr_snapshot,
        'state':'Ожидает оплаты' if active else ('Оплачено' if dep.status=='paid' else 'Оплата недоступна'),
    },headers=HEADERS)

@router.get('/pay/qr/{token}/image',name='qr_payment_image')
async def payment_image(token: str, db: AsyncSession=Depends(get_db)):
    dep=await find_payment(db,token)
    if dep.status not in {'created','pending'} or deposit_is_expired(dep):raise HTTPException(410,'Срок оплаты завершён',headers=HEADERS)
    png=await run_in_threadpool(image_png,dep.qr_snapshot['payment_url'])
    return Response(png,media_type='image/png',headers=HEADERS)
