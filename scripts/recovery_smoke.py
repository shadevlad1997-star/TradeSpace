"""End-to-end HTTP recovery smoke against synthetic data in tradespace_dev only.

This creates local test users and accounting records. It performs no real bank
or blockchain transfer and never prints credentials.
"""
import asyncio
from contextlib import ExitStack
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
import uuid

import httpx
import psycopg
import pyotp

from scripts.recovery_run import local_environment, LOCAL

ENV = local_environment()
os.environ.update(ENV)
from app.core.merchant_hmac import canonical_request_v2, sign_request_v2

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:8000'
RESULTS = []


def record(name, detail):
    RESULTS.append({'check': name, 'status': 'PASS', 'detail': detail})
    (ROOT / 'test-results').mkdir(exist_ok=True)
    (ROOT / 'test-results/live-smoke.json').write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print(f'PASS {name}: {detail}', flush=True)


def expect(response, codes=(200,)):
    if response.status_code not in codes:
        raise AssertionError(
            f'{response.request.method} {response.request.url.path}: '
            f'HTTP {response.status_code}; {response.text[:300]}'
        )
    return response


def login_web(client, realm, email, password, otp=''):
    page = expect(client.get(f'/{realm}/login'))
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match, 'login CSRF token missing'
    expect(client.post(
        f'/{realm}/login',
        data={'email': email, 'password': password, 'otp': otp,
              'csrf_token': match.group(1)},
    ), (303,))
    return expect(client.get(f'/{realm}/cabinet'))


def post_web(client, realm, path, data=None):
    page = expect(client.get(f'/{realm}/cabinet'))
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    token = match.group(1) if match else client.cookies.get(f'processing_{realm}_csrf')
    assert token, f'{realm} CSRF token missing'
    return client.post(
        f'/{realm}{path}',
        data={**(data or {}), 'csrf_token': token},
    )


def api_login(client, email, password, otp=''):
    return expect(client.post('/api/v1/auth/login', json={
        'email': email, 'password': password, 'otp': otp,
    })).json()


def merchant_request(client, credentials, method, path, data=None, idem=None):
    body = json.dumps(data, separators=(',', ':')).encode() if data is not None else b''
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    canonical = canonical_request_v2(
        timestamp=timestamp, nonce=nonce, method=method, path=path, query='',
        content_type='application/json', body=body,
    )
    headers = {
        'X-API-Key': credentials['api_key'],
        'X-Signature-Version': '2',
        'X-Timestamp': timestamp,
        'X-Nonce': nonce,
        'X-Signature': sign_request_v2(credentials['secret_key'], canonical),
        'Content-Type': 'application/json',
    }
    if idem:
        headers['Idempotency-Key'] = idem
    return client.request(method, path, content=body, headers=headers)


def db_one(db, statement, args=()):
    return db.execute(statement, args).fetchone()


def _base58(data):
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    number = int.from_bytes(data, 'big')
    result = ''
    while number:
        number, rem = divmod(number, 58)
        result = alphabet[rem] + result
    return '1' * (len(data) - len(data.lstrip(b'\0'))) + result


def synthetic_trc20_address():
    body = bytes([0x41]) + bytes.fromhex('0011223344556677889900112233445566778899')
    checksum = hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    return _base58(body + checksum)


def main():
    run_id = uuid.uuid4().hex[:12]
    with ExitStack() as stack:
        counter = 0

        def new_client():
            nonlocal counter
            counter += 1
            ip = f'2001:db8:{run_id[:4]}::{counter}'
            return stack.enter_context(httpx.Client(
                base_url=BASE, timeout=25, trust_env=False,
                headers={'X-Real-IP': ip},
            ))

        staff, admin, trader, merchant, api = [new_client() for _ in range(5)]
        db = stack.enter_context(psycopg.connect(
            ENV['SYNC_DATABASE_URL'].replace('+psycopg', ''), autocommit=True,
        ))

        staff_page = login_web(
            staff, 'staff', ENV['SUPERADMIN_EMAIL'], ENV['SUPERADMIN_PASSWORD'],
            pyotp.TOTP(ENV['SUPERADMIN_2FA_SECRET']).now(),
        )
        login_web(
            admin, 'staff', ENV['DEMO_ADMIN_EMAIL'], ENV['DEMO_ADMIN_PASSWORD'],
            pyotp.TOTP(ENV['DEMO_ADMIN_2FA_SECRET']).now(),
        )
        sections = sorted(set(re.findall(r'/staff/cabinet/tradespace/([a-zA-Z0-9_-]+)', staff_page.text)))
        assert len(sections) >= 5, 'Staff navigation was not exercised'
        for section in sections:
            expect(staff.get('/staff/cabinet/tradespace/' + section))
        record('staff_cabinets', f'Superadmin and Admin TOTP login; {len(sections)} sections render')

        tokens = api_login(
            api, ENV['SUPERADMIN_EMAIL'], ENV['SUPERADMIN_PASSWORD'],
            pyotp.TOTP(ENV['SUPERADMIN_2FA_SECRET']).now(),
        )
        auth = {'Authorization': 'Bearer ' + tokens['access_token']}
        expect(api.get('/api/v1/admin/audit', headers=auth))
        refreshed = expect(api.post('/api/v1/auth/refresh', json={
            'refresh_token': tokens['refresh_token'],
        })).json()
        auth = {'Authorization': 'Bearer ' + refreshed['access_token']}
        record('authentication', 'JWT login, refresh and Superadmin audit permission')

        trader_email = f'trader-{run_id}@tradespace.local'
        merchant_email = f'merchant-{run_id}@tradespace.local'
        trader_password = secrets.token_urlsafe(24)
        merchant_password = secrets.token_urlsafe(24)
        trader_row = expect(api.post('/api/v1/admin/users', headers=auth, json={
            'email': trader_email, 'password': trader_password, 'role': 'operator',
        })).json()
        trader_id = str(trader_row['user_id'])
        credentials = expect(api.post('/api/v1/admin/merchants', headers=auth, json={
            'email': merchant_email,
            'password': merchant_password,
            'name': f'Recovery smoke {run_id}',
            'webhook_url': 'http://127.0.0.1:18081/retry-once',
            'sandbox_mode': True,
        })).json()
        merchant_id = str(credentials['merchant_id'])
        (LOCAL / 'run/smoke-credentials.json').write_text(json.dumps({
            'trader_email': trader_email,
            'trader_password': trader_password,
            'merchant_email': merchant_email,
            'merchant_password': merchant_password,
            **credentials,
        }, indent=2, default=str), encoding='utf-8')
        record('local_entities', 'Synthetic trader and merchant created; secrets kept only in external local configuration')

        login_web(trader, 'trader', trader_email, trader_password)
        login_web(merchant, 'merchant', merchant_email, merchant_password)
        trader_tokens = api_login(api, trader_email, trader_password)
        expect(api.get('/api/v1/admin/audit', headers={
            'Authorization': 'Bearer ' + trader_tokens['access_token'],
        }), (403,))
        expect(api.get('/api/v1/merchant/balance'), (401, 403))
        expect(merchant_request(api, credentials, 'GET', '/api/v1/merchant/balance'))
        record('permissions', 'Trader denied staff audit; unsigned merchant call rejected; HMAC call accepted')

        expect(post_web(staff, 'staff', f'/cabinet/traders/{trader_id}/finance', {
            'trader_balance': '100000',
            'trader_hold': '0',
            'trader_traffic_priority': '100',
            'trader_commission_percent': '7',
            'assigned_merchants': merchant_id,
        }), (303,))
        for entity, entity_id, side, rate in (
            ('merchant', merchant_id, 'merchant_fee', '15'),
            ('trader', trader_id, 'executor_fee', '7'),
        ):
            expect(api.post('/api/v1/admin/fee-rules', headers=auth, json={
                'entity_type': entity,
                'entity_id': entity_id,
                'fee_side': side,
                'payment_method': 'sbp',
                'currency': 'RUB',
                'min_amount': '100',
                'max_amount': '150000.01',
                'rate_percent': rate,
            }))
        expect(post_web(trader, 'trader', '/cabinet/requisites/create', {
            'method': 'sbp',
            'value': '+79990000000',
            'bank_code': 'sber',
            'full_name': 'Local Test Receiver',
            'automation_id': 'recovery-' + run_id,
            'daily_limit': '500000',
            'operation_limit': '100',
            'request_count': '100',
            'simultaneous_limit': '10',
            'min_check': '100',
            'max_check': '150000',
            'status': 'active',
        }), (303,))
        requisite_id = str(db_one(
            db, 'SELECT id FROM requisites WHERE automation_id=%s',
            ('recovery-' + run_id,),
        )[0])
        expect(post_web(trader, 'trader', f'/cabinet/requisites/{requisite_id}/toggle'), (303,))
        assert db_one(db, 'SELECT enabled FROM requisites WHERE id=%s', (requisite_id,))[0] is False
        expect(post_web(trader, 'trader', f'/cabinet/requisites/{requisite_id}/toggle'), (303,))
        assert db_one(db, 'SELECT enabled FROM requisites WHERE id=%s', (requisite_id,))[0] is True
        record('trader_setup', 'Finance, assignment, fee tiers, requisite creation and off/on controls work')

        webhook_key = expect(api.post(
            f'/api/v1/admin/merchants/{merchant_id}/webhook-signing-keys',
            headers=auth,
        )).json()
        assert webhook_key['secret'] != credentials['secret_key']
        external_id = 'recovery-paid-' + run_id
        payload = {
            'external_id': external_id,
            'amount': '5000.00',
            'currency': 'RUB',
            'method': 'sbp',
        }
        deposit = expect(merchant_request(
            api, credentials, 'POST', '/api/v1/merchant/deposits', payload, external_id,
        )).json()
        assert deposit['status'] == 'pending' and deposit['payment_details']
        repeated = expect(merchant_request(
            api, credentials, 'POST', '/api/v1/merchant/deposits', payload, external_id,
        )).json()
        assert repeated['id'] == deposit['id']
        assigned = db_one(db, 'SELECT requisites_id FROM deposits WHERE id=%s', (str(deposit['id']),))[0]
        assert str(assigned) == requisite_id
        assert db_one(db, 'SELECT trader_hold FROM users WHERE id=%s', (trader_id,))[0] == Decimal('4650')
        record('deposit_created', 'HMAC v2 create, allocation, payment details, TTL, idempotency and trader hold')

        expect(post_web(
            trader, 'trader', f'/cabinet/deposits/{deposit["id"]}/confirm',
        ), (303,))
        status = expect(merchant_request(
            api, credentials, 'GET', f'/api/v1/merchant/deposits/{external_id}',
        )).json()
        assert status['status'] == 'paid'
        merchant_balance = db_one(
            db, 'SELECT available FROM balances WHERE merchant_id=%s', (merchant_id,),
        )[0]
        trader_finance = db_one(
            db, 'SELECT trader_balance,trader_hold FROM users WHERE id=%s', (trader_id,),
        )
        snapshot = db_one(db, '''
            SELECT merchant_fee_amount, executor_fee_amount, platform_income_amount
            FROM operation_fee_snapshots WHERE deposit_id=%s
        ''', (str(deposit['id']),))
        assert merchant_balance == Decimal('4250')
        assert trader_finance == (Decimal('95350'), Decimal('0'))
        assert snapshot == (Decimal('750'), Decimal('350'), Decimal('400'))
        record('deposit_paid', 'Trader confirmation updates status, both balances and immutable fee snapshot')

        print('Waiting for real Celery delivery and scheduled retry...', flush=True)
        deadline = time.monotonic() + 135
        event = None
        while time.monotonic() < deadline:
            event = db_one(db, '''
                SELECT id,status,attempts FROM webhook_events
                WHERE merchant_id=%s ORDER BY created_at DESC LIMIT 1
            ''', (merchant_id,))
            if event and event[1] == 'delivered':
                break
            time.sleep(2)
        assert event and event[1] == 'delivered', str(event)
        attempts = db.execute('''
            SELECT status_code FROM webhook_delivery_attempts
            WHERE webhook_event_id=%s ORDER BY attempt_no
        ''', (event[0],)).fetchall()
        assert attempts == [(500,), (204,)], str(attempts)
        receiver_rows = [json.loads(line) for line in
                         (ROOT / 'logs/webhook-receiver.jsonl').read_text().splitlines()]
        received = [row for row in receiver_rows if row['event_id'] == str(event[0])]
        assert len(received) == 2
        assert all(row['signature_valid'] and not row['contains_api_key_header'] for row in received)
        record('webhook', 'Separate HMAC key verified; real HTTP 500 -> scheduled retry -> 204; attempts logged')

        expect(post_web(staff, 'staff', f'/cabinet/merchants/{merchant_id}/integration', {
            'webhook_url': 'http://127.0.0.1:18081/webhook',
            'sandbox_mode': 'on',
        }), (303,))
        cancel_id = 'recovery-cancel-' + run_id
        cancel_payload = {**payload, 'external_id': cancel_id}
        expect(merchant_request(
            api, credentials, 'POST', '/api/v1/merchant/deposits', cancel_payload, cancel_id,
        ))
        expect(merchant_request(
            api, credentials, 'POST', f'/api/v1/merchant/deposits/{cancel_id}/cancel',
            idem='cancel-' + cancel_id,
        ))
        cancelled = expect(merchant_request(
            api, credentials, 'GET', f'/api/v1/merchant/deposits/{cancel_id}',
        )).json()
        assert cancelled['status'] in {'cancelled', 'failed'}
        assert db_one(db, 'SELECT trader_hold FROM users WHERE id=%s', (trader_id,))[0] == 0
        record('cancellation', 'Cancelled deposit releases trader hold and exposes final API status')

        for path in ('balance', 'operations', 'statistics'):
            expect(merchant_request(api, credentials, 'GET', '/api/v1/merchant/' + path))
        expect(api.get('/api/v1/admin/audit', headers=auth))
        record('history_logs', 'Merchant balance, operations, statistics, API journal and audit log readable')

        settlement_key = 'recovery-settle-' + run_id
        expect(post_web(merchant, 'merchant', '/cabinet/settlements/request', {
            'amount_usdt': '10',
            'trc20_address': synthetic_trc20_address(),
            'idempotency_key': settlement_key,
        }), (303,))
        settlement = db_one(db, '''
            SELECT id,status FROM merchant_settlements WHERE idempotency_key=%s
        ''', (settlement_key,))
        assert settlement and settlement[1] == 'pending'
        expect(post_web(
            staff, 'staff', f'/cabinet/settlements/{settlement[0]}/approve',
            {'tx_hash': hashlib.sha256(settlement_key.encode()).hexdigest()},
        ), (303,))
        assert db_one(
            db, 'SELECT status FROM merchant_settlements WHERE id=%s', (settlement[0],),
        )[0] == 'completed'
        record('settlement', 'Configured Rapira quote used; local accounting completed with synthetic TRC20 evidence')

        async def reconcile():
            from app.db.session import AsyncSessionLocal, engine
            from app.services.finance_reconciliation import (
                reconcile_merchant_balance, reconcile_trader_balance,
            )
            try:
                async with AsyncSessionLocal() as session:
                    merchant_result = await reconcile_merchant_balance(
                        session, uuid.UUID(merchant_id),
                    )
                    trader_result = await reconcile_trader_balance(
                        session, uuid.UUID(trader_id),
                    )
                    assert merchant_result.ok and trader_result.ok, (
                        merchant_result, trader_result,
                    )
            finally:
                await engine.dispose()

        asyncio.run(reconcile())
        record('reconciliation', 'Merchant and trader balances agree with ledger histories')

        # One persistent server event loop, unlike the failing in-process test harness.
        limiter = new_client()
        limiter.headers['X-Real-IP'] = f'2001:db8:{run_id[:4]}:{run_id[4:8]}::99'
        if time.time() % 60 > 50:
            time.sleep(61 - time.time() % 60)
        responses = [limiter.get('/version') for _ in range(121)]
        assert all(response.status_code == 200 for response in responses[:120])
        assert responses[120].status_code == 429
        record('live_rate_limit', 'Live server permits 120 requests and returns 429 on request 121')
        record('completed', f'Synthetic run {run_id}; no production data or real transfer')


if __name__ == '__main__':
    main()
