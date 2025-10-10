from django.db import models
from django.conf import settings


class Consultation(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        CANCELLED = "cancelled", "Cancelled"
        COMPLETED = "completed", "Completed"

    id = models.AutoField(primary_key=True)
    title = models.CharField(max_length=255)
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    max_students = models.PositiveIntegerField(default=5)
    is_closed = models.BooleanField(default=False)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE
    )
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="consultations",
        limit_choices_to={"role": "teacher"},
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "start_time"]

    def __str__(self):
        return f"{self.title} ({self.teacher.get_full_name() if hasattr(self.teacher, 'get_full_name') else self.teacher})"

    def close_registration(self):
        self.is_closed = True
        self.save(update_fields=["is_closed"])

    def cancel(self):
        self.status = self.Status.CANCELLED
        self.save(update_fields=["status"])
