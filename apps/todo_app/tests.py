from contextlib import contextmanager
from datetime import timedelta
from typing import Optional
from unittest.mock import patch, Mock

from celery.exceptions import CeleryError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from googleapiclient.errors import HttpError
from rest_framework.test import APIClient, APITestCase

from apps.auth_app.models import User
from apps.notification_app.models import Notification
from apps.todo_app.calendar import managers as calendar_managers
from apps.todo_app.config import MAX_TITLE_LENGTH, MAX_DESCRIPTION_LENGTH, TEACHER_DEFAULT_REMINDERS
from apps.todo_app.fallback.services import FallbackReminderService
from apps.todo_app.models import ToDo
from apps.todo_app.utils import normalize_reminders_for_fallback, build_future_assignee_reminders
from core.exceptions import EventNotFound


class BaseTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        setattr(self.client, 'raise_request_exception', False)

        self.task_delay_patcher = patch('apps.notification_app.tasks.send_notification_task.delay')
        self.mock_task_delay = self.task_delay_patcher.start()
        self.addCleanup(self.task_delay_patcher.stop)

        self.task_apply_async_patcher = patch('apps.notification_app.tasks.send_notification_task.apply_async')
        self.mock_task_apply_async = self.task_apply_async_patcher.start()
        self.addCleanup(self.task_apply_async_patcher.stop)
        mock_apply_async_ret = Mock()
        mock_apply_async_ret.id = 'mock-task-id'
        self.mock_task_apply_async.return_value = mock_apply_async_ret

        self.models_logger_exc_patcher = patch('apps.todo_app.models.logger.exception')
        self.mock_models_logger_exc = self.models_logger_exc_patcher.start()
        self.addCleanup(self.models_logger_exc_patcher.stop)
        self.mock_models_logger_exc.return_value = None

        self.utils_logger_exc_patcher = patch('apps.todo_app.utils.logger.exception')
        self.mock_utils_logger_exc = self.utils_logger_exc_patcher.start()
        self.addCleanup(self.utils_logger_exc_patcher.stop)
        self.mock_utils_logger_exc.return_value = None
        self.calendar_services_logger_exc_patcher = patch('apps.todo_app.calendar.services.logger.exception')
        self.mock_services_logger_exc = self.calendar_services_logger_exc_patcher.start()
        self.addCleanup(self.calendar_services_logger_exc_patcher.stop)
        self.mock_services_logger_exc.return_value = None
        self.fallback_services_logger_exc_patcher = patch('apps.todo_app.fallback.services.logger.exception')
        self.mock_services_logger_exc = self.fallback_services_logger_exc_patcher.start()
        self.addCleanup(self.fallback_services_logger_exc_patcher.stop)
        self.mock_services_logger_exc.return_value = None

    @contextmanager
    def patched_calendar_and_fallback(self, mock_service=None):
        if mock_service is None:
            mock_service = Mock()
        with patch('apps.todo_app.calendar.managers.GoogleCalendarService',
                   return_value=mock_service), patch('apps.todo_app.fallback.services.FallbackReminderService.'
                                                     'schedule_fallback_reminders') as mock_fallback:
            yield mock_service, mock_fallback

    def post_with_calendar_and_fallback(self, user, data, mock_service=None):
        self.client.force_authenticate(user=user)
        with self.patched_calendar_and_fallback(mock_service) as (svc, mock_fallback):
            resp = self.client.post('/todo/', data, format='json')
        return resp, svc, mock_fallback

    @staticmethod
    def make_calendar_mock(service: Optional[bool] = True, create_event_return=None,
                           create_event_side_effect=None) -> Mock:
        mock_service = Mock()
        mock_service.service = bool(service)
        mock_service.update_event.return_value = False
        if create_event_return is not None:
            mock_service.create_event.return_value = create_event_return
        if create_event_side_effect is not None:
            mock_service.create_event.side_effect = create_event_side_effect
        return mock_service

    def post_todo_as(self, user, data, *, service: Optional[bool] = True, create_event_return=None,
                     create_event_side_effect=None):
        mock_service = self.make_calendar_mock(service=service,
                                               create_event_return=create_event_return,
                                               create_event_side_effect=create_event_side_effect)
        return self.post_with_calendar_and_fallback(user, data, mock_service)


class ToDoCreateTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.teacher1 = User.objects.create_user(email='t1@example.com', username='t1', role='teacher')
        self.teacher2 = User.objects.create_user(email='t2@example.com', username='t2', role='teacher')
        self.dean = User.objects.create_user(email='dean@example.com', username='dean', role='dean')
        self.student = User.objects.create_user(email='s@example.com', username='s', role='student')

        self.signals_delay_patcher = patch('apps.notification_app.signals.send_notification_task.delay')
        self.mock_signals_delay = self.signals_delay_patcher.start()
        self.addCleanup(self.signals_delay_patcher.stop)
        self.mock_signals_delay.return_value = None

    def post_as(self, user, data):
        self.client.force_authenticate(user=user)
        return self.client.post('/todo/', data, format='json')

    def assert_task_for_self_created(self, title, data, expected_reminders, expected_assignee_reminders):
        resp = self.post_as(self.teacher1, data)
        self.assertEqual(resp.status_code, 201)

        todo = ToDo.objects.get(title=title)
        self.assertEqual(todo.creator_id, self.teacher1.id)
        self.assertEqual(todo.assignee_id, self.teacher1.id)

        self.assertTrue(isinstance(todo.reminders, list) and len(todo.reminders) == expected_reminders)

        self.assertTrue(
            isinstance(todo.assignee_reminders, list) and len(todo.assignee_reminders) == expected_assignee_reminders)

    def test_teacher_can_create_with_title_only(self):
        resp = self.post_as(self.teacher1, {'title': 'T Title Only'})
        self.assertEqual(resp.status_code, 201)
        todo = ToDo.objects.get(title='T Title Only')
        self.assertEqual(todo.creator_id, self.teacher1.id)
        self.assertEqual(todo.assignee_id, self.teacher1.id)
        self.assertTrue(isinstance(todo.reminders, list))

    def test_teacher_can_create_with_title_and_description(self):
        resp = self.post_as(self.teacher1, {
            'title': 'T With Desc',
            'description': 'Some description'
        })
        self.assertEqual(resp.status_code, 201)
        todo = ToDo.objects.get(title='T With Desc')
        self.assertEqual(todo.description, 'Some description')
        self.assertEqual(todo.creator_id, self.teacher1.id)

    def test_teacher_can_create_with_deadline(self):
        future = timezone.now() + timedelta(minutes=10)
        resp = self.post_as(self.teacher1, {
            'title': 'T With Deadline',
            'deadline': future.isoformat()
        })
        self.assertEqual(resp.status_code, 201)
        todo = ToDo.objects.get(title='T With Deadline')
        self.assertIsNotNone(todo.deadline)
        delta = abs((todo.deadline - future).total_seconds())
        self.assertLessEqual(delta, 2)

    def test_teacher_can_create_full_fields(self):
        future = timezone.now() + timedelta(minutes=15)
        data = {
            'title': 'T Full',
            'description': 'Full',
            'deadline': future.isoformat(),
            'assignee_id': self.teacher1.id,
            'reminders': [{'method': 'popup', 'minutes': 15}],
        }
        resp = self.post_as(self.teacher1, data)
        self.assertEqual(resp.status_code, 201)
        todo = ToDo.objects.get(title='T Full')
        self.assertEqual(todo.description, 'Full')
        self.assertEqual(todo.creator_id, self.teacher1.id)
        self.assertEqual(todo.assignee_id, self.teacher1.id)
        self.assertTrue(isinstance(todo.reminders, list) and len(todo.reminders) > 0)

    def test_teacher_cannot_set_other_assignee(self):
        data = {
            'title': 'T Wrong Assignee',
            'assignee_id': self.teacher2.id
        }
        resp = self.post_as(self.teacher1, data)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('assignee_id', resp.data.get('message', {}))

    def test_deadline_cannot_be_in_past(self):
        past = timezone.now() - timedelta(minutes=10)
        resp = self.post_as(self.teacher1, {
            'title': 'T Past',
            'deadline': past.isoformat()
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn('deadline', resp.data.get('message', {}))

    def test_dean_must_provide_assignee_and_can_create(self):
        resp = self.post_as(self.dean, {
            'title': 'Dean Missing Assignee'
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn('assignee_id', resp.data.get('message', {}))

        resp = self.post_as(self.dean, {
            'title': 'Dean Task',
            'assignee_id': self.teacher1.id
        })
        self.assertEqual(resp.status_code, 201)
        todo = ToDo.objects.get(title='Dean Task')
        self.assertEqual(todo.creator_id, self.dean.id)
        self.assertEqual(todo.assignee_id, self.teacher1.id)
        self.assertTrue(isinstance(todo.assignee_reminders, list))

    def test_dean_cannot_assign_non_teacher(self):
        resp = self.post_as(self.dean, {
            'title': 'Dean Bad Assignee',
            'assignee_id': self.student.id
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn('assignee_id', resp.data.get('message', {}))

    def test_dean_cannot_assign_self(self):
        resp = self.post_as(self.dean, {
            'title': 'Dean Self Task',
            'assignee_id': self.dean.id
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn('assignee_id', resp.data.get('message', {}))

    def test_dean_provided_reminders_are_for_author_and_default_for_assignee(self):
        future = timezone.now() + timedelta(minutes=20)
        data = {
            'title': 'Dean With Reminders',
            'assignee_id': self.teacher1.id,
            'deadline': future.isoformat(),
            'reminders': [{'method': 'popup', 'minutes': 15}],
        }
        resp = self.post_as(self.dean, data)
        self.assertEqual(resp.status_code, 201)
        todo = ToDo.objects.get(title='Dean With Reminders')
        self.assertTrue(isinstance(todo.reminders, list) and len(todo.reminders) > 0)
        self.assertTrue(isinstance(todo.assignee_reminders, list))
        notif_exists = Notification.objects.filter(user=self.teacher1, title__icontains='Новая задача').exists()
        self.assertTrue(notif_exists)

    def test_dean_creates_task_for_teacher_with_given_reminders(self):
        future = timezone.now() + timedelta(hours=2)
        data = {
            'title': 'Task by Dean for Teacher',
            'assignee_id': self.teacher1.id,
            'deadline': future.isoformat(),
            'reminders': [{'method': 'popup', 'minutes': 60}],
        }

        resp = self.post_as(self.dean, data)
        self.assertEqual(resp.status_code, 201)

        todo = ToDo.objects.get(title='Task by Dean for Teacher')
        self.assertEqual(todo.assignee_id, self.teacher1.id)
        self.assertTrue(isinstance(todo.reminders, list) and len(todo.reminders) > 0)
        self.assertTrue(isinstance(todo.assignee_reminders, list) and len(
            todo.assignee_reminders) > 0)

    def test_dean_creates_task_for_teacher_with_empty_reminders(self):
        future = timezone.now() + timedelta(hours=2)
        data = {
            'title': 'Task by Dean for Teacher with Empty Reminders',
            'assignee_id': self.teacher1.id,
            'deadline': future.isoformat(),
            'reminders': []
        }

        resp = self.post_as(self.dean, data)
        self.assertEqual(resp.status_code, 201)

        todo = ToDo.objects.get(title='Task by Dean for Teacher with Empty Reminders')
        self.assertEqual(todo.assignee_id, self.teacher1.id)
        self.assertTrue(isinstance(todo.reminders, list) and len(todo.reminders) == 0)
        self.assertTrue(isinstance(todo.assignee_reminders, list) and len(todo.assignee_reminders) > 0)

    def test_dean_creates_task_for_teacher_without_reminders_key(self):
        future = timezone.now() + timedelta(hours=2)
        data = {
            'title': 'Task by Dean for Teacher without Reminders Key',
            'assignee_id': self.teacher1.id,
            'deadline': future.isoformat(),
        }

        resp = self.post_as(self.dean, data)
        self.assertEqual(resp.status_code, 201)

        todo = ToDo.objects.get(title='Task by Dean for Teacher without Reminders Key')
        self.assertEqual(todo.assignee_id, self.teacher1.id)
        self.assertTrue(isinstance(todo.reminders, list) and len(todo.reminders) > 0)
        self.assertTrue(isinstance(todo.assignee_reminders, list) and len(todo.assignee_reminders) > 0)

    def test_teacher_creates_task_for_self_with_only_given_reminders(self):
        future = timezone.now() + timedelta(hours=2)
        data = {
            'title': 'Task by Teacher for Self',
            'deadline': future.isoformat(),
            'reminders': [{'method': 'popup', 'minutes': 60}],
        }

        self.assert_task_for_self_created('Task by Teacher for Self', data, expected_reminders=1,
                                          expected_assignee_reminders=0)

    def test_teacher_creates_task_for_self_with_empty_reminders(self):
        future = timezone.now() + timedelta(hours=2)
        data = {
            'title': 'Task by Teacher for Self',
            'deadline': future.isoformat(),
            'reminders': [],
        }

        self.assert_task_for_self_created('Task by Teacher for Self', data, expected_reminders=0,
                                          expected_assignee_reminders=0)

    def test_teacher_creates_task_for_self_without_reminders_key(self):
        future = timezone.now() + timedelta(hours=2)
        data = {
            'title': 'Task by Teacher for Self',
            'deadline': future.isoformat()
        }

        self.assert_task_for_self_created('Task by Teacher for Self', data, expected_reminders=1,
                                          expected_assignee_reminders=0)


class ToDoPermissionTests(TestCase):
    def setUp(self):
        self.teacher1 = User.objects.create_user(email='t1@example.com', username='t1', role='teacher')
        self.teacher2 = User.objects.create_user(email='t2@example.com', username='t2', role='teacher')
        self.dean1 = User.objects.create_user(email='dean1@example.com', username='dean1', role='dean')
        self.dean2 = User.objects.create_user(email='dean2@example.com', username='dean2', role='dean')

        self.client = APIClient()

        self.t1_personal = ToDo.objects.create(title='T1 Personal', creator=self.teacher1, assignee=self.teacher1)
        self.dean1_for_t1 = ToDo.objects.create(title='Dean1 -> T1', creator=self.dean1, assignee=self.teacher1)
        self.dean1_for_t2 = ToDo.objects.create(title='Dean1 -> T2', creator=self.dean1, assignee=self.teacher2)
        self.t2_personal = ToDo.objects.create(title='T2 Personal', creator=self.teacher2, assignee=self.teacher2)
        self.dean2_for_t1 = ToDo.objects.create(title='Dean2 -> T1', creator=self.dean2, assignee=self.teacher1)

    def list_as(self, user):
        self.client.force_authenticate(user=user)
        resp = self.client.get(reverse('todo-list'))

        try:
            data = getattr(resp, 'data', None)
            if isinstance(data, dict) and 'results' in data:
                resp.data = data['results']
        except (AttributeError, TypeError):
            pass
        return resp

    def retrieve_as(self, user, todo_id):
        self.client.force_authenticate(user=user)
        return self.client.get(f'/todo/{todo_id}/')

    def test_teacher_sees_only_own_todos(self):
        resp = self.list_as(self.teacher1)
        self.assertEqual(resp.status_code, 200)
        titles = {item['title'] for item in resp.data}
        self.assertIn('T1 Personal', titles)
        self.assertIn('Dean1 -> T1', titles)
        self.assertIn('Dean2 -> T1', titles)
        self.assertNotIn('T2 Personal', titles)
        self.assertNotIn('Dean1 -> T2', titles)

    def test_teacher_cannot_see_todos_created_by_dean_for_other_teachers(self):
        resp = self.list_as(self.teacher2)
        self.assertEqual(resp.status_code, 200)
        titles = {item['title'] for item in resp.data}
        self.assertIn('T2 Personal', titles)
        self.assertNotIn('Dean1 -> T1', titles)

    def test_teacher_cannot_see_other_teachers_todos(self):
        resp = self.retrieve_as(self.teacher1, self.t2_personal.id)
        self.assertIn(resp.status_code, (403, 404))

    def test_dean_sees_only_todos_they_created(self):
        resp = self.list_as(self.dean1)
        self.assertEqual(resp.status_code, 200)
        titles = {item['title'] for item in resp.data}
        self.assertIn('Dean1 -> T1', titles)
        self.assertIn('Dean1 -> T2', titles)
        self.assertNotIn('T1 Personal', titles)
        self.assertNotIn('T2 Personal', titles)
        self.assertNotIn('Dean2 -> T1', titles)

    def test_dean_cannot_see_teachers_personal_todos(self):
        resp = self.retrieve_as(self.dean1, self.t1_personal.id)
        self.assertIn(resp.status_code, (403, 404))

    def test_dean_cannot_see_todos_created_by_other_deans(self):
        resp = self.list_as(self.dean1)
        self.assertEqual(resp.status_code, 200)
        titles = {item['title'] for item in resp.data}
        self.assertNotIn('Dean2 -> T1', titles)

    def test_unauthenticated_access_is_unauthorized(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get('/todo/')
        self.assertEqual(resp.status_code, 401)
        resp = self.client.get(f'/todo/{self.t1_personal.id}/')
        self.assertEqual(resp.status_code, 401)


class ToDoCalendarTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.teacher = User.objects.create_user(email='gc@example.com', username='gc', role='teacher')
        self.dean = User.objects.create_user(email='gcdean@example.com', username='gcdean', role='dean')

    def raise_http(*args, **kwargs):
        raise HttpError(Mock(), b'error')

    def test_create_with_deadline_uses_google_calendar_when_author_integrated_teacher(self):
        future = timezone.now() + timedelta(hours=1)
        data = {
            'title': 'GC Teacher Task',
            'deadline': future.isoformat(),
            'reminders': [{'method': 'popup', 'minutes': 15}]
        }

        resp, svc, mock_fallback = self.post_todo_as(self.teacher, data, service=True,
                                                     create_event_return='gcal-evt-teacher-1')

        self.assertEqual(resp.status_code, 201)
        todo = ToDo.objects.get(title='GC Teacher Task')
        self.assertEqual(todo.calendar_event_id, 'gcal-evt-teacher-1')
        svc.create_event.assert_called()
        mock_fallback.assert_not_called()

    def test_create_with_deadline_uses_google_calendar_when_author_integrated_dean(self):
        future = timezone.now() + timedelta(hours=1)
        data = {
            'title': 'GC Dean Task',
            'assignee_id': self.teacher.id,
            'deadline': future.isoformat(),
            'reminders': [{'method': 'popup', 'minutes': 15}]
        }

        resp, svc, mock_fallback = self.post_todo_as(self.dean, data, service=True,
                                                     create_event_return='gcal-evt-dean-1')

        self.assertEqual(resp.status_code, 201)
        todo = ToDo.objects.get(title='GC Dean Task')
        self.assertEqual(todo.calendar_event_id, 'gcal-evt-dean-1')
        svc.create_event.assert_called()
        mock_fallback.assert_not_called()

    def test_no_integration_schedules_celery_reminders(self):
        future = timezone.now() + timedelta(hours=1)
        data = {
            'title': 'No GC Task',
            'deadline': future.isoformat(),
            'reminders': [{'method': 'popup', 'minutes': 15}]
        }

        resp, svc, mock_fallback = self.post_todo_as(self.teacher, data, service=None)

        self.assertEqual(resp.status_code, 201)
        todo = ToDo.objects.get(title='No GC Task')
        self.assertIsNone(getattr(todo, 'calendar_event_id', None))
        mock_fallback.assert_called()

    def test_google_api_error_schedules_fallback_and_propagates(self):
        future = timezone.now() + timedelta(hours=1)
        data = {
            'title': 'GC Error Task',
            'deadline': future.isoformat(),
            'reminders': [{'method': 'popup', 'minutes': 15}]
        }

        resp, svc, mock_fallback = self.post_todo_as(self.teacher, data, service=True,
                                                     create_event_side_effect=self.raise_http)

        self.assertIn(resp.status_code, (500, 201))
        todo = ToDo.objects.filter(title='GC Error Task').first()
        self.assertIsNotNone(todo)
        mock_fallback.assert_called()


class ToDoRemindersTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.teacher = User.objects.create_user(email='r_gc@example.com', username='r_gc', role='teacher')
        self.dean = User.objects.create_user(email='r_gc_dean@example.com', username='r_gc_dean', role='dean')
        self.future = timezone.now() + timedelta(hours=2)
        self.reminders = [{'method': 'popup', 'minutes': 15}, {'method': 'popup', 'minutes': 60}]
        self.assignee_reminders = [{'method': 'popup', 'minutes': 15}]

    def post_as(self, user, data):
        self.client.force_authenticate(user=user)
        return self.client.post('/todo/', data, format='json')

    def test_reminders_created_for_author_if_specified(self):
        future = timezone.now() + timedelta(hours=1)
        data = {
            'title': 'Rem Author',
            'deadline': future.isoformat(),
            'reminders': [{'method': 'popup', 'minutes': 15}]
        }

        resp, svc, _ = self.post_todo_as(self.teacher, data, service=None)

        self.assertEqual(resp.status_code, 201)
        todo = ToDo.objects.get(title='Rem Author')
        self.assertTrue(isinstance(todo.reminders, list) and len(todo.reminders) > 0)

    def test_reminders_created_for_assignee_if_teacher(self):
        future = timezone.now() + timedelta(hours=1)
        data = {
            'title': 'Rem For Assignee',
            'assignee_id': self.teacher.id,
            'deadline': future.isoformat()
        }

        resp, svc, _ = self.post_todo_as(self.dean, data, service=None)

        self.assertEqual(resp.status_code, 201)
        todo = ToDo.objects.get(title='Rem For Assignee')
        self.assertTrue(isinstance(todo.assignee_reminders, list))
        self.assertGreaterEqual(len(todo.assignee_reminders), 1)

    def test_invalid_reminder_interval_rejected(self):
        future = timezone.now() + timedelta(hours=1)
        data = {
            'title': 'Bad Rem',
            'deadline': future.isoformat(),
            'reminders': [{'method': 'unknown', 'minutes': 15}, {'method': 'popup', 'minutes': 'abc'}]
        }

        self.client.force_authenticate(user=self.teacher)
        resp = self.client.post('/todo/', data, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('reminders', str(resp.data))

    @patch('apps.notification_app.tasks.send_notification_task.apply_async')
    def test_schedule_fallback_creates_notifications_with_correct_times(self, mock_apply_async):
        future = timezone.now() + timedelta(hours=2)
        todo = ToDo.objects.create(title='FallbackRem', creator=self.teacher, assignee=self.teacher, deadline=future)

        reminders = [{'method': 'popup', 'minutes': 15}, {'method': 'popup', 'minutes': 60}]

        mock_celery_task = Mock()
        mock_celery_task.id = 'mock-task-id'
        mock_apply_async.return_value = mock_celery_task

        FallbackReminderService().schedule_fallback_reminders(todo, reminders, target_user=self.teacher)

        notifs = Notification.objects.filter(user=self.teacher, title__icontains='Напоминание о задаче')
        self.assertTrue(notifs.exists())
        minutes = normalize_reminders_for_fallback(reminders)
        expected_times = [todo.deadline - timedelta(minutes=m) for m in minutes]

        scheduled = [n.scheduled_for for n in notifs]
        for et in expected_times:
            found = any(nf is not None and abs((nf - et).total_seconds()) < 2 for nf in scheduled)
            self.assertTrue(found)

    def test_no_immediate_notifications_created(self):
        todo = ToDo.objects.create(title="Test Task", creator=self.teacher, assignee=self.teacher, deadline=self.future)

        FallbackReminderService().schedule_fallback_reminders(todo, self.reminders, target_user=self.teacher)

        notifs = Notification.objects.filter(todo=todo, scheduled_for__lte=timezone.now())
        self.assertEqual(notifs.count(), 0)

    def test_only_deferred_notifications_created(self):
        todo = ToDo.objects.create(title="Test Task", creator=self.teacher, assignee=self.teacher, deadline=self.future)

        FallbackReminderService().schedule_fallback_reminders(todo, self.reminders, target_user=self.teacher)

        notifications = Notification.objects.filter(todo=todo)
        self.assertEqual(notifications.count(), 2)

        for notif in notifications:
            self.assertGreater(notif.scheduled_for, timezone.now())

    def test_correct_times_for_deferred_notifications(self):
        todo = ToDo.objects.create(title="Test Task", creator=self.teacher, assignee=self.teacher, deadline=self.future)

        FallbackReminderService().schedule_fallback_reminders(todo, self.reminders, target_user=self.teacher)

        notifications = Notification.objects.filter(todo=todo)

        expected_times = [todo.deadline - timedelta(minutes=m) for m in [15, 60]]

        for notif, expected_time in zip(notifications, expected_times):
            self.assertAlmostEqual(notif.scheduled_for, expected_time, delta=timedelta(seconds=5))

    def test_no_notifications_for_past_deadline(self):
        past = timezone.now() - timedelta(hours=1)
        todo = ToDo.objects.create(title="Test Task", creator=self.teacher, assignee=self.teacher, deadline=past)

        FallbackReminderService().schedule_fallback_reminders(todo, self.reminders, target_user=self.teacher)

        notifications = Notification.objects.filter(todo=todo)
        self.assertEqual(notifications.count(), 0)

    def test_notifications_for_assignee_and_creator(self):
        todo = ToDo.objects.create(title="Test Task", creator=self.dean, assignee=self.teacher, deadline=self.future)

        FallbackReminderService().schedule_fallback_reminders(todo, self.reminders, target_user=self.dean)
        FallbackReminderService().schedule_fallback_reminders(todo, self.assignee_reminders, target_user=self.teacher)

        notifications = Notification.objects.filter(user=self.dean)
        self.assertGreater(notifications.count(), 0)

        notifications_for_assignee = Notification.objects.filter(user=self.teacher)
        self.assertGreater(notifications_for_assignee.count(), 0)


class ToDoNotificationTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.teacher = User.objects.create_user(email='n_gc@example.com', username='n_gc', role='teacher')
        self.dean = User.objects.create_user(email='n_gc_dean@example.com', username='n_gc_dean', role='dean')

    def test_teacher_gets_notification_when_dean_creates_todo_for_them(self):
        future = timezone.now() + timedelta(hours=1)
        data = {
            'title': 'Notify Dean -> Teacher',
            'assignee_id': self.teacher.id,
            'deadline': future.isoformat(),
            'reminders': [{'method': 'popup', 'minutes': 15}],
        }

        resp, svc, _ = self.post_todo_as(self.dean, data, service=None)

        self.assertEqual(resp.status_code, 201)
        notif_exists = Notification.objects.filter(user=self.teacher, title__icontains='Новая задача').exists()
        self.assertTrue(notif_exists)


class ToDoValidationTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.teacher = User.objects.create_user(email='v@example.com', username='v', role='teacher')

    def post_as(self, user, data):
        self.client.force_authenticate(user=user)
        return self.client.post('/todo/', data, format='json')

    def test_title_is_required(self):
        resp = self.post_as(self.teacher, {'description': 'No title'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('title', resp.data.get('message', {}))

    def test_title_and_description_length_limits(self):
        long_title = 'a' * (MAX_TITLE_LENGTH + 1)
        long_desc = 'b' * (MAX_DESCRIPTION_LENGTH + 1)
        resp = self.post_as(self.teacher, {'title': long_title})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('title', resp.data.get('message', {}))

        resp2 = self.post_as(self.teacher, {'title': 'Ok', 'description': long_desc})
        self.assertEqual(resp2.status_code, 400)
        self.assertIn('description', resp2.data.get('message', {}))

    def test_invalid_date_format_returns_error(self):
        resp = self.post_as(self.teacher, {'title': 'Bad Date', 'deadline': 'not-a-date'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('deadline', str(resp.data))

    def test_invalid_assignee_id_returns_error(self):
        resp = self.post_as(self.teacher, {'title': 'Bad Assignee', 'assignee_id': 999999})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('User with id', str(resp.data))


class ToDoErrorHandlingTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.teacher = User.objects.create_user(email='err_gc@example.com', username='err_gc', role='teacher')

    def test_celery_failure_is_logged_and_does_not_raise(self):
        future = timezone.now() + timedelta(hours=2)
        todo = ToDo.objects.create(title='CeleryFail', creator=self.teacher, assignee=self.teacher, deadline=future)

        reminders = [{'method': 'popup', 'minutes': 15}]

        self.mock_task_delay.side_effect = CeleryError('delay boom')
        self.mock_task_apply_async.side_effect = CeleryError('apply_async boom')

        FallbackReminderService().schedule_fallback_reminders(todo, reminders, target_user=self.teacher)

        self.assertTrue(self.mock_services_logger_exc.called)

    def test_google_api_http_error_handled_and_fallback_used(self):
        future = timezone.now() + timedelta(hours=1)
        data = {
            'title': 'GC Error Handling Test',
            'deadline': future.isoformat(),
            'reminders': [{'method': 'popup', 'minutes': 15}],
        }

        def raise_http(*args, **kwargs):
            raise HttpError(Mock(), b'error')

        resp, svc, mock_fallback = self.post_todo_as(self.teacher, data, service=True,
                                                     create_event_side_effect=raise_http)

        self.assertIn(resp.status_code, (500, 201))
        self.assertTrue(mock_fallback.called)


class ToDoListViewTestCase(APITestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(email="teacher@example.com", username="teacher", role="teacher")
        self.dean = User.objects.create_user(email="dean@example.com", username="dean", role="dean")
        self.student = User.objects.create_user(email="student@example.com", username="student", role="student")

        self.todo1 = ToDo.objects.create(title="Teacher's Task", creator=self.teacher, assignee=self.teacher,
                                         status="in progress")
        self.todo2 = ToDo.objects.create(title="Assigned Task", creator=self.dean, assignee=self.teacher, status="done")
        self.todo3 = ToDo.objects.create(title="Another Teacher's Task", creator=self.dean, assignee=self.teacher,
                                         status="in progress")

    def create_tasks(self, count, creator=None):
        if creator is None:
            creator = self.teacher
        for i in range(count):
            ToDo.objects.create(title=f"Task {i + 1}", creator=creator, assignee=creator)

    def authenticate_and_check_todos(self, user, expected_todos_count, expected_titles):
        self.client.force_authenticate(user=user)

        response = self.client.get(reverse('todo-list'))

        self.assertEqual(response.status_code, 200)
        todos = response.data['results']
        self.assertEqual(len(todos), expected_todos_count)

        for title in expected_titles:
            self.assertTrue(any(todo['title'] == title for todo in todos))

    def test_admin_cannot_access_todos(self):
        admin = User.objects.create_user(email="admin_test@example.com", username="admin_test", role="admin")
        self.client.force_authenticate(user=admin)

        response = self.client.get(reverse('todo-list'))

        self.assertEqual(response.status_code, 403)

    def test_teacher_can_access_own_and_assigned_todos(self):
        expected_titles = ["Teacher's Task", "Assigned Task", "Another Teacher's Task"]
        self.authenticate_and_check_todos(self.teacher, 3, expected_titles)

    def test_dean_can_access_all_todos(self):
        expected_titles = ["Assigned Task", "Another Teacher's Task"]
        self.authenticate_and_check_todos(self.dean, 2, expected_titles)

    def test_student_cannot_access_todos(self):
        self.client.force_authenticate(user=self.student)

        response = self.client.get(reverse('todo-list'))

        self.assertEqual(response.status_code, 403)

    def test_filter_by_status_in_progress(self):
        self.client.force_authenticate(user=self.teacher)

        response = self.client.get(reverse('todo-list'), {'status': 'in progress'})

        self.assertEqual(response.status_code, 200)
        todos = response.data['results']
        self.assertEqual(len(todos), 2)
        self.assertTrue(any(todo['title'] == "Teacher's Task" for todo in todos))
        self.assertTrue(any(todo['title'] == "Another Teacher's Task" for todo in todos))

    def test_filter_by_status_done(self):
        self.client.force_authenticate(user=self.teacher)

        response = self.client.get(reverse('todo-list'), {'status': 'done'})

        self.assertEqual(response.status_code, 200)
        todos = response.data['results']
        self.assertEqual(len(todos), 1)
        self.assertTrue(any(todo['title'] == "Assigned Task" for todo in todos))

    def test_pagination(self):
        self.client.force_authenticate(user=self.teacher)

        self.create_tasks(15)

        response = self.client.get(reverse('todo-list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['results']), 10)
        self.assertIsNotNone(response.data['next'])
        self.assertIsNone(response.data['previous'])

    def test_second_page_pagination(self):
        self.client.force_authenticate(user=self.teacher)

        self.create_tasks(15)

        response = self.client.get(reverse('todo-list'), {'page': 2})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['results']), 8)
        self.assertIsNone(response.data['next'])
        self.assertIsNotNone(response.data['previous'])

    def test_empty_todos_list(self):
        self.client.force_authenticate(user=self.teacher)

        ToDo.objects.all().delete()

        response = self.client.get(reverse('todo-list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['results']), 0)

    def test_unauthorized_access(self):
        response = self.client.get(reverse('todo-list'))

        self.assertEqual(response.status_code, 401)

    def test_teacher_can_only_see_own_and_assigned_todos(self):
        expected_titles = ["Teacher's Task", "Assigned Task", "Another Teacher's Task"]
        self.authenticate_and_check_todos(self.teacher, 3, expected_titles)

        another_teacher = User.objects.create_user(email="teacher2@example.com", username="teacher2", role="teacher")
        ToDo.objects.create(title="Other Teacher's Task", creator=another_teacher,
                            assignee=another_teacher, status="in progress")

        response = self.client.get(reverse('todo-list'))
        self.assertEqual(response.status_code, 200)
        todos = response.data['results']
        self.assertEqual(len(todos), 3)
        self.assertFalse(any(todo['title'] == "Other Teacher's Task" for todo in todos))

    def test_task_assignment_changes_access(self):
        self.client.force_authenticate(user=self.teacher)

        response = self.client.get(reverse('todo-list'))
        self.assertEqual(response.status_code, 200)
        todos = response.data['results']
        self.assertTrue(any(todo['title'] == "Another Teacher's Task" for todo in todos))

        new_teacher = User.objects.create_user(email="new_teacher@example.com", username="new_teacher", role="teacher")
        self.todo3.assignee = new_teacher
        self.todo3.save()

        response = self.client.get(reverse('todo-list'))
        self.assertEqual(response.status_code, 200)
        todos = response.data['results']
        self.assertFalse(any(todo['title'] == "Another Teacher's Task" for todo in todos))
        self.assertTrue(any(todo['title'] == "Assigned Task" for todo in todos))
        self.assertTrue(any(todo['title'] == "Teacher's Task" for todo in todos))

        self.client.force_authenticate(user=new_teacher)
        response = self.client.get(reverse('todo-list'))
        self.assertEqual(response.status_code, 200)
        todos = response.data['results']
        self.assertTrue(any(todo['title'] == "Another Teacher's Task" for todo in todos))


class ToDoDetailViewTestCase(APITestCase):
    def setUp(self):
        self.creator = User.objects.create_user(email="creator@example.com", username="creator",
                                                password="password", role="dean")
        self.assignee = User.objects.create_user(email="assignee@example.com", username="assignee",
                                                 password="password", role="teacher")
        self.other_user = User.objects.create_user(email="other@example.com", username="other",
                                                   password="password", role="student")

        self.todo = ToDo.objects.create(
            title="Test Task",
            description="Test Description",
            deadline=timezone.now() + timedelta(days=1),
            creator=self.creator,
            assignee=self.assignee,
        )

    def test_creator_can_access_todo(self):
        self.client.force_authenticate(user=self.creator)
        response = self.client.get(f"/todo/{self.todo.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['title'], "Test Task")

    def test_assignee_can_access_todo(self):
        self.client.force_authenticate(user=self.assignee)
        response = self.client.get(f"/todo/{self.todo.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['title'], "Test Task")

    def test_other_user_cannot_access_todo(self):
        self.client.force_authenticate(user=self.other_user)
        response = self.client.get(f"/todo/{self.todo.id}/")
        self.assertEqual(response.status_code, 403)

    def test_todo_not_found(self):
        self.client.force_authenticate(user=self.creator)
        response = self.client.get("/todo/999999/")
        self.assertEqual(response.status_code, 404)

    def test_invalid_todo_id(self):
        self.client.force_authenticate(user=self.creator)
        response = self.client.get("/todo/invalid_id/")
        self.assertEqual(response.status_code, 400)

        self.client.force_authenticate(user=None)
        response = self.client.get(f"/todo/{self.todo.id}/")
        self.assertEqual(response.status_code, 401)


class ToDoUpdateTests(APITestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(email="teacher@example.com", username="teacher", role="teacher")
        self.dean = User.objects.create_user(email="dean@example.com", username="dean", role="dean")
        self.other_teacher = User.objects.create_user(email="other@example.com", username="other", role="teacher")

        self.todo = ToDo.objects.create(
            title="Initial title",
            description="Initial desc",
            deadline=timezone.now() + timedelta(days=1),
            creator=self.dean,
            assignee=self.teacher,
            reminders=[{"method": "popup", "minutes": 5}]
        )

        self.url = f"/todo/{self.todo.id}/"
        self._send_telegram_patcher = patch('apps.notification_app.services.send_telegram_notification')
        self._mock_send_telegram = self._send_telegram_patcher.start()
        self.addCleanup(self._send_telegram_patcher.stop)
        self._mock_send_telegram.return_value = None

        self.send_telegram_tasks_patcher = patch('apps.notification_app.tasks.send_telegram_notification')
        self.mock_send_telegram_tasks = self.send_telegram_tasks_patcher.start()
        self.addCleanup(self.send_telegram_tasks_patcher.stop)
        self.mock_send_telegram_tasks.return_value = None

    def post_as(self, user, data):
        self.client.force_authenticate(user=user)
        return self.client.post('/todo/', data, format='json')

    def _patch_reminders_and_get(self, user, url=None, minutes=10):
        if url is None:
            url = self.url
        self.client.force_authenticate(user=user)
        new_reminders = [{'method': 'popup', 'minutes': minutes}]
        resp = self.client.patch(url, {'reminders': new_reminders}, format='json')
        self.assertEqual(resp.status_code, 200)

        if url == self.url:
            self.todo.refresh_from_db()
            return self.todo, new_reminders
        else:
            tid = int(url.rstrip('/').split('/')[-1])
            todo = ToDo.objects.get(id=tid)
            return todo, new_reminders

    def _patch_deadline(self, user, url=None, days=2):
        if url is None:
            url = self.url
        self.client.force_authenticate(user=user)
        new_deadline = timezone.now() + timedelta(days=days)
        resp = self.client.patch(url, {'deadline': new_deadline.isoformat()}, format='json')
        self.assertEqual(resp.status_code, 200)
        return resp, new_deadline

    def _patch_assignee(self, user, assignee_id):
        self.client.force_authenticate(user=user)
        resp = self.client.patch(self.url, {'assignee_id': assignee_id}, format='json')
        self.todo.refresh_from_db()
        return resp

    def _patch_status(self, user, status):
        self.client.force_authenticate(user=user)
        return self.client.patch(self.url, {'status': status}, format='json')

    def test_update_title(self):
        self.client.force_authenticate(user=self.dean)
        new_title = "Updated Title"
        resp = self.client.patch(self.url, {'title': new_title}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.title, new_title)

    def test_update_description(self):
        self.client.force_authenticate(user=self.dean)
        new_desc = "Updated Description"
        resp = self.client.patch(self.url, {'description': new_desc}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.description, new_desc)

    def test_update_deadline(self):
        resp, future = self._patch_deadline(self.dean, days=2)
        self.todo.refresh_from_db()
        self.assertIsNotNone(self.todo.deadline)
        delta = abs((self.todo.deadline - future).total_seconds())
        self.assertLessEqual(delta, 2)

    def test_update_assignee(self):
        resp = self._patch_assignee(self.dean, self.other_teacher.id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.todo.assignee_id, self.other_teacher.id)

    def test_dean_reassigns_teacher_cleans_up_old_assignee_integrations(self):
        self.todo.deadline = timezone.now() + timedelta(hours=2)
        self.todo.save(update_fields=['deadline'])

        with patch('apps.todo_app.utils.GoogleCalendarService') as mock_gc_cls, \
                patch('apps.todo_app.utils.cancel_pending_notifications_for_user') as mock_cancel:
            mock_gc = mock_gc_cls.return_value
            mock_gc.service = True
            mock_gc.delete_event = Mock()

            resp = self._patch_assignee(self.dean, self.other_teacher.id)

        self.assertEqual(resp.status_code, 200)
        mock_gc_cls.assert_called_once_with(user=self.teacher)
        mock_gc.delete_event.assert_called_once()
        mock_cancel.assert_called_once()
        cancel_args, cancel_kwargs = mock_cancel.call_args
        self.assertEqual(cancel_args[1], self.teacher)
        self.assertEqual(cancel_kwargs.get('reason'), 'Assignee changed by dean')

    def test_dean_reassigns_teacher_creates_future_assignee_reminders(self):
        future_deadline = timezone.now() + timedelta(hours=3)
        self.todo.deadline = future_deadline
        self.todo.save(update_fields=['deadline'])
        expected_defaults = build_future_assignee_reminders(future_deadline, TEACHER_DEFAULT_REMINDERS)

        resp = self._patch_assignee(self.dean, self.other_teacher.id)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.todo.assignee_id, self.other_teacher.id)
        self.assertEqual(self.todo.assignee_reminders, expected_defaults)

    def test_dean_reassigns_teacher_skips_defaults_when_deadline_past(self):
        self.todo.deadline = timezone.now() - timedelta(hours=1)
        self.todo.save(update_fields=['deadline'])

        resp = self._patch_assignee(self.dean, self.other_teacher.id)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.todo.assignee_reminders, [])

    def test_dean_reassigns_teacher_skips_defaults_when_notify_time_passed(self):
        self.todo.deadline = timezone.now() + timedelta(minutes=5)
        self.todo.save(update_fields=['deadline'])

        resp = self._patch_assignee(self.dean, self.other_teacher.id)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.todo.assignee_reminders, [])

    def test_dean_reassigns_same_assignee_keeps_state(self):
        self.todo.assignee_reminders = [{'method': 'popup', 'minutes': 45}]
        self.todo.assignee_calendar_event_id = 'keep-existing'
        self.todo.assignee_calendar_event_active = True
        self.todo.save(update_fields=['assignee_reminders', 'assignee_calendar_event_id',
                                      'assignee_calendar_event_active'])

        self.client.force_authenticate(user=self.dean)
        resp = self._patch_assignee(self.dean, self.teacher.id)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.todo.assignee_id, self.teacher.id)
        self.assertEqual(self.todo.assignee_reminders, [{'method': 'popup', 'minutes': 45}])
        self.assertEqual(self.todo.assignee_calendar_event_id, 'keep-existing')
        self.assertTrue(self.todo.assignee_calendar_event_active)

    def test_teacher_cannot_update_assignee(self):
        resp = self._patch_assignee(self.teacher, self.other_teacher.id)

        self.assertEqual(resp.status_code, 400)
        self.assertIn('Teachers can only assign tasks to themselves', str(resp.data))
        self.assertEqual(self.todo.assignee_id, self.teacher.id)

    def test_update_reminders(self):
        todo, new_reminders = self._patch_reminders_and_get(self.dean)
        self.assertTrue(isinstance(todo.reminders, list) and len(todo.reminders) > 0)

    def test_cannot_set_empty_title_on_update(self):
        self.client.force_authenticate(user=self.dean)
        resp = self.client.patch(self.url, {'title': ''}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('title', str(resp.data))

    def test_title_and_description_length_limits_on_update(self):
        self.client.force_authenticate(user=self.dean)
        long_title = 'a' * (MAX_TITLE_LENGTH + 1)
        resp = self.client.patch(self.url, {'title': long_title}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('title', str(resp.data))

        long_desc = 'b' * (MAX_DESCRIPTION_LENGTH + 1)
        resp2 = self.client.patch(self.url, {'description': long_desc}, format='json')
        self.assertEqual(resp2.status_code, 400)
        self.assertIn('description', str(resp2.data))

    def test_deadline_cannot_be_in_past_on_update(self):
        self.client.force_authenticate(user=self.dean)
        past = timezone.now() - timedelta(minutes=10)
        resp = self.client.patch(self.url, {'deadline': past.isoformat()}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('deadline', str(resp.data))

    def test_invalid_assignee_on_update(self):
        self.client.force_authenticate(user=self.dean)
        resp = self.client.patch(self.url, {'assignee_id': 999999}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('User with id', str(resp.data))

        student = User.objects.create_user(email='stu@example.com', username='stu', role='student')
        resp2 = self.client.patch(self.url, {'assignee_id': student.id}, format='json')
        self.assertEqual(resp2.status_code, 400)
        self.assertIn('assignee_id', str(resp2.data))

    def test_dean_cannot_remove_assignee(self):
        self.client.force_authenticate(user=self.dean)
        resp = self.client.patch(self.url, {'assignee_id': None}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.assignee_id, self.teacher.id)

    def test_invalid_reminders_on_update(self):
        self.client.force_authenticate(user=self.dean)
        bad_reminders = [{'method': 'unknown', 'minutes': 15}, {'method': 'popup', 'minutes': 'abc'}]
        resp = self.client.patch(self.url, {'reminders': bad_reminders}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('reminders', str(resp.data))

    def test_status_validation_on_update(self):
        self.client.force_authenticate(user=self.dean)
        resp = self.client.patch(self.url, {'status': 'non-existent-status'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('status', str(resp.data))

    def test_dean_cannot_change_status_when_not_assignee(self):
        resp = self._patch_status(self.dean, ToDo.Status.DONE)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Only assignee may change status', str(resp.data))
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.status, ToDo.Status.IN_PROGRESS)

    def test_assignee_can_change_status(self):
        resp = self._patch_status(self.teacher, ToDo.Status.DONE)
        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.status, ToDo.Status.DONE)

    def test_assignee_can_reopen_task(self):
        self.todo.status = ToDo.Status.DONE
        self.todo.save(update_fields=['status'])

        resp = self._patch_status(self.teacher, ToDo.Status.IN_PROGRESS)

        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.status, ToDo.Status.IN_PROGRESS)

    def test_dean_updates_reminders_updates_creator_reminders(self):
        self.client.force_authenticate(user=self.dean)
        new_reminders = [{'method': 'popup', 'minutes': 10}]
        resp = self.client.patch(self.url, {'reminders': new_reminders}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()
        self.assertTrue(isinstance(self.todo.reminders, list) and len(self.todo.reminders) > 0)
        self.assertTrue(any(r.get('minutes') == 10 for r in self.todo.reminders))
        ar = self.todo.assignee_reminders
        if ar is None:
            self.assertIsNone(ar)
        else:
            self.assertTrue(isinstance(ar, list))
            self.assertFalse(any(r.get('minutes') == 10 for r in ar))

    def test_assignee_updates_reminders_updates_assignee_reminders(self):
        self.client.force_authenticate(user=self.teacher)
        new_reminders = [{'method': 'popup', 'minutes': 20}]
        resp = self.client.patch(self.url, {'reminders': new_reminders}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()
        self.assertTrue(isinstance(self.todo.assignee_reminders, list))
        self.assertTrue(any(r.get('minutes') == 20 for r in self.todo.assignee_reminders))
        self.assertTrue(isinstance(self.todo.reminders, list))
        self.assertFalse(any(r.get('minutes') == 20 for r in self.todo.reminders))

    def test_cannot_update_reminders_when_deadline_overdue_for_creator(self):
        self.todo.deadline = timezone.now() - timedelta(hours=1)
        self.todo.save(update_fields=['deadline'])

        self.client.force_authenticate(user=self.dean)
        resp = self.client.patch(self.url, {'reminders': [{'method': 'popup', 'minutes': 10}]}, format='json')

        self.assertEqual(resp.status_code, 400)
        self.assertIn('reminders', str(resp.data))

    def test_cannot_update_reminders_when_deadline_overdue_for_assignee(self):
        self.todo.deadline = timezone.now() - timedelta(hours=2)
        self.todo.save(update_fields=['deadline'])

        self.client.force_authenticate(user=self.teacher)
        resp = self.client.patch(self.url, {'reminders': [{'method': 'popup', 'minutes': 25}]}, format='json')

        self.assertEqual(resp.status_code, 400)
        self.assertIn('reminders', str(resp.data))

    def test_creator_equals_assignee_updates_only_reminders(self):
        same_teacher = User.objects.create_user(email='same@example.com', username='same', role='teacher')
        todo = ToDo.objects.create(
            title='Self Task',
            description='Self assigned',
            deadline=timezone.now() + timedelta(days=1),
            creator=same_teacher,
            assignee=same_teacher,
            reminders=[{'method': 'popup', 'minutes': 5}],
            assignee_reminders=[]
        )

        url = f"/todo/{todo.id}/"
        self.client.force_authenticate(user=same_teacher)
        new_reminders = [{'method': 'popup', 'minutes': 30}]
        resp = self.client.patch(url, {'reminders': new_reminders}, format='json')
        self.assertEqual(resp.status_code, 200)
        todo.refresh_from_db()
        self.assertTrue(isinstance(todo.reminders, list))
        self.assertTrue(any(r.get('minutes') == 30 for r in todo.reminders))
        self.assertEqual(todo.assignee_reminders, [])

    def test_deadline_change_cancels_only_deadline_reminders(self):
        now = timezone.now()
        future1 = now + timedelta(hours=3)
        future2 = now + timedelta(hours=4)

        notif_deadline = Notification.objects.create(
            user=self.teacher,
            todo=self.todo,
            title='Напоминание о задаче',
            message='deadline reminder',
            status=Notification.Status.PENDING,
            scheduled_for=future1,
        )

        notif_other = Notification.objects.create(
            user=self.teacher,
            todo=self.todo,
            title='Custom Reminder',
            message='other reminder',
            status=Notification.Status.PENDING,
            scheduled_for=future2,
        )

        with patch('apps.todo_app.utils.has_calendar_integration', return_value=False):
            self._patch_deadline(self.dean, days=2)

        notif_deadline.refresh_from_db()
        notif_other.refresh_from_db()

        self.assertEqual(notif_deadline.status, Notification.Status.FAILED)
        self.assertIsNone(notif_deadline.celery_task_id)
        self.assertIn('Deadline changed', (notif_deadline.last_error or ''))

        self.assertEqual(notif_other.status, Notification.Status.PENDING)

    def test_update_with_integration_recreates_event_when_deleted_from_calendar(self):
        self.todo.calendar_event_id = 'existing-event-id'
        self.todo.calendar_event_active = True

        self.todo.assignee_calendar_event_id = 'existing-assignee-event-id'
        self.todo.assignee_calendar_event_active = True
        self.todo.save(update_fields=['calendar_event_id', 'calendar_event_active',
                                      'assignee_calendar_event_id', 'assignee_calendar_event_active'])

        with patch('apps.todo_app.calendar.managers.GoogleCalendarService') as mock_gc_cls, \
                patch('apps.todo_app.views.has_calendar_integration', return_value=True):
            mock_gc = mock_gc_cls.return_value
            mock_gc.service = True
            mock_gc.update_event = Mock(side_effect=EventNotFound("Event not found"))
            mock_gc.create_event = Mock(return_value='new-event-id')

            self.client.force_authenticate(user=self.dean)
            new_title = "Updated Title"
            resp = self.client.patch(self.url, {'title': new_title}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_gc.update_event.call_count, 2)
        self.assertEqual(mock_gc.create_event.call_count, 2)

        self.todo.refresh_from_db()
        self.assertEqual(self.todo.title, new_title)
        self.assertEqual(self.todo.calendar_event_id, 'new-event-id')
        self.assertTrue(self.todo.calendar_event_active)
        self.assertEqual(self.todo.assignee_calendar_event_id, 'new-event-id')
        self.assertTrue(self.todo.assignee_calendar_event_active)

    def test_assignee_update_with_integration_recreates_event_when_deleted_from_calendar(self):
        self.todo.assignee_calendar_event_id = 'assignee-event-id'
        self.todo.assignee_calendar_event_active = True
        self.todo.assignee_reminders = [{'method': 'popup', 'minutes': 30}]
        self.todo.save(update_fields=['assignee_calendar_event_id', 'assignee_calendar_event_active',
                                      'assignee_reminders'])

        with patch('apps.todo_app.calendar.managers.GoogleCalendarService') as mock_gc_cls, \
                patch('apps.todo_app.views.has_calendar_integration', return_value=True):
            mock_gc = mock_gc_cls.return_value
            mock_gc.service = True
            mock_gc.update_event = Mock(side_effect=EventNotFound("Event not found"))
            mock_gc.create_event = Mock(return_value='new-assignee-event-id')

            self.client.force_authenticate(user=self.teacher)
            new_reminders = [{'method': 'popup', 'minutes': 45}]
            resp = self.client.patch(self.url, {'reminders': new_reminders}, format='json')

        self.assertEqual(resp.status_code, 200)
        mock_gc.update_event.assert_called_once()
        mock_gc.create_event.assert_called_once()

        self.todo.refresh_from_db()
        self.assertEqual(self.todo.assignee_reminders, new_reminders)
        self.assertEqual(self.todo.assignee_calendar_event_id, 'new-assignee-event-id')
        self.assertTrue(self.todo.assignee_calendar_event_active)

    def test_creator_update_recreates_only_deleted_creator_event_when_assignee_event_exists(self):
        self.todo.calendar_event_id = 'existing-creator-event-id'
        self.todo.calendar_event_active = True
        self.todo.assignee_calendar_event_id = 'existing-assignee-event-id'
        self.todo.assignee_calendar_event_active = True
        self.todo.save(update_fields=['calendar_event_id', 'calendar_event_active',
                                      'assignee_calendar_event_id', 'assignee_calendar_event_active'])

        def update_event_side_effect(todo, reminders):
            if update_event_side_effect.call_count == 0:
                update_event_side_effect.call_count += 1
                raise EventNotFound("Creator event not found")
            else:
                update_event_side_effect.call_count += 1
                return True

        update_event_side_effect.call_count = 0

        with patch('apps.todo_app.calendar.managers.GoogleCalendarService') as mock_gc_cls, \
                patch('apps.todo_app.views.has_calendar_integration', return_value=True):
            mock_gc = mock_gc_cls.return_value
            mock_gc.service = True
            mock_gc.update_event = Mock(side_effect=update_event_side_effect)
            mock_gc.create_event = Mock(return_value='new-creator-event-id')

            self.client.force_authenticate(user=self.dean)
            new_title = "Updated Title By Creator"
            resp = self.client.patch(self.url, {'title': new_title}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_gc.update_event.call_count, 2)
        mock_gc.create_event.assert_called_once()

        self.todo.refresh_from_db()
        self.assertEqual(self.todo.title, new_title)
        self.assertEqual(self.todo.calendar_event_id, 'new-creator-event-id')
        self.assertTrue(self.todo.calendar_event_active)
        self.assertEqual(self.todo.assignee_calendar_event_id, 'existing-assignee-event-id')
        self.assertTrue(self.todo.assignee_calendar_event_active)

    def test_put_accepts_full_object_with_all_fields(self):
        new_deadline = timezone.now() + timedelta(days=3)
        full_payload = {
            'title': 'Fully Updated Title',
            'description': 'Fully updated description',
            'deadline': new_deadline.isoformat(),
            'assignee_id': self.other_teacher.id,
            'reminders': [{'method': 'popup', 'minutes': 20}]
        }

        self.client.force_authenticate(user=self.dean)
        resp = self.client.put(self.url, full_payload, format='json')

        self.assertEqual(resp.status_code, 200, f"Response error: {resp.data}")
        self.todo.refresh_from_db()

        self.assertEqual(self.todo.title, 'Fully Updated Title')
        self.assertEqual(self.todo.description, 'Fully updated description')
        self.assertIsNotNone(self.todo.deadline)
        delta = abs((self.todo.deadline - new_deadline).total_seconds())
        self.assertLessEqual(delta, 2)
        self.assertEqual(self.todo.assignee_id, self.other_teacher.id)
        self.assertTrue(isinstance(self.todo.reminders, list))
        self.assertTrue(any(r.get('minutes') == 20 for r in self.todo.reminders))

    def test_put_accepts_partial_object_with_single_field(self):
        original_description = self.todo.description
        original_deadline = self.todo.deadline
        original_assignee_id = self.todo.assignee_id
        original_status = self.todo.status

        partial_payload = {
            'title': 'Partially Updated Title'
        }

        self.client.force_authenticate(user=self.dean)
        resp = self.client.put(self.url, partial_payload, format='json')

        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()

        self.assertEqual(self.todo.title, 'Partially Updated Title')
        self.assertEqual(self.todo.description, original_description)
        self.assertEqual(self.todo.deadline, original_deadline)
        self.assertEqual(self.todo.assignee_id, original_assignee_id)
        self.assertEqual(self.todo.status, original_status)

    def test_put_accepts_partial_object_with_multiple_fields(self):
        original_deadline = self.todo.deadline
        original_assignee_id = self.todo.assignee_id
        original_status = self.todo.status

        partial_payload = {
            'title': 'New Title',
            'description': 'New Description'
        }

        self.client.force_authenticate(user=self.dean)
        resp = self.client.put(self.url, partial_payload, format='json')

        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()

        self.assertEqual(self.todo.title, 'New Title')
        self.assertEqual(self.todo.description, 'New Description')
        self.assertEqual(self.todo.deadline, original_deadline)
        self.assertEqual(self.todo.assignee_id, original_assignee_id)
        self.assertEqual(self.todo.status, original_status)

    def test_put_partial_object_preserves_reminders_when_omitted(self):
        original_reminders = [{'method': 'popup', 'minutes': 15}]
        self.todo.reminders = original_reminders
        self.todo.save(update_fields=['reminders'])

        partial_payload = {
            'title': 'Title Without Reminders Update'
        }

        self.client.force_authenticate(user=self.dean)
        resp = self.client.put(self.url, partial_payload, format='json')

        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()

        self.assertEqual(self.todo.title, 'Title Without Reminders Update')
        self.assertEqual(self.todo.reminders, original_reminders)

    def test_put_partial_object_deadline_and_status_only(self):
        original_title = self.todo.title
        original_description = self.todo.description
        new_deadline = timezone.now() + timedelta(days=5)

        partial_payload = {
            'deadline': new_deadline.isoformat()
        }

        self.client.force_authenticate(user=self.dean)
        resp = self.client.put(self.url, partial_payload, format='json')

        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()

        self.assertIsNotNone(self.todo.deadline)
        delta = abs((self.todo.deadline - new_deadline).total_seconds())
        self.assertLessEqual(delta, 2)
        self.assertEqual(self.todo.title, original_title)
        self.assertEqual(self.todo.description, original_description)

    def test_update_succeeds_when_calendar_sync_raises_http_error(self):
        with patch('apps.todo_app.calendar.managers.CalendarSyncManager.sync_creator',
                   side_effect=HttpError(Mock(), b'Calendar API error')) as mock_logger:
            self.client.force_authenticate(user=self.dean)
            new_title = "Updated Title Despite Calendar Error"
            resp = self.client.patch(self.url, {'title': new_title}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.title, new_title)

    def test_update_succeeds_when_calendar_sync_raises_runtime_error(self):
        with patch('apps.todo_app.calendar.managers.CalendarSyncManager.sync_assignee',
                   side_effect=RuntimeError('Unexpected calendar error')):
            self.client.force_authenticate(user=self.teacher)
            new_reminders = [{'method': 'popup', 'minutes': 30}]
            resp = self.client.patch(self.url, {'reminders': new_reminders}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.assignee_reminders, new_reminders)

    def test_update_succeeds_when_calendar_sync_raises_generic_exception(self):
        with patch('apps.todo_app.calendar.managers.CalendarSyncManager.sync_creator',
                   side_effect=Exception('Unknown error in sync_calendars')):
            self.client.force_authenticate(user=self.dean)
            new_desc = "Updated Description Despite Generic Error"
            resp = self.client.patch(self.url, {'description': new_desc}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.description, new_desc)

    def _assert_sync_calendars_call_params(self, call, expected_todo, expected_actor, expected_old_assignee):
        self.assertEqual(call[0][0].id, expected_todo.id)
        self.assertEqual(call[0][1].id, expected_actor.id)
        self.assertEqual(call[0][2].id, expected_old_assignee.id)

    def test_sequential_updates_by_creator_and_assignee_no_conflicts(self):
        with patch('apps.todo_app.views.sync_calendars') as mock_sync:
            self.client.force_authenticate(user=self.dean)
            resp1 = self.client.patch(self.url, {'title': 'New Title by Creator'}, format='json')
            self.assertEqual(resp1.status_code, 200)

            self.client.force_authenticate(user=self.teacher)
            resp2 = self.client.patch(self.url, {'status': ToDo.Status.DONE}, format='json')
            self.assertEqual(resp2.status_code, 200)

            self.assertEqual(mock_sync.call_count, 2)

        self.todo.refresh_from_db()
        self.assertEqual(self.todo.title, 'New Title by Creator')
        self.assertEqual(self.todo.status, ToDo.Status.DONE)

    def test_multiple_sequential_updates_sync_called_correct_number_of_times(self):
        with patch('apps.todo_app.views.sync_calendars') as mock_sync:
            self.client.force_authenticate(user=self.dean)
            resp1 = self.client.patch(self.url, {'title': 'First Update'}, format='json')
            self.assertEqual(resp1.status_code, 200)

            resp2 = self.client.patch(self.url, {'description': 'Second Update'}, format='json')
            self.assertEqual(resp2.status_code, 200)

            self.client.force_authenticate(user=self.teacher)
            resp3 = self.client.patch(self.url, {'reminders': [{'method': 'popup', 'minutes': 45}]}, format='json')
            self.assertEqual(resp3.status_code, 200)

            resp4 = self.client.patch(self.url, {'status': ToDo.Status.DONE}, format='json')
            self.assertEqual(resp4.status_code, 200)

            self.assertEqual(mock_sync.call_count, 4)

        self.todo.refresh_from_db()
        self.assertEqual(self.todo.title, 'First Update')
        self.assertEqual(self.todo.description, 'Second Update')
        self.assertEqual(self.todo.assignee_reminders, [{'method': 'popup', 'minutes': 45}])
        self.assertEqual(self.todo.status, ToDo.Status.DONE)

    def test_concurrent_field_updates_no_data_loss(self):
        with patch('apps.todo_app.views.sync_calendars') as mock_sync:
            self.client.force_authenticate(user=self.dean)
            resp1 = self.client.patch(self.url, {
                'title': 'Updated by Dean',
                'description': 'Description by Dean'
            }, format='json')
            self.assertEqual(resp1.status_code, 200)

            self.client.force_authenticate(user=self.teacher)
            new_reminders = [{'method': 'popup', 'minutes': 60}]
            resp2 = self.client.patch(self.url, {'reminders': new_reminders}, format='json')
            self.assertEqual(resp2.status_code, 200)

            self.assertEqual(mock_sync.call_count, 2)

        self.todo.refresh_from_db()
        self.assertEqual(self.todo.title, 'Updated by Dean')
        self.assertEqual(self.todo.description, 'Description by Dean')
        self.assertEqual(self.todo.assignee_reminders, new_reminders)
        self.assertEqual(self.todo.reminders, [{"method": "popup", "minutes": 5}])

    def test_sync_calendars_called_with_correct_parameters_on_sequential_updates(self):
        with patch('apps.todo_app.views.sync_calendars') as mock_sync:
            self.client.force_authenticate(user=self.dean)
            resp1 = self.client.patch(self.url, {'title': 'Updated by Dean'}, format='json')
            self.assertEqual(resp1.status_code, 200)

            self.assertEqual(mock_sync.call_count, 1)
            first_call = mock_sync.call_args_list[0]
            self._assert_sync_calendars_call_params(first_call, self.todo, self.dean, self.teacher)

            self.client.force_authenticate(user=self.teacher)
            resp2 = self.client.patch(self.url, {'reminders': [{'method': 'popup', 'minutes': 30}]}, format='json')
            self.assertEqual(resp2.status_code, 200)

            self.assertEqual(mock_sync.call_count, 2)
            second_call = mock_sync.call_args_list[1]
            self._assert_sync_calendars_call_params(second_call, self.todo, self.teacher, self.teacher)

    def test_sync_calendars_receives_old_assignee_when_assignee_changes(self):
        with patch('apps.todo_app.views.sync_calendars') as mock_sync:
            self.client.force_authenticate(user=self.dean)
            resp = self.client.patch(self.url, {'assignee_id': self.other_teacher.id}, format='json')
            self.assertEqual(resp.status_code, 200)

            self.assertEqual(mock_sync.call_count, 1)
            call = mock_sync.call_args_list[0]
            self._assert_sync_calendars_call_params(call, self.todo, self.dean, self.teacher)

            self.todo.refresh_from_db()
            self.assertEqual(self.todo.assignee_id, self.other_teacher.id)

    def test_creator_update_with_integration_updates_calendar_event(self):
        self.todo.calendar_event_id = 'creator-event-123'
        self.todo.calendar_event_active = True
        self.todo.reminders = [{'method': 'popup', 'minutes': 10}]
        self.todo.save(update_fields=['calendar_event_id', 'calendar_event_active', 'reminders'])

        with patch('apps.todo_app.calendar.managers.GoogleCalendarService') as mock_gc_cls, \
                patch('apps.todo_app.views.has_calendar_integration', return_value=True):
            mock_gc = mock_gc_cls.return_value
            mock_gc.service = True
            mock_gc.update_event = Mock(return_value=True)

            self.client.force_authenticate(user=self.dean)
            new_title = "Updated Title with Integration"
            resp = self.client.patch(self.url, {'title': new_title}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(mock_gc.update_event.call_count, 1)

        self.todo.refresh_from_db()
        self.assertEqual(self.todo.title, new_title)
        self.assertEqual(self.todo.calendar_event_id, 'creator-event-123')
        self.assertTrue(self.todo.calendar_event_active)

    def test_assignee_update_with_integration_updates_calendar_event(self):
        self.todo.assignee_calendar_event_id = 'assignee-event-456'
        self.todo.assignee_calendar_event_active = True
        self.todo.assignee_reminders = [{'method': 'popup', 'minutes': 20}]
        self.todo.save(update_fields=['assignee_calendar_event_id', 'assignee_calendar_event_active',
                                      'assignee_reminders'])

        with patch('apps.todo_app.calendar.managers.GoogleCalendarService') as mock_gc_cls, \
                patch('apps.todo_app.views.has_calendar_integration', return_value=True):
            mock_gc = mock_gc_cls.return_value
            mock_gc.service = True
            mock_gc.update_event = Mock(return_value=True)

            self.client.force_authenticate(user=self.teacher)
            new_reminders = [{'method': 'popup', 'minutes': 50}]
            resp = self.client.patch(self.url, {'reminders': new_reminders}, format='json')

        self.assertEqual(resp.status_code, 200)
        mock_gc.update_event.assert_called_once()

        self.todo.refresh_from_db()
        self.assertEqual(self.todo.assignee_reminders, new_reminders)
        self.assertEqual(self.todo.assignee_calendar_event_id, 'assignee-event-456')
        self.assertTrue(self.todo.assignee_calendar_event_active)

    def test_both_creator_and_assignee_events_updated_when_creator_changes_task(self):
        self.todo.calendar_event_id = 'creator-event-789'
        self.todo.calendar_event_active = True
        self.todo.reminders = [{'method': 'popup', 'minutes': 15}]
        self.todo.assignee_calendar_event_id = 'assignee-event-789'
        self.todo.assignee_calendar_event_active = True
        self.todo.assignee_reminders = [{'method': 'popup', 'minutes': 30}]
        self.todo.save(update_fields=['calendar_event_id', 'calendar_event_active', 'reminders',
                                      'assignee_calendar_event_id', 'assignee_calendar_event_active',
                                      'assignee_reminders'])

        with patch('apps.todo_app.calendar.managers.GoogleCalendarService') as mock_gc_cls, \
                patch('apps.todo_app.views.has_calendar_integration', return_value=True):
            mock_gc = mock_gc_cls.return_value
            mock_gc.service = True
            mock_gc.update_event = Mock(return_value=True)

            self.client.force_authenticate(user=self.dean)
            new_description = "Updated description affecting both calendars"
            resp = self.client.patch(self.url, {'description': new_description}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_gc.update_event.call_count, 2)

        self.todo.refresh_from_db()
        self.assertEqual(self.todo.description, new_description)
        self.assertEqual(self.todo.calendar_event_id, 'creator-event-789')
        self.assertTrue(self.todo.calendar_event_active)
        self.assertEqual(self.todo.assignee_calendar_event_id, 'assignee-event-789')
        self.assertTrue(self.todo.assignee_calendar_event_active)

    def test_creator_update_with_integration_creates_event_if_not_exists(self):
        self.todo.calendar_event_id = None
        self.todo.calendar_event_active = False
        self.todo.reminders = [{'method': 'popup', 'minutes': 25}]
        self.todo.save(update_fields=['calendar_event_id', 'calendar_event_active', 'reminders'])

        with patch('apps.todo_app.calendar.managers.GoogleCalendarService') as mock_gc_cls, \
                patch('apps.todo_app.views.has_calendar_integration', return_value=True):
            mock_gc = mock_gc_cls.return_value
            mock_gc.service = True
            mock_gc.update_event = Mock(return_value=False)
            mock_gc.create_event = Mock(return_value='new-creator-event-id')

            self.client.force_authenticate(user=self.dean)
            new_title = "Title triggers event creation"
            resp = self.client.patch(self.url, {'title': new_title}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(mock_gc.create_event.call_count, 1)

        self.todo.refresh_from_db()
        self.assertEqual(self.todo.title, new_title)
        self.assertIsNotNone(self.todo.calendar_event_id)
        self.assertTrue(self.todo.calendar_event_active)

    def test_assignee_update_with_integration_creates_event_if_not_exists(self):
        self.todo.assignee_calendar_event_id = None
        self.todo.assignee_calendar_event_active = False
        self.todo.assignee_reminders = [{'method': 'popup', 'minutes': 35}]
        self.todo.save(update_fields=['assignee_calendar_event_id', 'assignee_calendar_event_active',
                                      'assignee_reminders'])

        with patch('apps.todo_app.calendar.managers.GoogleCalendarService') as mock_gc_cls, \
                patch('apps.todo_app.views.has_calendar_integration', return_value=True):
            mock_gc = mock_gc_cls.return_value
            mock_gc.service = True
            mock_gc.update_event = Mock(return_value=False)
            mock_gc.create_event = Mock(return_value='new-assignee-event-id')

            self.client.force_authenticate(user=self.teacher)
            new_reminders = [{'method': 'popup', 'minutes': 40}]
            resp = self.client.patch(self.url, {'reminders': new_reminders}, format='json')

        self.assertEqual(resp.status_code, 200)
        mock_gc.create_event.assert_called_once()

        self.todo.refresh_from_db()
        self.assertEqual(self.todo.assignee_reminders, new_reminders)
        self.assertIsNotNone(self.todo.assignee_calendar_event_id)
        self.assertTrue(self.todo.assignee_calendar_event_active)


class ToDoRemindersDeadlineUpdateTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.creator = User.objects.create_user(email="upd_creator@example.com", username="upd_creator",
                                                role="dean")
        self.assignee = User.objects.create_user(email="upd_assignee@example.com", username="upd_assignee",
                                                 role="teacher")

        self.deadline = timezone.now() + timedelta(hours=4)
        self.todo = ToDo.objects.create(
            title="Update Rem/Deadline",
            creator=self.creator,
            assignee=self.assignee,
            deadline=self.deadline,
            reminders=[{"method": "popup", "minutes": 15}],
            assignee_reminders=[{"method": "popup", "minutes": 60}],
        )

        self.gc_patcher = patch('apps.todo_app.calendar.managers.GoogleCalendarService')
        self.mock_gc_class = self.gc_patcher.start()
        self.addCleanup(self.gc_patcher.stop)
        self.mock_gc = self.mock_gc_class.return_value
        self.mock_gc.service = False

        self.utils_gc_patcher = patch('apps.todo_app.utils.GoogleCalendarService')
        self.mock_utils_gc_class = self.utils_gc_patcher.start()
        self.addCleanup(self.utils_gc_patcher.stop)
        self.mock_utils_gc = self.mock_utils_gc_class.return_value
        self.mock_utils_gc.service = False

        self.sync_patcher = patch('apps.todo_app.views.sync_calendars',
                                  side_effect=calendar_managers.sync_calendars)
        self.mock_sync = self.sync_patcher.start()
        self.addCleanup(self.sync_patcher.stop)

        self.revoke_patcher = patch('apps.todo_app.utils.current_app.control.revoke')
        self.mock_revoke = self.revoke_patcher.start()
        self.addCleanup(self.revoke_patcher.stop)
        self.mock_revoke.return_value = None

    def _create_notifications_for_reminders(self, user, minutes_list, status=Notification.Status.PENDING, shift=0):
        notifs = []
        for m in minutes_list:
            sched = self.deadline - timedelta(minutes=m) + timedelta(seconds=shift)
            n = Notification.objects.create(
                user=user,
                todo=self.todo,
                title='Напоминание о задаче',
                message='msg',
                type=Notification.Type.TELEGRAM,
                status=status,
                scheduled_for=sched,
                celery_task_id='task-%s' % m
            )
            notifs.append(n)
        return notifs

    def _put_as(self, user, payload):
        url = f"/todo/{self.todo.id}/"
        self.client.force_authenticate(user=user)
        return self.client.put(url, payload, format='json')

    def _put_deadline_none_as_creator(self):
        return self._put_as(self.creator, {"deadline": None})

    def test_creator_equals_assignee_reminders_omitted_no_change(self):
        teacher = User.objects.create_user(email='single@example.com', username='single', role='teacher')
        todo = ToDo.objects.create(title='SelfOwned', creator=teacher, assignee=teacher,
                                   deadline=timezone.now() + timedelta(hours=2),
                                   reminders=[{"method": "popup", "minutes": 30}],
                                   assignee_reminders=[{"method": "popup", "minutes": 30}])

        url = f"/todo/{todo.id}/"
        self.client.force_authenticate(user=teacher)
        resp = self.client.put(url, {"title": "New title no rem"}, format='json')
        self.assertEqual(resp.status_code, 200)
        todo.refresh_from_db()
        self.assertEqual(todo.reminders, [{"method": "popup", "minutes": 30}])
        self.assertEqual(todo.assignee_reminders, [{"method": "popup", "minutes": 30}])

    def test_creator_equals_assignee_reminders_empty_list_cancels_pending_only(self):
        self._create_notifications_for_reminders(self.creator, [15], status=Notification.Status.PENDING,
                                                 shift=60)
        self._create_notifications_for_reminders(self.creator, [30], status=Notification.Status.SENT,
                                                 shift=-3600)

        overdue_notif = Notification.objects.create(
            user=self.creator,
            todo=self.todo,
            title='Напоминание о задаче',
            message='msg',
            type=Notification.Type.TELEGRAM,
            status=Notification.Status.PENDING,
            scheduled_for=timezone.now() - timedelta(days=1),
            celery_task_id='task-45'
        )

        url = f"/todo/{self.todo.id}/"
        self.client.force_authenticate(user=self.creator)

        resp = self.client.put(url, {"reminders": []}, format='json')

        self.assertEqual(resp.status_code, 200)
        failed = Notification.objects.filter(todo=self.todo, status=Notification.Status.FAILED)
        sent = Notification.objects.filter(todo=self.todo, status=Notification.Status.SENT)

        self.assertTrue(failed.exists())
        self.assertTrue(sent.exists())

        overdue_notif.refresh_from_db()
        self.assertEqual(overdue_notif.status, Notification.Status.PENDING)

    def test_creator_equals_assignee_reminders_new_list_replaces_and_creates_only_future(self):
        self._create_notifications_for_reminders(self.creator, [15], status=Notification.Status.PENDING,
                                                 shift=60)

        url = f"/todo/{self.todo.id}/"
        self.client.force_authenticate(user=self.creator)

        new_reminders = [{"method": "popup", "minutes": 15}, {"method": "popup", "minutes": 60}]

        resp = self.client.put(url, {"reminders": new_reminders}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.reminders, new_reminders)

        minutes = normalize_reminders_for_fallback(new_reminders)
        expected_times = [self.todo.deadline - timedelta(minutes=m) for m in minutes]

        for et in expected_times:
            found = Notification.objects.filter(todo=self.todo, status=Notification.Status.PENDING,
                                                scheduled_for__gt=timezone.now())
            exists_close = any(abs((n.scheduled_for - et).total_seconds()) < 5 for n in found)
            self.assertTrue(exists_close, msg=f"No pending notification close to expected time {et}")

    def test_creator_not_equal_assignee_reminders_omitted_no_change_for_either(self):
        url = f"/todo/{self.todo.id}/"

        self.client.force_authenticate(user=self.assignee)
        resp = self.client.put(url, {"title": "Assignee edit no rem"}, format='json')

        self.assertEqual(resp.status_code, 400)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.reminders, [{"method": "popup", "minutes": 15}])
        self.assertEqual(self.todo.assignee_reminders, [{"method": "popup", "minutes": 60}])

    def test_creator_not_equal_assignee_reminders_empty_list_cancels_only_updater(self):
        self._create_notifications_for_reminders(self.assignee, [60], status=Notification.Status.PENDING,
                                                 shift=60)
        url = f"/todo/{self.todo.id}/"
        self.client.force_authenticate(user=self.assignee)

        resp = self.client.put(url, {"reminders": []}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.assignee_reminders, [])
        self.assertEqual(self.todo.reminders, [{"method": "popup", "minutes": 15}])

        failed = Notification.objects.filter(todo=self.todo, user=self.assignee, status=Notification.Status.FAILED)
        self.assertTrue(failed.exists())

    def test_creator_not_equal_assignee_reminders_new_list_replaces_only_updater_and_creates_future_only(self):
        url = f"/todo/{self.todo.id}/"
        self.client.force_authenticate(user=self.assignee)

        new_assignee_reminders = [{"method": "popup", "minutes": 5}, {"method": "popup", "minutes": 300}]

        resp = self.client.put(url, {"reminders": new_assignee_reminders}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.assignee_reminders, new_assignee_reminders)

        notifs = Notification.objects.filter(todo=self.todo, user=self.assignee, status=Notification.Status.PENDING)
        for n in notifs:
            self.assertGreater(n.scheduled_for, timezone.now())

    def test_deadline_change_without_reminders_creator_equals_assignee_recalculates_when_no_integration(self):
        self._create_notifications_for_reminders(self.creator, [15, 60], status=Notification.Status.PENDING,
                                                 shift=60)

        url = f"/todo/{self.todo.id}/"
        self.client.force_authenticate(user=self.creator)

        new_deadline = timezone.now() + timedelta(hours=1)

        resp = self.client.put(url, {"deadline": new_deadline.isoformat()}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()
        self.assertTrue(Notification.objects.filter(todo=self.todo, status=Notification.Status.FAILED).exists())
        new_pending = Notification.objects.filter(todo=self.todo, status=Notification.Status.PENDING,
                                                  scheduled_for__gt=timezone.now())
        self.assertTrue(new_pending.exists())

    def test_deadline_change_with_integration_updates_calendar_event(self):
        self.client.force_authenticate(user=self.creator)

        new_deadline = timezone.now() + timedelta(hours=1)

        self.mock_gc.service = True
        self.mock_gc.update_event = Mock()

        resp = self._put_as(self.creator, {"deadline": new_deadline.isoformat()})

        self.assertEqual(resp.status_code, 200)
        self.mock_gc.update_event.assert_called()
        self.mock_gc.service = False

    def test_deadline_change_creator_not_equal_assignee_recalculates_for_both_where_applicable(self):
        self._create_notifications_for_reminders(self.creator, [15], status=Notification.Status.PENDING,
                                                 shift=60)
        self._create_notifications_for_reminders(self.assignee, [60], status=Notification.Status.PENDING,
                                                 shift=60)

        new_deadline = timezone.now() + timedelta(hours=2)

        resp = self._put_as(self.creator, {"deadline": new_deadline.isoformat()})

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Notification.objects.filter(todo=self.todo, status=Notification.Status.FAILED,
                                                    user=self.creator).exists())
        self.assertTrue(Notification.objects.filter(todo=self.todo, status=Notification.Status.FAILED,
                                                    user=self.assignee).exists())
        self.assertTrue(Notification.objects.filter(todo=self.todo, status=Notification.Status.PENDING,
                                                    user=self.creator).exists())
        self.assertTrue(Notification.objects.filter(todo=self.todo, status=Notification.Status.PENDING,
                                                    user=self.assignee).exists())

    def test_deadline_change_with_reminders_empty_list_cancels_only_updater_when_not_same_user(self):
        self._create_notifications_for_reminders(self.creator, [15], status=Notification.Status.PENDING,
                                                 shift=60)
        self._create_notifications_for_reminders(self.assignee, [60], status=Notification.Status.PENDING,
                                                 shift=60)

        payload = {
            "deadline": (timezone.now() + timedelta(hours=6)).isoformat(),
            "reminders": []
        }
        resp = self._put_as(self.creator, payload)

        self.assertEqual(resp.status_code, 200)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.reminders, [])
        self.assertTrue(
            Notification.objects.filter(todo=self.todo, user=self.creator, status=Notification.Status.FAILED).exists())
        self.assertTrue(
            Notification.objects.filter(todo=self.todo, user=self.assignee, status=Notification.Status.FAILED).exists())

    def test_deadline_removed_cancels_fallback_notifications_when_no_integration(self):
        self._create_notifications_for_reminders(self.creator, [15], status=Notification.Status.PENDING,
                                                 shift=60)
        self._create_notifications_for_reminders(self.assignee, [60], status=Notification.Status.PENDING,
                                                 shift=60)

        self.client.force_authenticate(user=self.creator)

        resp = self._put_deadline_none_as_creator()
        self.assertEqual(resp.status_code, 200)

        failed_creator = Notification.objects.filter(todo=self.todo, user=self.creator,
                                                     status=Notification.Status.FAILED)
        failed_assignee = Notification.objects.filter(todo=self.todo, user=self.assignee,
                                                      status=Notification.Status.FAILED)
        self.assertTrue(failed_creator.exists())
        self.assertTrue(failed_assignee.exists())

        for n in list(failed_creator) + list(failed_assignee):
            self.assertIsNone(n.celery_task_id)
            self.assertIn('Deadline removed', (n.last_error or ''))

        self.assertTrue(self.mock_revoke.called)

    def test_deadline_removed_deletes_calendar_event_when_user_integrated(self):
        self.client.force_authenticate(user=self.creator)

        with patch('apps.todo_app.views.has_calendar_integration', return_value=True), \
                patch('apps.todo_app.views.GoogleCalendarService') as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.service = True
            mock_service.delete_event = Mock()

            resp = self._put_deadline_none_as_creator()

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(mock_service.delete_event.called)
        self.assertGreaterEqual(mock_service.delete_event.call_count, 1)
