from django.contrib import admin
from apps.notification_app.models import Notification

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "type", "status", "title", "created_at", "sent_at")
    search_fields = ("title", "message", "user__email")
    list_filter = ("type", "status")
