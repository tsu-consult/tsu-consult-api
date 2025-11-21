from django.db import models
from django.conf import settings


class Notification(models.Model):
    class Type(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        EMAIL = "email", "Email"
        SYSTEM = "system", "System"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    type = models.CharField(max_length=20, choices=Type.choices, default=Type.TELEGRAM)
    title = models.CharField(max_length=255)
    message = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(null=True, blank=True)

    todo = models.ForeignKey(
        "todo_app.ToDo",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications"
    )
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)

    def __str__(self):
        return f"Notification({self.user_id}, {self.type}, {self.status})"
