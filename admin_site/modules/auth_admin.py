from apps.auth_app.models import User
from django.contrib.auth.admin import UserAdmin
from ..site import admin_site

admin_site.register(User, UserAdmin)