from django.urls import path
from .views import (ProfileView, ResubmitTeacherApprovalView, ResubmitDeanApprovalView,
                    ChangeEmailView, ChangePasswordView)
from .google_calendar_views import GoogleCalendarInitView, GoogleCalendarRedirectView, GoogleCalendarDisconnectView

urlpatterns = [
    path('', ProfileView.as_view(), name='profile'),
    path("approval/resubmit/", ResubmitTeacherApprovalView.as_view(), name="resubmit-teacher-approval"),
    path("approval/resubmit/dean/", ResubmitDeanApprovalView.as_view(), name="resubmit-dean-approval"),
    path('change/email/', ChangeEmailView.as_view(), name='change-email'),
    path('change/password/', ChangePasswordView.as_view(), name='change-password'),
    path('calendar/init/', GoogleCalendarInitView.as_view(), name='google-calendar-init'),
    path('calendar/redirect/', GoogleCalendarRedirectView.as_view(), name='google-calendar-redirect'),
    path('calendar/disconnect/', GoogleCalendarDisconnectView.as_view(), name='google-calendar-disconnect'),
]
