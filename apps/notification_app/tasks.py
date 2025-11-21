import logging
from datetime import timedelta, datetime
from typing import Optional, Type

from celery import shared_task, current_app
from celery.exceptions import CeleryError
from django.contrib.auth import get_user_model
from django.db import DatabaseError, IntegrityError
from django.utils import timezone
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from requests.exceptions import RequestException

from apps.notification_app.models import Notification
from apps.notification_app.services import send_telegram_notification
from apps.todo_app.models import ToDo
from apps.todo_app.services import FallbackReminderService
from apps.todo_app.services import GoogleCalendarService
from apps.todo_app.utils import normalize_reminders_for_fallback
from apps.todo_app.utils import normalize_reminders_permissive
from core.exceptions import GoogleCalendarAuthRequired, EventNotFound
from apps.profile_app.models import GoogleToken

logger = logging.getLogger(__name__)

User = get_user_model()


def _save_last_sync_error(todo: ToDo, exc):
    try:
        todo.last_sync_error = str(exc)
        todo.save(update_fields=["last_sync_error"])
    except Exception as e:
        logger.exception("Failed to save last_sync_error for todo %s: %s", getattr(todo, "id", None), e)


def _ensure_future_deadline(todo: ToDo) -> bool:
    if not getattr(todo, "deadline", None):
        return False
    now = timezone.now()
    return todo.deadline > now


def _create_or_skip_notification(user_id: Type[int], todo: ToDo, title: str,
                                 message: str, scheduled_for: Optional[datetime]):
    try:
        n, created = Notification.objects.get_or_create(
            user_id=user_id,
            todo=todo,
            title=title,
            scheduled_for=scheduled_for,
            defaults={
                "message": message,
                "type": Notification.Type.TELEGRAM,
                "status": Notification.Status.PENDING,
            },
        )

        if created:
            return n

        if n.status == Notification.Status.PENDING:
            logger.debug(
                "Notification skipped as duplicate (already pending): user=%s title=%r scheduled_for=%s",
                user_id, title, scheduled_for
            )
            return None

        try:
            n.status = Notification.Status.PENDING
            n.message = message
            n.last_error = None
            n.celery_task_id = None
            n.save(update_fields=["status", "message", "last_error", "celery_task_id"])
            logger.info("Reactivated notification %s for user %s todo %s (scheduled_for=%s)", n.id, user_id, getattr(todo, 'id', None), scheduled_for)
            return n
        except Exception as exc:
            logger.exception("Failed to reactivate existing notification %s: %s", getattr(n, 'id', None), exc)
            return None
    except (IntegrityError, DatabaseError) as exc:
        logger.exception("DB error while creating/reactivating Notification: %s", exc)
    return None


def _normalize_unique_minutes(reminders_raw) -> list[int]:
    try:
        return normalize_reminders_for_fallback(reminders_raw)
    except Exception as exc:
        logger.debug("normalize_reminders_for_fallback failed: %s", exc)
        return []


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

    def _process(td: ToDo, role: str, participant_user):
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
                    try:
                        calendar_service.get_event(existing_event_id)
                    except EventNotFound:
                        raise

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
            _process(todo, "creator", user)
        except Exception as exc:
            _save_last_sync_error(todo, exc)
            logger.exception("Unexpected error processing creator todo %s for user %s: %s",
                             getattr(todo, "id", None), user_id, exc)

    for todo in assignee_qs:
        assignee_user = getattr(todo, "assignee", None)
        if not assignee_user:
            continue
        try:
            _process(todo, "assignee", assignee_user)
        except Exception as exc:
            _save_last_sync_error(todo, exc)
            logger.exception("Unexpected error processing assignee todo %s for user %s: %s",
                             getattr(todo, "id", None), user_id, exc)

    logger.info("sync_existing_todos finished for user_id=%s", user_id)


@shared_task
def transfer_unsent_reminders_task(user_id: int):
    now = timezone.now()
    logger.info("transfer_unsent_reminders_task start for user_id=%s", user_id)

    try:
        if GoogleToken.objects.filter(user_id=user_id).exists():
            logger.info("User %s has GoogleToken again — skipping transfer_unsent_reminders_task", user_id)
            return
    except Exception as e:
        logger.debug("Error checking GoogleToken existence for user %s: %s", user_id, e)

    try:
        creator_qs = ToDo.objects.filter(
            creator_id=user_id, deadline__gt=now, calendar_event_id__isnull=False
        )
        assignee_qs = ToDo.objects.filter(
            assignee_id=user_id, deadline__gt=now, assignee_calendar_event_id__isnull=False
        )
    except DatabaseError as exc:
        logger.exception("DB error while querying ToDo: %s", exc)
        return

    frs = FallbackReminderService()

    def _process(td: ToDo, role: str):
        if role == "creator":
            reminders_raw = td.reminders
            target_user_id = td.creator_id
            id_field = "calendar_event_id"
            active_field = "calendar_event_active"
        else:
            reminders_raw = td.assignee_reminders
            target_user_id = td.assignee_id
            id_field = "assignee_calendar_event_id"
            active_field = "assignee_calendar_event_active"

        minutes_list = _normalize_unique_minutes(reminders_raw)
        if not minutes_list:
            return

        created_any = False

        for minutes_val in minutes_list:
            deadline = td.deadline
            scheduled_for = deadline - timedelta(minutes=minutes_val)

            if scheduled_for <= now:
                logger.debug(
                    "Skipping past-due reminder for todo %s user %s (scheduled_for=%s)",
                    td.id, target_user_id, scheduled_for
                )
                continue

            interval_str = frs.humanize_minutes(minutes_val)
            title = "Напоминание о задаче"
            message = f'Через {interval_str} наступает дедлайн задачи "{td.title}".'

            n = _create_or_skip_notification(target_user_id, td, title, message, scheduled_for)
            if not n:
                continue

            created_any = True

            try:
                celery_task = send_notification_task.apply_async(args=[n.id], eta=scheduled_for)
                n.celery_task_id = celery_task.id
                n.save(update_fields=["celery_task_id"])
            except CeleryError as ex:
                logger.exception(
                    "Failed to schedule notification %s for todo %s: %s",
                    n.id, td.id, ex
                )

        if created_any:
            try:
                update_fields = []
                setattr(td, id_field, None)
                update_fields.append(id_field)

                if hasattr(td, active_field):
                    setattr(td, active_field, False)
                    update_fields.append(active_field)

                td.save(update_fields=update_fields)
            except DatabaseError as ex:
                logger.exception("Failed to clear calendar fields for todo %s: %s", td.id, ex)

    for t in creator_qs:
        try:
            _process(t, "creator")
        except Exception as exc:
            logger.exception("Error processing creator todo %s: %s", t.id, exc)

    for t in assignee_qs:
        try:
            _process(t, "assignee")
        except Exception as exc:
            logger.exception("Error processing assignee todo %s: %s", t.id, exc)

    logger.info("transfer_unsent_reminders_task finished for user_id=%s", user_id)


@shared_task
def cancel_pending_fallbacks_for_user(user_id: int):
    qs = Notification.objects.filter(
        user_id=user_id,
        type=Notification.Type.TELEGRAM,
        status=Notification.Status.PENDING,
        todo__isnull=False,
    )

    count = qs.count()
    if count == 0:
        logger.debug("No pending user fallback notifications to cancel for user %s", user_id)
        return

    notif_info = ", ".join([f"id={n.id} task={n.celery_task_id}" for n in qs])
    logger.info("Cancelling %s user fallback notifications for user %s: %s", count, user_id, notif_info)

    for n in qs:
        revoked = False
        try:
            if n.celery_task_id:
                current_app.control.revoke(n.celery_task_id, terminate=False)
                revoked = True
                logger.debug("Revoked celery task %s for notification %s", n.celery_task_id, n.id)
        except Exception as e:
            logger.warning("Failed to revoke task %s for notification %s: %s", n.celery_task_id, n.id, e)

        try:
            n.status = Notification.Status.CANCELLED
            n.last_error = "Cancelled due to Google Calendar integration re-enabled"
            n.celery_task_id = None
            n.save(update_fields=["status", "last_error", "celery_task_id"])
            logger.debug("Marked notification %s as CANCELLED (revoked=%s)", n.id, revoked)
        except Exception as e:
            logger.warning("Failed to update notification %s: %s", n.id, e)
