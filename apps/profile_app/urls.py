from django.urls import path
from .views import ProfileView, ResubmitTeacherApprovalView

urlpatterns = [
    path('', ProfileView.as_view(), name='profile'),
    path("approval/resubmit/", ResubmitTeacherApprovalView.as_view(), name="resubmit-teacher-approval"),
]