import asyncio
from datetime import datetime, timezone

from celery import Celery
from celery.signals import after_setup_logger, after_setup_task_logger
from app.core.logging import protect_log_handlers
from sqlalchemy import and_, or_, select

from app.core.config import settings
from app.db.session import AsyncSessionLocal, engine
from app.models import AggregatorCallbackLog
from app.services.appeals import approve_expired_appeals
from app.services.aggregators import deliver_callback_log
from app.services.antiscam import run_periodic_antiscam_scan
from app.services.deposit_lifecycle import expire_due_deposits
from app.services.webhooks import deliver_event, due_webhook_event_ids

celery_app = Celery('processing_platform', broker=settings.CELERY_BROKER_URL, backend=settings.CELERY_RESULT_BACKEND)


@after_setup_logger.connect
@after_setup_task_logger.connect
def sanitize_worker_logging(**kwargs):
    protect_log_handlers()


def _run_async_task(coro_factory):
    async def wrapper():
        try:
            from app.services.integration_modes import verify_environment
            async with AsyncSessionLocal() as db:
                await verify_environment(db)
            return await coro_factory()
        finally:
            await engine.dispose()
    return asyncio.run(wrapper())


@celery_app.task(name='deliver_webhook')
def deliver_webhook_task(event_id: str):
    async def run():
        async with AsyncSessionLocal() as db:
            await deliver_event(db, event_id)
            await db.commit()
    return _run_async_task(run)


@celery_app.task(name='deliver_aggregator_callback')
def deliver_aggregator_callback_task(callback_log_id: str):
    async def run():
        async with AsyncSessionLocal() as db:
            await deliver_callback_log(db, callback_log_id)
            await db.commit()
    return _run_async_task(run)


@celery_app.task(name='deliver_due_webhooks')
def deliver_due_webhooks_task() -> int:
    async def run() -> int:
        async with AsyncSessionLocal() as db:
            event_ids = await due_webhook_event_ids(db, limit=100)
        from app.services.webhook_enqueue import enqueue_webhook_delivery

        return sum(
            1 for event_id in event_ids
            if enqueue_webhook_delivery(event_id)
        )
    return _run_async_task(run)


@celery_app.task(name='deliver_due_aggregator_callbacks')
def deliver_due_aggregator_callbacks_task() -> int:
    async def run() -> int:
        async with AsyncSessionLocal() as db:
            now = datetime.now(timezone.utc)
            callback_ids = (await db.execute(
                select(AggregatorCallbackLog.id)
                .where(
                    or_(
                        AggregatorCallbackLog.status.in_(['pending', 'failed']),
                        and_(
                            AggregatorCallbackLog.status == 'delivering',
                            AggregatorCallbackLog.next_retry_at <= now,
                        ),
                    ),
                    AggregatorCallbackLog.attempt < 5,
                    (AggregatorCallbackLog.next_retry_at.is_(None) | (AggregatorCallbackLog.next_retry_at <= now)),
                )
                .order_by(AggregatorCallbackLog.created_at.asc())
                .limit(100)
            )).scalars().all()
        from app.services.aggregator_enqueue import (
            enqueue_aggregator_callback_delivery,
        )

        return sum(
            1 for callback_id in callback_ids
            if enqueue_aggregator_callback_delivery(callback_id)
        )
    return _run_async_task(run)


@celery_app.task(name='expire_stale_deposits')
def expire_stale_deposits_task() -> int:
    async def run() -> int:
        from app.services.aggregator_enqueue import enqueue_aggregator_callback_delivery
        from app.services.webhook_enqueue import enqueue_webhook_delivery

        async with AsyncSessionLocal() as db:
            results = await expire_due_deposits(db)
            await db.commit()
            for result in results:
                if result.webhook_event_id:
                    enqueue_webhook_delivery(result.webhook_event_id)
                for callback_id in result.aggregator_callback_log_ids:
                    enqueue_aggregator_callback_delivery(callback_id)
            return len(results)
    return _run_async_task(run)


@celery_app.task(name='approve_expired_appeals')
def approve_expired_appeals_task() -> int:
    async def run() -> int:
        from app.services.webhook_enqueue import enqueue_webhook_delivery

        async with AsyncSessionLocal() as db:
            events = await approve_expired_appeals(db)
            await db.commit()
            for event in events:
                enqueue_webhook_delivery(event.id)
            return len(events)
    return _run_async_task(run)


@celery_app.task(name='run_antiscam_scan')
def run_antiscam_scan_task() -> int:
    async def run() -> int:
        async with AsyncSessionLocal() as db:
            changed = await run_periodic_antiscam_scan(db)
            await db.commit()
            return changed
    return _run_async_task(run)


celery_app.conf.broker_connection_retry_on_startup = True
celery_app.conf.timezone = 'UTC'
celery_app.conf.beat_schedule = {
    'expire-stale-deposits-every-minute': {
        'task': 'expire_stale_deposits',
        'schedule': 60.0,
    },
    'approve-expired-appeals-every-minute': {
        'task': 'approve_expired_appeals',
        'schedule': 60.0,
    },
    'deliver-due-webhooks-every-minute': {
        'task': 'deliver_due_webhooks',
        'schedule': 60.0,
    },
    'deliver-due-aggregator-callbacks-every-minute': {
        'task': 'deliver_due_aggregator_callbacks',
        'schedule': 60.0,
    },
    'run-antiscam-scan-every-minute': {
        'task': 'run_antiscam_scan',
        'schedule': 60.0,
    },
}
