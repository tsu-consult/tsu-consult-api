from django.contrib import admin
from apps.consultation_app.models import Consultation, Booking


@admin.register(Consultation)
class ConsultationAdmin(admin.ModelAdmin):
    list_display = (
        "id", "title", "teacher", "date", "start_time", "end_time",
        "max_students", "is_closed", "status", "created_at"
    )
    list_filter = ("status", "is_closed", "date", "teacher")
    search_fields = (
        "title",
        "teacher__username",
        "teacher__first_name",
        "teacher__last_name"
    )
    ordering = ("-date", "start_time")
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("teacher",)
    list_per_page = 25


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("id", "student", "consultation", "message", "created_at")
    list_filter = ("created_at", "consultation__date", "consultation__teacher")
    search_fields = (
        "student__username",
        "student__first_name",
        "student__last_name",
        "consultation__title",
        "consultation__teacher__username"
    )
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)
    autocomplete_fields = ("student", "consultation")
    list_per_page = 25
