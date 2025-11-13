import json

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.exceptions import RefreshError

from apps.profile_app.models import GoogleToken


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
                'overrides': [{'method': 'popup', 'minutes': 30}],
            }
        elif isinstance(reminders, list):
            if not reminders:
                event['reminders'] = {'useDefault': False, 'overrides': []}
            else:
                filtered_overrides = []
                for r in reminders:
                    method = r.get('method')
                    minutes = r.get('minutes')
                    if method in ('popup', 'email'):
                        try:
                            minutes_int = int(minutes)
                        except (TypeError, ValueError):
                            continue
                        if minutes_int > 0:
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
