import pytz
from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html

from admin_site import admin_site
from apps.todo_app.admin_api_service import ToDoAdminAPIService
from apps.todo_app.models import ToDo


@admin.register(ToDo, site=admin_site)
class ToDoAdmin(admin.ModelAdmin):
    list_display = ('title', 'status_badge', 'creator', 'assignee', 'deadline_display', 'created_at_tomsk')
    list_filter = ('status', 'created_at', 'deadline')
    search_fields = ('title', 'description', 'creator__username', 'assignee__username')
    ordering = ('-created_at',)

    fieldsets = (
        ('Main Information', {
            'fields': ('title', 'description', 'status')
        }),
        ('Participants', {
            'fields': ('creator', 'assignee')
        }),
        ('Deadlines', {
            'fields': ('deadline', 'created_at_tomsk', 'updated_at_tomsk')
        })
    )

    readonly_fields = ('status', 'created_at_tomsk', 'updated_at_tomsk')

    def to_tomsk_time(self, dt):
        if dt is None:
            return None
        tomsk_tz = pytz.timezone('Asia/Tomsk')
        if timezone.is_aware(dt):
            return dt.astimezone(tomsk_tz)
        return tomsk_tz.localize(dt)

    def deadline_display(self, obj):
        if obj.deadline:
            tomsk_time = self.to_tomsk_time(obj.deadline)
            return tomsk_time.strftime('%Y-%m-%d %H:%M:%S %Z')
        return '-'

    deadline_display.short_description = 'Deadline (Tomsk)'
    deadline_display.admin_order_field = 'deadline'

    def created_at_tomsk(self, obj):
        if obj.created_at:
            tomsk_time = self.to_tomsk_time(obj.created_at)
            return tomsk_time.strftime('%Y-%m-%d %H:%M:%S %Z')
        return '-'

    created_at_tomsk.short_description = 'Created at (Tomsk)'

    def updated_at_tomsk(self, obj):
        if obj.updated_at:
            tomsk_time = self.to_tomsk_time(obj.updated_at)
            return tomsk_time.strftime('%Y-%m-%d %H:%M:%S %Z')
        return '-'

    updated_at_tomsk.short_description = 'Updated at (Tomsk)'

    def status_badge(self, obj):
        if obj.status == ToDo.Status.DONE:
            color = 'green'
            icon = '✓'
        else:
            color = 'orange'
            icon = '⏳'
        return format_html(
            '<span style="color: {}; font-weight: bold;">{} {}</span>',
            color, icon, obj.get_status_display()
        )

    status_badge.short_description = 'Status'
    status_badge.admin_order_field = 'status'

    def has_module_permission(self, request):
        if request.user.is_superuser:
            return True
        if hasattr(request.user, 'role'):
            return request.user.role in ['dean', 'admin']
        return False

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if hasattr(request.user, 'role'):
            if request.user.role == 'admin':
                return True
            if request.user.role == 'dean':
                if obj is None:
                    return True
                return obj.creator_id == request.user.id
        return False

    def has_change_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if hasattr(request.user, 'role'):
            if request.user.role == 'admin':
                return True
            if request.user.role == 'dean' and obj is not None:
                return obj.creator_id == request.user.id
        return False

    def has_delete_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if hasattr(request.user, 'role'):
            if request.user.role == 'admin':
                return True
            if request.user.role == 'dean' and obj is not None:
                return obj.creator_id == request.user.id
        return False

    def has_add_permission(self, request):
        if request.user.is_superuser:
            return True
        if hasattr(request.user, 'role'):
            return request.user.role in ['dean', 'admin']
        return False

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        if hasattr(request.user, 'role'):
            if request.user.role == 'admin':
                return qs
            if request.user.role == 'dean':
                return qs.filter(creator=request.user)
        return qs.none()

    def get_readonly_fields(self, request, obj=None):
        readonly = list(self.readonly_fields)
        if hasattr(request.user, 'role') and request.user.role == 'dean':
            if 'creator' not in readonly:
                readonly.append('creator')
        return readonly

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "assignee":
            from apps.auth_app.models import User
            kwargs["queryset"] = User.objects.filter(role='teacher')
            kwargs["required"] = True
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        api_service = ToDoAdminAPIService(request.user)

        data = {
            'title': obj.title,
            'description': obj.description,
            'deadline': obj.deadline.isoformat() if obj.deadline else None,
            'assignee_id': obj.assignee_id,
        }

        if not change:
            updated_obj = api_service.create_todo(data)
            obj.pk = updated_obj.pk
        else:
            api_service.update_todo(obj.pk, data)

    def delete_model(self, request, obj):
        api_service = ToDoAdminAPIService(request.user)

        api_service.delete_todo(obj.pk)

    def delete_queryset(self, request, queryset):
        api_service = ToDoAdminAPIService(request.user)

        count = 0
        for obj in queryset:
            api_service.delete_todo(obj.pk)
            count += 1
