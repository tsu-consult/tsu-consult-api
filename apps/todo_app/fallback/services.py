import logging
from datetime import timedelta
from typing import Optional, Tuple

from celery.exceptions import CeleryError
from django.utils import timezone

from apps.notification_app.models import Notification
from apps.todo_app.config import ALLOWED_MINUTES
from apps.todo_app.utils import normalize_reminders_for_fallback

logger = logging.getLogger(__name__)


class FallbackReminderService:
    def __init__(self, allowed_minutes: Optional[list[int]] = None, max_reminders: int = 5):
        self.allowed_minutes = allowed_minutes if allowed_minutes is not None else ALLOWED_MINUTES
        self.max_reminders = max_reminders

    @staticmethod
    def _russian_plural(n: int, forms: Tuple[str, str, str]) -> str:
        n_abs = abs(n)
        last_two = n_abs % 100
        last = n_abs % 10
        if 11 <= last_two <= 14:
            return forms[2]
        if last == 1:
            return forms[0]
        if 2 <= last <= 4:
            return forms[1]
        return forms[2]

    def humanize_minutes(self, m: int) -> str:
        if m <= 0:
            return "0 минут"

        if m % 10080 == 0:
            w = m // 10080
            if w == 1:
                return "неделю"
            return f"{w} " + self._russian_plural(w, ("неделю", "недели", "недель"))

        if m % 1440 == 0:
            d = m // 1440
            if d == 1:
                return "сутки"
            return f"{d} суток"

        if m % 60 == 0:
            h = m // 60
            form = self._russian_plural(h, ("час", "часа", "часов"))
            return f"{h} {form}"

        form = self._russian_plural(m, ("минуту", "минуты", "минут"))
        return f"{m} {form}"

    def schedule_fallback_reminders(self, todo, reminders, target_user):
        if not reminders or not getattr(todo, "deadline", None):
            return

        logger.debug(
            "schedule_fallback_reminders called with todo=%s target_user=%s reminders=%r",
            getattr(todo, 'id', None), getattr(target_user, 'id', None) if target_user else None, reminders
        )

        now = timezone.now()

        unique_reminders = normalize_reminders_for_fallback(reminders, self.allowed_minutes, self.max_reminders)

        for minutes_int in unique_reminders:
            notify_at = todo.deadline - timedelta(minutes=minutes_int)

            if notify_at <= now:
                continue

            interval_str = self.humanize_minutes(minutes_int)
            n = Notification.objects.create(
                user=target_user,
                todo=todo,
                title="Напоминание о задаче",
                message=f'Через {interval_str} наступает дедлайн задачи "{todo.title}".',
                type=Notification.Type.TELEGRAM,
                status=Notification.Status.PENDING,
                scheduled_for=notify_at,
            )
            try:
                from apps.notification_app.tasks import send_notification_task
                celery_task = send_notification_task.apply_async(args=[n.id], eta=notify_at)
                n.celery_task_id = celery_task.id
                n.save(update_fields=["celery_task_id"])
                logger.info("Scheduled deferred notification %s for todo %s at %s", n.id, todo.id, notify_at)
            except (CeleryError, RuntimeError) as e:
                logger.exception("Failed scheduling deferred notification %s for todo %s: %s", n.id, todo.id, e)
