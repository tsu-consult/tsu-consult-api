from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings
from django.utils import timezone

from apps.auth_app.models import TeacherApproval
from apps.notification_app.models import Notification
from apps.notification_app.tasks import send_notification_task, sync_existing_todos
from apps.profile_app.models import GoogleToken


@receiver(post_save, sender=Notification)
def trigger_notification_send(sender, instance, created, **kwargs):
    if not getattr(settings, "NOTIFICATIONS_DELIVERY_ENABLED", True):
        return
    if created and instance.status == Notification.Status.PENDING:
        if instance.scheduled_for and instance.scheduled_for > timezone.now():
            return
        send_notification_task.delay(instance.id)


@receiver(post_save, sender=TeacherApproval)
def notify_teacher_on_approval_status(sender, instance: TeacherApproval, created, **kwargs):
    if created:
        return

    if instance.status in [TeacherApproval.Status.APPROVED, TeacherApproval.Status.REJECTED]:
        user = instance.user

        if instance.status == TeacherApproval.Status.APPROVED:
            title = "Заявка одобрена"
            message = "Поздравляем! Ваша заявка на подтверждение преподавателя была одобрена."
        else:
            title = "Заявка отклонена"
            reason_text = f"\n\n<b>Причина:</b> {instance.reason}" if instance.reason else ""
            message = f"К сожалению, ваша заявка на подтверждение преподавателя была отклонена.{reason_text}"

        Notification.objects.create(
            user=user,
            title=title,
            message=message,
            status=Notification.Status.PENDING,
        )


@receiver(post_save, sender=GoogleToken)
def sync_after_integration(sender, instance, created, **kwargs):
    sync_existing_todos.delay(instance.user.id)
