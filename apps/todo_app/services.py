import json
import logging
from datetime import timedelta
from typing import Optional, List, Dict, Any, Tuple

from celery.exceptions import CeleryError
from django.utils import timezone
from google.auth.exceptions import RefreshError, GoogleAuthError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from requests.exceptions import RequestException

from apps.notification_app.models import Notification
from apps.profile_app.models import GoogleToken
from apps.todo_app.config import ALLOWED_MINUTES
from apps.todo_app.models import ToDo
from apps.todo_app.utils import normalize_reminders_for_fallback
from core.exceptions import GoogleCalendarAuthRequired, EventNotFound

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
            "schedule_fallback_reminders called for todo %s, target_user=%s",
            getattr(todo, "id", None),
            getattr(target_user, "id", None) if target_user else None,
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


class GoogleCalendarService:
    CALENDAR_NAME = "TSU Consult"
    TIMEZONE = "Asia/Tomsk"

    def __init__(self, user=None):
        self.user = user
        self.service = None
        self.calendar_id = None
        self.creds: Optional[Credentials] = None

        if not user or not getattr(user, "is_authenticated", False):
            logger.debug("GoogleCalendarService: no authenticated user provided (user=%s)",
                         getattr(user, 'id', None))
            return

        try:
            google_token = GoogleToken.objects.get(user=user)
        except GoogleToken.DoesNotExist:
            logger.info("GoogleCalendarService: no GoogleToken for user id=%s", getattr(user, 'id', None))
            return

        try:
            creds = Credentials.from_authorized_user_info(json.loads(google_token.credentials))
            self.creds = creds
            self._ensure_credentials_valid()
            if self.creds and getattr(self.creds, "valid", False):
                self.service = build("calendar", "v3", credentials=self.creds)
            else:
                logger.warning("GoogleCalendarService: credentials invalid or not refreshable for user id=%s",
                               getattr(user, 'id', None))
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.exception(
                "Invalid GoogleToken.credentials for user id=%s: %s", getattr(user, "id", None), exc
            )
            self.service = None

    def _handle_refresh_error(self):
        if self.user:
            GoogleToken.objects.filter(user=self.user).delete()
        self.service = None
        self.calendar_id = None
        raise GoogleCalendarAuthRequired()

    def _refresh_credentials(self) -> bool:
        if not self.creds:
            return False

        if getattr(self.creds, "valid", False):
            return True

        if getattr(self.creds, "expired", False) and getattr(self.creds, "refresh_token", None):
            try:
                self.creds.refresh(GoogleRequest())
                if self.user:
                    GoogleToken.objects.filter(user=self.user).update(credentials=self.creds.to_json())
                self.service = build("calendar", "v3", credentials=self.creds)
                return getattr(self.creds, "valid", False)
            except RefreshError:
                logger.warning(
                    "RefreshError while refreshing credentials for user id=%s", getattr(self.user, "id", None)
                )
                self._handle_refresh_error()
                return False

        logger.warning(
            "Credentials invalid and no refresh_token for user id=%s", getattr(self.user, "id", None)
        )
        self._handle_refresh_error()
        return False

    def _check_credentials(self):
        if not self.creds:
            return

        try:
            self._refresh_credentials()
        except (RefreshError, GoogleAuthError) as exc:
            logger.exception("Auth error ensuring credentials for user id=%s: %s", getattr(self.user, "id", None), exc)
            self.service = None
        except HttpError as exc:
            logger.exception("Google API HttpError while ensuring credentials for user id=%s: %s",
                             getattr(self.user, "id", None), exc)
            raise

    def _ensure_credentials_valid(self):
        return self._check_credentials()

    def _get_or_create_calendar(self) -> Optional[str]:
        if not self.service:
            return None
        try:
            page_token = None
            while True:
                resp = self.service.calendarList().list(pageToken=page_token).execute()
                for calendar_entry in resp.get("items", []):
                    if calendar_entry.get("summary") == self.CALENDAR_NAME:
                        self.calendar_id = calendar_entry.get("id")
                        return self.calendar_id
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

            created_calendar = self.service.calendars().insert(
                body={"summary": self.CALENDAR_NAME, "timeZone": self.TIMEZONE}
            ).execute()
            self.calendar_id = created_calendar.get("id")
            return self.calendar_id
        except RefreshError:
            self._handle_refresh_error()
        except HttpError as exc:
            logger.exception(
                "Google API HttpError while getting/creating calendar for user id=%s: %s",
                getattr(self.user, "id", None),
                exc,
            )
            raise
        except (ValueError, TypeError) as exc:
            logger.exception(
                "Invalid data while getting/creating calendar for user id=%s: %s",
                getattr(self.user, "id", None),
                exc,
            )
        return None

    def _format_event_description(self, todo: ToDo) -> str:
        base_description = (todo.description or "").strip()

        role = getattr(self.user, "role", None)

        if role == "dean":
            assignee = getattr(todo, "assignee", None)
            if assignee:
                full_name = (assignee.get_full_name() or "").strip()
                if not full_name:
                    full_name = (getattr(assignee, "username", "") or getattr(assignee, "email", "")).strip()
                assignee_line = f"Назначен: {full_name}" if full_name else "Назначен: отсутствует"
            else:
                assignee_line = "Назначен: отсутствует"
            return f"{base_description}\n\n{assignee_line}" if base_description else assignee_line

        creator_role = getattr(todo.creator, "role", None)
        if creator_role != "teacher":
            full_name = (todo.creator.get_full_name() or "").strip()
            if not full_name:
                full_name = (getattr(todo.creator, "username", "") or getattr(todo.creator, "email", "")).strip()
            if full_name:
                author_line = f"Автор: {full_name}"
                return f"{base_description}\n\n{author_line}" if base_description else author_line

        return base_description

    @staticmethod
    def _make_aware_datetime(dt):
        if timezone.is_naive(dt):
            try:
                return timezone.make_aware(dt, timezone.get_current_timezone())
            except (ValueError, TypeError):
                return timezone.localtime(dt)
        return dt

    def create_event(self, todo: ToDo, reminders: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
        if not getattr(todo, "deadline", None):
            return None

        self._ensure_credentials_valid()
        if not self.service:
            return None
        if not self.calendar_id:
            try:
                self._get_or_create_calendar()
            except HttpError as exc:
                logger.exception(
                    "Google API HttpError while getting/creating system calendar for user id=%s: %s",
                    getattr(self.user, 'id', None), exc,
                )
                return None
            if not self.calendar_id:
                logger.warning(
                    "System calendar not available for user id=%s, skipping event creation",
                    getattr(self.user, 'id', None),
                )
                return None

        try:
            existing = self.find_event_for_todo(todo)
        except (HttpError, RequestException, ValueError, TypeError) as exc:
            existing = None
            logger.debug("find_event_for_todo raised while creating event for todo %s: %s",
                         getattr(todo, 'id', None), exc)

        if existing:
            eid = existing.get('id')
            logger.debug("create_event: existing event found for todo %s -> %s", getattr(todo, 'id', None), eid)
            return eid

        description = self._format_event_description(todo)

        start = self._make_aware_datetime(todo.deadline)
        end = start

        is_creator = bool(self.user and getattr(self.user, 'id', None) == getattr(todo, 'creator_id', None))

        extended_props = {
            'private': {
                'todo_id': str(getattr(todo, 'id', '')),
                'role': 'creator' if is_creator else 'assignee'
            }
        }

        event_body = {
            "summary": f"[{todo.get_status_display()}] {todo.title} — To Do",
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": self.TIMEZONE},
            "end": {"dateTime": end.isoformat(), "timeZone": self.TIMEZONE},
            "reminders": {"useDefault": False, "overrides": reminders},
            "extendedProperties": extended_props,
        }

        try:
            created_event = self.service.events().insert(calendarId=self.calendar_id, body=event_body).execute()
            return created_event.get("id")
        except RefreshError:
            self._handle_refresh_error()
        except HttpError as exc:
            logger.exception(
                "Google API HttpError while creating event for user id=%s, todo id=%s: %s",
                getattr(self.user, "id", None),
                getattr(todo, "id", None),
                exc,
            )
            raise
        except (ValueError, TypeError) as exc:
            logger.exception(
                "Invalid data while creating event for user id=%s, todo id=%s: %s",
                getattr(self.user, "id", None),
                getattr(todo, "id", None),
                exc,
            )
        return None

    def get_event(self, event_id: str):
        if not event_id:
            raise ValueError("event_id must be provided")

        try:
            self._ensure_credentials_valid()
        except RefreshError:
            raise

        if not self.service:
            raise GoogleCalendarAuthRequired()

        if not self.calendar_id:
            try:
                self._get_or_create_calendar()
            except HttpError as exc:
                logger.exception(
                    "Google API HttpError while getting/creating system calendar for user id=%s: %s",
                    getattr(self.user, 'id', None), exc,
                )
                raise
            if not self.calendar_id:
                raise GoogleCalendarAuthRequired()

        try:
            return self.service.events().get(calendarId=self.calendar_id, eventId=event_id).execute()
        except RefreshError:
            self._handle_refresh_error()
        except HttpError as e:
            status = None
            try:
                status_raw = getattr(e.resp, 'status', None)
                if status_raw is not None:
                    status = int(status_raw)
            except (AttributeError, TypeError, ValueError):
                status = None

            if status == 404:
                raise EventNotFound(event_id)
            if status in (401, 403):
                self._handle_refresh_error()

            logger.exception(
                "Google API HttpError while getting event for user id=%s, event_id=%s: %s",
                getattr(self.user, 'id', None), event_id, e,
            )
            raise
        except (ValueError, TypeError) as exc:
            logger.exception(
                "Invalid data while getting event for user id=%s, event_id=%s: %s",
                getattr(self.user, 'id', None), event_id, exc,
            )
            raise

    def edit_event(self):
        return True

    def delete_event(self):
        return True

    def find_event_for_todo(self, todo: ToDo) -> Optional[Dict[str, Any]]:
        if not self.service:
            return None

        if not self.calendar_id:
            try:
                self._get_or_create_calendar()
            except HttpError as exc:
                logger.exception(
                    "Google API HttpError while getting/creating system calendar for user id=%s: %s",
                    getattr(self.user, 'id', None), exc,
                )
                return None
            if not self.calendar_id:
                return None

        try:
            query = f"todo_id={getattr(todo, 'id', '')}"
            resp = self.service.events().list(calendarId=self.calendar_id,
                                              privateExtendedProperty=query, maxResults=5).execute()
            items = resp.get('items', []) if resp else []
            if items:
                return items[0]
            return None
        except RefreshError:
            self._handle_refresh_error()
        except HttpError as exc:
            logger.exception(
                "Google API HttpError while searching events for todo id=%s user id=%s: %s",
                getattr(todo, 'id', None), getattr(self.user, 'id', None), exc,
            )
            return None
        except (ValueError, TypeError) as exc:
            logger.exception(
                "Invalid data while searching events for todo id=%s user id=%s: %s",
                getattr(todo, 'id', None), getattr(self.user, 'id', None), exc,
            )
            return None
