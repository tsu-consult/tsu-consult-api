from django.urls import path
from apps.teacher_app.views import TeacherListView

urlpatterns = [
    path('', TeacherListView.as_view(), name="teacher-list"),
]
