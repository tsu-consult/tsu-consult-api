from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from apps.consultation_app.models import Consultation
from apps.notification_app.models import Notification
from apps.notification_app.tasks import send_notification_task

@receiver(post_save, sender=Notification)
def trigger_notification_send(sender, instance, created, **kwargs):
    if created and instance.status == Notification.Status.PENDING:
        send_notification_task.delay(instance.id)


@receiver([post_save, post_delete], sender=Consultation)
def notify_subscribers_on_consultation_change(sender, instance, **kwargs):
    teacher = instance.teacher
    subscriptions = teacher.subscribers.all().select_related("student")

    for sub in subscriptions:
        student = sub.student
        Notification.objects.create(
            user=student,
            title=f"Изменение расписания преподавателя {teacher.get_full_name()}",
            message=f"Консультация '{instance.title}' была обновлена. "
                    f"Дата: {instance.date}, время: {instance.start_time}-{instance.end_time}.",
            type=Notification.Type.TELEGRAM,
        )