"""F11-F13 assertions against owned domain records, not template appearance alone."""
import asyncio
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Deposit,User,Requisite
from app.presentation.tradespace.trader.view_models import load_ledger,load_operation_detail
from app.presentation.tradespace.financial_copy import financial_description
from tests.test_acceptance_financial_postgres import setup,request,finish,replay_client_per_event_loop
from scripts.qa_seed import assert_qa_environment
import pytest


def test_finance_descriptions_and_owner_typed_links_current_vs_initial_reserve():
    async def run():
        engine,key,secret,mid,tid=await setup()
        try:
            ext=uuid.uuid4().hex;r=await request(key,secret,ext);assert r.status_code==200;oid=uuid.UUID(r.json()['id'])
            async with AsyncSession(engine) as db:
                trader=await db.get(User,tid);op=await load_operation_detail(db,trader,cabinet_base='/trader/cabinet',operation_id=oid)
                assert op.current_hold=='950.00' and op.trader_hold=='950.00'
                rows=await load_ledger(db,tid);assert len(rows)==1
                assert rows[0].reference_type=='requisite' and not rows[0].operation_id
                assert rows[0].description=='Резерв для назначения операции'
            assert (await request(key,secret,ext,cancel=True)).status_code==200
            async with AsyncSession(engine) as db:
                trader=await db.get(User,tid);op=await load_operation_detail(db,trader,cabinet_base='/trader/cabinet',operation_id=oid)
                assert op.current_hold=='0.00' and op.trader_hold=='950.00' and op.reserve_label=='Резерв освобождён'
                rows=await load_ledger(db,tid)
                release=next(row for row in rows if row.entry_type=='release_hold')
                assert release.operation_id==str(oid) and release.reference_type=='deposit'
                assert release.description=='Резерв операции освобождён'
                foreign=await load_ledger(db,uuid.uuid4());assert not foreign
        finally:await finish(engine)
    asyncio.run(run())


def test_system_fallback_preserves_user_comments_and_blocks_nonlocal_seed():
    assert financial_description('unknown_machine_event')=='Финансовое движение'
    assert financial_description('Customer requested correction',user_comment=True)=='Customer requested correction'
    assert financial_description('Комментарий пользователя',user_comment=True)=='Комментарий пользователя'
    for values in [dict(ENV='production',DATABASE_URL='postgresql://x@127.0.0.1:55432/tradespace_dev'),dict(ENV='local',DATABASE_URL='postgresql://x@remote:55432/tradespace_dev'),dict(ENV='local',DATABASE_URL='postgresql://x@127.0.0.1:55432/production')]:
        with pytest.raises(RuntimeError):assert_qa_environment(values)


def test_staff_ledger_projection_preserves_manual_comments_and_translates_system_reason():
    from app.models import TraderLedgerEntry,TeamLeadLedgerEntry
    from app.presentation.tradespace.staff.view_models import panel
    manual=TraderLedgerEntry(entry_type='balance_adjustment',description='Customer requested correction')
    system=TraderLedgerEntry(entry_type='hold',description='deposit requisite reserved')
    result=panel('Ledger',[manual,system],('description',))
    assert result['rows'][0][0]['value']=='Customer requested correction'
    assert result['rows'][1][0]['value']=='Резерв для назначения операции'
    row=TeamLeadLedgerEntry(entry_type='accrual',reason='TeamLead commission from paid deposit gross')
    assert panel('Ledger',[row],('reason',))['rows'][0][0]['value']=='Комиссия за операцию привлечённого трейдера'


def test_rolling_and_staff_financial_codes_are_display_only_localized():
    from app.presentation.tradespace.staff.view_models import cell
    assert financial_description('merchant confirmed Rolling transfer receipt')=='Подтверждено получение Rolling'
    assert financial_description('Rolling pending exposure released: merchant_cancelled')=='Обязательство Rolling освобождено: Отменено мерчантом'
    assert financial_description('available_credit: Customer correction',user_comment=True)=='Зачисление: Customer correction'
    assert cell('entry_type','release_hold')['value']=='Освобождение резерва'
    assert cell('entry_type','future_internal_event')['value']=='Финансовое движение'
    assert cell('source_type','merchant_referral')['value']=='За мерчанта'
