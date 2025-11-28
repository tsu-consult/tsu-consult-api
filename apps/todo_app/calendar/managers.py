import logging
from typing import Optional, List, Dict, Any

from celery.exceptions import CeleryError
from googleapiclient.errors import HttpError

from apps.auth_app.models import User
from apps.todo_app.fallback.services import FallbackReminderService
from apps.todo_app.models import ToDo
from apps.todo_app.calendar.services import GoogleCalendarService
from core.exceptions import GoogleCalendarAuthRequired
from apps.todo_app.utils import _create_notification_safe, _notify_new_assignee_and_cleanup_old

logger = logging.getLogger(__name__)


class CalendarSyncManager:
    def __init__(self, todo: ToDo, actor_user: User):
        self.todo = todo
        self.actor_user = actor_user
        self.calendar_service = GoogleCalendarService(actor_user)

    def _create_event(self, reminders: Optional[List[Dict[str, Any]]], target_user: User, for_creator: bool) -> bool:
        if not getattr(self.todo, 'deadline', None):
            return False

        event_id = None
        try:
            if getattr(self.calendar_service, 'service', None):
                event_id = self.todo.create_calendar_event(self.calendar_service, reminders, for_creator)
        except (HttpError, GoogleCalendarAuthRequired) as exc:
            logger.exception("Calendar sync failed for todo id=%s with Google API error: %s",
                             getattr(self.todo, 'id', None), exc)
        except (ValueError, TypeError, AttributeError) as exc:
            logger.exception("Calendar sync failed for todo id=%s: %s", getattr(self.todo, 'id', None), exc)

        if (not getattr(self.calendar_service, 'service', None) or event_id is None) and reminders:
            try:
                FallbackReminderService().schedule_fallback_reminders(self.todo, reminders, target_user)
            except (CeleryError, RuntimeError) as exc:
                logger.exception("Failed to schedule fallback reminders for todo id=%s: %s",
                                 getattr(self.todo, 'id', None), exc)
                raise

        return bool(event_id)

    def _update_event(self, reminders: Optional[List[Dict[str, Any]]]) -> bool:
        if getattr(self.calendar_service, 'service', None) and callable(getattr(self.calendar_service,
                                                                                'update_event', None)):
            try:
                return bool(self.calendar_service.update_event(self.todo, reminders))
            except (HttpError, GoogleCalendarAuthRequired, ValueError, TypeError, RuntimeError) as exc:
                logger.exception("Calendar update_event failed for todo id=%s: %s", getattr(self.todo, 'id', None), exc)
        return False

    def _sync_calendar(self, reminders: Optional[List[Dict[str, Any]]], target_user: User,
                       for_creator: bool = True) -> bool:
        updated = self._update_event(reminders)

        created = False
        if not updated:
            created = self._create_event(reminders, target_user, for_creator)

        calendar_event_id = (getattr(self.todo, 'calendar_event_id', None) if for_creator else
                             getattr(self.todo, 'assignee_calendar_event_id', None))

        return bool(updated or created or calendar_event_id)

    def sync_creator(self):
        try:
            self._sync_calendar(getattr(self.todo, 'reminders', None), self.actor_user, True)
        except (HttpError, GoogleCalendarAuthRequired, ValueError, TypeError, RuntimeError) as exc:
            logger.exception("Failed to sync calendar for creator after creating/updating todo id=%s: %s",
                             getattr(self.todo, 'id', None), exc)

        if (self.todo.assignee and getattr(self.todo.assignee, 'id', None) != getattr(self.actor_user, 'id', None)
                and getattr(self.todo.assignee, 'id', None) != getattr(self.todo.creator, 'id', None)):
            try:
                assignee_manager = CalendarSyncManager(self.todo, self.todo.assignee)
                assignee_manager._sync_calendar(getattr(self.todo, 'assignee_reminders', None), self.actor_user, False)
            except (HttpError, GoogleCalendarAuthRequired, ValueError, TypeError, RuntimeError) as exc:
                logger.exception("Failed to sync calendar for assignee after creating/updating todo id=%s: %s",
                                 getattr(self.todo, 'id', None), exc)

    def sync_assignee(self):
        assignee_manager = CalendarSyncManager(self.todo, self.actor_user)
        try:
            assignee_manager._sync_calendar(getattr(self.todo, 'assignee_reminders', None), self.actor_user, False)
        except (HttpError, GoogleCalendarAuthRequired, ValueError, TypeError, RuntimeError) as exc:
            logger.exception("Failed to sync calendar for assignee (actor) todo id=%s: %s",
                             getattr(self.todo, 'id', None), exc)


def sync_calendars(todo: ToDo, actor_user: User, old_assignee: Optional[User] = None,
                   notify_assignee_on_create: bool = False):
    manager = CalendarSyncManager(todo, actor_user)
    try:
        if getattr(actor_user, 'id', None) == getattr(todo.creator, 'id', None):
            manager.sync_creator()
        elif getattr(actor_user, 'id', None) == getattr(getattr(todo, 'assignee', None), 'id', None):
            manager.sync_assignee()
    except (HttpError, GoogleCalendarAuthRequired, ValueError, TypeError, RuntimeError) as exc:
        logger.exception("Failed to sync calendar after creating/updating todo id=%s: %s",
                         getattr(todo, 'id', None), exc)
    except Exception as exc:
        logger.exception("Failed to sync calendar after creating/updating todo id=%s: %s",
                         getattr(todo, 'id', None), exc)

    if (notify_assignee_on_create and todo.assignee
            and getattr(todo.assignee, 'id', None) != getattr(actor_user, 'id', None)):
        _create_notification_safe(
            todo.assignee,
            "Новая задача",
            f'Вам назначена задача: "{todo.title}".\n\nЧтобы просмотреть детали, перейдите в раздел "📝 Мои задачи".',
            "notify_assignee_on_create"
        )

    if old_assignee is not None:
        _notify_new_assignee_and_cleanup_old(todo, old_assignee)
