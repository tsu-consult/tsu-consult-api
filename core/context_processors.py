def jazzmin_context(request):
    context = {}

    if request.user.is_authenticated and hasattr(request.user, 'role'):
        if request.user.role == 'dean':
            context['show_profile_link'] = True
        else:
            context['show_profile_link'] = False
    else:
        context['show_profile_link'] = False

    return context
