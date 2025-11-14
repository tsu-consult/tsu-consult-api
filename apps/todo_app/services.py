import json
from datetime import timedelta

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.exceptions import RefreshError
from django.utils import timezone

from apps.profile_app.models import GoogleToken
from apps.notification_app.models import Notification
from apps.notification_app.tasks import send_notification_task


FALLBACK_ALLOWED_MINUTES = {15, 30, 60, 1440}


def humanize_minutes(m: int) -> str:
    if m < 60:
        return f"{m} мин"
    if m % 60 == 0 and m < 1440:
        h = m // 60
        if h == 1:
            return "1 час"
        if 2 <= h <= 4:
            return f"{h} часа"
        return f"{h} часов"
    if m % 1440 == 0 and m < 10080:
        d = m // 1440
        if d == 1:
            return "1 день"
        if 2 <= d <= 4:
            return f"{d} дня"
        return f"{d} дней"
    if m % 10080 == 0:
        w = m // 10080
        if w == 1:
            return "1 неделя"
        if 2 <= w <= 4:
            return f"{w} недели"
        return f"{w} недель"
    if 60 <= m < 1440:
        h = m // 60
        return f"~{h} ч"
    if 1440 <= m < 10080:
        d = m // 1440
        return f"~{d} дн"
    w = m // 10080
    return f"~{w} нед"


def schedule_fallback_reminders(todo, reminders):
    if not reminders or not todo.deadline:
        return

    target_user = todo.assignee or todo.creator
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
            notification = Notification.objects.create(
                user=target_user,
                title="Напоминание о задаче",
                message=f'Задача "{todo.title}" подходит к дедлайну.',
                type=Notification.Type.TELEGRAM,
            )
            send_notification_task.delay(notification.id)
            continue

        interval_str = humanize_minutes(minutes_int)
        notification = Notification.objects.create(
            user=target_user,
            title="Напоминание о задаче",
            message=f'За {interval_str} до дедлайна задачи "{todo.title}" будет отправлено это напоминание.',
            type=Notification.Type.TELEGRAM,
        )
        send_notification_task.apply_async((notification.id,), eta=notify_at)


class GoogleCalendarService:
    def __init__(self, user=None):
        self.user = user
        self.service = None
        self.calendar_id = None

        if user and user.is_authenticated:
            try:
                google_token = GoogleToken.objects.get(user=self.user)
                creds = Credentials.from_authorized_user_info(json.loads(google_token.credentials))
                self.service = build('calendar', 'v3', credentials=creds)
            except GoogleToken.DoesNotExist:
                self.service = None

    def _handle_refresh_error(self):
        if self.user:
            GoogleToken.objects.filter(user=self.user).delete()
        self.service = None
        self.calendar_id = None

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
        except Exception:
            return None

    def create_event(self, todo, reminders=None):
        if not self.service or not todo.deadline:
            return None

        if not self.calendar_id:
            self._get_or_create_calendar()

        if not self.calendar_id:
            return None

        base_description = (todo.description or '').strip()
        description = base_description

        creator_role = getattr(todo.creator, 'role', None)
        if creator_role != 'teacher':
            full_name = (todo.creator.get_full_name() or '').strip()
            if not full_name:
                full_name = (getattr(todo.creator, 'username', '') or getattr(todo.creator, 'email', '') or '').strip()
            if full_name:
                author_line = f'Автор: {full_name}'
                description = f'{base_description}\n\n{author_line}' if base_description else author_line

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
            }
        }

        if reminders is None:
            event['reminders'] = {
                'useDefault': False,
                'overrides': [{'method': 'popup', 'minutes': 15}],
            }
        elif isinstance(reminders, list):
            if not reminders:
                event['reminders'] = {'useDefault': False, 'overrides': []}
            else:
                filtered_overrides = []
                seen_pairs = set()
                for r in reminders:
                    method = r.get('method')
                    minutes = r.get('minutes')
                    if method in ('popup', 'email'):
                        try:
                            minutes_int = int(minutes)
                        except (TypeError, ValueError):
                            continue
                        if minutes_int > 0:
                            pair = (method, minutes_int)
                            if pair in seen_pairs:
                                continue
                            seen_pairs.add(pair)
                            filtered_overrides.append({'method': method, 'minutes': minutes_int})
                if filtered_overrides:
                    event['reminders'] = {
                        'useDefault': False,
                        'overrides': filtered_overrides[:5]
                    }
                else:
                    event['reminders'] = {'useDefault': False, 'overrides': []}

        try:
            created_event = self.service.events().insert(calendarId=self.calendar_id, body=event).execute()
            return created_event.get('id')
        except RefreshError:
            self._handle_refresh_error()
            return None
        except Exception:
            return None

    def delete_event(self):
        return True
