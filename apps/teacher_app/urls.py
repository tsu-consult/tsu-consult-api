from django.urls import path

from apps.consultation_app.views import ConsultationsView
from apps.teacher_app.views import TeacherListView, TeacherSubscribeView, TeacherUnsubscribeView

urlpatterns = [
    path('', TeacherListView.as_view(), name="teacher-list"),
    path("<int:teacher_id>/consultations/", ConsultationsView.as_view(), name="teacher-consultations"),
    path("<int:teacher_id>/subscribe/", TeacherSubscribeView.as_view(), name="teacher-subscribe"),
    path("<int:teacher_id>/unsubscribe/", TeacherUnsubscribeView.as_view(), name="teacher-unsubscribe"),
]
