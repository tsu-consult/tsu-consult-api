from django.db import models
from apps.auth_app.models import User


class GoogleToken(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='google_token')
    credentials = models.TextField()
