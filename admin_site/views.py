from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.urls import reverse


@login_required
def my_profile_redirect(request):
    if request.user.is_authenticated:
        url = reverse('tsu_admin:auth_app_user_change', args=[request.user.id])
        return redirect(url)
    return redirect('tsu_admin:index')
