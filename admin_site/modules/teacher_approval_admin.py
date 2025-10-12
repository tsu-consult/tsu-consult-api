from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _
from apps.auth_app.models import TeacherApproval, User
from ..site import admin_site

@admin.register(TeacherApproval, site=admin_site)
class TeacherApprovalAdmin(admin.ModelAdmin):
    list_display = ('user', 'status', 'created_at', 'updated_at')
    list_filter = ('status', 'created_at')
    search_fields = ('user__username', 'user__email')
    actions = ['approve_teachers', 'reject_teachers']

    fields = ('user', 'status', 'reason')

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return ('user',)
        return ()

    def _update_teachers_status(self, request, queryset, new_status, user_status, success_message):
        count = 0
        for approval in queryset:
            if approval.status != new_status:
                approval.status = new_status
                approval.save()

                user: User = approval.user
                user.status = user_status
                user.save()

                print(f"Администратор {request.user.username} изменил статус преподавателя {user.username} → {new_status}")

                count += 1

        level = messages.SUCCESS if new_status == TeacherApproval.Status.APPROVED else messages.WARNING
        self.message_user(request, _(f"{count} teachers were {success_message}."), level=level)

    @admin.action(description=_("Confirm selected teachers"))
    def approve_teachers(self, request, queryset):
        self._update_teachers_status(
            request,
            queryset,
            new_status=TeacherApproval.Status.APPROVED,
            user_status=User.Status.ACTIVE,
            success_message="confirmed successfully",
        )

    @admin.action(description=_("Reject selected teachers"))
    def reject_teachers(self, request, queryset):
        self._update_teachers_status(
            request,
            queryset,
            new_status=TeacherApproval.Status.REJECTED,
            user_status=User.Status.REJECTED,
            success_message="rejected successfully",
        )

    def save_model(self, request, obj: TeacherApproval, form, change):
        if change:
            old_obj = TeacherApproval.objects.get(pk=obj.pk)
            user: User = obj.user

            if old_obj.status != obj.status:
                if obj.status == TeacherApproval.Status.APPROVED:
                    user.status = User.Status.ACTIVE
                    print(f"Администратор {request.user.username} подтвердил преподавателя {user.username}")
                elif obj.status == TeacherApproval.Status.REJECTED:
                    user.status = User.Status.REJECTED
                    print(f"Администратор {request.user.username} отклонил преподавателя {user.username}")
                user.save()

        super().save_model(request, obj, form, change)
