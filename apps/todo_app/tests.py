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

from core.exceptions import EventNotFound

from apps.auth_app.models import User
from apps.notification_app.models import Notification
from apps.todo_app.config import MAX_TITLE_LENGTH, MAX_DESCRIPTION_LENGTH, TEACHER_DEFAULT_REMINDERS
from apps.todo_app.fallback.services import FallbackReminderService
from apps.todo_app.models import ToDo
from apps.todo_app.utils import normalize_reminders_for_fallback


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

    def test_update_as_creator(self):
        self.client.force_authenticate(user=self.dean)
        data = {
            "title": "Updated title"
        }
        with patch("apps.todo_app.views.sync_calendars") as mock_sync:
            response = self.client.put(self.url, data, format="json")

        self.assertEqual(response.status_code, 200)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.title, "Updated title")
        mock_sync.assert_called_once()

    def test_update_as_assignee_allowed_fields(self):
        self.client.force_authenticate(user=self.teacher)
        reminders = [{"method": "popup", "minutes": 10}]
        data = {
            "status": "done",
            "reminders": reminders
        }
        with patch("apps.todo_app.views.sync_calendars") as mock_sync:
            response = self.client.put(self.url, data, format="json")

        self.assertEqual(response.status_code, 200)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.status, "done")
        self.assertEqual(self.todo.reminders, reminders)
        mock_sync.assert_called_once()

    def test_update_as_assignee_forbidden_fields(self):
        self.client.force_authenticate(user=self.teacher)
        data = {
            "title": "New title"
        }
        response = self.client.put(self.url, data, format="json")

        self.assertEqual(response.status_code, 400)

    def test_update_as_other_user_forbidden(self):
        self.client.force_authenticate(user=self.other_teacher)
        data = {
            "title": "Attempted update"
        }
        response = self.client.put(self.url, data, format="json")

        self.assertEqual(response.status_code, 403)

    def test_update_invalid_title_length(self):
        self.client.force_authenticate(user=self.dean)
        data = {
            "title": "x" * 300
        }
        response = self.client.put(self.url, data, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("title", response.data.get("message", {}))

    def test_update_invalid_deadline(self):
        self.client.force_authenticate(user=self.dean)
        data = {
            "deadline": timezone.now() - timedelta(days=1)
        }
        response = self.client.put(self.url, data, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("deadline", response.data.get("message", {}))

    def test_update_triggers_calendar_sync(self):
        self.client.force_authenticate(user=self.dean)
        data = {
            "title": "Updated title"
        }

        with patch("apps.todo_app.views.sync_calendars") as mock_sync, \
             patch("apps.todo_app.calendar.managers.GoogleCalendarService") as mock_calendar_service:
            response = self.client.put(self.url, data, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(mock_sync.called)

    def test_update_notifies_new_assignee(self):
        self.client.force_authenticate(user=self.dean)
        data = {
            "assignee_id": self.other_teacher.id
        }

        with patch("apps.todo_app.views.sync_calendars") as mock_sync_notify:
            response = self.client.put(self.url, data, format="json")

        self.assertEqual(response.status_code, 200)
        mock_sync_notify.assert_called_once()

    def test_put_updates_existing_calendar_event_not_creates_new(self):
        self.client.force_authenticate(user=self.dean)
        data = {"title": "Updated title calendar", "reminders": [{"method": "popup", "minutes": 10}]}

        with patch("apps.todo_app.calendar.managers.GoogleCalendarService") as mock_calendar_class:
            mock_inst = mock_calendar_class.return_value
            mock_inst.service = True
            mock_inst.update_event.return_value = True
            mock_inst.create_event.return_value = 'should-not-be-created'

            response = self.client.put(self.url, data, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(mock_inst.update_event.called)
        mock_inst.create_event.assert_not_called()
        calls = mock_inst.update_event.call_args_list
        found_with_reminders = any((len(c.args) >= 2 and c.args[1] == data["reminders"]) or
                                   (len(c[0]) >= 2 and c[0][1] == data["reminders"]) for c in calls)
        self.assertTrue(found_with_reminders)

    def test_put_when_calendar_event_missing_returns_404(self):
        self.client.force_authenticate(user=self.dean)
        data = {"title": "Any title"}

        with patch("apps.todo_app.views.sync_calendars", side_effect=EventNotFound('missing-eid')):
            response = self.client.put(self.url, data, format="json")

        self.assertEqual(response.status_code, 404)

    def test_put_changes_assignee_marks_fallback_notifications_failed_if_old_not_integrated(self):
        future = timezone.now() + timedelta(hours=1)
        pending_notif = Notification.objects.create(
            user=self.teacher,
            todo=self.todo,
            title='Напоминание о задаче',
            message='Test',
            type=Notification.Type.TELEGRAM,
            status=Notification.Status.PENDING,
            scheduled_for=future,
            celery_task_id='celery-123'
        )

        self.client.force_authenticate(user=self.dean)
        data = {"assignee_id": self.other_teacher.id}

        with patch("apps.todo_app.calendar.managers.GoogleCalendarService") as mock_calendar_manager_gc, \
             patch("apps.todo_app.utils.GoogleCalendarService") as mock_utils_gc, \
             patch('celery.current_app.control.revoke') as mock_revoke:
            mock_calendar_manager_gc.return_value.service = False

            mock_utils_inst = mock_utils_gc.return_value
            mock_utils_inst.service = False

            response = self.client.put(self.url, data, format="json")

        self.assertEqual(response.status_code, 200)

        pending_notif.refresh_from_db()
        self.assertEqual(pending_notif.status, Notification.Status.FAILED)
        self.assertIsNone(pending_notif.celery_task_id)
        self.assertIn('Assignee changed', (pending_notif.last_error or ''))
        mock_revoke.assert_called_once_with('celery-123', terminate=False)

    def test_put_changes_assignee_deletes_old_calendar_event_if_integrated(self):
        self.client.force_authenticate(user=self.dean)
        data = {
            "assignee_id": self.other_teacher.id
        }

        with patch("apps.todo_app.calendar.managers.GoogleCalendarService") as mock_calendar_manager_gc, \
             patch("apps.todo_app.utils.GoogleCalendarService") as mock_utils_gc:
            mock_calendar_manager_gc.return_value.service = False

            mock_utils_inst = mock_utils_gc.return_value
            mock_utils_inst.service = True
            mock_utils_inst.delete_event = Mock()

            response = self.client.put(self.url, data, format="json")

        self.assertEqual(response.status_code, 200)
        mock_utils_inst.delete_event.assert_called_once()
        notif_exists = Notification.objects.filter(user=self.other_teacher,
                                                   title__icontains='Вас назначили на задачу').exists()
        self.assertTrue(notif_exists)

    def test_only_assignee_can_change_status_dean_cannot(self):
        self.client.force_authenticate(user=self.dean)
        data = {"status": "done"}
        response = self.client.put(self.url, data, format="json")

        self.assertEqual(response.status_code, 400)
        message = response.data.get('message', {}) if isinstance(getattr(response, 'data', None),
                                                                 dict) else response.data
        self.assertTrue(isinstance(message, dict) and 'status' in message)

    def test_only_assignee_can_change_status_other_teacher_forbidden(self):
        self.client.force_authenticate(user=self.other_teacher)
        data = {"status": "done"}
        response = self.client.put(self.url, data, format="json")

        self.assertIn(response.status_code, (403, 404))

    def test_dean_edit_keeps_assignee_reminders_when_assignee_unchanged(self):
        custom = [{"method": "popup", "minutes": 7}]
        self.todo.assignee_reminders = custom
        self.todo.save()

        self.client.force_authenticate(user=self.dean)
        data = {
            "title": "Dean edit title"
        }
        with patch("apps.todo_app.views.sync_calendars") as mock_sync:
            response = self.client.put(self.url, data, format="json")

        self.assertEqual(response.status_code, 200)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.assignee_reminders, custom)

    def test_dean_edit_sets_assignee_reminders_to_default_on_assignee_change(self):
        custom = [{"method": "popup", "minutes": 7}]
        self.todo.assignee_reminders = custom
        self.todo.save()

        self.client.force_authenticate(user=self.dean)
        data = {"assignee_id": self.other_teacher.id}

        with patch("apps.todo_app.calendar.managers.GoogleCalendarService") as mock_calendar_manager_gc, \
             patch("apps.todo_app.utils.GoogleCalendarService") as mock_utils_gc:
            mock_calendar_manager_gc.return_value.service = False
            mock_utils_gc.return_value.service = False

            response = self.client.put(self.url, data, format="json")

        self.assertEqual(response.status_code, 200)
        self.todo.refresh_from_db()
        self.assertEqual(self.todo.assignee_reminders, TEACHER_DEFAULT_REMINDERS)
