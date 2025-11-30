import logging
from typing import Any, Dict, List, Optional

from celery import current_app
from celery.exceptions import CeleryError
from django.utils import timezone
from django.db import DatabaseError
from googleapiclient.errors import HttpError
from rest_framework.exceptions import ValidationError

from apps.auth_app.models import User
from apps.notification_app.models import Notification
from apps.todo_app.config import MAX_REMINDERS, TEACHER_DEFAULT_REMINDERS, DEAN_DEFAULT_REMINDERS, ALLOWED_MINUTES
from apps.todo_app.models import ToDo
from apps.todo_app.calendar.services import GoogleCalendarService
from core.exceptions import GoogleCalendarAuthRequired
from core.mixins import ErrorResponseMixin

logger = logging.getLogger(__name__)


def normalize_reminders_permissive(reminders: Optional[List[Dict[str, any]]]) -> List[Dict[str, int]]:
    """
    Permissively normalize a list of reminder dicts.

    This function converts reminder entries to a uniform form (``{'method': str, 'minutes': int}``),
    silently skips invalid or non-dict entries and removes duplicates, sorts by minutes ascending and
    limits the result to ``MAX_REMINDERS``.

    :param reminders: List of reminder dicts to normalize.
    :type reminders: Optional[List[Dict[str, Any]]]
    :return: A list of normalized reminder dicts (each with keys ``method`` and ``minutes``).
    :rtype: List[Dict[str, int]]
    :raises ValidationError: If no valid reminders remain after filtering.
    """
    if not reminders:
        return []

    normalized: List[Dict[str, int]] = []
    seen = set()

    for idx, r in enumerate(reminders):
        if not isinstance(r, dict):
            continue

        method = r.get('method')
        minutes = r.get('minutes')

        try:
            minutes_int = int(minutes)
        except (TypeError, ValueError):
            continue

        pair = (method, minutes_int)
        if pair in seen:
            continue

        seen.add(pair)
        normalized.append({'method': method, 'minutes': minutes_int})

        if len(normalized) >= MAX_REMINDERS * 2:
            break

    if not normalized:
        raise ValidationError({'reminders': 'All provided reminders are invalid.'})

    normalized.sort(key=lambda x: x['minutes'])
    return normalized[:MAX_REMINDERS]


def get_user_reminders(user: Any,
                       initial: Optional[Dict[str, Any]],
                       reminders: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """
    Determine which reminders should be used for a user's To Do.

    :param user: User object which is expected to have a ``role`` attribute.
    :param initial: Raw request data used to determine whether the ``reminders`` field was provided.
    :param reminders: Optional list of reminders provided by the caller. Returned unchanged when
                      ``reminders`` key is present in ``initial`` (or as an empty list when falsy).
    :return: A list of reminders ready to be used by the application (maybe empty).
    """

    if 'reminders' in (initial or {}):
        return reminders or []

    role = getattr(user, 'role', None)
    if role == 'teacher':
        return TEACHER_DEFAULT_REMINDERS
    if role == 'dean':
        return DEAN_DEFAULT_REMINDERS

    return []


def normalize_reminders_for_fallback(reminders: Optional[List[Dict[str, Any]]],
                                     allowed_minutes: Optional[List[int]] = None,
                                     limit: int = 5) -> List[int]:
    """
    Normalize and filter reminder dicts for fallback notification scheduling.

    This function extracts the 'minutes' value from each reminder dict, ensures it is an integer,
    filters out invalid or duplicate values, and only includes those minutes present in the allowed_minutes set.
    The result is a list of unique, valid reminder times (in minutes), up to the specified limit.

    :param reminders: List of reminder dicts, each expected to have a 'minutes' key.
    :type reminders: Optional[List[Dict[str, Any]]]
    :param allowed_minutes: List of allowed minute values. Only reminders with 'minutes' in this list are included.
        If None, uses the default ALLOWED_MINUTES from config.
    :type allowed_minutes: Optional[List[int]]
    :param limit: Maximum number of reminders to return.
    :type limit: int
    :return: List of unique, valid reminder times (in minutes), filtered and limited.
    :rtype: List[int]
    """
    if not reminders:
        return []

    allowed = set(allowed_minutes) if allowed_minutes is not None else set(ALLOWED_MINUTES)
    seen = set()
    out: List[int] = []

    for idx, r in enumerate(reminders if isinstance(reminders, list) else []):
        if not isinstance(r, dict):
            logger.debug("Skipping non-dict reminder at index %s: %r", idx, r)
            continue

        minutes = r.get('minutes')
        try:
            minutes_int = int(minutes)
        except (TypeError, ValueError):
            logger.debug("Skipping reminder at index %s due to invalid minutes: %r", idx, minutes)
            continue

        if minutes_int <= 0 or minutes_int not in allowed:
            logger.debug("Skipping reminder at index %s due to disallowed minutes: %s", idx, minutes_int)
            continue

        if minutes_int in seen:
            logger.debug("Skipping duplicate reminder minutes at index %s: %s", idx, minutes_int)
            continue

        seen.add(minutes_int)
        out.append(minutes_int)

        if len(out) >= limit:
            break

    return out


def create_notification_safe(user: User, title: str, message: str, note_context: str):
    try:
        Notification.objects.create(
            user=user,
            title=title,
            message=message,
            type=Notification.Type.TELEGRAM,
        )
    except DatabaseError as exc:
        logger.exception("Failed to create notification %s for todo id=%s: %s", note_context,
                         getattr(user, 'id', None), exc)


def get_todo(request: Any, todo_id: int):
    try:
        tid = int(todo_id)
    except (ValueError, TypeError):
        return None, ErrorResponseMixin.format_error(request, 400, "Bad Request",
                                                     f"Invalid todo id: {todo_id}")

    try:
        todo = ToDo.objects.get(id=tid)
    except ToDo.DoesNotExist:
        return None, ErrorResponseMixin.format_error(request, 404, "Not Found",
                                                     f"ToDo with id={tid} not found.")

    return todo, None


def notify_new_assignee_and_cleanup_old(todo: ToDo, old_assignee: Optional[User]):
    new_assignee = getattr(todo, 'assignee', None)

    if (new_assignee and
            (old_assignee is None or getattr(old_assignee, 'id', None) != getattr(new_assignee, 'id', None))):
        create_notification_safe(
            new_assignee,
            "Вас назначили на задачу",
            f'Вам назначена задача: "{todo.title}".\n\nЧтобы просмотреть детали, перейдите в раздел "📝 Мои задачи".',
            "new_assignee"
        )

    if old_assignee and new_assignee and getattr(old_assignee, 'id', None) != getattr(new_assignee, 'id', None):
        try:
            old_assignee_service = GoogleCalendarService(user=old_assignee)
            if getattr(old_assignee_service, 'service', None):
                if getattr(old_assignee_service, 'delete_event', None):
                    old_assignee_service.delete_event()
            else:
                try:
                    pending = Notification.objects.filter(user=old_assignee, todo=todo,
                                                          status=Notification.Status.PENDING)
                    for n in pending:
                        try:
                            if n.celery_task_id:
                                current_app.control.revoke(n.celery_task_id, terminate=False)
                        except (CeleryError, RuntimeError) as e:
                            logger.warning("Failed to revoke celery task %s for notification %s: %s",
                                           n.celery_task_id, n.id, e)

                        n.status = Notification.Status.FAILED
                        n.last_error = 'Assignee changed and no calendar integration; fallback disabled.'
                        n.celery_task_id = None
                        n.save(update_fields=['status', 'last_error', 'celery_task_id'])
                except Exception as e:
                    logger.exception("Failed to mark fallback notifications as failed for todo id=%s: %s",
                                     getattr(todo, 'id', None), e)
        except (HttpError, GoogleCalendarAuthRequired, ValueError, TypeError, RuntimeError) as exc:
            logger.exception("Failed to delete calendar event for old assignee for todo id=%s: %s",
                             getattr(todo, 'id', None), exc)


def cancel_pending_notifications_for_user(todo: ToDo, user: User, reason: str = 'Reminders updated') -> None:
    try:
        logger.debug("cancel_pending_notifications_for_user called for todo=%s user=%s reason=%s",
                     getattr(todo, 'id', None), getattr(user, 'id', None), reason)
        now = timezone.now()
        pending_qs = Notification.objects.filter(user=user, todo=todo, status=Notification.Status.PENDING)
        pending_qs = pending_qs.filter(scheduled_for__isnull=False, scheduled_for__gt=now)
        for n in pending_qs:
            logger.debug("Cancelling notification id=%s scheduled_for=%s celery_id=%s",
                         getattr(n, 'id', None), getattr(n, 'scheduled_for', None), getattr(n, 'celery_task_id', None))
            try:
                if n.celery_task_id:
                    try:
                        current_app.control.revoke(n.celery_task_id, terminate=False)
                    except Exception as e:
                        logger.warning("Failed to revoke celery task %s for notification %s: %s",
                                       n.celery_task_id, n.id, e)
            except (CeleryError, RuntimeError) as e:
                logger.warning("Failed to revoke celery task %s for notification %s: %s",
                               n.celery_task_id, n.id, e)

            n.status = Notification.Status.FAILED
            n.last_error = f"{reason}."
            n.celery_task_id = None
            n.save(update_fields=['status', 'last_error', 'celery_task_id'])
    except Exception as e:
        logger.exception("Failed to cancel pending notifications for todo id=%s user=%s: %s",
                         getattr(todo, 'id', None), getattr(user, 'id', None), e)
