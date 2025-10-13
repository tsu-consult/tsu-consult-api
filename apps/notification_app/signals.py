from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.auth_app.models import TeacherApproval
from apps.notification_app.models import Notification
from apps.notification_app.tasks import send_notification_task


@receiver(post_save, sender=Notification)
def trigger_notification_send(sender, instance, created, **kwargs):
    if created and instance.status == Notification.Status.PENDING:
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
            reason_text = f" Причина: {instance.reason}" if instance.reason else ""
            message = f"К сожалению, ваша заявка на подтверждение преподавателя была отклонена.{reason_text}"
        
        Notification.objects.create(
            user=user,
            title=title,
            message=message,
            status=Notification.Status.PENDING,
        )