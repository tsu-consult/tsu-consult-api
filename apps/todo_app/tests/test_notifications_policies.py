from datetime import timedelta
import json

from django.test import override_settings
from django.utils import timezone
from django.urls import reverse
from rest_framework.test import APITestCase
from unittest.mock import patch

from apps.auth_app.models import User
from apps.notification_app.models import Notification
from apps.profile_app.models import GoogleToken
from apps.todo_app.models import ToDo


FAKE_CREDS = {
    "token": "ya29.a0AfH6SMDfakeToken",
    "refresh_token": "1//fakeRefreshToken",
    "client_id": "fake-client-id.apps.googleusercontent.com",
    "client_secret": "fake-client-secret",
    "scopes": ["https://www.googleapis.com/auth/calendar"],
}


@override_settings(NOTIFICATIONS_DELIVERY_ENABLED=False)
class NotificationsPolicyTests(APITestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="teacher_np",
            email="tnp@example.com",
            password="pwd",
            role=User.Role.TEACHER,
            status=User.Status.ACTIVE,
        )
        self.dean = User.objects.create_user(
            username="dean_np",
            email="dnp@example.com",
            password="pwd",
            role=User.Role.DEAN,
            status=User.Status.ACTIVE,
        )
        self.url = reverse("todo-create")
        self.deadline = (timezone.now() + timedelta(days=1)).replace(microsecond=0)

    def test_only_teacher_receives_new_task_notification_and_not_dean(self):
        self.client.force_authenticate(self.dean)
        payload = {
            "title": "Assign to teacher",
            "description": "by dean",
            "deadline": self.deadline.isoformat().replace("+00:00", "Z"),
            "assignee_id": self.teacher.id,
        }
        resp = self.client.post(self.url, payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)

        self.assertEqual(
            Notification.objects.filter(user=self.teacher, title="Новая задача").count(),
            1,
            "Teacher must receive exactly one \"Новая задача\" notification",
        )
        self.assertEqual(
            Notification.objects.filter(user=self.dean).count(),
            0,
            "Dean must not receive any notifications on task creation",
        )

        self.client.force_authenticate(self.teacher)
        payload2 = {
            "title": "Self task",
            "description": "by teacher",
            "deadline": self.deadline.isoformat().replace("+00:00", "Z"),
            "assignee_id": self.teacher.id,
        }
        resp2 = self.client.post(self.url, payload2, format="json")
        self.assertEqual(resp2.status_code, 201, resp2.content)
        self.assertEqual(
            Notification.objects.filter(user=self.teacher, title="Новая задача").count(),
            1,
            "Self-assigned tasks should not duplicate \"Новая задача\" notification",
        )

    def test_dean_can_create_draft_without_assignee_no_notifications_and_no_calendar(self):
        self.client.force_authenticate(self.dean)
        payload = {
            "title": "Dean draft",
            "description": "draft",
            "deadline": self.deadline.isoformat().replace("+00:00", "Z"),
        }
        resp = self.client.post(self.url, payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIsNone(resp.data.get("assignee"))

        self.assertEqual(Notification.objects.count(), 0)

        todo = ToDo.objects.get(title="Dean draft")
        self.assertIsNone(todo.calendar_event_id)

    @patch("apps.todo_app.services.build")
    def test_dean_to_teacher_uses_default_15min_calendar_reminder(self, mock_build):
        mock_service = self._mock_google_service(event_id="evt-15min")
        mock_build.return_value = mock_service

        GoogleToken.objects.create(user=self.teacher, credentials=json.dumps(FAKE_CREDS))

        self.client.force_authenticate(self.dean)
        payload = {
            "title": "Dean -> Teacher default reminder",
            "description": "should go to teacher calendar",
            "deadline": self.deadline.isoformat().replace("+00:00", "Z"),
            "assignee_id": self.teacher.id,
        }
        resp = self.client.post(self.url, payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)

        todo = ToDo.objects.get(title="Dean -> Teacher default reminder")
        self.assertEqual(todo.calendar_event_id, "evt-15min")

        body = mock_service.events().insert.call_args.kwargs["body"]
        self.assertIn("reminders", body)
        self.assertFalse(body["reminders"]["useDefault"])
        self.assertEqual(body["reminders"]["overrides"], [{"method": "popup", "minutes": 15}])

        self.assertEqual(Notification.objects.filter(user=self.dean).count(), 0)

    def test_dean_draft_with_reminders_is_forbidden(self):
        self.client.force_authenticate(self.dean)
        payload = {
            "title": "Dean draft with reminders",
            "description": "invalid",
            "deadline": self.deadline.isoformat().replace("+00:00", "Z"),
            "reminders": [
                {"method": "popup", "minutes": 15}
            ],
        }
        resp = self.client.post(self.url, payload, format="json")
        self.assertEqual(resp.status_code, 400)
        msg = resp.data.get("message", {}).get("reminders", [""])[0]
        self.assertIn("Dean drafts cannot have reminders", msg)

    @patch("apps.todo_app.services.send_notification_task.apply_async")
    @patch("apps.todo_app.services.send_notification_task.delay")
    def test_dean_assigns_teacher_without_calendar_fallback_notifications_for_teacher_only(self, mock_delay, mock_apply_async):
        self.client.force_authenticate(self.dean)
        payload = {
            "title": "Dean->Teacher fallback",
            "description": "fallback reminders",
            "deadline": self.deadline.isoformat().replace("+00:00", "Z"),
            "assignee_id": self.teacher.id,
            "reminders": [
                {"method": "popup", "minutes": 15},
                {"method": "popup", "minutes": 30},
                {"method": "popup", "minutes": 60},
                {"method": "popup", "minutes": 1440},
                {"method": "popup", "minutes": 30},
            ],
        }
        resp = self.client.post(self.url, payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)

        self.assertEqual(Notification.objects.filter(user=self.dean).count(), 0)
        reminders_notifications = Notification.objects.filter(title="Напоминание о задаче", user=self.teacher)
        self.assertEqual(reminders_notifications.count(), 5)
        self.assertGreaterEqual(mock_delay.call_count + mock_apply_async.call_count, 1)

    def test_dean_has_calendar_but_teacher_has_not_event_not_created(self):
        GoogleToken.objects.create(user=self.dean, credentials=json.dumps(FAKE_CREDS))

        self.client.force_authenticate(self.dean)
        payload = {
            "title": "Dean token only",
            "description": "should not create event",
            "deadline": self.deadline.isoformat().replace("+00:00", "Z"),
            "assignee_id": self.teacher.id,
        }
        resp = self.client.post(self.url, payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)

        todo = ToDo.objects.get(title="Dean token only")
        self.assertIsNone(todo.calendar_event_id, "Event must not be created in dean's calendar for teacher task")
        self.assertEqual(Notification.objects.filter(user=self.dean).count(), 0)

    def _mock_google_service(self, event_id="evt-1"):
        from unittest.mock import MagicMock
        mock_service = MagicMock()

        calendar_list_obj = MagicMock()
        calendar_list_obj.list.return_value.execute.return_value = {
            "items": [{"summary": "TSU Consult", "id": "cal-1"}]
        }
        mock_service.calendarList.return_value = calendar_list_obj

        events_obj = MagicMock()
        events_obj.insert.return_value.execute.return_value = {"id": event_id}
        mock_service.events.return_value = events_obj

        calendars_obj = MagicMock()
        calendars_obj.insert.return_value.execute.return_value = {"id": "cal-created"}
        mock_service.calendars.return_value = calendars_obj

        return mock_service
