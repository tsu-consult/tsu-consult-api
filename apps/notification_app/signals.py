import logging

from django.conf import settings
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.utils import timezone
from celery.exceptions import CeleryError

from apps.auth_app.models import TeacherApproval
from apps.notification_app.models import Notification
from apps.notification_app.tasks import (send_notification_task, sync_existing_todos, transfer_unsent_reminders_task,
                                         cancel_pending_fallbacks_for_user)
from apps.profile_app.models import GoogleToken

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Notification)
def trigger_notification_send(sender, instance, created, **kwargs):
    if not getattr(settings, "NOTIFICATIONS_DELIVERY_ENABLED", True):
        return
    if created and instance.status == Notification.Status.PENDING:
        if instance.scheduled_for and instance.scheduled_for > timezone.now():
            return
        try:
            send_notification_task.delay(instance.id)
        except CeleryError as e:
            logger.warning("Failed to enqueue send_notification_task for notification %s: %s",
                           getattr(instance, 'id', None), e)
        except RuntimeError as e:
            logger.warning("Failed to enqueue send_notification_task for notification %s (runtime error): %s",
                           getattr(instance, 'id', None), e)


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
def sync_after_integration(sender, instance, **kwargs):
    try:
        cancel_pending_fallbacks_for_user.delay(instance.user.id)
    except CeleryError as e:
        logger.warning("Failed to enqueue cancel_pending_fallbacks_for_user for user %s: %s",
                       getattr(instance.user, 'id', None), e)
    except RuntimeError as e:
        logger.warning("Failed to enqueue cancel_pending_fallbacks_for_user for user %s (runtime): %s",
                       getattr(instance.user, 'id', None), e)
    try:
        sync_existing_todos.delay(instance.user.id)
    except CeleryError as e:
        logger.warning("Failed to enqueue sync_existing_todos for user %s: %s",
                       getattr(instance.user, 'id', None), e)
    except RuntimeError as e:
        logger.warning("Failed to enqueue sync_existing_todos for user %s (runtime): %s",
                       getattr(instance.user, 'id', None), e)


@receiver(post_delete, sender=GoogleToken)
def transfer_unsent_reminders_on_disconnect(sender, instance, **kwargs):
    try:
        transfer_unsent_reminders_task.delay(instance.user.id)
    except CeleryError as e:
        logger.warning("Failed to enqueue transfer_unsent_reminders_task for user %s: %s",
                       getattr(instance.user, 'id', None), e)
    except RuntimeError as e:
        logger.warning("Failed to enqueue transfer_unsent_reminders_task for user %s (runtime): %s",
                       getattr(instance.user, 'id', None), e)
