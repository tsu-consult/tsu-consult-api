import json

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

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

    def _get_or_create_calendar(self):
        if not self.service:
            return None

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

    def create_event(self, todo):
        if not self.service or not todo.deadline:
            return None

        if not self.calendar_id:
            self._get_or_create_calendar()

        if not self.calendar_id:
            return None

        description = todo.description
        if todo.creator != self.user:
            description += f'\n\nАвтор: {todo.creator.get_full_name()}'

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

        created_event = self.service.events().insert(calendarId=self.calendar_id, body=event).execute()
        return created_event.get('id')

    def delete_event(self):
        return True
