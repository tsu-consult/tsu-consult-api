from django.contrib import admin
from apps.teacher_app.models import Subscription


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("id", "student", "teacher", "created_at")
    list_filter = ("created_at",)
    search_fields = ("student__username", "student__first_name", "student__last_name",
                     "teacher__username", "teacher__first_name", "teacher__last_name")
    ordering = ("-created_at",)
    autocomplete_fields = ("student", "teacher")
    readonly_fields = ("created_at",)
    list_per_page = 25
