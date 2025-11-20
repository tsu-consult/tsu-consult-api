from django.conf import settings
from django.db import models
import logging

logger = logging.getLogger(__name__)


class ToDo(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = "in progress", "В процессе"
        DONE = "done", "Выполнено"

    id = models.AutoField(primary_key=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    deadline = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IN_PROGRESS)

    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="created_todos",
    )

    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="assigned_todos",
        null=True,
        blank=True,
    )

    calendar_event_id = models.CharField(max_length=255, null=True, blank=True)
    assignee_calendar_event_id = models.CharField(max_length=255, null=True, blank=True)
    calendar_event_active = models.BooleanField(default=False)
    assignee_calendar_event_active = models.BooleanField(default=False)
    reminders = models.JSONField(null=True, blank=True)
    assignee_reminders = models.JSONField(null=True, blank=True)

    last_sync_error = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"ToDo(id={self.id}, title={self.title}, creator={self.creator_id})"

    def is_accessible_by(self, user):
        return user and user.is_authenticated and (
            self.creator_id == user.id
            or (self.assignee_id and self.assignee_id == user.id)
            or getattr(user, 'role', None) == 'admin'
        )

    def is_editable_by(self, user):
        return bool(user and user.is_authenticated and
                    (self.creator_id == user.id or getattr(user, 'role', None) == 'admin'))

    def create_calendar_event(self, calendar_service, reminders=None, for_creator=False):
        if not self.deadline:
            return None
        try:
            if reminders is None:
                event_id = calendar_service.create_event(self)
            else:
                event_id = calendar_service.create_event(self, reminders=reminders)

            if event_id:
                id_field = "calendar_event_id" if for_creator else "assignee_calendar_event_id"
                active_field = "calendar_event_active" if for_creator else "assignee_calendar_event_active"
                setattr(self, id_field, event_id)
                setattr(self, active_field, True)
                self.save(update_fields=[id_field, active_field])
                return event_id
        except (AttributeError, TypeError, ValueError) as exc:
            logger.exception("Calendar sync failed for todo id=%s: %s", getattr(self, 'id', '<unknown>'), exc)
            return None
        except Exception as exc:
            logger.exception("Unexpected error while creating calendar event for todo id=%s: %s",
                             getattr(self, 'id', '<unknown>'), exc)
            raise
        return None
