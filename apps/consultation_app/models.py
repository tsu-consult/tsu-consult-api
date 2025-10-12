from django.contrib.auth import get_user_model
from django.db import models
from django.conf import settings

from apps.notification_app.models import Notification

User = get_user_model()


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
    closed_by_teacher = models.BooleanField(default=False)
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

    def close_registration(self, by_teacher=False):
        self.is_closed = True
        self.closed_by_teacher = by_teacher
        self.save(update_fields=["is_closed", "closed_by_teacher"])

    def open_registration_if_needed(self):
        if self.is_closed and not self.closed_by_teacher and self.bookings.count() < self.max_students:
            self.is_closed = False
            self.save(update_fields=["is_closed"])

            booked_students = set(self.bookings.values_list("student_id", flat=True))
            for sub in self.teacher.subscribers.exclude(student_id__in=booked_students):
                Notification.objects.create(
                    user=sub.student,
                    title="Переоткрытие записи на консультацию",
                    message=(
                        f"Запись на консультацию «{self.title}» преподавателя "
                        f"{self.teacher.get_full_name()} была переоткрыта — "
                        f"появилось свободное место. Запишитесь скорее!"
                    ),
                    type=Notification.Type.TELEGRAM,
                )

    def cancel(self):
        self.status = self.Status.CANCELLED
        self.is_closed = True
        self.save(update_fields=["status", "is_closed"])


class ConsultationRequest(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ACCEPTED = "accepted", "Accepted"
        CLOSED = "closed", "Closed"

    id = models.AutoField(primary_key=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    creator = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="consultation_requests"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.title} ({self.creator})"

class ConsultationRequestSubscription(models.Model):
    id = models.AutoField(primary_key=True)
    request = models.ForeignKey(
        "ConsultationRequest",
        on_delete=models.CASCADE,
        related_name="subscriptions"
    )
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="request_subscriptions",
        limit_choices_to={"role": "student"}
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("request", "student")

    def __str__(self):
        return f"Subscription(student={self.student_id}, request={self.request_id})"


class Booking(models.Model):
    id = models.AutoField(primary_key=True)
    consultation = models.ForeignKey(
        Consultation,
        on_delete=models.CASCADE,
        related_name="bookings"
    )
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="bookings",
        limit_choices_to={"role": "student"}
    )
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("consultation", "student")

    def __str__(self):
        return f"Booking(student={self.student_id}, consultation={self.consultation_id})"