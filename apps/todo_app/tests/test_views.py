from unittest.mock import patch

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.auth_app.models import User


class ToDoCreateViewTest(APITestCase):
    def setUp(self):
        self.teacher_user = User.objects.create_user(
            username="teacher",
            email="teacher@example.com",
            password="password",
            role=User.Role.TEACHER,
            status=User.Status.ACTIVE,
        )
        self.dean_user = User.objects.create_user(
            username="dean",
            email="dean@example.com",
            password="password",
            role=User.Role.DEAN,
            status=User.Status.ACTIVE,
        )
        self.student_user = User.objects.create_user(
            username="student",
            email="student@example.com",
            password="password",
            role=User.Role.STUDENT,
            status=User.Status.ACTIVE,
        )
        self.other_teacher = User.objects.create_user(
            username="other_teacher",
            email="other_teacher@example.com",
            password="password",
            role=User.Role.TEACHER,
            status=User.Status.ACTIVE,
        )
        self.url = reverse("todo-create")
        self.valid_data = {
            "title": "Test ToDo",
            "description": "Test Description",
            "deadline": "2025-11-12T12:00:00Z",
        }

    def _test_create_todo_success(self, user, assignee):
        self.client.force_authenticate(user=user)
        data = self.valid_data.copy()
        data["assignee_id"] = assignee.id
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response

    def _test_create_todo_without_assignee_success(self, user, mock_create_event, assignee_check):
        mock_create_event.return_value = "mock_event_id"
        self.client.force_authenticate(user=user)
        response = self.client.post(self.url, self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["creator"]["id"], user.id)
        assignee_check(response)
        mock_create_event.assert_called_once()

    @patch("apps.todo_app.services.GoogleCalendarService.create_event")
    def test_teacher_create_todo_for_self_success(self, mock_create_event):
        mock_create_event.return_value = "mock_event_id"
        response = self._test_create_todo_success(self.teacher_user, self.teacher_user)
        self.assertEqual(response.data["title"], self.valid_data["title"])
        self.assertEqual(response.data["creator"]["id"], self.teacher_user.id)
        self.assertEqual(response.data["assignee"]["id"], self.teacher_user.id)
        mock_create_event.assert_called_once()

    @patch("apps.notification_app.signals.send_notification_task.delay")
    def test_dean_create_todo_for_other_teacher_success(self, mock_send_notification):
        response = self._test_create_todo_success(self.dean_user, self.other_teacher)
        self.assertEqual(response.data["assignee"]["id"], self.other_teacher.id)
        mock_send_notification.assert_called_once()

    def test_dean_create_todo_for_self_fail(self):
        self.client.force_authenticate(user=self.dean_user)
        data = self.valid_data.copy()
        data["assignee_id"] = self.dean_user.id
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("apps.todo_app.services.GoogleCalendarService.create_event")
    def test_teacher_create_todo_without_assignee_success(self, mock_create_event):
        def assignee_check(response):
            self.assertEqual(response.data["assignee"]["id"], self.teacher_user.id)

        self._test_create_todo_without_assignee_success(self.teacher_user, mock_create_event, assignee_check)

    @patch("apps.todo_app.services.GoogleCalendarService.create_event")
    def test_dean_create_todo_without_assignee_success(self, mock_create_event):
        def assignee_check(response):
            self.assertIsNone(response.data["assignee"])

        self._test_create_todo_without_assignee_success(self.dean_user, mock_create_event, assignee_check)

    def test_teacher_create_todo_for_other_teacher_fail(self):
        self.client.force_authenticate(user=self.teacher_user)
        data = self.valid_data.copy()
        data["assignee_id"] = self.other_teacher.id
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_student_create_todo_fail(self):
        self.client.force_authenticate(user=self.student_user)
        data = self.valid_data.copy()
        data["assignee_id"] = self.student_user.id
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_user_create_todo_fail(self):
        response = self.client.post(self.url, self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_create_todo_invalid_data_fail(self):
        self.client.force_authenticate(user=self.teacher_user)
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
