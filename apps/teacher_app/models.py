from django.conf import settings
from django.db import models

User = settings.AUTH_USER_MODEL

class Subscription(models.Model):
    id = models.AutoField(primary_key=True)
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name="subscriptions", limit_choices_to={"role": "student"})
    teacher = models.ForeignKey(User, on_delete=models.CASCADE, related_name="subscribers", limit_choices_to={"role": "teacher"})
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("student", "teacher")

    def __str__(self):
        return f"{self.student} → {self.teacher}"
