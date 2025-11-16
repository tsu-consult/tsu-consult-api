from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError


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
    creator_calendar_event_id = models.CharField(max_length=255, null=True, blank=True)
    last_sync_error = models.TextField(null=True, blank=True)
    reminders = models.JSONField(null=True, blank=True)
    creator_reminders = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"ToDo(id={self.id}, title={self.title}, creator={self.creator_id})"

    def clean(self):
        if self.creator and getattr(self.creator, 'role', None) not in ('teacher', 'dean'):
            raise ValidationError({'creator': 'The author must be a teacher or dean.'})
        if not self.assignee and getattr(self.creator, 'role', None) != 'dean':
            raise ValidationError({'assignee': 'Assignee is required.'})
        if self.assignee and getattr(self.assignee, 'role', None) != 'teacher':
            raise ValidationError({'assignee': 'The assignee must be a teacher.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def is_accessible_by(self, user):
        return user and user.is_authenticated and (
            self.creator_id == user.id
            or (self.assignee_id and self.assignee_id == user.id)
            or getattr(user, 'role', None) == 'admin'
        )

    def is_editable_by(self, user):
        return bool(user and user.is_authenticated and
                    (self.creator_id == user.id or getattr(user, 'role', None) == 'admin'))

    def sync_calendar_event(self, calendar_service, reminders=None, for_creator=False):
        if not self.deadline:
            return None
        try:
            if reminders is None:
                event_id = calendar_service.create_event(self)
            else:
                event_id = calendar_service.create_event(self, reminders=reminders)

            if event_id:
                field = "creator_calendar_event_id" if for_creator else "calendar_event_id"
                setattr(self, field, event_id)
                self.save(update_fields=[field])
                return event_id
        except Exception:
            return None
        return None
