from django.urls import path

from apps.consultation_app.views import ConsultationsView
from apps.teacher_app.views import TeacherListView

urlpatterns = [
    path('', TeacherListView.as_view(), name="teacher-list"),
    path("<int:teacher_id>/consultations/", ConsultationsView.as_view(), name="teacher-consultations"),
]
