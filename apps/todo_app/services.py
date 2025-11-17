import json
import logging
import math
from datetime import timedelta
from typing import Optional, List, Dict, Any

from django.utils import timezone
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from celery.exceptions import CeleryError

from apps.notification_app.models import Notification
from apps.profile_app.models import GoogleToken
from apps.todo_app.models import ToDo
from core.exceptions import GoogleCalendarAuthRequired

logger = logging.getLogger(__name__)

FALLBACK_ALLOWED_MINUTES = {15, 30, 60, 1440}


def _russian_plural(n: int, forms: tuple[str, str, str]) -> str:
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


def humanize_minutes(m: int) -> str:
    if m <= 0:
        return "0 минут"

    if m % 10080 == 0:
        w = m // 10080
        if w == 1:
            return "неделю"
        return f"{w} " + _russian_plural(w, ("неделю", "недели", "недель"))

    if m % 1440 == 0:
        d = m // 1440
        if d == 1:
            return "сутки"
        return f"{d} суток"

    if m % 60 == 0:
        h = m // 60
        form = _russian_plural(h, ("час", "часа", "часов"))
        return f"{h} {form}"

    form = _russian_plural(m, ("минуту", "минуты", "минут"))
    return f"{m} {form}"


def schedule_fallback_reminders(todo, reminders, target_user=None):
    if not reminders or not todo.deadline:
        return

    from apps.notification_app.tasks import send_notification_task
    logger.debug("schedule_fallback_reminders called for todo %s, target_user=%s",
                 getattr(todo, 'id', None), getattr(target_user, 'id', None) if target_user else None)

    target_user = target_user or todo.assignee or todo.creator
    now = timezone.now()

    seen_minutes = set()
    unique_reminders = []
    for r in reminders if isinstance(reminders, list) else []:
        minutes = r.get('minutes')
        try:
            minutes_int = int(minutes)
        except (TypeError, ValueError):
            continue
        if minutes_int <= 0 or minutes_int not in FALLBACK_ALLOWED_MINUTES:
            continue
        if minutes_int in seen_minutes:
            continue
        seen_minutes.add(minutes_int)
        unique_reminders.append(minutes_int)
        if len(unique_reminders) >= 5:
            break

    for minutes_int in unique_reminders:
        notify_at = todo.deadline - timedelta(minutes=minutes_int)
        if notify_at <= now:
            remaining_seconds = (todo.deadline - now).total_seconds()
            if remaining_seconds > 0:
                remaining_minutes = int(math.ceil(remaining_seconds / 60))
                interval_str = humanize_minutes(remaining_minutes)
                message = f'Через {interval_str} наступает дедлайн задачи "{todo.title}".'
            else:
                message = f'Дедлайн задачи "{todo.title}" наступил.'

            n = Notification.objects.create(
                user=target_user,
                title="Напоминание о задаче",
                message=message,
                type=Notification.Type.TELEGRAM,
                status=Notification.Status.PENDING,
                scheduled_for=None,
            )
            try:
                send_notification_task.delay(n.id)
                logger.info("Scheduled immediate notification %s for todo %s", n.id, todo.id)
            except (CeleryError, RuntimeError) as e:
                logger.exception("Failed scheduling immediate notification %s for todo %s: %s", n.id, todo.id, e)
            continue

        interval_str = humanize_minutes(minutes_int)
        n = Notification.objects.create(
            user=target_user,
            title="Напоминание о задаче",
            message=f'Через {interval_str} наступает дедлайн задачи "{todo.title}".',
            type=Notification.Type.TELEGRAM,
            status=Notification.Status.PENDING,
            scheduled_for=notify_at,
        )
        try:
            send_notification_task.apply_async(args=[n.id], eta=notify_at)
            logger.info("Scheduled deferred notification %s for todo %s at %s", n.id, todo.id, notify_at)
        except (CeleryError, RuntimeError) as e:
            logger.exception("Failed scheduling deferred notification %s for todo %s: %s", n.id, todo.id, e)


class GoogleCalendarService:
    def __init__(self, user=None):
        self.user = user
        self.service = None
        self.calendar_id = None

        if user and user.is_authenticated:
            try:
                google_token = GoogleToken.objects.get(user=self.user)
                try:
                    creds = Credentials.from_authorized_user_info(json.loads(google_token.credentials))
                    self.service = build('calendar', 'v3', credentials=creds)
                except (json.JSONDecodeError, ValueError, TypeError) as exc:
                    logger.exception("Invalid GoogleToken.credentials for user id=%s: %s",
                                     getattr(self.user, 'id', None), exc)
                    self.service = None
            except GoogleToken.DoesNotExist:
                self.service = None

    def _handle_refresh_error(self):
        if self.user:
            GoogleToken.objects.filter(user=self.user).delete()
        self.service = None
        self.calendar_id = None
        raise GoogleCalendarAuthRequired()

    def _get_or_create_calendar(self):
        if not self.service:
            return None
        try:
            calendar_list = self.service.calendarList().list().execute()
            for calendar_list_entry in calendar_list.get('items', []):
                if calendar_list_entry['summary'] == 'TSU Consult':
                    self.calendar_id = calendar_list_entry['id']
                    return self.calendar_id

            calendar = {
                'summary': 'TSU Consult',
                'timeZone': 'Asia/Tomsk',
            }
            created_calendar = self.service.calendars().insert(body=calendar).execute()
            self.calendar_id = created_calendar['id']
            return self.calendar_id
        except RefreshError:
            self._handle_refresh_error()
            return None
        except HttpError as exc:
            logger.exception("Google API HttpError while getting/creating calendar for user id=%s: %s",
                             getattr(self.user, 'id', None), exc)
            return None
        except (ValueError, TypeError) as exc:
            logger.exception("Unexpected data error while getting/creating calendar for user id=%s: %s",
                             getattr(self.user, 'id', None), exc)
            return None

    def _format_event_description(self, todo) -> str:
        base_description = (todo.description or '').strip()
        description = base_description

        role = getattr(self.user, 'role', None)

        match role:
            case 'dean':
                assignee = getattr(todo, 'assignee', None)
                if assignee:
                    full_name = (assignee.get_full_name() or '').strip()
                    if not full_name:
                        full_name = (getattr(assignee, 'username', '') or getattr(assignee, 'email', '') or '').strip()
                    if full_name:
                        assignee_line = f'Назначен: {full_name}'
                        description = f'{base_description}\n\n{assignee_line}' if base_description else assignee_line
                        return description
                none_line = 'Назначен: отсутствует'
                description = f'{base_description}\n\n{none_line}' if base_description else none_line
                return description

            case _:
                creator_role = getattr(todo.creator, 'role', None)
                if creator_role != 'teacher':
                    full_name = (todo.creator.get_full_name() or '').strip()
                    if not full_name:
                        full_name = (getattr(todo.creator, 'username', '') or
                                     getattr(todo.creator, 'email', '') or '').strip()
                    if full_name:
                        author_line = f'Автор: {full_name}'
                        description = f'{base_description}\n\n{author_line}' if base_description else author_line
                return description

    def create_event(self, todo: ToDo, reminders: Optional[List[Dict[str, Any]]] = None):
        if not self.service or not todo.deadline:
            return None

        if not self.calendar_id:
            self._get_or_create_calendar()

        if not self.calendar_id:
            return None

        description = self._format_event_description(todo)

        event = {
            'summary': f'[{todo.get_status_display()}] {todo.title} — To Do',
            'description': description,
            'start': {
                'dateTime': todo.deadline.isoformat(),
                'timeZone': 'Asia/Tomsk',
            },
            'end': {
                'dateTime': todo.deadline.isoformat(),
                'timeZone': 'Asia/Tomsk',
            },
            'reminders': {
                'useDefault': False,
                'overrides': reminders if reminders is not None else [],
            }
        }

        try:
            created_event = self.service.events().insert(calendarId=self.calendar_id, body=event).execute()
            return created_event.get('id')
        except RefreshError:
            self._handle_refresh_error()
            return None
        except HttpError as exc:
            logger.exception("Google API HttpError while creating event for user id=%s, todo id=%s: %s",
                             getattr(self.user, 'id', None), getattr(todo, 'id', None), exc)
            return None
        except (ValueError, TypeError) as exc:
            logger.exception("Invalid data while creating event for user id=%s, todo id=%s: %s",
                             getattr(self.user, 'id', None), getattr(todo, 'id', None), exc)
            return None

    def delete_event(self):
        return True
