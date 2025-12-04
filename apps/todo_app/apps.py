from django.apps import AppConfig


class TodosAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.todo_app'
    verbose_name = "To Do Management"
