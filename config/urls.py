from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView
from drf_yasg import openapi
from drf_yasg.views import get_schema_view

from admin_site import admin_site
from config.settings import BASE_DIR

PROJECT_NAME = BASE_DIR.name

schema_view = get_schema_view(
    openapi.Info(
        title=f"TSU Consult API",
        default_version='v1',
        description=f"API docs for TSU Consult.\n\n"
                    f"<a href='/admin/' target='_blank'>➡️ Go to Django Admin</a>",
    ),
    public=True,
)

urlpatterns = [
    path('admin/', admin_site.urls),
    path('project/admin/', admin.site.urls),
    path('swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
    path('auth/', include('apps.auth_app.urls')),
    path('profile/', include('apps.profile_app.urls')),
    path('teachers/', include('apps.teacher_app.urls')),
    path('consultations/', include('apps.consultation_app.urls')),
    path('todo/', include('apps.todo_app.urls')),

    path('', RedirectView.as_view(url='/swagger/', permanent=False)),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
