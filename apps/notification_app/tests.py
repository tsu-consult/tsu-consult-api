from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
from django.contrib.auth import get_user_model
from unittest.mock import patch

from apps.todo_app.models import ToDo
from apps.profile_app.models import GoogleToken
from apps.notification_app.tasks import sync_existing_todos


class SyncExistingTodosTests(TestCase):
    def setUp(self):
        self.User = get_user_model()

    def _create_user(self, email: str, username: str):
        return self.User.objects.create_user(email=email, username=username)

    def test_signal_triggers_task_on_google_token_save(self):
        user = self._create_user("u1@example.com", "u1")

        with patch("apps.notification_app.signals.sync_existing_todos.delay") as mock_delay:
            GoogleToken.objects.create(user=user, credentials='{}')
            mock_delay.assert_called_once_with(user.id)

    def test_sync_existing_todos_sets_calendar_event_id_for_creator(self):
        user = self._create_user("u2@example.com", "u2")
        todo = ToDo.objects.create(
            title="Test",
            description="",
            creator=user,
            deadline=timezone.now() + timedelta(days=1),
        )

        class FakeGoogleCalendarService:
            def __init__(self, user_arg):
                self.user = user_arg
                self.service = True
                self.calendar_id = "cal-1"

            @staticmethod
            def create_event(todo_arg, reminders=None):
                _ = reminders
                return f"event-{todo_arg.id}"

        with patch("apps.notification_app.tasks.GoogleCalendarService", new=FakeGoogleCalendarService):
            from apps.notification_app.tasks import sync_existing_todos
            sync_existing_todos.run(user.id)

        todo.refresh_from_db()
        self.assertEqual(todo.calendar_event_id, f"event-{todo.id}")

    def test_sync_existing_todos_sets_assignee_calendar_event_id(self):
        creator = self._create_user("creator@example.com", "creator")
        assignee = self._create_user("assignee@example.com", "assignee")

        todo = ToDo.objects.create(
            title="Task for assignee",
            description="",
            creator=creator,
            assignee=assignee,
            deadline=timezone.now() + timedelta(days=1),
        )

        class FakeGoogleCalendarService:
            def __init__(self, user_arg):
                self.user = user_arg
                self.service = True
                self.calendar_id = "cal-1"

            @staticmethod
            def create_event(todo_arg, reminders=None):
                _ = reminders
                return f"assignee-event-{todo_arg.id}"

        with patch("apps.notification_app.tasks.GoogleCalendarService", new=FakeGoogleCalendarService):
            sync_existing_todos.run(assignee.id)

        todo.refresh_from_db()
        self.assertEqual(todo.assignee_calendar_event_id, f"assignee-event-{todo.id}")

    def test_sync_existing_todos_no_service_records_error(self):
        user = self._create_user("u3@example.com", "u3")
        todo = ToDo.objects.create(
            title="No service",
            description="",
            creator=user,
            deadline=timezone.now() + timedelta(days=1),
        )

        class NoServiceGoogleCalendarService:
            def __init__(self, user_arg):
                self.user = user_arg
                self.service = None

        with patch("apps.notification_app.tasks.GoogleCalendarService", new=NoServiceGoogleCalendarService):
            from apps.notification_app.tasks import sync_existing_todos
            sync_existing_todos.run(user.id)

        todo.refresh_from_db()
        self.assertEqual(todo.last_sync_error, "no_calendar_service")
