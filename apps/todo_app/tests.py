from datetime import timedelta
from unittest.mock import patch, Mock
from contextlib import contextmanager
from typing import Optional

from django.test import TestCase
from django.utils import timezone
from googleapiclient.errors import HttpError
from rest_framework.test import APIClient

from apps.auth_app.models import User
from apps.notification_app.models import Notification
from apps.todo_app.models import ToDo
from apps.todo_app.services import FallbackReminderService
from apps.todo_app.utils import normalize_reminders_for_fallback
from apps.todo_app.config import MAX_TITLE_LENGTH, MAX_DESCRIPTION_LENGTH
from celery.exceptions import CeleryError


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
        self.mock_task_apply_async.return_value = None

        self.models_logger_exc_patcher = patch('apps.todo_app.models.logger.exception')
        self.mock_models_logger_exc = self.models_logger_exc_patcher.start()
        self.addCleanup(self.models_logger_exc_patcher.stop)
        self.mock_models_logger_exc.return_value = None

        self.utils_logger_exc_patcher = patch('apps.todo_app.utils.logger.exception')
        self.mock_utils_logger_exc = self.utils_logger_exc_patcher.start()
        self.addCleanup(self.utils_logger_exc_patcher.stop)
        self.mock_utils_logger_exc.return_value = None
        self.services_logger_exc_patcher = patch('apps.todo_app.services.logger.exception')
        self.mock_services_logger_exc = self.services_logger_exc_patcher.start()
        self.addCleanup(self.services_logger_exc_patcher.stop)
        self.mock_services_logger_exc.return_value = None

    @contextmanager
    def patched_calendar_and_fallback(self, mock_service=None):
        if mock_service is None:
            mock_service = Mock()
        with patch('apps.todo_app.views.GoogleCalendarService', return_value=mock_service), \
                patch('apps.todo_app.services.FallbackReminderService.schedule_fallback_reminders') as mock_fallback:
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

        self.sync_patcher = patch('apps.todo_app.views.sync_and_handle_event')
        self.mock_sync = self.sync_patcher.start()
        self.addCleanup(self.sync_patcher.stop)
        self.mock_sync.return_value = None

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
        return self.client.get('/todo/')

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

    @patch('apps.notification_app.tasks.send_notification_task.apply_async')
    def test_teacher_gets_notification_when_reminder_triggers_immediate(self, mock_apply_async):
        future = timezone.now() + timedelta(minutes=5)
        todo = ToDo.objects.create(title='NotifyImmediate', creator=self.teacher,
                                   assignee=self.teacher, deadline=future)

        reminders = [{'method': 'popup', 'minutes': 15}]

        mock_celery_task = Mock()
        mock_celery_task.id = 'mock-task-id'
        mock_apply_async.return_value = mock_celery_task

        FallbackReminderService().schedule_fallback_reminders(todo, reminders, target_user=self.teacher)

        notif = Notification.objects.filter(user=self.teacher, title__icontains='Напоминание о задаче').first()
        self.assertIsNotNone(notif)
        self.assertTrue(self.mock_task_delay.called)

    def test_dean_gets_notifications_only_for_todos_he_created_when_specified(self):
        future = timezone.now() + timedelta(hours=1)
        data = {
            'title': 'Dean Own With Reminders',
            'assignee_id': self.teacher.id,
            'deadline': future.isoformat(),
            'reminders': [{'method': 'popup', 'minutes': 15}],
        }

        resp, svc, mock_fallback = self.post_todo_as(self.dean, data, service=None)

        self.assertEqual(resp.status_code, 201)
        self.assertTrue(mock_fallback.called)
        called_args, called_kwargs = mock_fallback.call_args
        self.assertIn('target_user', called_kwargs)
        self.assertEqual(called_kwargs.get('target_user'), self.dean)

        future2 = timezone.now() + timedelta(hours=1)
        data2 = {
            'title': 'Teacher Personal With Reminders',
            'deadline': future2.isoformat(),
            'reminders': [{'method': 'popup', 'minutes': 15}],
        }
        resp2, svc2, _ = self.post_todo_as(self.teacher, data2, service=None)

        self.assertEqual(resp2.status_code, 201)
        dean_notifs = Notification.objects.filter(user=self.dean, title__icontains='Напоминание о задаче',
                                                  message__icontains='Teacher Personal With Reminders')
        self.assertFalse(dean_notifs.exists())


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
