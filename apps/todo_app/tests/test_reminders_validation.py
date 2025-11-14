from datetime import timedelta
from django.utils import timezone
from django.urls import reverse
from rest_framework.test import APITestCase

from apps.auth_app.models import User


class RemindersValidationTests(APITestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="teacher_val",
            email="tv@example.com",
            password="pwd",
            role=User.Role.TEACHER,
            status=User.Status.ACTIVE,
        )
        self.url = reverse("todo-create")
        self.deadline = (timezone.now() + timedelta(days=1)).replace(microsecond=0)

    def test_reminders_method_must_be_popup_when_no_calendar(self):
        self.client.force_authenticate(self.teacher)
        payload = {
            "title": "R1",
            "description": "desc",
            "deadline": self.deadline.isoformat().replace("+00:00", "Z"),
            "reminders": [
                {"method": "email", "minutes": 30}
            ]
        }
        resp = self.client.post(self.url, payload, format='json')
        self.assertEqual(resp.status_code, 400)
        msg = resp.data.get('message', {}).get('reminders', [""])[0]
        self.assertIn('Only method="popup" is allowed', msg)

    def test_reminders_minutes_must_be_in_allowed_set_when_no_calendar_too_small(self):
        self.client.force_authenticate(self.teacher)
        payload = {
            "title": "R2",
            "description": "desc",
            "deadline": self.deadline.isoformat().replace("+00:00", "Z"),
            "reminders": [
                {"method": "popup", "minutes": 10}
            ]
        }
        resp = self.client.post(self.url, payload, format='json')
        self.assertEqual(resp.status_code, 400)
        msg = resp.data.get('message', {}).get('reminders', [""])[0]
        self.assertIn('Minutes must be one of', msg)
        self.assertIn('15', msg)

    def test_reminders_minutes_must_be_in_allowed_set_when_no_calendar_invalid_value(self):
        self.client.force_authenticate(self.teacher)
        payload = {
            "title": "R3",
            "description": "desc",
            "deadline": self.deadline.isoformat().replace("+00:00", "Z"),
            "reminders": [
                {"method": "popup", "minutes": 20}
            ]
        }
        resp = self.client.post(self.url, payload, format='json')
        self.assertEqual(resp.status_code, 400)
        msg = resp.data.get('message', {}).get('reminders', [""])[0]
        self.assertIn('Minutes must be one of', msg)
        self.assertIn('15', msg)
        self.assertNotIn('20 мин', msg)
