from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from apps.auth_app.models import User
from ..site import admin_site


@admin.register(User, site=admin_site)
class UserAdmin(BaseUserAdmin):
    list_display = ('email', 'username', 'role', 'status', 'is_staff', 'is_superuser')
    list_filter = ('role', 'status', 'is_staff', 'is_superuser')
    search_fields = ('email', 'username', 'telegram_id')
    ordering = ('email',)

    fieldsets = (
        (None, {'fields': ('email', 'username', 'password')}),
        ('Personal Info', {'fields': ('first_name', 'last_name', 'telegram_id', 'phone_number')}),
        ('Permissions',
         {'fields': ('role', 'status', 'is_active', 'is_staff', 'is_superuser')}),
        ('Important Dates', {'fields': ('last_login', 'date_joined')}),
    )

    readonly_fields = ('status', 'last_login', 'date_joined')

    actions = ['make_student', 'make_teacher', 'make_admin']

    @admin.action(description="Make selected users students")
    def make_student(self, request, queryset):
        updated = queryset.update(role=User.Role.STUDENT)
        self.message_user(request, f"{updated} пользователей теперь имеют роль Student.", level=messages.SUCCESS)

    @admin.action(description="Make selected users teachers")
    def make_teacher(self, request, queryset):
        updated = queryset.update(role=User.Role.TEACHER)
        self.message_user(request, f"{updated} пользователей теперь имеют роль Teacher.", level=messages.SUCCESS)

    @admin.action(description="Make selected users administrators")
    def make_admin(self, request, queryset):
        updated = queryset.update(role=User.Role.ADMIN)
        self.message_user(request, f"{updated} пользователей теперь имеют роль Admin.", level=messages.SUCCESS)
