import json
import logging
from typing import Optional, List, Dict, Any

from django.utils import timezone
from google.auth.exceptions import RefreshError, GoogleAuthError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from requests.exceptions import RequestException

from apps.profile_app.models import GoogleToken
from apps.todo_app.models import ToDo
from core.exceptions import GoogleCalendarAuthRequired, EventNotFound

logger = logging.getLogger(__name__)


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

    def _build_event_body(self, todo: ToDo, reminders: Optional[List[Dict[str, Any]]]):
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

        return event_body

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

        event_body = self._build_event_body(todo, reminders)

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

    def update_event(self, todo: ToDo = None, reminders: Optional[List[Dict[str, Any]]] = None):
        if todo is None or not getattr(todo, 'deadline', None):
            return False

        try:
            self._ensure_credentials_valid()
        except RefreshError:
            raise

        if not self.service:
            return False

        if not self.calendar_id:
            try:
                self._get_or_create_calendar()
            except HttpError as exc:
                logger.exception(
                    "Google API HttpError while getting/creating system calendar for user id=%s: %s",
                    getattr(self.user, 'id', None), exc,
                )
                return False
            if not self.calendar_id:
                return False

        try:
            existing = self.find_event_for_todo(todo)
        except (HttpError, RequestException, ValueError, TypeError) as exc:
            existing = None
            logger.debug("find_event_for_todo raised while updating event for todo %s: %s",
                         getattr(todo, 'id', None), exc)

        if not existing:
            raise EventNotFound(f"Event missing for todo id={getattr(todo, 'id', None)}")

        event_id = existing.get('id')

        event_body = self._build_event_body(todo, reminders)

        try:
            updated = self.service.events().patch(calendarId=self.calendar_id, eventId=event_id,
                                                  body=event_body).execute()
            return bool(updated)
        except RefreshError:
            self._handle_refresh_error()
        except HttpError as exc:
            status = None
            try:
                status_raw = getattr(exc.resp, 'status', None)
                if status_raw is not None:
                    status = int(status_raw)
            except (AttributeError, TypeError, ValueError):
                status = None

            if status == 404:
                raise EventNotFound(event_id)
            if status in (401, 403):
                self._handle_refresh_error()

            logger.exception(
                "Google API HttpError while updating event for user id=%s, todo id=%s: %s",
                getattr(self.user, 'id', None), getattr(todo, 'id', None), exc,
            )
            return False
        except (ValueError, TypeError) as exc:
            logger.exception(
                "Invalid data while updating event for user id=%s, todo id=%s: %s",
                getattr(self.user, 'id', None), getattr(todo, 'id', None), exc,
            )
            return False

    def delete_event(self):
        return True
