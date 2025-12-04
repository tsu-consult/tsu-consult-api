from django.conf import settings
import copy


class JazzminRoleBasedMenuMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.original_jazzmin_settings = copy.deepcopy(getattr(settings, 'JAZZMIN_SETTINGS', {}))

    def __call__(self, request):
        settings.JAZZMIN_SETTINGS = copy.deepcopy(self.original_jazzmin_settings)

        if request.user.is_authenticated and hasattr(request.user, 'role'):
            if request.user.role == 'dean':
                if 'usermenu_links' not in settings.JAZZMIN_SETTINGS:
                    settings.JAZZMIN_SETTINGS['usermenu_links'] = []

                settings.JAZZMIN_SETTINGS['usermenu_links'].append({
                    "name": "See Profile",
                    "url": "tsu_admin:my_profile",
                    "icon": "fas fa-user"
                })

        response = self.get_response(request)
        return response
