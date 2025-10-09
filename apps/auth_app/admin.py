from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _
from django.contrib.auth import get_user_model

from apps.auth_app.models import TeacherApproval

User = get_user_model()

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ('id', 'email', 'username', 'role', 'status', 'is_staff', 'is_active')
    list_filter = ('role', 'status', 'is_staff', 'is_superuser', 'is_active', 'groups')

    fieldsets = (
        (None, {'fields': ('email', 'username', 'password')}),
        (_('Personal info'), {'fields': ('telegram_id', 'phone_number', 'first_name', 'last_name', 'role', 'status')}),
        (_('Permissions'), {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        (_('Important dates'), {'fields': ('last_login', 'date_joined')}),
    )

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'username', 'password1', 'password2', 'role'),
        }),
    )

    search_fields = ('email', 'username', 'telegram_id')
    ordering = ('id',)


@admin.register(TeacherApproval)
class TeacherApprovalAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "status", "created_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("user__username", "user__email")
    readonly_fields = ("created_at", "updated_at")