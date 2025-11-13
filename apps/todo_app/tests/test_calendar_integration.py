from unittest.mock import patch, MagicMock
import json
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from django.urls import reverse
from rest_framework.test import APITestCase

from apps.auth_app.models import User
from apps.profile_app.models import GoogleToken
from apps.todo_app.models import ToDo
from apps.todo_app.services import GoogleCalendarService


FAKE_CREDS = {
    "token": "ya29.a0AfH6SMDfakeToken",
    "refresh_token": "1//fakeRefreshToken",
    "client_id": "fake-client-id.apps.googleusercontent.com",
    "client_secret": "fake-client-secret",
    "scopes": ["https://www.googleapis.com/auth/calendar"]
}


class GoogleCalendarServiceUnitTests(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="teacher",
            email="teacher@example.com",
            password="pwd",
            role=User.Role.TEACHER,
            status=User.Status.ACTIVE,
        )
        self.dean = User.objects.create_user(
            username="dean",
            email="dean@example.com",
            password="pwd",
            role=User.Role.DEAN,
            status=User.Status.ACTIVE,
            first_name="Dean",
            last_name="User",
        )
        GoogleToken.objects.create(user=self.teacher, credentials=json.dumps(FAKE_CREDS))
        GoogleToken.objects.create(user=self.dean, credentials=json.dumps(FAKE_CREDS))

        self.deadline = timezone.now() + timedelta(days=1)

    def _build_mock_service(self, calendar_items=None, created_calendar_id="cal-created", event_id="event-123"):
        mock_service = MagicMock()

        calendar_list_obj = MagicMock()
        calendar_list_obj.list.return_value.execute.return_value = {"items": calendar_items or []}
        mock_service.calendarList.return_value = calendar_list_obj

        calendars_obj = MagicMock()
        calendars_obj.insert.return_value.execute.return_value = {"id": created_calendar_id}
        mock_service.calendars.return_value = calendars_obj

        events_obj = MagicMock()
        events_obj.insert.return_value.execute.return_value = {"id": event_id}
        mock_service.events.return_value = events_obj

        return mock_service, events_obj, calendars_obj, calendar_list_obj

    @patch("apps.todo_app.services.build")
    def test_create_event_existing_calendar_default_reminders(self, mock_build):
        calendar_items = [{"summary": "TSU Consult", "id": "cal-123"}]
        mock_service, events_obj, _, _ = self._build_mock_service(calendar_items=calendar_items)
        mock_build.return_value = mock_service

        service = GoogleCalendarService(user=self.teacher)
        todo = ToDo.objects.create(
            title="Task", description="Desc", deadline=self.deadline, creator=self.teacher, assignee=self.teacher
        )
        event_id = service.create_event(todo)

        self.assertEqual(event_id, "event-123")
        args, kwargs = events_obj.insert.call_args
        body = kwargs["body"]
        self.assertIn("reminders", body)
        self.assertFalse(body["reminders"]["useDefault"])
        self.assertEqual(len(body["reminders"]["overrides"]), 1)
        self.assertEqual(body["reminders"]["overrides"][0]["minutes"], 30)
        self.assertEqual(body["summary"], f"[{todo.get_status_display()}] {todo.title} — To Do")

    @patch("apps.todo_app.services.build")
    def test_create_event_creates_calendar_if_missing(self, mock_build):
        mock_service, events_obj, calendars_obj, _ = self._build_mock_service(calendar_items=[])
        mock_build.return_value = mock_service

        service = GoogleCalendarService(user=self.teacher)
        todo = ToDo.objects.create(
            title="Task2", description="Desc2", deadline=self.deadline, creator=self.teacher, assignee=self.teacher
        )
        event_id = service.create_event(todo)
        self.assertEqual(event_id, "event-123")
        calendars_obj.insert.assert_called_once()
        self.assertTrue(events_obj.insert.called)

    @patch("apps.todo_app.services.build")
    def test_create_event_custom_reminders_filtering(self, mock_build):
        mock_service, events_obj, _, _ = self._build_mock_service(calendar_items=[
            {
                "summary": "TSU Consult",
                "id": "cal-9"
            }
        ])
        mock_build.return_value = mock_service
        service = GoogleCalendarService(user=self.teacher)
        todo = ToDo.objects.create(
            title="Task3", description="Desc3", deadline=self.deadline, creator=self.teacher, assignee=self.teacher
        )
        reminders = [
            {"method": "popup", "minutes": 15},
            {"method": "email", "minutes": 60},
            {"method": "sms", "minutes": 10},
            {"method": "popup", "minutes": 0},
            {"method": "email", "minutes": "bad"},
        ]
        event_id = service.create_event(todo, reminders=reminders)
        self.assertEqual(event_id, "event-123")
        body = events_obj.insert.call_args.kwargs["body"]
        overrides = body["reminders"]["overrides"]
        self.assertEqual(len(overrides), 2)
        self.assertIn({"method": "popup", "minutes": 15}, overrides)
        self.assertIn({"method": "email", "minutes": 60}, overrides)

    @patch("apps.todo_app.services.build")
    def test_create_event_empty_reminders_list(self, mock_build):
        mock_service, events_obj, _, _ = self._build_mock_service(calendar_items=[
            {
                "summary": "TSU Consult",
                "id": "cal-10"
            }
        ])
        mock_build.return_value = mock_service
        service = GoogleCalendarService(user=self.teacher)
        todo = ToDo.objects.create(
            title="Task4", description="Desc4", deadline=self.deadline, creator=self.teacher, assignee=self.teacher
        )
        service.create_event(todo, reminders=[])
        body = events_obj.insert.call_args.kwargs["body"]
        self.assertEqual(body["reminders"], {"useDefault": False, "overrides": []})

    @patch("apps.todo_app.services.build")
    def test_create_event_adds_author_for_non_teacher(self, mock_build):
        mock_service, events_obj, _, _ = self._build_mock_service(calendar_items=[
            {
                "summary": "TSU Consult",
                "id": "cal-11"
            }
        ])
        mock_build.return_value = mock_service
        service = GoogleCalendarService(user=self.dean)
        todo = ToDo.objects.create(
            title="Task5", description="Base description", deadline=self.deadline, creator=self.dean
        )
        service.create_event(todo)
        body = events_obj.insert.call_args.kwargs["body"]
        self.assertIn("Автор: Dean User", body["description"])
        self.assertIn("Base description", body["description"])

    @patch("apps.todo_app.services.build")
    def test_create_event_no_deadline_returns_none_and_not_called(self, mock_build):
        mock_service, events_obj, _, _ = self._build_mock_service(calendar_items=[
            {
                "summary": "TSU Consult",
                "id": "cal-12"
            }
        ])
        mock_build.return_value = mock_service
        service = GoogleCalendarService(user=self.teacher)
        todo = ToDo.objects.create(
            title="Task6", description="No deadline", deadline=None, creator=self.teacher, assignee=self.teacher
        )
        event_id = service.create_event(todo)
        self.assertIsNone(event_id)
        events_obj.insert.assert_not_called()

    def test_service_without_token_returns_none(self):
        user_no_token = User.objects.create_user(
            username="no_token", email="no_token@example.com", password="pwd",
            role=User.Role.TEACHER, status=User.Status.ACTIVE
        )
        service = GoogleCalendarService(user=user_no_token)
        todo = ToDo.objects.create(
            title="Task7", description="Desc", deadline=self.deadline, creator=user_no_token, assignee=user_no_token
        )
        event_id = service.create_event(todo)
        self.assertIsNone(event_id)

    @patch("apps.todo_app.services.build")
    def test_sync_calendar_event_updates_model_field(self, mock_build):
        mock_service, _, _, _ = self._build_mock_service(calendar_items=[
            {
                "summary": "TSU Consult",
                "id": "cal-13"
            }
        ], event_id="event-sync")
        mock_build.return_value = mock_service
        service = GoogleCalendarService(user=self.teacher)
        todo = ToDo.objects.create(
            title="Task8", description="Sync", deadline=self.deadline, creator=self.teacher, assignee=self.teacher
        )
        returned_id = todo.sync_calendar_event(service)
        self.assertEqual(returned_id, "event-sync")
        todo.refresh_from_db()
        self.assertEqual(todo.calendar_event_id, "event-sync")

    @patch("apps.todo_app.services.build")
    def test_sync_calendar_event_swallows_exception(self, mock_build):
        class DummyService(GoogleCalendarService):
            def create_event(self, todo, reminders=None):
                raise RuntimeError("boom")
        service = DummyService(user=self.teacher)
        todo = ToDo.objects.create(
            title="Task9", description="Sync fail", deadline=self.deadline, creator=self.teacher, assignee=self.teacher
        )
        returned_id = todo.sync_calendar_event(service)
        self.assertIsNone(returned_id)
        self.assertIsNone(todo.calendar_event_id)

    @patch("apps.todo_app.services.build")
    def test_create_event_limits_reminders_to_5(self, mock_build):
        mock_service, events_obj, _, _ = self._build_mock_service(calendar_items=[
            {
                "summary": "TSU Consult",
                "id": "cal-15"
            }
        ], event_id="event-5max")
        mock_build.return_value = mock_service
        service = GoogleCalendarService(user=self.teacher)
        todo = ToDo.objects.create(
            title="Task10", description="limit", deadline=self.deadline, creator=self.teacher, assignee=self.teacher
        )
        reminders = [
            {"method": "popup", "minutes": 5},
            {"method": "email", "minutes": 10},
            {"method": "popup", "minutes": 15},
            {"method": "email", "minutes": 20},
            {"method": "popup", "minutes": 25},
            {"method": "email", "minutes": 30},
        ]
        eid = service.create_event(todo, reminders=reminders)
        self.assertEqual(eid, "event-5max")
        body = events_obj.insert.call_args.kwargs["body"]
        overrides = body["reminders"]["overrides"]
        self.assertEqual(len(overrides), 5)
        self.assertNotIn({"method": "email", "minutes": 30}, overrides)


@override_settings(NOTIFICATIONS_DELIVERY_ENABLED=False)
class GoogleCalendarViewIntegrationTests(APITestCase):
    @patch("apps.todo_app.services.build")
    def setUp(self, mock_build):
        self.teacher = User.objects.create_user(
            username="teacher2",
            email="teacher2@example.com",
            password="pwd",
            role=User.Role.TEACHER,
            status=User.Status.ACTIVE,
        )
        GoogleToken.objects.create(user=self.teacher, credentials=json.dumps(FAKE_CREDS))
        self.url = reverse("todo-create")
        self.deadline = (timezone.now() + timedelta(days=2)).replace(microsecond=0)

        mock_service, events_obj, _, _ = GoogleCalendarServiceUnitTests()._build_mock_service(
            calendar_items=[{"summary": "TSU Consult", "id": "cal-view"}], event_id="event-view-1"
        )
        mock_build.return_value = mock_service
        self._mock_events_obj = events_obj

    @patch("apps.todo_app.services.build")
    def test_todo_creation_sets_calendar_event_id(self, mock_build):
        mock_service, events_obj, _, _ = GoogleCalendarServiceUnitTests()._build_mock_service(
            calendar_items=[{"summary": "TSU Consult", "id": "cal-view"}], event_id="event-view-xyz"
        )
        mock_build.return_value = mock_service

        self.client.force_authenticate(self.teacher)
        payload = {
            "title": "Calendar Task",
            "description": "Calendar Desc",
            "deadline": self.deadline.isoformat().replace("+00:00", "Z"),
        }
        resp = self.client.post(self.url, payload, format='json')
        self.assertEqual(resp.status_code, 201)
        todo = ToDo.objects.get(title="Calendar Task")
        self.assertEqual(todo.calendar_event_id, "event-view-xyz")
        body = events_obj.insert.call_args.kwargs["body"]
        self.assertIn("Calendar Desc", body["description"])
        self.assertEqual(body["start"]["dateTime"], todo.deadline.isoformat())
        self.assertEqual(body["end"]["dateTime"], todo.deadline.isoformat())

    @patch("apps.todo_app.services.build")
    def test_todo_creation_with_reminders(self, mock_build):
        mock_service, events_obj, _, _ = GoogleCalendarServiceUnitTests()._build_mock_service(
            calendar_items=[{"summary": "TSU Consult", "id": "cal-view"}], event_id="event-view-rem"
        )
        mock_build.return_value = mock_service

        self.client.force_authenticate(self.teacher)
        payload = {
            "title": "Calendar Task Rem",
            "description": "Calendar Desc Rem",
            "deadline": self.deadline.isoformat().replace("+00:00", "Z"),
            "reminders": [
                {"method": "popup", "minutes": 45},
                {"method": "email", "minutes": 120},
            ]
        }
        resp = self.client.post(self.url, payload, format='json')
        self.assertEqual(resp.status_code, 201)
        body = events_obj.insert.call_args.kwargs["body"]
        todo = ToDo.objects.get(title="Calendar Task Rem")
        self.assertEqual(todo.calendar_event_id, "event-view-rem")
        overrides = body["reminders"]["overrides"]
        self.assertEqual(len(overrides), 2, f"Overrides: {overrides}")
        self.assertIn({"method": "popup", "minutes": 45}, overrides)
        self.assertIn({"method": "email", "minutes": 120}, overrides)

    @patch("apps.todo_app.services.build")
    def test_todo_creation_limits_reminders_to_5(self, mock_build):
        mock_service, events_obj, _, _ = GoogleCalendarServiceUnitTests()._build_mock_service(
            calendar_items=[{"summary": "TSU Consult", "id": "cal-view"}], event_id="event-view-5max"
        )
        mock_build.return_value = mock_service

        self.client.force_authenticate(self.teacher)
        payload = {
            "title": "Calendar Task 5max",
            "description": "Calendar Desc 5max",
            "deadline": self.deadline.isoformat().replace("+00:00", "Z"),
            "reminders": [
                {"method": "popup", "minutes": 5},
                {"method": "email", "minutes": 10},
                {"method": "popup", "minutes": 15},
                {"method": "email", "minutes": 20},
                {"method": "popup", "minutes": 25},
                {"method": "email", "minutes": 30},
            ]
        }
        resp = self.client.post(self.url, payload, format='json')
        self.assertEqual(resp.status_code, 201)
        todo = ToDo.objects.get(title="Calendar Task 5max")
        self.assertEqual(todo.calendar_event_id, "event-view-5max")
        body = events_obj.insert.call_args.kwargs["body"]
        overrides = body["reminders"]["overrides"]
        self.assertEqual(len(overrides), 5)
        self.assertNotIn({"method": "email", "minutes": 30}, overrides)

    @patch("apps.todo_app.services.build")
    def test_dean_creates_task_for_teacher_event_in_assignee_calendar(self, mock_build):
        from apps.auth_app.models import User as UserModel
        dean = UserModel.objects.create_user(
            username="dean3",
            email="dean3@example.com",
            password="pwd",
            role=UserModel.Role.DEAN,
            status=UserModel.Status.ACTIVE,
        )
        teacher = UserModel.objects.create_user(
            username="teacher3",
            email="teacher3@example.com",
            password="pwd",
            role=UserModel.Role.TEACHER,
            status=UserModel.Status.ACTIVE,
        )
        GoogleToken.objects.create(user=teacher, credentials=json.dumps(FAKE_CREDS))

        mock_service, events_obj, _, _ = GoogleCalendarServiceUnitTests()._build_mock_service(
            calendar_items=[{"summary": "TSU Consult", "id": "cal-teacher"}], event_id="event-assignee"
        )
        mock_build.return_value = mock_service

        self.client.force_authenticate(dean)
        payload = {
            "title": "Dean->Teacher",
            "description": "should go to teacher calendar",
            "deadline": self.deadline.isoformat().replace("+00:00", "Z"),
            "assignee_id": teacher.id,
        }
        resp = self.client.post(self.url, payload, format='json')
        self.assertEqual(resp.status_code, 201)
        todo = ToDo.objects.get(title="Dean->Teacher")
        self.assertEqual(todo.calendar_event_id, "event-assignee")
        body = events_obj.insert.call_args.kwargs["body"]
        self.assertIn("should go to teacher calendar", body["description"])
