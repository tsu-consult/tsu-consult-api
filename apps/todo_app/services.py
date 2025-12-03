import logging
from typing import Optional, List, Dict, Any

from celery.exceptions import CeleryError
from django.db import DatabaseError

from apps.auth_app.models import User
from apps.todo_app.calendar.managers import sync_calendars
from apps.todo_app.calendar.services import GoogleCalendarService
from apps.todo_app.models import ToDo
from apps.todo_app.utils import cancel_pending_notifications_for_user, has_calendar_integration
from core.exceptions import GoogleCalendarAuthRequired

logger = logging.getLogger(__name__)


class ToDoUpdateService:
    def __init__(self, todo: ToDo, actor_user: User):
        self.todo = todo
        self.actor_user = actor_user
        self.old_assignee: Optional[User] = None
        self.old_deadline = None
        self.old_creator_reminders = None
        self.old_assignee_reminders = None

    def save_old_state(self):
        self.old_assignee = self.todo.assignee
        self.old_deadline = getattr(self.todo, 'deadline', None)
        self.old_creator_reminders = getattr(self.todo, 'reminders', None)
        self.old_assignee_reminders = getattr(self.todo, 'assignee_reminders', None)

    def restore_reminders_if_needed(self, reminders_in_request: bool):
        if reminders_in_request:
            return

        fields_to_update = []
        if self.old_creator_reminders is not None and getattr(self.todo, 'reminders', None) is None:
            self.todo.reminders = self.old_creator_reminders
            fields_to_update.append('reminders')
        if self.old_assignee_reminders is not None and getattr(self.todo, 'assignee_reminders', None) is None:
            self.todo.assignee_reminders = self.old_assignee_reminders
            fields_to_update.append('assignee_reminders')

        if fields_to_update:
            try:
                self.todo.save(update_fields=fields_to_update)
            except Exception as exc:
                logger.exception("Failed to restore reminders fields for todo id=%s: %s",
                                 getattr(self.todo, 'id', None), exc)

    def handle_deadline_removed(self):
        deadline_removed = (self.old_deadline is not None and getattr(self.todo, 'deadline', None) is None)
        if not deadline_removed:
            return

        potential_users = self._get_potential_users()

        for user in potential_users:
            try:
                if has_calendar_integration(user):
                    self._delete_calendar_event_for_user(user)
                else:
                    self._cancel_notifications_for_user(user, 'Deadline removed')
            except (GoogleCalendarAuthRequired, Exception) as exc:
                logger.exception("Error handling deadline removal for todo id=%s user=%s: %s",
                                 getattr(self.todo, 'id', None), getattr(user, 'id', None), exc)

    def handle_deadline_changed(self):
        deadline_changed = (
            self.old_deadline is not None
            and getattr(self.todo, 'deadline', None) is not None
            and self.old_deadline != getattr(self.todo, 'deadline', None)
        )
        if not deadline_changed:
            return

        potential_users = self._get_potential_users()

        for user in potential_users:
            if has_calendar_integration(user):
                continue

            try:
                cancel_pending_notifications_for_user(self.todo, user, reason='Deadline changed')
            except (DatabaseError, CeleryError, RuntimeError, ValueError) as exc:
                logger.exception("Failed to cancel pending notifications during deadline change for todo "
                                 "id=%s user=%s: %s", getattr(self.todo, 'id', None),
                                 getattr(user, 'id', None), exc)

    def handle_reminders_update(self, reminders_value: Optional[List[Dict[str, Any]]]):
        if reminders_value is None:
            return

        if isinstance(reminders_value, list) and len(reminders_value) == 0:
            self._handle_reminders_cleared()
        else:
            self._handle_reminders_changed()

    def sync_calendars(self):
        sync_calendars(self.todo, self.actor_user, self.old_assignee)

    def _get_potential_users(self) -> List[User]:
        potential_users = []
        creator_user = getattr(self.todo, 'creator', None)
        assignee_user = getattr(self.todo, 'assignee', None)

        if creator_user:
            potential_users.append(creator_user)
        if assignee_user and getattr(assignee_user, 'id', None) != getattr(creator_user, 'id', None):
            potential_users.append(assignee_user)

        return potential_users

    def _delete_calendar_event_for_user(self, user: User):
        try:
            service = GoogleCalendarService(user)
            if getattr(service, 'service', None) and getattr(service, 'delete_event', None):
                service.delete_event(self.todo)
        except (DatabaseError, CeleryError, RuntimeError, ValueError) as exc:
            logger.exception("Failed to delete calendar event during deadline removal "
                             "for todo id=%s user=%s: %s", getattr(self.todo, 'id', None),
                             getattr(user, 'id', None), exc)

    def _cancel_notifications_for_user(self, user: User, reason: str):
        try:
            cancel_pending_notifications_for_user(self.todo, user, reason=reason)
        except (DatabaseError, CeleryError, RuntimeError, ValueError) as exc:
            logger.exception("Failed to cancel pending notifications during deadline "
                             "removal for todo id=%s user=%s: %s", getattr(self.todo, 'id', None),
                             getattr(user, 'id', None), exc)

    def _handle_reminders_cleared(self):
        try:
            cancel_pending_notifications_for_user(self.todo, self.actor_user, reason='Reminders cleared via PUT')
        except (DatabaseError, CeleryError, RuntimeError, ValueError) as exc:
            logger.exception("Failed to cancel pending notifications on explicit empty "
                             "reminders for todo id=%s: %s", getattr(self.todo, 'id', None), exc)

    def _handle_reminders_changed(self):
        try:
            cancel_pending_notifications_for_user(self.todo, self.actor_user, reason='Reminders updated via PUT')
        except (DatabaseError, CeleryError, RuntimeError, ValueError) as exc:
            logger.exception("Failed to cancel pending notifications on reminders update for todo id=%s: %s",
                             getattr(self.todo, 'id', None), exc)

        if not has_calendar_integration(self.actor_user):
            logger.debug("Actor has no calendar integration; fallback scheduling delegated to "
                         "sync_calendars for todo id=%s", getattr(self.todo, 'id', None))
