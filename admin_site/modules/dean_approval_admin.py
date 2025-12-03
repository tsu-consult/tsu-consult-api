from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _
from apps.auth_app.models import DeanApproval, User
from ..site import admin_site


@admin.register(DeanApproval, site=admin_site)
class DeanApprovalAdmin(admin.ModelAdmin):
    list_display = ('user', 'status', 'created_at', 'updated_at')
    list_filter = ('status', 'created_at')
    search_fields = ('user__username', 'user__email')
    actions = ['approve_deans', 'reject_deans']

    fields = ('user', 'status', 'reason')

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return ('user',)
        return ()

    def _update_deans_status(self, request, queryset, new_status, user_status, success_message):
        count = 0
        for approval in queryset:
            if approval.status != new_status:
                approval.status = new_status
                approval.save()

                user: User = approval.user
                user.status = user_status
                user.save()

                print(f"Администратор {request.user.username} изменил статус деканата {user.username} → {new_status}")

                count += 1

        level = messages.SUCCESS if new_status == DeanApproval.Status.APPROVED else messages.WARNING
        self.message_user(request, _(f"{count} deans were {success_message}."), level=level)

    @admin.action(description=_("Confirm selected deans"))
    def approve_deans(self, request, queryset):
        self._update_deans_status(
            request,
            queryset,
            new_status=DeanApproval.Status.APPROVED,
            user_status=User.Status.ACTIVE,
            success_message="confirmed successfully",
        )

    @admin.action(description=_("Reject selected deans"))
    def reject_deans(self, request, queryset):
        self._update_deans_status(
            request,
            queryset,
            new_status=DeanApproval.Status.REJECTED,
            user_status=User.Status.REJECTED,
            success_message="rejected successfully",
        )

    def save_model(self, request, obj: DeanApproval, form, change):
        if change:
            old_obj = DeanApproval.objects.get(pk=obj.pk)
            user: User = obj.user

            if old_obj.status != obj.status:
                if obj.status == DeanApproval.Status.APPROVED:
                    user.status = User.Status.ACTIVE
                    print(f"Администратор {request.user.username} подтвердил деканат {user.username}")
                elif obj.status == DeanApproval.Status.REJECTED:
                    user.status = User.Status.REJECTED
                    print(f"Администратор {request.user.username} отклонил деканат {user.username}")
                user.save()

        super().save_model(request, obj, form, change)
