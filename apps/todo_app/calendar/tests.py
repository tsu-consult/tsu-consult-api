from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from apps.auth_app.models import User
from apps.todo_app.calendar.services import GoogleCalendarService
from apps.todo_app.models import ToDo
from core.exceptions import EventNotFound


class BaseGoogleCalendarServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='test@example.com',
            username='testuser',
            role='teacher'
        )
        self.todo = ToDo.objects.create(
            title='Test Task',
            description='Test Description',
            deadline=timezone.now() + timedelta(hours=2),
            creator=self.user,
            assignee=self.user,
            reminders=[{'method': 'popup', 'minutes': 10}]
        )


class GoogleCalendarServiceUpdateEventTests(BaseGoogleCalendarServiceTests):
    @staticmethod
    def _create_mock_service_with_patch_error(error_or_side_effect):
        mock_service = Mock()
        mock_events = Mock()
        mock_patch = Mock()
        mock_patch.execute = Mock(side_effect=error_or_side_effect)
        mock_events.patch = Mock(return_value=mock_patch)
        mock_service.events = Mock(return_value=mock_events)
        return mock_service

    @staticmethod
    def _create_mock_service_with_success(return_value):
        mock_service = Mock()
        mock_events = Mock()
        mock_patch = Mock()
        mock_patch.execute = Mock(return_value=return_value)
        mock_events.patch = Mock(return_value=mock_patch)
        mock_service.events = Mock(return_value=mock_events)
        return mock_service

    def _setup_service_with_calendar(self, mock_service, calendar_id='test-calendar-id'):
        service = GoogleCalendarService(self.user)
        service.service = mock_service
        service.calendar_id = calendar_id
        return service

    def test_update_event_returns_false_when_todo_is_none(self):
        service = GoogleCalendarService(self.user)
        result = service.update_event(todo=None)
        self.assertFalse(result)

    def test_update_event_returns_false_when_deadline_is_none(self):
        self.todo.deadline = None
        self.todo.save()

        service = GoogleCalendarService(self.user)
        result = service.update_event(self.todo)
        self.assertFalse(result)

    def test_update_event_returns_false_when_service_is_not_available(self):
        service = GoogleCalendarService(self.user)
        service.service = None

        result = service.update_event(self.todo)
        self.assertFalse(result)

    @patch('apps.todo_app.calendar.services.GoogleCalendarService._get_or_create_calendar')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_update_event_returns_false_when_calendar_id_unavailable(
            self, mock_ensure_creds, mock_get_calendar):
        mock_ensure_creds.return_value = None
        mock_get_calendar.return_value = None

        service = GoogleCalendarService(self.user)
        service.service = Mock()
        service.calendar_id = None

        result = service.update_event(self.todo)
        self.assertFalse(result)
        mock_get_calendar.assert_called_once()

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_update_event_raises_event_not_found_when_event_missing(
            self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = None

        service = GoogleCalendarService(self.user)
        service.service = Mock()
        service.calendar_id = 'test-calendar-id'

        with self.assertRaises(EventNotFound):
            service.update_event(self.todo)

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_update_event_successfully_updates_existing_event(
            self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-123', 'summary': 'Old Summary'}

        mock_service = self._create_mock_service_with_success({'id': 'event-123', 'summary': 'Updated Summary'})
        service = self._setup_service_with_calendar(mock_service)

        reminders = [{'method': 'popup', 'minutes': 15}]
        result = service.update_event(self.todo, reminders)

        self.assertTrue(result)
        mock_find_event.assert_called_once_with(self.todo)
        mock_events = mock_service.events.return_value
        mock_events.patch.assert_called_once()

        call_args = mock_events.patch.call_args
        self.assertEqual(call_args[1]['calendarId'], 'test-calendar-id')
        self.assertEqual(call_args[1]['eventId'], 'event-123')
        self.assertIn('body', call_args[1])

        body = call_args[1]['body']
        self.assertIn('summary', body)
        self.assertIn('Test Task', body['summary'])
        self.assertIn('reminders', body)
        self.assertEqual(body['reminders']['overrides'], reminders)

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_update_event_handles_http_error_404(self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-123'}

        mock_response = Mock()
        mock_response.status = 404
        http_error = HttpError(mock_response, b'Not Found')

        mock_service = self._create_mock_service_with_patch_error(http_error)
        service = self._setup_service_with_calendar(mock_service)

        with self.assertRaises(EventNotFound):
            service.update_event(self.todo)

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._handle_refresh_error')
    def test_update_event_handles_http_error_401(
            self, mock_handle_error, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-123'}

        mock_response = Mock()
        mock_response.status = 401
        http_error = HttpError(mock_response, b'Unauthorized')

        mock_service = self._create_mock_service_with_patch_error(http_error)
        service = self._setup_service_with_calendar(mock_service)

        result = service.update_event(self.todo)

        self.assertFalse(result)
        mock_handle_error.assert_called_once()

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_update_event_handles_http_error_403(self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-123'}

        mock_response = Mock()
        mock_response.status = 403
        http_error = HttpError(mock_response, b'Forbidden')

        mock_service = self._create_mock_service_with_patch_error(http_error)
        service = self._setup_service_with_calendar(mock_service)

        with patch.object(service, '_handle_refresh_error') as mock_handle:
            result = service.update_event(self.todo)
            self.assertFalse(result)
            mock_handle.assert_called_once()

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_update_event_handles_http_error_500(self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-123'}

        mock_response = Mock()
        mock_response.status = 500
        http_error = HttpError(mock_response, b'Internal Server Error')

        mock_service = self._create_mock_service_with_patch_error(http_error)
        service = self._setup_service_with_calendar(mock_service)

        result = service.update_event(self.todo)
        self.assertFalse(result)

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_update_event_handles_refresh_error(self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-123'}

        mock_service = self._create_mock_service_with_patch_error(RefreshError('Token expired'))
        service = self._setup_service_with_calendar(mock_service)

        with patch.object(service, '_handle_refresh_error') as mock_handle:
            service.update_event(self.todo)
            mock_handle.assert_called_once()

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_update_event_handles_value_error(self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-123'}

        mock_service = self._create_mock_service_with_patch_error(ValueError('Invalid value'))
        service = self._setup_service_with_calendar(mock_service)

        result = service.update_event(self.todo)
        self.assertFalse(result)

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_update_event_updates_reminders_in_event_body(
            self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-456'}

        mock_service = self._create_mock_service_with_success({'id': 'event-456'})
        service = self._setup_service_with_calendar(mock_service)

        new_reminders = [
            {'method': 'popup', 'minutes': 30},
            {'method': 'email', 'minutes': 60}
        ]
        result = service.update_event(self.todo, new_reminders)

        self.assertTrue(result)
        mock_events = mock_service.events.return_value
        call_args = mock_events.patch.call_args
        body = call_args[1]['body']
        self.assertEqual(body['reminders']['overrides'], new_reminders)
        self.assertFalse(body['reminders']['useDefault'])

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_update_event_updates_task_metadata(self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-789'}

        mock_service = self._create_mock_service_with_success({'id': 'event-789'})
        service = self._setup_service_with_calendar(mock_service)

        self.todo.title = 'Updated Task Title'
        self.todo.description = 'Updated Description'
        new_deadline = timezone.now() + timedelta(hours=5)
        self.todo.deadline = new_deadline
        self.todo.status = ToDo.Status.DONE
        self.todo.save()

        result = service.update_event(self.todo)

        self.assertTrue(result)
        mock_events = mock_service.events.return_value
        call_args = mock_events.patch.call_args
        body = call_args[1]['body']

        self.assertIn('Updated Task Title', body['summary'])
        self.assertIn('[Done]', body['summary'])
        self.assertIn('Updated Description', body['description'])
        self.assertIn('dateTime', body['start'])
        self.assertIn('dateTime', body['end'])

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_update_event_handles_find_event_exception(
            self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.side_effect = HttpError(Mock(status=500), b'Server Error')

        service = GoogleCalendarService(self.user)
        service.service = Mock()
        service.calendar_id = 'test-calendar-id'

        with self.assertRaises(EventNotFound):
            service.update_event(self.todo)

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_update_event_creates_calendar_if_not_exists(
            self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-999'}

        mock_service = self._create_mock_service_with_success({'id': 'event-999'})
        service = GoogleCalendarService(self.user)
        service.service = mock_service
        service.calendar_id = None

        def set_calendar_id():
            service.calendar_id = 'new-calendar-id'
            return 'new-calendar-id'

        with patch.object(service, '_get_or_create_calendar', side_effect=set_calendar_id) as mock_get_calendar:
            result = service.update_event(self.todo)

            self.assertTrue(result)
            mock_get_calendar.assert_called_once()
            self.assertEqual(service.calendar_id, 'new-calendar-id')


class GoogleCalendarServiceDeleteEventTests(BaseGoogleCalendarServiceTests):
    @staticmethod
    def _create_mock_service_with_delete_success():
        mock_service = Mock()
        mock_events = Mock()
        mock_delete = Mock()
        mock_delete.execute = Mock(return_value={})
        mock_events.delete = Mock(return_value=mock_delete)
        mock_service.events = Mock(return_value=mock_events)
        return mock_service

    @staticmethod
    def _create_mock_service_with_delete_error(error_or_side_effect):
        mock_service = Mock()
        mock_events = Mock()
        mock_delete = Mock()
        mock_delete.execute = Mock(side_effect=error_or_side_effect)
        mock_events.delete = Mock(return_value=mock_delete)
        mock_service.events = Mock(return_value=mock_events)
        return mock_service

    def _setup_service_with_calendar(self, mock_service, calendar_id='test-calendar-id'):
        service = GoogleCalendarService(self.user)
        service.service = mock_service
        service.calendar_id = calendar_id
        return service

    def test_delete_event_returns_false_when_todo_is_none(self):
        service = GoogleCalendarService(self.user)
        result = service.delete_event(todo=None)
        self.assertFalse(result)

    def test_delete_event_returns_false_when_service_is_not_available(self):
        service = GoogleCalendarService(self.user)
        service.service = None

        result = service.delete_event(self.todo)
        self.assertFalse(result)

    @patch('apps.todo_app.calendar.services.GoogleCalendarService._get_or_create_calendar')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_delete_event_returns_false_when_calendar_id_unavailable(
            self, mock_ensure_creds, mock_get_calendar):
        mock_ensure_creds.return_value = None
        mock_get_calendar.return_value = None

        service = GoogleCalendarService(self.user)
        service.service = Mock()
        service.calendar_id = None

        result = service.delete_event(self.todo)
        self.assertFalse(result)
        mock_get_calendar.assert_called_once()

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_delete_event_returns_true_when_event_not_found(
            self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = None

        service = GoogleCalendarService(self.user)
        service.service = Mock()
        service.calendar_id = 'test-calendar-id'

        result = service.delete_event(self.todo)
        self.assertTrue(result)

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_delete_event_returns_false_when_event_has_no_id(
            self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'summary': 'Event without ID'}

        service = GoogleCalendarService(self.user)
        service.service = Mock()
        service.calendar_id = 'test-calendar-id'

        result = service.delete_event(self.todo)
        self.assertFalse(result)

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_delete_event_successfully_deletes_existing_event(
            self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-123'}

        mock_service = self._create_mock_service_with_delete_success()
        service = self._setup_service_with_calendar(mock_service)

        result = service.delete_event(self.todo)

        self.assertTrue(result)
        mock_find_event.assert_called_once_with(self.todo)
        mock_events = mock_service.events.return_value
        mock_events.delete.assert_called_once_with(
            calendarId='test-calendar-id',
            eventId='event-123'
        )

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_delete_event_handles_http_error_404_as_success(
            self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-123'}

        mock_response = Mock()
        mock_response.status = 404
        http_error = HttpError(mock_response, b'Not Found')

        mock_service = self._create_mock_service_with_delete_error(http_error)
        service = self._setup_service_with_calendar(mock_service)

        result = service.delete_event(self.todo)
        self.assertTrue(result)

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._handle_refresh_error')
    def test_delete_event_handles_http_error_401(
            self, mock_handle_error, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-123'}

        mock_response = Mock()
        mock_response.status = 401
        http_error = HttpError(mock_response, b'Unauthorized')

        mock_service = self._create_mock_service_with_delete_error(http_error)
        service = self._setup_service_with_calendar(mock_service)

        result = service.delete_event(self.todo)

        self.assertFalse(result)
        mock_handle_error.assert_called_once()

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_delete_event_handles_http_error_403(
            self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-123'}

        mock_response = Mock()
        mock_response.status = 403
        http_error = HttpError(mock_response, b'Forbidden')

        mock_service = self._create_mock_service_with_delete_error(http_error)
        service = self._setup_service_with_calendar(mock_service)

        with patch.object(service, '_handle_refresh_error') as mock_handle:
            result = service.delete_event(self.todo)
            self.assertFalse(result)
            mock_handle.assert_called_once()

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_delete_event_handles_http_error_500(
            self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-123'}

        mock_response = Mock()
        mock_response.status = 500
        http_error = HttpError(mock_response, b'Internal Server Error')

        mock_service = self._create_mock_service_with_delete_error(http_error)
        service = self._setup_service_with_calendar(mock_service)

        result = service.delete_event(self.todo)
        self.assertFalse(result)

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_delete_event_handles_refresh_error(
            self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-123'}

        mock_service = self._create_mock_service_with_delete_error(RefreshError('Token expired'))
        service = self._setup_service_with_calendar(mock_service)

        with patch.object(service, '_handle_refresh_error') as mock_handle:
            result = service.delete_event(self.todo)
            self.assertFalse(result)
            mock_handle.assert_called_once()

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_delete_event_handles_value_error(
            self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-123'}

        mock_service = self._create_mock_service_with_delete_error(ValueError('Invalid value'))
        service = self._setup_service_with_calendar(mock_service)

        result = service.delete_event(self.todo)
        self.assertFalse(result)

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_delete_event_handles_find_event_exception(
            self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.side_effect = HttpError(Mock(status=500), b'Server Error')

        service = GoogleCalendarService(self.user)
        service.service = Mock()
        service.calendar_id = 'test-calendar-id'

        result = service.delete_event(self.todo)
        self.assertFalse(result)

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_delete_event_creates_calendar_if_not_exists(
            self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.return_value = {'id': 'event-999'}

        mock_service = self._create_mock_service_with_delete_success()
        service = GoogleCalendarService(self.user)
        service.service = mock_service
        service.calendar_id = None

        def set_calendar_id():
            service.calendar_id = 'new-calendar-id'
            return 'new-calendar-id'

        with patch.object(service, '_get_or_create_calendar', side_effect=set_calendar_id) as mock_get_calendar:
            result = service.delete_event(self.todo)

            self.assertTrue(result)
            mock_get_calendar.assert_called_once()
            self.assertEqual(service.calendar_id, 'new-calendar-id')

    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_delete_event_handles_ensure_credentials_refresh_error(
            self, mock_ensure_creds):
        mock_ensure_creds.side_effect = RefreshError('Token expired')

        service = GoogleCalendarService(self.user)
        service.service = Mock()
        service.calendar_id = 'test-calendar-id'

        result = service.delete_event(self.todo)
        self.assertFalse(result)

    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_delete_event_handles_ensure_credentials_generic_exception(
            self, mock_ensure_creds):
        mock_ensure_creds.side_effect = Exception('Generic error')

        service = GoogleCalendarService(self.user)
        service.service = Mock()
        service.calendar_id = 'test-calendar-id'

        result = service.delete_event(self.todo)
        self.assertFalse(result)

    @patch('apps.todo_app.calendar.services.GoogleCalendarService.find_event_for_todo')
    @patch('apps.todo_app.calendar.services.GoogleCalendarService._ensure_credentials_valid')
    def test_delete_event_handles_type_error_in_find_event(
            self, mock_ensure_creds, mock_find_event):
        mock_ensure_creds.return_value = None
        mock_find_event.side_effect = TypeError('Type error')

        service = GoogleCalendarService(self.user)
        service.service = Mock()
        service.calendar_id = 'test-calendar-id'

        result = service.delete_event(self.todo)
        self.assertFalse(result)
