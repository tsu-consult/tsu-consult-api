from celery import shared_task
from apps.notification_app.models import Notification
from apps.notification_app.services import send_telegram_notification


@shared_task
def send_notification_task(notification_id):
    try:
        notification = Notification.objects.get(id=notification_id)
    except Notification.DoesNotExist:
        return
    if notification.type == Notification.Type.TELEGRAM:
        send_telegram_notification(notification)


@shared_task
def retry_pending_notifications():
    pending = Notification.objects.filter(status=Notification.Status.PENDING)
    for n in pending:
        send_telegram_notification(n)
