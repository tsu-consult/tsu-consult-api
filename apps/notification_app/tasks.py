import logging

from celery import shared_task
from django.contrib.auth import get_user_model
from django.utils import timezone
from requests.exceptions import RequestException

from apps.notification_app.models import Notification
from apps.notification_app.services import send_telegram_notification

logger = logging.getLogger(__name__)

User = get_user_model()


@shared_task(bind=True, max_retries=5, default_retry_delay=60)
def send_notification_task(self, notification_id):
    logger.debug("send_notification_task started: %s", notification_id)
    try:
        notification = Notification.objects.get(id=notification_id)
    except Notification.DoesNotExist:
        return

    if notification.scheduled_for and notification.scheduled_for > timezone.now():
        return

    if notification.type == Notification.Type.TELEGRAM:
        try:
            send_telegram_notification(notification)
        except RequestException as e:
            notification.last_error = str(e)
            notification.save(update_fields=["last_error"])
            logger.warning("Network error sending notification %s: %s", notification_id, e)
            retries = getattr(self.request, 'retries', 0)
            countdown = min(2 ** retries * 60, 3600)
            raise self.retry(exc=e, countdown=countdown)
        except Exception as e:
            logger.exception("Failed to send notification %s", notification_id)
            notification.status = Notification.Status.FAILED
            notification.last_error = str(e)
            notification.save(update_fields=["status", "last_error"])


@shared_task
def retry_pending_notifications():
    now = timezone.now()
    pending = Notification.objects.filter(status=Notification.Status.PENDING)
    for n in pending:
        if n.scheduled_for and n.scheduled_for > now:
            continue
        try:
            send_telegram_notification(n)
        except RequestException as e:
            n.last_error = str(e)
            n.save(update_fields=["last_error"])
            logger.warning("Network error retrying notification %s: %s", n.id, e)
        except Exception as e:
            n.last_error = str(e)
            n.save(update_fields=["last_error"])
            logger.exception("Error retrying notification %s", n.id)
