import logging
from datetime import timedelta

from celery import shared_task
from django.contrib.auth import get_user_model
from django.db import DatabaseError, IntegrityError
from django.utils import timezone
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from requests.exceptions import RequestException
from rest_framework.exceptions import ValidationError

from apps.notification_app.models import Notification
from apps.notification_app.services import send_telegram_notification
from apps.todo_app.models import ToDo
from apps.todo_app.services import FallbackReminderService
from apps.todo_app.services import GoogleCalendarService
from apps.todo_app.utils import normalize_reminders_for_fallback
from apps.todo_app.utils import normalize_reminders_permissive
from core.exceptions import GoogleCalendarAuthRequired, EventNotFound
from celery.exceptions import CeleryError
from kombu.exceptions import OperationalError as KombuOperationalError
from redis.exceptions import ConnectionError as RedisConnectionError

logger = logging.getLogger(__name__)

User = get_user_model()


def _save_last_sync_error(todo, e):
    todo.last_sync_error = str(e)
    try:
        todo.save(update_fields=["last_sync_error"])
    except DatabaseError as exc:
        logger.exception("Failed saving last_sync_error for todo %s: %s", getattr(todo, 'id', None), exc)


def _safe_find_event(calendar_service, todo):
    if not hasattr(calendar_service, 'find_event_for_todo'):
        return None
    try:
        return calendar_service.find_event_for_todo(todo)
    except (HttpError, RequestException, ValueError, TypeError) as exc:
        logger.debug("safe_find_event failed for todo %s: %s", getattr(todo, 'id', None), exc)
        return None


def _handle_auth_required(todo, user_ident, role, e):
    _save_last_sync_error(todo, e)
    logger.info("Google auth required for user %s, todo %s (%s): %s", user_ident, todo.id, role, e)


def _handle_request_and_retry(task_self, todo, user_ident, role, e):
    _save_last_sync_error(todo, e)
    logger.warning("Network error while creating event for todo %s (%s) for user %s: %s",
                   todo.id, role, user_ident, e)
    retries = getattr(task_self.request, 'retries', 0) if task_self else 0
    countdown = min(2 ** retries * 60, 3600)
    if task_self:
        raise task_self.retry(exc=e, countdown=countdown)
    raise e


def _handle_http_error(todo, user_ident, role, e):
    _save_last_sync_error(todo, e)
    logger.exception("Google API error while creating event for todo %s (%s) for user %s: %s",
                     todo.id, role, user_ident, e)


def _handle_invalid_data(todo, user_ident, role, e):
    _save_last_sync_error(todo, e)
    logger.exception("Invalid data while creating event for todo %s (%s) for user %s: %s",
                     todo.id, role, user_ident, e)


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
        except (ValueError, TypeError, RuntimeError) as e:
            logger.exception("Failed to send notification %s: %s", notification_id, e)
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
        except (ValueError, TypeError, RuntimeError) as e:
            n.last_error = str(e)
            n.save(update_fields=["last_error"])
            logger.exception("Error retrying notification %s: %s", n.id, e)


@shared_task(bind=True, max_retries=5, default_retry_delay=60)
def sync_existing_todos(self, user_id):
    logger.info("sync_existing_todos start for user_id=%s, retries=%s", user_id, getattr(self.request, 'retries', 0))

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.warning("User with id=%s does not exist", user_id)
        return
    except (ValueError, TypeError):
        logger.exception("Invalid user_id passed to sync_existing_todos: %s", user_id)
        return

    creator_qs = ToDo.objects.filter(creator=user)
    assignee_qs = ToDo.objects.filter(assignee=user)

    processed = set()

    for todo in creator_qs:
        if todo.id in processed:
            continue
        processed.add(todo.id)

        if not todo.deadline:
            continue

        reminders = normalize_reminders_permissive(todo.reminders)
        calendar_service = GoogleCalendarService(user)

        if not calendar_service.service:
            todo.last_sync_error = "no_calendar_service"
            todo.save(update_fields=["last_sync_error"])
            logger.info("No calendar service for user %s when syncing todo %s (creator)", user_id, todo.id)
            continue

        try:
            if todo.calendar_event_id:
                try:
                    calendar_service.get_event(todo.calendar_event_id)
                    if not getattr(todo, 'calendar_event_active', True):
                        todo.calendar_event_active = True
                        todo.save(update_fields=['calendar_event_active'])
                        logger.info("Re-activated existing calendar event for todo %s (creator)", todo.id)
                    else:
                        logger.debug("Existing calendar event verified for todo %s (creator)", todo.id)
                    continue
                except EventNotFound:
                    try:
                        existing = _safe_find_event(calendar_service, todo)
                    except (HttpError, RequestException, ValueError, TypeError) as exc:
                        existing = None
                        logger.debug(
                            "find_event_for_todo raised while handling EventNotFound for todo %s: %s",
                            todo.id, exc,
                        )

                    if existing:
                        eid = existing.get('id')
                        todo.calendar_event_id = eid
                        todo.calendar_event_active = True
                        todo.save(update_fields=['calendar_event_id', 'calendar_event_active'])
                        logger.info("Found existing calendar event for todo %s -> calendar_event_id=%s (creator)",
                                    todo.id, eid)
                        continue

                    try:
                        event_id = calendar_service.create_event(todo, reminders)
                        if event_id:
                            todo.calendar_event_id = event_id
                            todo.calendar_event_active = True
                            todo.save(update_fields=['calendar_event_id', 'calendar_event_active'])
                            logger.info(
                                "Re-created calendar event for todo %s -> calendar_event_id=%s (creator)",
                                todo.id, event_id,
                            )
                            continue
                        todo.last_sync_error = "create_event_failed"
                        todo.save(update_fields=["last_sync_error"])
                        logger.warning("create_event returned None for todo %s (creator) user %s", todo.id, user_id)
                        continue
                    except (RefreshError, GoogleCalendarAuthRequired) as e:
                        _handle_auth_required(todo, user_id, 'creator', e)
                        continue
                    except RequestException as e:
                        _handle_request_and_retry(self, todo, user_id, 'creator', e)
                    except HttpError as e:
                        _handle_http_error(todo, user_id, 'creator', e)
                        continue
                    except (ValueError, TypeError) as e:
                        _handle_invalid_data(todo, user_id, 'creator', e)
                        continue
            else:
                try:
                    try:
                        existing = _safe_find_event(calendar_service, todo)
                    except (HttpError, RequestException, ValueError, TypeError) as exc:
                        existing = None
                        logger.debug("find_event_for_todo raised while syncing todo %s (creator): %s", todo.id, exc)

                    if existing:
                        eid = existing.get('id')
                        todo.calendar_event_id = eid
                        todo.calendar_event_active = True
                        todo.save(update_fields=['calendar_event_id', 'calendar_event_active'])
                        logger.info("Found existing calendar event for todo %s -> calendar_event_id=%s (creator)",
                                    todo.id, eid)
                        continue
                    try:
                        event_id = calendar_service.create_event(todo, reminders)
                        if event_id:
                            todo.calendar_event_id = event_id
                            todo.calendar_event_active = True
                            todo.save(update_fields=['calendar_event_id', 'calendar_event_active'])
                            logger.info(
                                "Created calendar event for todo %s -> calendar_event_id=%s (creator)",
                                todo.id, event_id,
                            )
                            continue
                        todo.last_sync_error = "create_event_failed"
                        todo.save(update_fields=["last_sync_error"])
                        logger.warning("create_event returned None for todo %s (creator) user %s", todo.id, user_id)
                        continue
                    except (RefreshError, GoogleCalendarAuthRequired) as e:
                        _handle_auth_required(todo, user_id, 'creator', e)
                        continue
                    except RequestException as e:
                        _handle_request_and_retry(self, todo, user_id, 'creator', e)
                    except HttpError as e:
                        _handle_http_error(todo, user_id, 'creator', e)
                        continue
                    except (ValueError, TypeError) as e:
                        _handle_invalid_data(todo, user_id, 'creator', e)
                        continue
                except RequestException as e:
                    _handle_request_and_retry(self, todo, user_id, 'creator', e)
                except HttpError as e:
                    _handle_http_error(todo, user_id, 'creator', e)
                    continue
                except (ValueError, TypeError) as e:
                    _handle_invalid_data(todo, user_id, 'creator', e)
                    continue

        except (RefreshError, GoogleCalendarAuthRequired) as e:
            _handle_auth_required(todo, user_id, 'creator', e)
            continue
        except RequestException as e:
            _handle_request_and_retry(self, todo, user_id, 'creator', e)
        except HttpError as e:
            _handle_http_error(todo, user_id, 'creator', e)
            continue
        except (ValueError, TypeError) as e:
            _handle_invalid_data(todo, user_id, 'creator', e)
            continue

    for todo in assignee_qs:
        if todo.id in processed:
            continue
        processed.add(todo.id)

        if not todo.deadline:
            continue

        reminders = normalize_reminders_permissive(todo.assignee_reminders)
        calendar_service = GoogleCalendarService(todo.assignee)

        if not calendar_service.service:
            todo.last_sync_error = "no_calendar_service"
            todo.save(update_fields=["last_sync_error"])
            logger.info("No calendar service for assignee %s when syncing todo %s",
                        getattr(todo.assignee, 'id', None), todo.id)
            continue

        try:
            if todo.assignee_calendar_event_id:
                try:
                    calendar_service.get_event(todo.assignee_calendar_event_id)
                    if not getattr(todo, 'assignee_calendar_event_active', True):
                        todo.assignee_calendar_event_active = True
                        todo.save(update_fields=['assignee_calendar_event_active'])
                        logger.info("Re-activated existing calendar event for todo %s (assignee)", todo.id)
                    else:
                        logger.debug("Existing calendar event verified for todo %s (assignee)", todo.id)
                    continue
                except EventNotFound:
                    try:
                        existing = _safe_find_event(calendar_service, todo)
                    except (HttpError, RequestException, ValueError, TypeError) as exc:
                        existing = None
                        logger.debug("find_event_for_todo raised while handling EventNotFound for assignee todo %s: %s",
                                     todo.id, exc)

                    if existing:
                        eid = existing.get('id')
                        todo.assignee_calendar_event_id = eid
                        todo.assignee_calendar_event_active = True
                        todo.save(update_fields=['assignee_calendar_event_id', 'assignee_calendar_event_active'])
                        logger.info("Found existing calendar event for todo %s -> assignee_calendar_event_id=%s ("
                                    "assignee)", todo.id, eid)
                        continue

                    try:
                        event_id = calendar_service.create_event(todo, reminders)
                        if event_id:
                            todo.assignee_calendar_event_id = event_id
                            todo.assignee_calendar_event_active = True
                            todo.save(update_fields=['assignee_calendar_event_id', 'assignee_calendar_event_active'])
                            logger.info(
                                "Re-created calendar event for todo %s -> assignee_calendar_event_id=%s (assignee)",
                                todo.id, event_id,
                            )
                            continue
                        todo.last_sync_error = "create_event_failed"
                        todo.save(update_fields=["last_sync_error"])
                        logger.warning("create_event returned None for todo %s (assignee) user %s",
                                       todo.id, getattr(todo.assignee, 'id', None))
                        continue
                    except (RefreshError, GoogleCalendarAuthRequired) as e:
                        todo.last_sync_error = str(e)
                        todo.save(update_fields=["last_sync_error"])
                        logger.info("Google auth required for assignee %s, todo %s: %s",
                                    getattr(todo.assignee, 'id', None), todo.id, e)
                        continue
                    except RequestException as e:
                        todo.last_sync_error = str(e)
                        todo.save(update_fields=["last_sync_error"])
                        logger.warning("Network error while creating event for todo %s (assignee) for user %s: %s",
                                       todo.id, getattr(todo.assignee, 'id', None), e)
                        retries = getattr(self.request, 'retries', 0)
                        countdown = min(2 ** retries * 60, 3600)
                        raise self.retry(exc=e, countdown=countdown)
                    except HttpError as e:
                        todo.last_sync_error = str(e)
                        todo.save(update_fields=["last_sync_error"])
                        logger.exception(
                            "Google API error while creating event for todo %s (assignee) for user %s: %s",
                            todo.id, getattr(todo.assignee, 'id', None), e,
                        )
                        continue
                    except (ValueError, TypeError) as e:
                        todo.last_sync_error = str(e)
                        todo.save(update_fields=["last_sync_error"])
                        logger.exception(
                            "Invalid data while creating event for todo %s (assignee) for user %s: %s",
                            todo.id, getattr(todo.assignee, 'id', None), e,
                        )
                        continue
            else:
                try:
                    event_id = calendar_service.create_event(todo, reminders)
                    if event_id:
                        todo.assignee_calendar_event_id = event_id
                        todo.assignee_calendar_event_active = True
                        todo.save(update_fields=["assignee_calendar_event_id", 'assignee_calendar_event_active'])
                        logger.info("Synced todo %s -> assignee_calendar_event_id=%s", todo.id, event_id)
                    else:
                        todo.last_sync_error = "create_event_failed"
                        todo.save(update_fields=["last_sync_error"])
                        logger.warning("create_event returned None for todo %s (assignee) user %s",
                                       todo.id, getattr(todo.assignee, 'id', None))
                        continue
                except (RefreshError, GoogleCalendarAuthRequired) as e:
                    todo.last_sync_error = str(e)
                    todo.save(update_fields=["last_sync_error"])
                    try:
                        existing = _safe_find_event(calendar_service, todo)
                    except (HttpError, RequestException, ValueError, TypeError):
                        existing = None

                    if existing:
                        eid = existing.get('id')
                        todo.assignee_calendar_event_id = eid
                        todo.assignee_calendar_event_active = True
                        todo.save(update_fields=["assignee_calendar_event_id", 'assignee_calendar_event_active'])
                        logger.info("Found existing calendar event for todo %s -> assignee_calendar_event_id=%s ("
                                    "assignee)", todo.id, eid)
                        continue

                    logger.exception("Google API error while syncing todo %s for assignee %s: %s", todo.id,
                                     getattr(todo.assignee, 'id', None), e)
                    continue
                except (ValueError, TypeError) as e:
                    todo.last_sync_error = str(e)
                    todo.save(update_fields=["last_sync_error"])
                    logger.exception("Invalid data while syncing todo %s for assignee %s: %s", todo.id,
                                     getattr(todo.assignee, 'id', None), e)
                    continue
        except (RefreshError, GoogleCalendarAuthRequired) as e:
            todo.last_sync_error = str(e)
            todo.save(update_fields=["last_sync_error"])
            logger.info("Google auth required for assignee %s, todo %s: %s",
                        getattr(todo.assignee, 'id', None), todo.id, e)
            continue
        except RequestException as e:
            todo.last_sync_error = str(e)
            todo.save(update_fields=["last_sync_error"])
            logger.warning("Network error while syncing todo %s for assignee %s: %s", todo.id,
                           getattr(todo.assignee, 'id', None), e)
            retries = getattr(self.request, 'retries', 0)
            countdown = min(2 ** retries * 60, 3600)
            raise self.retry(exc=e, countdown=countdown)
        except HttpError as e:
            todo.last_sync_error = str(e)
            todo.save(update_fields=["last_sync_error"])
            logger.exception("Google API error while syncing todo %s for assignee %s: %s", todo.id,
                             getattr(todo.assignee, 'id', None), e)
            continue
        except (ValueError, TypeError) as e:
            todo.last_sync_error = str(e)
            todo.save(update_fields=["last_sync_error"])
            logger.exception("Invalid data while syncing todo %s for assignee %s: %s", todo.id,
                             getattr(todo.assignee, 'id', None), e)
            continue

    logger.info("sync_existing_todos finished for user_id=%s", user_id)


@shared_task
def transfer_unsent_reminders_task(user_id: int):
    now = timezone.now()

    try:
        creator_qs = ToDo.objects.filter(creator_id=user_id, deadline__gt=now, calendar_event_id__isnull=False)
        assignee_qs = ToDo.objects.filter(assignee_id=user_id, deadline__gt=now,
                                          assignee_calendar_event_id__isnull=False)
    except DatabaseError as e:
        logger.exception("Failed to query ToDo for user %s while transferring reminders: %s", user_id, e)
        return

    def _process_todo_for_role(todo, role):
        if role == 'creator':
            reminders_raw = getattr(todo, 'reminders', None)
            target_user_id = todo.creator_id
        else:
            reminders_raw = getattr(todo, 'assignee_reminders', None)
            target_user_id = todo.assignee_id

        try:
            unique_minutes = normalize_reminders_for_fallback(reminders_raw)
        except ValidationError:
            logger.debug("No valid reminders for todo %s when transferring for user %s",
                         getattr(todo, 'id', '<unknown>'), user_id)
            unique_minutes = []

        if not unique_minutes:
            logger.debug("Skipping todo %s: no reminders to transfer", getattr(todo, 'id', '<unknown>'))
            return

        frs = FallbackReminderService()
        for minutes_int in unique_minutes:
            scheduled = getattr(todo, 'deadline', None)
            if scheduled is None:
                continue
            scheduled_for = scheduled - timedelta(minutes=minutes_int)

            if scheduled_for <= now:
                remaining_seconds = (todo.deadline - now).total_seconds()
                if remaining_seconds <= 0:
                    logger.debug(
                        "Skipping overdue reminder for todo %s: deadline passed (now=%s, deadline=%s)",
                        getattr(todo, 'id', None), now, getattr(todo, 'deadline', None),
                    )
                    continue

                remaining_minutes = int(__import__('math').ceil(remaining_seconds / 60))
                interval_str = frs.humanize_minutes(remaining_minutes)
                message = f'Через {interval_str} наступает дедлайн задачи "{getattr(todo, "title", "")}".'

                try:
                    n = Notification.objects.create(
                        user_id=target_user_id,
                        title=getattr(todo, 'title', 'Напоминание о задаче'),
                        message=message,
                        type=Notification.Type.TELEGRAM,
                        status=Notification.Status.PENDING,
                        scheduled_for=None,
                    )
                except (IntegrityError, DatabaseError) as exc:
                    logger.exception("Failed to create Notification for todo %s user %s: %s",
                                     getattr(todo, 'id', '<unknown>'), target_user_id, exc)
                    continue
                try:
                    send_notification_task.delay(n.id)
                    logger.info("Scheduled immediate notification %s for todo %s", n.id, getattr(todo, 'id', None))
                except (KombuOperationalError, RedisConnectionError, CeleryError, RuntimeError) as exc:
                    logger.exception(
                        "Failed scheduling immediate notification %s for todo %s: %s",
                        n.id, getattr(todo, 'id', None), exc
                    )
                continue

            interval_str = frs.humanize_minutes(minutes_int)
            message = f'Через {interval_str} наступает дедлайн задачи "{getattr(todo, "title", "")}".'
            try:
                n = Notification.objects.create(
                    user_id=target_user_id,
                    title=getattr(todo, 'title', 'Напоминание о задаче'),
                    message=message,
                    type=Notification.Type.TELEGRAM,
                    status=Notification.Status.PENDING,
                    scheduled_for=scheduled_for,
                )
            except (IntegrityError, DatabaseError) as exc:
                logger.exception("Failed to create Notification for todo %s user %s: %s",
                                 getattr(todo, 'id', '<unknown>'), target_user_id, exc)
                continue

            try:
                send_notification_task.apply_async(args=[n.id], eta=scheduled_for)
                logger.info("Scheduled deferred notification %s for todo %s at %s",
                            n.id, getattr(todo, 'id', None), scheduled_for)
            except (KombuOperationalError, RedisConnectionError, CeleryError, RuntimeError) as exc:
                logger.exception("Failed scheduling deferred notification %s for todo %s: %s",
                                 n.id, getattr(todo, 'id', None), exc)

        active_field = 'calendar_event_active' if role == 'creator' else 'assignee_calendar_event_active'
        id_field = 'calendar_event_id' if role == 'creator' else 'assignee_calendar_event_id'
        try:
            if hasattr(todo, active_field):
                if hasattr(todo, id_field):
                    setattr(todo, id_field, None)
                setattr(todo, active_field, False)
                update_fields = []
                if hasattr(todo, id_field):
                    update_fields.append(id_field)
                update_fields.append(active_field)
                todo.save(update_fields=update_fields)
        except DatabaseError as exc:
            logger.exception("Failed to set inactive flag %s for todo %s: %s", active_field,
                             getattr(todo, 'id', '<unknown>'), exc)

    for t in creator_qs:
        try:
            _process_todo_for_role(t, 'creator')
        except (DatabaseError, IntegrityError, RequestException, HttpError, RefreshError, GoogleCalendarAuthRequired,
                CeleryError, ValueError, TypeError, RuntimeError) as e:
            logger.exception("Unexpected error while transferring reminders for todo %s user %s: %s",
                             getattr(t, 'id', '<unknown>'), user_id, e)

    for t in assignee_qs:
        try:
            _process_todo_for_role(t, 'assignee')
        except (DatabaseError, IntegrityError, RequestException, HttpError, RefreshError, GoogleCalendarAuthRequired,
                CeleryError, ValueError, TypeError, RuntimeError) as e:
            logger.exception("Unexpected error while transferring reminders for todo %s user %s: %s",
                             getattr(t, 'id', '<unknown>'), user_id, e)

    logger.info("transfer_unsent_reminders_task finished for user_id=%s", user_id)
