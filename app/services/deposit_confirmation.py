import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DepositStatus
from app.models import Deposit
from app.services.aggregator_enqueue import enqueue_aggregator_callback_delivery
from app.services.aggregators import sync_payment_from_deposit
from app.services.antiscam import check_payment_result_for_risk
from app.services.audit import audit
from app.services.deposit_lifecycle import (
    deposit_is_expired,
    finalize_unsuccessful_deposit,
)
from app.services.fees import settle_deposit_credit
from app.services.requisites import settle_trader_deposit_balance
from app.services.webhook_enqueue import enqueue_webhook_delivery
from app.services.webhook_payloads import build_deposit_webhook_payload
from app.services.webhooks import queue_webhook


logger = logging.getLogger('app.finance')
CONFIRMABLE_DEPOSIT_STATUSES = {
    DepositStatus.created.value,
    DepositStatus.pending.value,
    DepositStatus.appeal_opened.value,
}


class DepositConfirmationError(Exception):
    status_code = 409

    def __init__(self, public_message: str, *, api_detail: str | None = None):
        super().__init__(public_message)
        self.public_message = public_message
        self.api_detail = api_detail or public_message


class DepositConfirmationNotFound(DepositConfirmationError):
    status_code = 404


class DepositConfirmationConflict(DepositConfirmationError):
    status_code = 409


class DepositConfirmationSystemError(DepositConfirmationError):
    status_code = 500


@dataclass(frozen=True)
class DepositConfirmationResult:
    deposit_id: UUID
    confirmed: bool
    status: str
    reason: str | None
    settlement: dict | None
    webhook_event_id: UUID | None
    aggregator_callback_log_ids: tuple[UUID, ...] = ()


async def _rollback(db: AsyncSession, operation_id: UUID | str, actor_id: UUID | str) -> None:
    try:
        await db.rollback()
    except Exception:
        logger.exception(
            'deposit_confirmation_rollback_failed',
            extra={'operation_id': str(operation_id), 'actor_id': str(actor_id)},
        )


async def confirm_deposit_payment(
    db: AsyncSession,
    operation_id: UUID | str,
    *,
    actor_id: UUID,
    actor_ip: str,
    audit_action: str,
    description: str,
) -> DepositConfirmationResult:
    """Apply one locked, atomic deposit settlement and persist delivery outbox rows."""
    try:
        try:
            deposit_id = UUID(str(operation_id))
        except (TypeError, ValueError) as exc:
            raise DepositConfirmationNotFound('Заявка не найдена', api_detail='not found') from exc

        deposit = (await db.execute(
            select(Deposit).where(Deposit.id == deposit_id).with_for_update()
        )).scalar_one_or_none()
        if not deposit:
            raise DepositConfirmationNotFound('Заявка не найдена', api_detail='not found')
        if deposit.status == DepositStatus.paid.value:
            raise DepositConfirmationConflict('Заявка уже подтверждена', api_detail='already paid')
        if deposit.status not in CONFIRMABLE_DEPOSIT_STATUSES:
            raise DepositConfirmationConflict(
                'Заявку в текущем статусе нельзя подтвердить',
                api_detail=f'deposit status {deposit.status} cannot be confirmed',
            )
        if deposit_is_expired(deposit):
            finalization = await finalize_unsuccessful_deposit(
                db,
                deposit,
                reason='trader_timeout',
                actor_id=actor_id,
                actor_role='system',
                actor_ip=actor_ip,
                audit_action='deposit_confirm_rejected_after_ttl',
            )
            await db.commit()
            return DepositConfirmationResult(
                deposit_id=deposit.id,
                confirmed=False,
                status=deposit.status,
                reason='trader_timeout',
                settlement=None,
                webhook_event_id=finalization.webhook_event_id,
                aggregator_callback_log_ids=finalization.aggregator_callback_log_ids,
            )

        await settle_trader_deposit_balance(db, deposit)
        deposit.status = DepositStatus.paid.value
        settlement = await settle_deposit_credit(
            db,
            merchant_id=deposit.merchant_id,
            method=deposit.method,
            amount=deposit.amount,
            operation_id=deposit.id,
            description=description,
        )
        await check_payment_result_for_risk(db, deposit)
        # Keep all payload fields loaded before the synchronous snapshot
        # builder; server-generated updated_at is expired by PostgreSQL flush.
        await db.flush()
        await db.refresh(deposit, attribute_names=['updated_at'])
        webhook_event = await queue_webhook(
            db,
            deposit.merchant_id,
            'deposit.paid',
            build_deposit_webhook_payload(deposit, 'deposit.paid', settlement),
        )
        aggregator_logs = await sync_payment_from_deposit(db, deposit)
        await audit(db, audit_action, 'deposit', actor_id, deposit.id, actor_ip)
        await db.commit()
    except DepositConfirmationError:
        await _rollback(db, operation_id, actor_id)
        raise
    except ValueError as exc:
        await _rollback(db, operation_id, actor_id)
        logger.warning(
            'deposit_confirmation_business_error',
            extra={
                'operation_id': str(operation_id),
                'actor_id': str(actor_id),
                'error_type': type(exc).__name__,
            },
            exc_info=True,
        )
        raise DepositConfirmationConflict(
            'Не удалось подтвердить пополнение: проверьте hold, баланс и настройки комиссий',
            api_detail='deposit confirmation failed',
        ) from exc
    except Exception as exc:
        await _rollback(db, operation_id, actor_id)
        logger.exception(
            'deposit_confirmation_failed',
            extra={
                'operation_id': str(operation_id),
                'actor_id': str(actor_id),
                'error_type': type(exc).__name__,
            },
        )
        raise DepositConfirmationSystemError(
            'Не удалось подтвердить пополнение. Повторите попытку позже',
            api_detail='deposit confirmation failed',
        ) from exc

    return DepositConfirmationResult(
        deposit_id=deposit.id,
        confirmed=True,
        status=deposit.status,
        reason=None,
        settlement=settlement,
        webhook_event_id=webhook_event.id,
        aggregator_callback_log_ids=tuple(log.id for log in aggregator_logs),
    )


def enqueue_deposit_confirmation_deliveries(result: DepositConfirmationResult) -> dict:
    """Kick queued deliveries after commit; queue outages must not undo settlement."""
    webhook_enqueued = False
    try:
        if result.webhook_event_id:
            webhook_enqueued = bool(enqueue_webhook_delivery(result.webhook_event_id))
    except Exception:
        logger.exception(
            'deposit_confirmation_webhook_enqueue_failed',
            extra={'operation_id': str(result.deposit_id), 'event_id': str(result.webhook_event_id)},
        )

    aggregator_callbacks_enqueued = 0
    for log_id in result.aggregator_callback_log_ids:
        try:
            if enqueue_aggregator_callback_delivery(log_id):
                aggregator_callbacks_enqueued += 1
        except Exception:
            logger.exception(
                'deposit_confirmation_aggregator_enqueue_failed',
                extra={'operation_id': str(result.deposit_id), 'callback_log_id': str(log_id)},
            )

    return {
        'webhook_enqueued': webhook_enqueued,
        'aggregator_callbacks_enqueued': aggregator_callbacks_enqueued,
    }
