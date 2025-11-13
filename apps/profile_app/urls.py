from django.urls import path
from .views import ProfileView, ResubmitTeacherApprovalView
from .google_calendar_views import GoogleCalendarInitView, GoogleCalendarRedirectView, GoogleCalendarDisconnectView

urlpatterns = [
    path('', ProfileView.as_view(), name='profile'),
    path("approval/resubmit/", ResubmitTeacherApprovalView.as_view(), name="resubmit-teacher-approval"),
    path('calendar/init/', GoogleCalendarInitView.as_view(), name='google-calendar-init'),
    path('calendar/redirect/', GoogleCalendarRedirectView.as_view(), name='google-calendar-redirect'),
    path('calendar/disconnect/', GoogleCalendarDisconnectView.as_view(), name='google-calendar-disconnect'),
]
