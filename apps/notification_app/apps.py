from django.apps import AppConfig


class NotificationAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = "apps.notification_app"

    def ready(self):
        import apps.notification_app.signals
