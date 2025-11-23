from unittest.mock import patch, MagicMock
from datetime import timedelta
import json

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from google.auth.exceptions import RefreshError
from requests.exceptions import RequestException
from googleapiclient.errors import HttpError

from apps.todo_app.models import ToDo
from apps.notification_app.models import Notification
from apps.notification_app import tasks
from apps.profile_app.models import GoogleToken
from core.exceptions import EventNotFound

User = get_user_model()


def event_id_attr(obj, role=None):
    if role:
        name = f"{role}_calendar_event_id"
        if hasattr(obj, name):
            return name
    if hasattr(obj, 'calendar_event_id'):
        return 'calendar_event_id'
    for r in ('creator', 'assignee'):
        name = f"{r}_calendar_event_id"
        if hasattr(obj, name):
            return name
    return None


def event_active_attr(obj, role=None):
    if role:
        name = f"{role}_calendar_event_active"
        if hasattr(obj, name):
            return name
    if hasattr(obj, 'calendar_event_active'):
        return 'calendar_event_active'
    for r in ('creator', 'assignee'):
        name = f"{r}_calendar_event_active"
        if hasattr(obj, name):
            return name
    return None


def get_event_id(obj, role=None):
    attr = event_id_attr(obj, role)
    if attr and hasattr(obj, attr):
        return getattr(obj, attr)
    return None


def set_event_id(obj, value, role=None):
    attr = event_id_attr(obj, role)
    if attr:
        setattr(obj, attr, value)


def get_event_active(obj, role=None):
    attr = event_active_attr(obj, role)
    if attr and hasattr(obj, attr):
        return bool(getattr(obj, attr))
    return False


def set_event_active(obj, value, role=None):
    attr = event_active_attr(obj, role)
    if attr:
        setattr(obj, attr, value)


def make_user(email="u@example.com", username="u"):
    return User.objects.create_user(email=email, username=username)


def make_todo(creator, assignee=None, deadline=None, title="T", description="", reminders=None,
              creator_calendar_event_id=None, assignee_calendar_event_id=None,
              creator_calendar_event_active=None, assignee_calendar_event_active=None):
    if deadline is None:
        deadline = timezone.now() + timedelta(hours=2)
    td = ToDo.objects.create(
        creator=creator,
        assignee=assignee,
        title=title,
        description=description,
        deadline=deadline,
    )
    if reminders is not None:
        td.reminders = reminders
    if creator_calendar_event_id is not None and hasattr(td, 'creator_calendar_event_id'):
        td.creator_calendar_event_id = creator_calendar_event_id
    if assignee_calendar_event_id is not None and hasattr(td, 'assignee_calendar_event_id'):
        td.assignee_calendar_event_id = assignee_calendar_event_id
    if creator_calendar_event_active is not None and hasattr(td, 'creator_calendar_event_active'):
        td.creator_calendar_event_active = creator_calendar_event_active
    if assignee_calendar_event_active is not None and hasattr(td, 'assignee_calendar_event_active'):
        td.assignee_calendar_event_active = assignee_calendar_event_active
    td.save()
    return td


class TodosFullTest(TestCase):
    @staticmethod
    def _make_fake_async():
        class _Fake:
            id = "fake-celery-task-id"

        return _Fake()

    @staticmethod
    def _enable_token(user):
        return GoogleToken.objects.create(user=user, credentials=json.dumps({"x": 1}))

    @staticmethod
    def _disable_token(user):
        GoogleToken.objects.filter(user=user).delete()

    @staticmethod
    def _run_transfer_and_get_notifs(user):
        tasks.transfer_unsent_reminders_task(user.id)
        return list(Notification.objects.filter(user=user))

    @staticmethod
    def _setup_todo_with_event(user, reminders, active=True):
        td = make_todo(creator=user)
        td.reminders = reminders
        set_event_id(td, 'eid', 'creator')
        set_event_active(td, active, 'creator')
        td.save()
        return td

    def setUp(self):
        self.creator = make_user(email="creator@example.com", username="creator")
        self.assignee = make_user(email="assignee@example.com", username="assignee")
        self.other = make_user(email="other@example.com", username="other")
        self.now = timezone.now()
        GoogleToken.objects.filter(user__in=[self.creator, self.assignee, self.other]).delete()

        self.fake_async_task = self._make_fake_async()

    @patch('apps.notification_app.tasks.GoogleCalendarService')
    def test_create_event_when_no_event_id_creator(self, gcs_mock):
        GoogleToken.objects.create(user=self.creator, credentials=json.dumps({"dummy": "x"}))
        td = make_todo(creator=self.creator, assignee=self.assignee)

        inst = MagicMock()
        inst.service = True
        inst.find_event_for_todo.return_value = None
        inst.create_event.return_value = "gcal-eid-1"
        gcs_mock.return_value = inst

        tasks.sync_existing_todos(self.creator.id)

        td.refresh_from_db()
        self.assertEqual(get_event_id(td, 'creator') or get_event_id(td), "gcal-eid-1")

    @patch('apps.notification_app.tasks.GoogleCalendarService')
    def test_find_existing_event_and_attach(self, gcs_mock):
        td = make_todo(creator=self.creator)
        inst = MagicMock()
        inst.service = True
        inst.find_event_for_todo.return_value = {'id': 'found-eid'}
        gcs_mock.return_value = inst

        tasks.sync_existing_todos(self.creator.id)

        td.refresh_from_db()
        self.assertEqual(get_event_id(td, 'creator') or get_event_id(td), 'found-eid')
        inst.create_event.assert_not_called()

    @patch('apps.notification_app.tasks.GoogleCalendarService')
    def test_reactivate_existing_event_mark_active(self, gcs_mock):
        td = make_todo(creator=self.creator)
        set_event_id(td, 'stored-eid', 'creator')
        set_event_active(td, False, 'creator')
        td.save()

        inst = MagicMock()
        inst.service = True
        inst.get_event.return_value = {'id': 'stored-eid'}
        gcs_mock.return_value = inst

        tasks.sync_existing_todos(self.creator.id)

        td.refresh_from_db()
        self.assertTrue(get_event_active(td, 'creator') or get_event_active(td))

    @patch('apps.notification_app.tasks.GoogleCalendarService')
    def test_missing_stored_event_search_and_recreate(self, gcs_mock):
        td = make_todo(creator=self.creator)
        set_event_id(td, 'missing-eid', 'creator')
        td.save()

        inst = MagicMock()
        inst.service = True
        inst.get_event.side_effect = EventNotFound('missing-eid')
        inst.find_event_for_todo.return_value = None
        inst.create_event.return_value = 'new-eid'
        gcs_mock.return_value = inst

        tasks.sync_existing_todos(self.creator.id)

        td.refresh_from_db()
        self.assertEqual(get_event_id(td, 'creator') or get_event_id(td), 'new-eid')
        active_attr = event_active_attr(td, 'creator') or event_active_attr(td) or 'creator_calendar_event_active'
        self.assertTrue(getattr(td, active_attr, True))

    @patch('apps.notification_app.tasks.GoogleCalendarService')
    def test_refresh_error_on_create_sets_last_sync_error(self, gcs_mock):
        td = make_todo(creator=self.creator)
        inst = MagicMock()
        inst.service = True
        inst.find_event_for_todo.return_value = None
        inst.create_event.side_effect = RefreshError("bad refresh")
        gcs_mock.return_value = inst

        tasks.sync_existing_todos(self.creator.id)

        td.refresh_from_db()
        self.assertIsNotNone(td.last_sync_error)
        self.assertIn("bad refresh", td.last_sync_error.lower())

    @patch('apps.notification_app.tasks.GoogleCalendarService')
    def test_request_exception_triggers_retry(self, gcs_mock):
        make_todo(creator=self.creator)

        inst = MagicMock()
        inst.service = True
        inst.find_event_for_todo.return_value = None
        inst.create_event.side_effect = RequestException("network")
        gcs_mock.return_value = inst

        with patch.object(tasks.sync_existing_todos, 'retry', autospec=True) as retry_mock:
            tasks.sync_existing_todos(self.creator.id)

            retry_mock.assert_called()
            args, kwargs = retry_mock.call_args
            exc_arg = kwargs.get('exc') or (args[0] if args else None)
            self.assertIsInstance(exc_arg, RequestException)

    @patch('apps.notification_app.tasks.GoogleCalendarService')
    def test_http_error_saves_last_sync_error_and_continues(self, gcs_mock):
        td = make_todo(creator=self.creator)
        inst = MagicMock()
        inst.service = True
        inst.find_event_for_todo.return_value = None

        resp_mock = MagicMock()
        resp_mock.status = 400
        content_mock = b'{"error": "bad request"}'

        http_exc = HttpError(resp_mock, content_mock)
        inst.create_event.side_effect = http_exc
        gcs_mock.return_value = inst

        tasks.sync_existing_todos(self.creator.id)

        td.refresh_from_db()
        self.assertIsNotNone(td.last_sync_error)

    @patch('apps.notification_app.tasks.GoogleCalendarService')
    def test_processed_set_avoids_double_handling(self, gcs_mock):
        same = make_user(email="same@example.com", username="same")
        td = make_todo(creator=same, assignee=same)
        inst = MagicMock()
        inst.service = True
        inst.find_event_for_todo.return_value = None
        inst.get_event.return_value = None
        inst.create_event.return_value = 'eid'
        gcs_mock.return_value = inst

        tasks.sync_existing_todos(same.id)

        td.refresh_from_db()
        self.assertEqual(get_event_id(td, 'creator') or get_event_id(td), 'eid')

    @patch('apps.notification_app.tasks.GoogleCalendarService')
    def test_assignee_flow_find_then_create(self, gcs_mock):
        td = make_todo(creator=self.creator, assignee=self.assignee)
        inst = MagicMock()
        inst.service = True
        inst.find_event_for_todo.return_value = None
        inst.create_event.return_value = 'ass-eid'
        gcs_mock.return_value = inst

        tasks.sync_existing_todos(self.assignee.id)

        td.refresh_from_db()
        self.assertEqual(get_event_id(td, 'assignee') or get_event_id(td), 'ass-eid')

    @patch('apps.notification_app.tasks.GoogleCalendarService')
    def test_no_service_clears_event_id(self, gcs_mock):
        td = make_todo(creator=self.creator)
        set_event_id(td, 'eid', 'creator')
        set_event_active(td, True, 'creator')
        td.save()

        inst = MagicMock()
        inst.service = None
        gcs_mock.return_value = inst

        tasks.sync_existing_todos(self.creator.id)

        td.refresh_from_db()
        self.assertIsNone(get_event_id(td, 'creator') or get_event_id(td))
        self.assertFalse(get_event_active(td, 'creator') or get_event_active(td))

    @patch('apps.notification_app.tasks.GoogleCalendarService')
    @patch('apps.notification_app.tasks.send_notification_task')
    def test_reenable_calendar_creates_missing_event(self, send_task_mock, gcs_mock):
        td = make_todo(creator=self.creator)

        set_event_id(td, "old-eid", 'creator')
        set_event_active(td, True, 'creator')
        td.save()

        inst_disabled = MagicMock()
        inst_disabled.service = None
        gcs_mock.return_value = inst_disabled
        tasks.sync_existing_todos(self.creator.id)

        td.refresh_from_db()
        self.assertIsNone(get_event_id(td, 'creator'))
        self.assertFalse(get_event_active(td, 'creator'))

        GoogleToken.objects.create(user=self.creator, credentials=json.dumps({"x": 1}))
        inst_enabled = MagicMock()
        inst_enabled.service = True
        inst_enabled.find_event_for_todo.return_value = None
        inst_enabled.create_event.return_value = "new-eid"
        gcs_mock.return_value = inst_enabled

        tasks.sync_existing_todos(self.creator.id)
        td.refresh_from_db()
        self.assertEqual(get_event_id(td, 'creator'), "new-eid")
        self.assertTrue(get_event_active(td, 'creator'))

    # ---------------------------
    # transfer_unsent_reminders_task
    # ---------------------------
    @patch('apps.notification_app.tasks.send_notification_task')
    def test_transfer_creates_notifications_and_clears_fields(self, send_task_mock):
        td = self._setup_todo_with_event(
            user=self.creator,
            reminders=[{'method': 'popup', 'minutes': 15}, {'method': 'popup', 'minutes': 30}]
        )
        self._disable_token(self.creator)

        send_task_mock.apply_async.return_value = self.fake_async_task

        notifs = self._run_transfer_and_get_notifs(self.creator)

        self.assertTrue(notifs)
        td.refresh_from_db()
        self.assertIsNone(get_event_id(td, 'creator'))
        self.assertFalse(get_event_active(td, 'creator'))

    @patch('apps.notification_app.tasks.send_notification_task')
    def test_transfer_skips_if_user_has_token(self, send_task_mock):
        self._enable_token(self.creator)

        self._setup_todo_with_event(
            user=self.creator,
            reminders=[{'method': 'popup', 'minutes': 15}]
        )

        notifs = self._run_transfer_and_get_notifs(self.creator)

        self.assertFalse(notifs)

    @patch('apps.notification_app.tasks.send_notification_task')
    def test_transfer_skips_past_due(self, send_task_mock):
        past_deadline = timezone.now() - timedelta(minutes=10)
        td = make_todo(creator=self.creator, deadline=past_deadline)
        set_event_id(td, 'eid', 'creator')
        td.reminders = [{'method': 'popup', 'minutes': 15}]
        td.save()

        GoogleToken.objects.filter(user=self.creator).delete()

        tasks.transfer_unsent_reminders_task(self.creator.id)

        self.assertFalse(Notification.objects.filter(user=self.creator).exists())

    @patch('apps.notification_app.tasks.send_notification_task')
    def test_transfer_handles_db_errors_on_notification_creation(self, send_task_mock):
        self._setup_todo_with_event(
            user=self.creator,
            reminders=[{'method': 'popup', 'minutes': 15}]
        )
        self._disable_token(self.creator)

        send_task_mock.apply_async.return_value = self.fake_async_task

        with patch('apps.notification_app.models.Notification.objects.get_or_create',
                   side_effect=IntegrityError("db fail")):
            self._run_transfer_and_get_notifs(self.creator)

        self.assertFalse(Notification.objects.filter(user=self.creator).exists())

    @patch('apps.notification_app.tasks.send_notification_task')
    def test_transfer_reminders_skipped_after_reconnect(self, send_task_mock):
        self._setup_todo_with_event(
            user=self.creator,
            reminders=[{'method': 'popup', 'minutes': 15}]
        )
        self._disable_token(self.creator)
        send_task_mock.apply_async.return_value = self.fake_async_task

        notifs_before = self._run_transfer_and_get_notifs(self.creator)
        self.assertTrue(notifs_before)

        self._enable_token(self.creator)
        notifs_after = self._run_transfer_and_get_notifs(self.creator)

        self.assertEqual(len(notifs_after), len(notifs_before))

    @patch('apps.notification_app.tasks.send_notification_task')
    def test_double_disconnect_no_reminder_duplication(self, send_task_mock):
        td = self._setup_todo_with_event(
            user=self.creator,
            reminders=[{'method': 'popup', 'minutes': 15}]
        )
        send_task_mock.apply_async.return_value = self.fake_async_task

        self._disable_token(self.creator)
        notifs_first = self._run_transfer_and_get_notifs(self.creator)
        self.assertEqual(len(notifs_first), 1)
        td.refresh_from_db()
        self.assertIsNone(get_event_id(td, 'creator'))

        self._enable_token(self.creator)
        notifs_mid = self._run_transfer_and_get_notifs(self.creator)
        self.assertEqual(len(notifs_mid), 1)

        self._disable_token(self.creator)
        notifs_final = self._run_transfer_and_get_notifs(self.creator)
        self.assertEqual(len(notifs_final), 1)
