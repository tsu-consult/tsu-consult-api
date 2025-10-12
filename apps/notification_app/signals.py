from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.notification_app.models import Notification
from apps.notification_app.tasks import send_notification_task


@receiver(post_save, sender=Notification)
def trigger_notification_send(sender, instance, created, **kwargs):
    if created and instance.status == Notification.Status.PENDING:
        send_notification_task.delay(instance.id)
