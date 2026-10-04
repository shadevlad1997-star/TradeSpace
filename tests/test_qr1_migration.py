"""Exercise real Alembic DDL, immutable snapshots and the database uniqueness barrier."""
import uuid
import psycopg
from psycopg import sql
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError,IntegrityError
import pytest
from app import models as m
from tests.test_migrations_postgres import _database_urls,_alembic

def test_qr_migration_constraints_and_non_destructive_downgrade():
    admin,url,name=_database_urls()
    with psycopg.connect(admin,autocommit=True) as db:db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        migrated=_alembic(url,'upgrade','head');assert migrated.returncode==0,migrated.stderr
        engine=create_engine(url.replace('postgresql://','postgresql+psycopg://'))
        with Session(engine,expire_on_commit=False) as db:
            user=m.User(email='synthetic@example.test',role='merchant',password_hash='not-used');db.add(user);db.flush()
            merchant=m.Merchant(owner_id=user.id,name='QR migration synthetic');db.add(merchant);db.flush()
            fields=dict(merchant_id=merchant.id,amount=1000,currency='RUB',method='sbp_qr',status='pending',qr_identity='A'*32,qr_token='x'*43,qr_snapshot={'payment_url':'synthetic'},qr_reserved=True)
            dep=m.Deposit(external_id='first',**fields);db.add(dep);db.commit();oid=dep.id
            for column,value in [('qr_snapshot',{'payment_url':'changed'}),('amount',2000),('qr_token','y'*43)]:
                setattr(dep,column,value)
                with pytest.raises(DBAPIError):db.commit()
                db.rollback();dep=db.get(m.Deposit,oid)
            duplicate=m.Deposit(external_id='second',**{**fields,'qr_token':'z'*43});db.add(duplicate)
            with pytest.raises(IntegrityError):db.commit()
            db.rollback();dep=db.get(m.Deposit,oid)
            dep.status='failed';db.commit();db.refresh(dep);assert not dep.qr_reserved
            duplicate=m.Deposit(external_id='third',**{**fields,'qr_token':'z'*43});db.add(duplicate);db.commit()
            assert db.scalar(text("SELECT count(*) FROM deposits"))==2
        engine.dispose()
        blocked=_alembic(url,'downgrade','0027_aggregator_credentials')
        assert blocked.returncode!=0 and 'refusing QR downgrade after activity' in blocked.stderr+blocked.stdout
        with psycopg.connect(url) as db:
            assert db.execute('SELECT count(*) FROM deposits').fetchone()[0]==2
            assert db.execute('SELECT version_num FROM alembic_version').fetchone()[0]=='0029_bank_connection_drafts'
    finally:
        with psycopg.connect(admin,autocommit=True) as db:db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
