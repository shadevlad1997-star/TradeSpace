import asyncio
import hashlib
import hmac
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from datetime import datetime,timedelta,timezone
import pytest
from sqlalchemy import select,delete,func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from app import models as m
from app.core.security import hash_password,encrypt_secret
from app.services import aggregators
from tests.test_acceptance_financial_postgres import setup,request,state,finish,replay_client_per_event_loop
from tests.test_acceptance_known_gaps import callback_fixture


def test_aggregator_real_local_http_500_retry_204_and_signature():
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']))
            expected=hmac.new(b'synthetic-callback-secret',self.headers['X-Timestamp'].encode()+b'.'+body,hashlib.sha256).hexdigest()
            # Existing aggregator contract uses timestamp + dot + body (not merchant v2).
            received.append({'valid':hmac.compare_digest(expected,self.headers['X-Signature']),
                             'key':self.headers['X-Idempotency-Key'],'payload':json.loads(body)})
            self.send_response(500 if len(received)==1 else 204);self.send_header('Content-Length','0');self.end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    async def run():
        engine,key,secret,mid,tid=await setup()
        try:
            r=await request(key,secret,uuid.uuid4().hex);lid=await callback_fixture(engine,mid,uuid.UUID(r.json()['id']));before=await state(engine,mid,tid)
            async with AsyncSession(engine,expire_on_commit=False) as db:
                row=await db.get(m.AggregatorCallbackLog,lid);row.target_url=f'http://127.0.0.1:{server.server_port}/callback';await db.commit()
                row=await aggregators.deliver_callback_log(db,lid);await db.commit();assert row.response_status_code==500 and row.status=='pending'
                row.next_retry_at=datetime.now(timezone.utc)-timedelta(seconds=1);await db.commit()
                row=await aggregators.deliver_callback_log(db,lid);await db.commit();assert row.status=='sent' and row.response_status_code==204
                await aggregators.deliver_callback_log(db,lid);await db.commit();assert len(received)==2
                assert all(r['valid'] for r in received) and received[0]['key']==received[1]['key']
            assert await state(engine,mid,tid)==before
        finally:await finish(engine)
    try:asyncio.run(run())
    finally:server.shutdown();server.server_close();thread.join(timeout=3)


def test_antiscam_fk_create_delete_and_reject_orphans():
    async def run():
        engine,key,secret,mid,tid=await setup()
        try:
            async with AsyncSession(engine,expire_on_commit=False) as db:
                user=m.User(email=f'fk-{uuid.uuid4()}@example.test',password_hash=hash_password('synthetic-password'),role='trader');db.add(user);await db.flush()
                req=m.Requisite(trader_id=user.id,owner_name='Synthetic FK',method='sbp',value_encrypted=encrypt_secret('synthetic'));db.add(req);await db.flush()
                settings=m.RequisiteAntiscamSettings(requisite_id=req.id);ts=m.TraderAntiscamSettings(trader_id=user.id);db.add_all([settings,ts]);await db.commit()
                sid=settings.id;tsid=ts.id
                await db.execute(delete(m.Requisite).where(m.Requisite.id==req.id));await db.commit()
                assert await db.scalar(select(func.count()).select_from(m.RequisiteAntiscamSettings).where(m.RequisiteAntiscamSettings.id==sid))==0
                await db.execute(delete(m.User).where(m.User.id==user.id));await db.commit()
                assert await db.scalar(select(func.count()).select_from(m.TraderAntiscamSettings).where(m.TraderAntiscamSettings.id==tsid))==0
                for cls,field in [(m.RequisiteAntiscamSettings,'requisite_id'),(m.TraderAntiscamSettings,'trader_id')]:
                    with pytest.raises(IntegrityError):
                        async with db.begin_nested():db.add(cls(**{field:uuid.uuid4()}));await db.flush()
        finally:await finish(engine)
    asyncio.run(run())
