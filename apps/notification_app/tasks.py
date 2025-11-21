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


def _save_last_sync_error(todo: ToDo, exc):
    try:
        todo.last_sync_error = str(exc)
        todo.save(update_fields=["last_error"])
    except Exception as e:
        logger.exception("Failed to save last_sync_error for todo %s: %s", getattr(todo, "id", None), e)


def _ensure_future_deadline(todo: ToDo) -> bool:
    if not getattr(todo, "deadline", None):
        return False
    now = timezone.now()
    return todo.deadline > now


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
def sync_existing_todos(self, user_id: int):
    logger.info("sync_existing_todos start for user_id=%s retries=%s",
                user_id, getattr(self.request, "retries", 0))
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.warning("User %s does not exist, abort sync_existing_todos", user_id)
        return
    except Exception as exc:
        logger.exception("Unexpected error loading user %s: %s", user_id, exc)
        return

    creator_qs = ToDo.objects.filter(creator=user)
    assignee_qs = ToDo.objects.filter(assignee=user)
    processed = set()

    def process_role(td: ToDo, role: str, participant_user):
        if td.id in processed:
            return
        processed.add(td.id)

        if not _ensure_future_deadline(td):
            logger.debug("skip todo %s: no future deadline", td.id)
            return

        if role == "creator":
            raw_reminders = getattr(td, "creator_reminders", None) if (
                hasattr(td, "creator_reminders")) else getattr(td, "reminders", None)
            event_field = "creator_calendar_event_id" if (
                hasattr(td, "creator_calendar_event_id")) else "calendar_event_id"
            active_field = "creator_calendar_event_active" if (
                hasattr(td, "creator_calendar_event_active")) else "calendar_event_active"
        else:
            raw_reminders = getattr(td, "assignee_reminders", None) if (
                hasattr(td, "assignee_reminders")) else getattr(td, "reminders", None)
            event_field = "assignee_calendar_event_id" if (
                hasattr(td, "assignee_calendar_event_id")) else "calendar_event_id"
            active_field = "assignee_calendar_event_active" if (
                hasattr(td, "assignee_calendar_event_active")) else "calendar_event_active"

        reminders = normalize_reminders_permissive(raw_reminders)

        calendar_service = GoogleCalendarService(user=participant_user)

        if not getattr(calendar_service, "service", None):
            if getattr(td, event_field, None):
                try:
                    setattr(td, event_field, None)
                    if hasattr(td, active_field):
                        setattr(td, active_field, False)
                    update_fields = [f for f in (event_field, active_field) if hasattr(td, f)]
                    td.save(update_fields=update_fields)
                    logger.info("Cleared %s for todo %s because user %s has no calendar service",
                                event_field, td.id, getattr(participant_user, "id", None))
                except Exception as e:
                    logger.exception("Failed clearing calendar fields for todo %s: %s", td.id, e)
            else:
                logger.debug("No calendar service and no event id for todo %s (role=%s)", td.id, role)
            return

        try:
            existing_event_id = getattr(td, event_field, None)
            if existing_event_id:
                try:
                    if hasattr(td, active_field):
                        if not getattr(td, active_field, True):
                            setattr(td, active_field, True)
                            td.save(update_fields=[active_field])
                            logger.info("Re-activated calendar event for todo %s (role=%s)", td.id, role)
                        else:
                            logger.debug("Verified existing event for todo %s (role=%s)", td.id, role)
                    if hasattr(calendar_service, "edit_event"):
                        try:
                            calendar_service.edit_event()
                            # TODO: calendar_service.edit_event(todo, existing_event_id, reminders=reminders)
                        except Exception as e:
                            logger.debug("edit_event failed/absent for todo %s: %s", td.id, e)
                    return
                except EventNotFound:
                    logger.info("Stored event_id %s for todo %s not found in Google, will search or recreate",
                                existing_event_id, td.id)
                    try:
                        found = calendar_service.find_event_for_todo(td) if (
                            hasattr(calendar_service, "find_event_for_todo")) else None
                    except (RequestException, HttpError, ValueError, TypeError) as e:
                        logger.debug("find_event_for_todo failed when handling missing event for todo %s: %s",
                                     td.id, e)
                        found = None

                    if found:
                        eid = found.get("id")
                        try:
                            setattr(td, event_field, eid)
                            if hasattr(td, active_field):
                                setattr(td, active_field, True)
                            update_fields = [f for f in (event_field, active_field) if hasattr(td, f)]
                            td.save(update_fields=update_fields)
                            logger.info("Attached found existing event %s -> todo %s (role=%s)",
                                        eid, td.id, role)
                        except Exception as e:
                            logger.exception("Failed attaching found event id %s to todo %s: %s",
                                             eid, td.id, e)
                        return

                    try:
                        created_id = calendar_service.create_event(td, reminders=reminders)
                        if created_id:
                            setattr(td, event_field, created_id)
                            if hasattr(td, active_field):
                                setattr(td, active_field, True)
                            update_fields = [f for f in (event_field, active_field) if hasattr(td, f)]
                            td.save(update_fields=update_fields)
                            logger.info("Re-created calendar event %s for todo %s (role=%s)",
                                        created_id, td.id, role)
                        else:
                            _save_last_sync_error(td, "create_event_returned_none")
                            logger.warning("create_event returned None for todo %s (role=%s)",
                                           td.id, role)
                        return
                    except RefreshError as e:
                        _save_last_sync_error(td, e)
                        logger.info("RefreshError while recreating event for todo %s (role=%s): %s",
                                    td.id, role, e)
                        return
                    except RequestException as e:
                        _save_last_sync_error(td, e)
                        logger.warning("Network error while recreating event for todo %s (role=%s): %s",
                                       td.id, role, e)
                        retries = getattr(self.request, "retries", 0)
                        countdown = min(2 ** retries * 60, 3600)
                        raise self.retry(exc=e, countdown=countdown)
                    except HttpError as e:
                        _save_last_sync_error(td, e)
                        logger.exception("Google HttpError while recreating event for todo %s (role=%s): %s",
                                         td.id, role, e)
                        return
                    except Exception as e:
                        _save_last_sync_error(td, e)
                        logger.exception("Unexpected error while recreating event for todo %s (role=%s): %s",
                                         td.id, role, e)
                        return
            else:
                try:
                    found = calendar_service.find_event_for_todo(td) if (
                        hasattr(calendar_service, "find_event_for_todo")) else None
                except (RequestException, HttpError, ValueError, TypeError) as e:
                    logger.debug("find_event_for_todo failed while syncing todo %s: %s", td.id, e)
                    found = None

                if found:
                    eid = found.get("id")
                    try:
                        setattr(td, event_field, eid)
                        if hasattr(td, active_field):
                            setattr(td, active_field, True)
                        update_fields = [f for f in (event_field, active_field) if hasattr(td, f)]
                        td.save(update_fields=update_fields)
                        logger.info("Found and attached event %s -> todo %s (role=%s)", eid, td.id, role)
                    except Exception as e:
                        logger.exception("Failed attaching found event id %s to todo %s: %s", eid, td.id, e)
                    return

                try:
                    created_id = calendar_service.create_event(td, reminders=reminders)
                    if created_id:
                        setattr(td, event_field, created_id)
                        if hasattr(td, active_field):
                            setattr(td, active_field, True)
                        update_fields = [f for f in (event_field, active_field) if hasattr(td, f)]
                        td.save(update_fields=update_fields)
                        logger.info("Created calendar event %s for todo %s (role=%s)",
                                    created_id, td.id, role)
                    else:
                        _save_last_sync_error(td, "create_event_returned_none")
                        logger.warning("create_event returned None for todo %s (role=%s)", td.id, role)
                except RefreshError as e:
                    _save_last_sync_error(td, e)
                    logger.info("RefreshError creating event for todo %s (role=%s): %s", td.id, role, e)
                except RequestException as e:
                    _save_last_sync_error(td, e)
                    logger.warning("Network error creating event for todo %s (role=%s): %s",
                                   td.id, role, e)
                    retries = getattr(self.request, "retries", 0)
                    countdown = min(2 ** retries * 60, 3600)
                    raise self.retry(exc=e, countdown=countdown)
                except HttpError as e:
                    _save_last_sync_error(td, e)
                    logger.exception("Google HttpError creating event for todo %s (role=%s): %s",
                                     td.id, role, e)
                except Exception as e:
                    _save_last_sync_error(td, e)
                    logger.exception("Unexpected error creating event for todo %s (role=%s): %s",
                                     td.id, role, e)
        except (RefreshError, GoogleCalendarAuthRequired) as e:
            _save_last_sync_error(td, e)
            logger.info("Google auth required for user %s when syncing todo %s (role=%s): %s",
                        getattr(participant_user, "id", None), td.id, role, e)
            return
        except RequestException as e:
            _save_last_sync_error(td, e)
            logger.warning("Network error while syncing todo %s (role=%s): %s", td.id, role, e)
            retries = getattr(self.request, "retries", 0)
            countdown = min(2 ** retries * 60, 3600)
            raise self.retry(exc=e, countdown=countdown)
        except HttpError as e:
            _save_last_sync_error(td, e)
            logger.exception("Google HttpError while syncing todo %s (role=%s): %s", td.id, role, e)
            return
        except (ValueError, TypeError) as e:
            _save_last_sync_error(td, e)
            logger.exception("Invalid data while syncing todo %s (role=%s): %s", td.id, role, e)
            return

    for todo in creator_qs:
        try:
            process_role(todo, "creator", user)
        except Exception as exc:
            _save_last_sync_error(todo, exc)
            logger.exception("Unexpected error processing creator todo %s for user %s: %s",
                             getattr(todo, "id", None), user_id, exc)

    for todo in assignee_qs:
        assignee_user = getattr(todo, "assignee", None)
        if not assignee_user:
            continue
        try:
            process_role(todo, "assignee", assignee_user)
        except Exception as exc:
            _save_last_sync_error(todo, exc)
            logger.exception("Unexpected error processing assignee todo %s for user %s: %s",
                             getattr(todo, "id", None), user_id, exc)

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
                    logger.info("Scheduled immediate notification %s for todo %s",
                                n.id, getattr(todo, 'id', None))
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
