import logging

from celery import shared_task
from django.contrib.auth import get_user_model
from django.utils import timezone
from requests.exceptions import RequestException

from apps.notification_app.models import Notification
from apps.notification_app.services import send_telegram_notification

from apps.todo_app.models import ToDo
from apps.todo_app.services import GoogleCalendarService
from apps.todo_app.utils import normalize_reminders_permissive
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from core.exceptions import GoogleCalendarAuthRequired

logger = logging.getLogger(__name__)

User = get_user_model()


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
        except Exception as e:
            logger.exception("Failed to send notification %s", notification_id)
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
        except Exception as e:
            n.last_error = str(e)
            n.save(update_fields=["last_error"])
            logger.exception("Error retrying notification %s", n.id)


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

        if todo.calendar_event_id:
            continue

        reminders = normalize_reminders_permissive(todo.reminders)
        calendar_service = GoogleCalendarService(user)

        if not calendar_service.service:
            todo.last_sync_error = "no_calendar_service"
            todo.save(update_fields=["last_sync_error"])
            logger.info("No calendar service for user %s when syncing todo %s (creator)", user_id, todo.id)
            continue

        try:
            event_id = calendar_service.create_event(todo, reminders)
            if event_id:
                todo.calendar_event_id = event_id
                todo.save(update_fields=["calendar_event_id"])
                logger.info("Synced creator todo %s -> calendar_event_id=%s", todo.id, event_id)
        except GoogleCalendarAuthRequired as e:
            todo.last_sync_error = str(e)
            todo.save(update_fields=["last_sync_error"])
            logger.info("Google auth required for user %s, todo %s (creator): %s", user_id, todo.id, e)
        except RefreshError as e:
            todo.last_sync_error = str(e)
            todo.save(update_fields=["last_sync_error"])
            logger.info("RefreshError for user %s, todo %s (creator): %s", user_id, todo.id, e)
        except RequestException as e:
            todo.last_sync_error = str(e)
            todo.save(update_fields=["last_sync_error"])
            logger.warning("Network error while syncing todo %s (creator) for user %s: %s", todo.id, user_id, e)
            retries = getattr(self.request, 'retries', 0)
            countdown = min(2 ** retries * 60, 3600)
            raise self.retry(exc=e, countdown=countdown)
        except HttpError as e:
            todo.last_sync_error = str(e)
            todo.save(update_fields=["last_sync_error"])
            logger.exception("Google API error while syncing todo %s (creator) for user %s: %s", todo.id, user_id, e)
        except (ValueError, TypeError) as e:
            todo.last_sync_error = str(e)
            todo.save(update_fields=["last_sync_error"])
            logger.exception("Invalid data while syncing todo %s (creator) for user %s: %s", todo.id, user_id, e)

    for todo in assignee_qs:
        if todo.id in processed:
            continue
        processed.add(todo.id)

        if not todo.deadline:
            continue

        if todo.assignee_calendar_event_id:
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
            event_id = calendar_service.create_event(todo, reminders)
            if event_id:
                todo.assignee_calendar_event_id = event_id
                todo.save(update_fields=["assignee_calendar_event_id"])
                logger.info("Synced todo %s -> assignee_calendar_event_id=%s", todo.id, event_id)
        except GoogleCalendarAuthRequired as e:
            todo.last_sync_error = str(e)
            todo.save(update_fields=["last_sync_error"])
            logger.info("Google auth required for assignee %s, todo %s: %s",
                        getattr(todo.assignee, 'id', None), todo.id, e)
        except RefreshError as e:
            todo.last_sync_error = str(e)
            todo.save(update_fields=["last_sync_error"])
            logger.info("RefreshError for assignee %s, todo %s: %s",
                        getattr(todo.assignee, 'id', None), todo.id, e)
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
        except (ValueError, TypeError) as e:
            todo.last_sync_error = str(e)
            todo.save(update_fields=["last_sync_error"])
            logger.exception("Invalid data while syncing todo %s for assignee %s: %s", todo.id,
                             getattr(todo.assignee, 'id', None), e)

    logger.info("sync_existing_todos finished for user_id=%s", user_id)
