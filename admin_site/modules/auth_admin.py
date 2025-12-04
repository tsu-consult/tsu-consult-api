from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from apps.auth_app.models import User, TeacherApproval, DeanApproval
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

    actions = ['make_student', 'make_teacher', 'make_dean', 'make_admin']

    def has_module_permission(self, request):
        if request.user.is_superuser:
            return True
        if hasattr(request.user, 'role'):
            return request.user.role in [User.Role.DEAN, User.Role.ADMIN]
        return False

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if hasattr(request.user, 'role'):
            return request.user.role in [User.Role.DEAN, User.Role.ADMIN]
        return False

    def has_change_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if hasattr(request.user, 'role'):
            if request.user.role == User.Role.ADMIN:
                return True
            if request.user.role == User.Role.DEAN and obj is not None:
                return obj.id == request.user.id
        return False

    def has_delete_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if hasattr(request.user, 'role'):
            return request.user.role == User.Role.ADMIN
        return False

    def has_add_permission(self, request):
        if request.user.is_superuser:
            return True
        if hasattr(request.user, 'role'):
            return request.user.role == User.Role.ADMIN
        return False

    def get_readonly_fields(self, request, obj=None):
        readonly = list(self.readonly_fields)
        if hasattr(request.user, 'role') and request.user.role == User.Role.DEAN and not request.user.is_superuser:
            if obj and obj.id == request.user.id:
                return ['username', 'telegram_id', 'phone_number', 'password', 'role', 'status', 'is_active',
                        'is_staff', 'is_superuser', 'last_login', 'date_joined']
            else:
                all_fields = []
                for fieldset in self.fieldsets:
                    all_fields.extend(fieldset[1]['fields'])
                return all_fields
        return readonly

    def get_actions(self, request):
        actions = super().get_actions(request)
        if hasattr(request.user, 'role') and request.user.role == User.Role.DEAN and not request.user.is_superuser:
            return {}
        return actions

    def change_view(self, request, object_id, form_url='', extra_context=None):
        extra_context = extra_context or {}
        if hasattr(request.user, 'role') and request.user.role == User.Role.DEAN and not request.user.is_superuser:
            try:
                obj = self.get_object(request, object_id)
                if obj and obj.id != request.user.id:
                    extra_context['show_save'] = False
                    extra_context['show_save_and_continue'] = False
                    extra_context['show_save_and_add_another'] = False
                    extra_context['show_delete'] = False
                else:
                    extra_context['show_delete'] = False
                    extra_context['show_save_and_add_another'] = False
            except:
                pass
        return super().change_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj: User, form, change):
        if change:
            old_obj = User.objects.get(pk=obj.pk)
            if old_obj.role != obj.role:
                if obj.role in [User.Role.TEACHER, User.Role.DEAN]:
                    obj.status = User.Status.ACTIVE
                elif old_obj.role == User.Role.TEACHER and obj.role != User.Role.TEACHER:
                    approvals = TeacherApproval.objects.filter(user=obj, status=TeacherApproval.Status.PENDING)
                    for approval in approvals:
                        approval.status = TeacherApproval.Status.REJECTED
                        approval.save()
                    obj.status = User.Status.ACTIVE
                elif old_obj.role == User.Role.DEAN and obj.role != User.Role.DEAN:
                    approvals = DeanApproval.objects.filter(user=obj, status=DeanApproval.Status.PENDING)
                    for approval in approvals:
                        approval.status = DeanApproval.Status.REJECTED
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

    @admin.action(description="Make selected users deans")
    def make_dean(self, request, queryset):
        updated_count = 0
        for user in queryset:
            if user.role != User.Role.DEAN:
                user.role = User.Role.DEAN
                user.status = User.Status.ACTIVE
                user.save()
                updated_count += 1
        self.message_user(request, f"{updated_count} users now have the role of Dean.", level=messages.SUCCESS)

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
            elif old_role == User.Role.DEAN:
                approvals = DeanApproval.objects.filter(user=user, status=DeanApproval.Status.PENDING)
                for approval in approvals:
                    approval.status = DeanApproval.Status.REJECTED
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
            elif old_role == User.Role.DEAN:
                approvals = DeanApproval.objects.filter(user=user, status=DeanApproval.Status.PENDING)
                for approval in approvals:
                    approval.status = DeanApproval.Status.REJECTED
                    approval.save()
            updated_count += 1
        self.message_user(request, f"{updated_count} users now have the role of Admin.", level=messages.SUCCESS)
