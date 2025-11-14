from datetime import timedelta
from django.utils import timezone
from django.urls import reverse
from rest_framework.test import APITestCase
from unittest.mock import patch

from apps.auth_app.models import User
from apps.notification_app.models import Notification


class FallbackReminderTests(APITestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="teacher_fallback",
            email="tf@example.com",
            password="pwd",
            role=User.Role.TEACHER,
            status=User.Status.ACTIVE,
        )
        self.url = reverse("todo-create")
        self.deadline = (timezone.now() + timedelta(hours=2)).replace(microsecond=0)

    @patch("apps.todo_app.services.send_notification_task.apply_async")
    @patch("apps.todo_app.services.send_notification_task.delay")
    def test_fallback_creates_telegram_notifications_with_allowed_minutes(self, mock_delay, mock_apply_async):
        self.client.force_authenticate(self.teacher)
        payload = {
            "title": "Fallback Task",
            "description": "No calendar integration",
            "deadline": self.deadline.isoformat().replace("+00:00", "Z"),
            "reminders": [
                {"method": "popup", "minutes": 15},
                {"method": "popup", "minutes": 30},
                {"method": "popup", "minutes": 60},
                {"method": "popup", "minutes": 1440},
                {"method": "popup", "minutes": 30},
            ]
        }
        resp = self.client.post(self.url, payload, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)

        reminders_notifications = Notification.objects.filter(title="Напоминание о задаче", user=self.teacher)
        self.assertEqual(reminders_notifications.count(), 4)

        messages = list(reminders_notifications.values_list("message", flat=True))
        self.assertTrue(any("За 15 мин" in m for m in messages), messages)
        self.assertTrue(any("За 30 мин" in m for m in messages), messages)
        self.assertTrue(any("За 1 час" in m for m in messages), messages)
        self.assertTrue(any("подходит к дедлайну" in m for m in messages), messages)

        self.assertEqual(mock_apply_async.call_count, 3)
        self.assertGreaterEqual(mock_delay.call_count, 1)

    @patch("apps.todo_app.services.send_notification_task.apply_async")
    @patch("apps.todo_app.services.send_notification_task.delay")
    def test_fallback_deduplicates_same_minutes(self, mock_delay, mock_apply_async):
        self.client.force_authenticate(self.teacher)
        payload = {
            "title": "Fallback Task Dedupe",
            "description": "Duplicates",
            "deadline": self.deadline.isoformat().replace("+00:00", "Z"),
            "reminders": [
                {"method": "popup", "minutes": 15},
                {"method": "popup", "minutes": 15},
                {"method": "popup", "minutes": 30},
                {"method": "popup", "minutes": 30},
                {"method": "popup", "minutes": 60},
                {"method": "popup", "minutes": 1440},
                {"method": "popup", "minutes": 1440},
            ]
        }
        resp = self.client.post(self.url, payload, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)

        reminders_notifications = Notification.objects.filter(title="Напоминание о задаче", user=self.teacher)
        self.assertEqual(reminders_notifications.count(), 4)

        messages = list(reminders_notifications.values_list("message", flat=True))
        self.assertEqual(sum(1 for m in messages if "За 15 мин" in m), 1)
        self.assertEqual(sum(1 for m in messages if "За 30 мин" in m), 1)
        self.assertEqual(sum(1 for m in messages if "За 1 час" in m), 1)
        self.assertEqual(sum(1 for m in messages if "подходит к дедлайну" in m), 1)

        self.assertEqual(mock_apply_async.call_count, 3)
        self.assertGreaterEqual(mock_delay.call_count, 1)
