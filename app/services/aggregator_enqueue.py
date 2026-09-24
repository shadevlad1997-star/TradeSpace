import logging

from app.workers.celery_app import deliver_aggregator_callback_task

logger = logging.getLogger('app.aggregator_callbacks')


def enqueue_aggregator_callback_delivery(callback_log_id) -> bool:
    callback_id_text = str(callback_log_id)
    try:
        deliver_aggregator_callback_task.delay(callback_id_text)
    except Exception as exc:
        logger.exception(
            'aggregator_callback_enqueue_failed',
            extra={'callback_log_id': callback_id_text, 'reason': str(exc)[:500]},
        )
        return False
    logger.info('aggregator_callback_enqueued', extra={'callback_log_id': callback_id_text})
    return True
