"""Owner-declared static NSPK QR: actual isolated PostgreSQL, no bank HTTP."""
import asyncio
from decimal import Decimal
from hashlib import sha256
import uuid
import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app import models as m
from app.core.security import encrypt_secret, reveal_text
from app.services.payment_qr import inspect_payload, validate_payload, decode_image, image_png, QrValidationError
from tests.test_qr1 import PAYLOAD, prepare, create, result
from tests.test_appeal_confirmation_rule_postgres import isolated
from tests.test_integration_modes_postgres import client
from tests.tradespace.test_wave4_teamlead import login, csrf

SHORT = PAYLOAD.split('?')[0]

@pytest.mark.parametrize('prefix', ['AS', 'BS'])
def test_short_url_uses_selection_not_prefix_or_bank_attestation(prefix):
    value = 'https://qr.nspk.ru/' + prefix + '0' * 30
    inspected = inspect_payload(value)
    assert inspected.qr_type == 'unknown' and inspected.type_source == 'undetermined'
    with pytest.raises(QrValidationError):
        validate_payload(value)
    qr = decode_image(image_png(value), declared_static=True)
    assert qr.payload == value and qr.qr_type == 'static_open_amount'
    assert qr.type_source == 'trader_selection' and qr.source == 'uploaded_static'
    assert validate_payload(value, declared_static=True) == qr


def test_explicit_static_and_missing_type_keep_provenance_and_currency_checks():
    qr = decode_image(image_png(PAYLOAD), declared_static=True)
    assert qr.type_source == 'payload' and qr.payload == PAYLOAD
    unknown = PAYLOAD.replace('type=01&', '')
    assert validate_payload(unknown, declared_static=True).type_source == 'trader_selection'
    for invalid in (unknown+'&cur=USD', unknown+'&sum=1', unknown.replace('bank=100000000111', 'bank=1')):
        with pytest.raises(QrValidationError):
            validate_payload(invalid, declared_static=True)


@pytest.mark.parametrize('payload,code', [
    (PAYLOAD.replace('type=01', 'type=02')+'&sum=10000', 'qr_dynamic'),
    (PAYLOAD.replace('type=01', 'type=03'), 'qr_unsupported'),
    (PAYLOAD+'&sum=10000', 'qr_fixed_amount'),
    ('https://example.test/qr', 'qr_unsupported'),
    (SHORT.replace('https:', 'http:'), 'qr_unsupported'),
    (SHORT.replace('qr.nspk.ru', 'qr.nspk.ru.evil.test'), 'qr_unsupported'),
    (SHORT+'?type=01&bank=100000000111&crc=ABCD&cur=USD', 'qr_unsupported'),
])
def test_selected_static_cannot_override_explicit_contradictions(payload, code):
    with pytest.raises(QrValidationError) as error:
        decode_image(image_png(payload), declared_static=True)
    assert error.value.code == code


def test_selected_static_does_not_allow_unreadable_image():
    with pytest.raises(QrValidationError) as error:
        decode_image(b'not an image', declared_static=True)
    assert error.value.code == 'qr_unreadable'
    assert 'Не удалось прочитать' in str(error.value)


@pytest.mark.parametrize('payload,source', [(SHORT, 'trader_selection'), (PAYLOAD, 'payload')])
def test_create_active_reload_edit_and_persisted_declaration(monkeypatch, payload, source):
    async def run():
        async with isolated(monkeypatch) as (engine, merchant, users, keys), client() as c:
            await login(c, users['trader'], realm='trader')
            page = await c.get('/trader/cabinet/tradespace/requisites')
            fields = {'csrf_token': csrf(page), 'method': 'sbp_qr', 'qr_mode': 'static', 'bank_code': 'alfabank',
                      'full_name': 'Synthetic declared static QR', 'status': 'active'}
            files = {'qr_image': ('static.png', image_png(payload), 'image/png')}
            assert (await c.post('/trader/cabinet/requisites/qr-preview', files=files)).status_code == 403
            preview = await c.post('/trader/cabinet/requisites/qr-preview', data=fields, files=files)
            assert preview.status_code == 200 and preview.json()['qr_type_source'] == source
            assert preview.json()['qr_type'] == 'static_open_amount'
            async with AsyncSession(engine) as db:
                assert await db.scalar(select(func.count()).select_from(m.Requisite)) == 1
                assert await db.scalar(select(func.count()).select_from(m.Deposit)) == 0
            created = await c.post('/trader/cabinet/requisites/create', data=fields, files=files)
            assert created.status_code == 303 and 'ui_error' not in created.headers['location']
            async with AsyncSession(engine) as db:
                req = await db.scalar(select(m.Requisite).where(m.Requisite.full_name == fields['full_name']))
                assert req and req.enabled and req.status == 'active'
                assert reveal_text(req.value_encrypted) == payload
                rid = str(req.id)
                log = await db.scalar(select(m.AuditLog).where(m.AuditLog.target_id == rid, m.AuditLog.action == 'requisite_created'))
                assert log.actor_id == users['trader'].id
                assert log.details['qr_type_source'] == source and log.details['qr_mode'] == 'static'
                assert log.details['qr_payload_sha256'] == sha256(payload.encode()).hexdigest()
                assert payload not in str(log.details)
            # A separate login renders the persisted active state, not JS optimism.
            async with client() as fresh:
                await login(fresh, users['trader'], realm='trader')
                rendered = await fresh.get('/trader/cabinet/tradespace/requisites')
                assert 'Сохраните неактивный черновик' not in rendered.text
                assert 'короткая ссылка требует метаданных банка' not in rendered.text
                assert 'Активен' in rendered.text and fields['full_name'] in rendered.text
                fields['csrf_token'] = csrf(rendered)
                edited = await fresh.post('/trader/cabinet/requisites/'+rid+'/edit', data={**fields, 'full_name': 'Edited static QR'})
                assert edited.status_code == 303 and 'ui_error' not in edited.headers['location']
            async with AsyncSession(engine) as db:
                req = await db.get(m.Requisite, rid)
                assert req.enabled and req.status == 'active' and reveal_text(req.value_encrypted) == payload
                log = await db.scalar(select(m.AuditLog).where(m.AuditLog.target_id == rid, m.AuditLog.action == 'requisite_updated'))
                assert log.details['qr_type_source'] == source
                assert await db.scalar(select(func.count()).select_from(m.Deposit)) == 0
                assert (await db.get(m.User, users['trader'].id)).trader_hold == 0
    asyncio.run(run())


def test_existing_short_qr_enable_without_upload_no_bulk_activation_and_ownership(monkeypatch):
    async def run():
        async with isolated(monkeypatch) as (engine, merchant, users, keys), client() as c:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                req = await db.scalar(select(m.Requisite))
                req.method='sbp_qr';req.qr_identity=SHORT.rsplit('/',1)[1];req.value_encrypted=encrypt_secret(SHORT)
                req.enabled=False;req.status='review';rid=str(req.id)
                other = m.Requisite(trader_id=users['trader'].id,owner_name='Intentionally disabled',method='sbp_qr',
                    qr_identity=req.qr_identity,value_encrypted=encrypt_secret(SHORT),bank_code='alfabank',enabled=False,status='disabled')
                db.add(other)
                foreign = m.User(email=uuid.uuid4().hex+'@example.test',role='trader',password_hash=users['trader'].password_hash)
                db.add(foreign);await db.commit();other_id=other.id
            await login(c,users['trader'],realm='trader')
            page=await c.get('/trader/cabinet/tradespace/requisites')
            assert 'тип указан трейдером' in page.text
            async with AsyncSession(engine) as db:
                assert not (await db.get(m.Requisite,rid)).enabled  # GET did not activate it.
            assert (await c.post('/trader/cabinet/requisites/'+rid+'/toggle')).status_code==403
            async with client() as stranger:
                await login(stranger,foreign,realm='trader')
                token=csrf(await stranger.get('/trader/cabinet/tradespace/requisites'))
                denied=await stranger.post('/trader/cabinet/requisites/'+rid+'/toggle',data={'csrf_token':token})
                assert denied.status_code in {303,403}
                async with AsyncSession(engine) as db:assert not (await db.get(m.Requisite,rid)).enabled
            toggled=await c.post('/trader/cabinet/requisites/'+rid+'/toggle',data={'csrf_token':csrf(page)})
            assert toggled.status_code==303 and 'ui_error' not in toggled.headers['location']
            async with AsyncSession(engine) as db:
                req=await db.get(m.Requisite,rid);assert req.enabled and req.status=='active'
                assert reveal_text(req.value_encrypted)==SHORT
                assert not (await db.get(m.Requisite,other_id)).enabled
                log=await db.scalar(select(m.AuditLog).where(m.AuditLog.target_id==rid,m.AuditLog.action=='requisite_toggled'))
                assert log.details['qr_type_source']=='trader_selection'
                assert await db.scalar(select(func.count()).select_from(m.Deposit))==0
            async with client() as merchant_client:
                await login(merchant_client,users['merchant'],realm='merchant')
                token=csrf(await merchant_client.get('/merchant/cabinet/tradespace/overview'))
                denied=await merchant_client.post('/merchant/cabinet/requisites/qr-preview',data={'csrf_token':token},files={'qr_image':('s.png',image_png(SHORT),'image/png')})
                assert denied.status_code==403
    asyncio.run(run())


@pytest.mark.parametrize('origin',['merchant','aggregator'])
@pytest.mark.parametrize('blocked_by',['dynamic','disabled','balance','amount_limit'])
def test_declared_static_keeps_routing_guards(monkeypatch,origin,blocked_by):
    async def run():
        async with isolated(monkeypatch) as (engine,merchant,users,keys),client() as c:
            agg=await prepare(engine,merchant,users,origin)
            async with AsyncSession(engine) as db:
                req=await db.scalar(select(m.Requisite));req.value_encrypted=encrypt_secret(SHORT)
                if blocked_by=='dynamic':req.value_encrypted=encrypt_secret(PAYLOAD.replace('type=01','type=02'))
                elif blocked_by=='disabled':req.enabled=False;req.status='disabled'
                elif blocked_by=='balance':
                    trader=await db.get(m.User,users['trader'].id);trader.trader_balance=0
                elif blocked_by=='amount_limit':req.max_check=999
                await db.commit()
            response=await create(c,origin,keys,agg)
            assert response.status_code in {400,409},response.text
            async with AsyncSession(engine) as db:
                assert await db.scalar(select(func.count()).select_from(m.Deposit))==0
                assert (await db.get(m.User,users['trader'].id)).trader_hold==0
    asyncio.run(run())


def test_declared_short_qr_cross_api_concurrency_duplicates_snapshot_and_pending(monkeypatch):
    async def run():
        async with isolated(monkeypatch) as (engine,merchant,users,keys),client() as c:
            agg=await prepare(engine,merchant,users,'aggregator')
            async with AsyncSession(engine) as db:
                req=await db.scalar(select(m.Requisite));req.value_encrypted=encrypt_secret(SHORT)
                db.add(m.Requisite(trader_id=users['trader'].id,owner_name='Same QR duplicate',method='sbp_qr',
                    qr_identity=req.qr_identity,value_encrypted=encrypt_secret(SHORT),bank_code='sberbank',bank_name='СберБанк',
                    min_check=1,max_check=100000,simultaneous_limit=20))
                await db.commit()
            responses=await asyncio.wait_for(asyncio.gather(create(c,'merchant',keys,agg),create(c,'aggregator',keys,agg)),20)
            assert sorted(r.status_code for r in responses)==[200,409],[r.text for r in responses]
            oid,qr=result(responses[0],'merchant') if responses[0].status_code==200 else result(responses[1],'aggregator')
            assert qr['payment_url']==SHORT and qr['type']=='static_open_amount'
            result(await create(c,'merchant',keys,agg,amount='1001'),'merchant')
            async with AsyncSession(engine) as db:
                dep=await db.get(m.Deposit,oid)
                assert dep.status=='pending' and dep.qr_reserved
                assert dep.qr_snapshot['qr_type_source']=='trader_selection'
                assert dep.qr_snapshot['payment_url']==SHORT
                assert await db.scalar(select(func.count()).select_from(m.Deposit))==2
                assert await db.scalar(select(func.count()).select_from(m.LedgerEntry))==0
                assert (await db.get(m.User,users['trader'].id)).trader_balance==Decimal('100000')
                # Editing the requisite does not rewrite already issued bank URLs.
                for req in await db.scalars(select(m.Requisite)):
                    req.value_encrypted=encrypt_secret(PAYLOAD);req.enabled=False
                await db.commit();await db.refresh(dep)
                assert dep.qr_snapshot['payment_url']==SHORT and dep.qr_snapshot['qr_type_source']=='trader_selection'
    asyncio.run(run())
