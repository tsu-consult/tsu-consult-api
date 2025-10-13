from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from apps.auth_app.models import User, TeacherApproval
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

    def save_model(self, request, obj: User, form, change):
        if change:
            old_obj = User.objects.get(pk=obj.pk)
            if old_obj.role != obj.role:
                if obj.role == User.Role.TEACHER:
                    obj.status = User.Status.ACTIVE
                elif old_obj.role == User.Role.TEACHER and obj.role != User.Role.TEACHER:
                    approvals = TeacherApproval.objects.filter(user=obj, status=TeacherApproval.Status.PENDING)
                    for approval in approvals:
                        approval.status = TeacherApproval.Status.REJECTED
                        approval.save()
                    obj.status = User.Status.ACTIVE

        super().save_model(request, obj, form, change)

    @admin.action(description="Make selected users teachers")
    def make_teacher(self, request, queryset):
        updated_count = 0
        for user in queryset:
            if user.role != User.Role.TEACHER:
                user.role = User.Role.TEACHER
                user.status = User.Status.ACTIVE
                user.save()
                updated_count += 1
        self.message_user(request, f"{updated_count} users now have the role of Teacher.", level=messages.SUCCESS)

    @admin.action(description="Make selected users students")
    def make_student(self, request, queryset):
        updated_count = 0
        for user in queryset:
            old_role = user.role
            user.role = User.Role.STUDENT
            user.status = User.Status.ACTIVE
            user.save()
            if old_role == User.Role.TEACHER:
                approvals = TeacherApproval.objects.filter(user=user, status=TeacherApproval.Status.PENDING)
                for approval in approvals:
                    approval.status = TeacherApproval.Status.REJECTED
                    approval.save()
            updated_count += 1
        self.message_user(request, f"{updated_count} users now have the role of Student.", level=messages.SUCCESS)

    @admin.action(description="Make selected users administrators")
    def make_admin(self, request, queryset):
        updated_count = 0
        for user in queryset:
            old_role = user.role
            user.role = User.Role.ADMIN
            user.status = User.Status.ACTIVE
            user.save()
            if old_role == User.Role.TEACHER:
                approvals = TeacherApproval.objects.filter(user=user, status=TeacherApproval.Status.PENDING)
                for approval in approvals:
                    approval.status = TeacherApproval.Status.REJECTED
                    approval.save()
            updated_count += 1
        self.message_user(request, f"{updated_count} users now have the role of Admin.", level=messages.SUCCESS)
