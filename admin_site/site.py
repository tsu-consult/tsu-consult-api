from django.contrib.admin import AdminSite
from django.urls import path, reverse

from admin_site.views import my_profile_redirect


class TSUAdminSite(AdminSite):
    site_header = "TSU Consult"
    site_title = "TSU Consult"
    index_title = "Welcome to the TSU Consult"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('my-profile/', my_profile_redirect, name='my_profile'),
        ]
        return custom_urls + urls

    def each_context(self, request):
        context = super().each_context(request)
        if request.user.is_authenticated and hasattr(request.user, 'id'):
            try:
                profile_url = reverse('tsu_admin:auth_app_user_change', args=[request.user.id])
                context['user_profile_url'] = profile_url
            except:
                context['user_profile_url'] = None
        return context


admin_site = TSUAdminSite(name='tsu_admin')
