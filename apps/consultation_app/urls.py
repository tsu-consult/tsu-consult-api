from django.urls import path

from apps.consultation_app.views import BookConsultationView

urlpatterns = [
    path("<int:consultation_id>/book/", BookConsultationView.as_view(), name="book-consultation"),
]
