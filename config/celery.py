import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    "retry-pending-notifications-every-5-min": {
        "task": "apps.notification_app.tasks.retry_pending_notifications",
        "schedule": 120.0,
    },
}
