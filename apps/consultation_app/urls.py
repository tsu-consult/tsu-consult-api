from django.urls import path

from apps.consultation_app.views import BookConsultationView, CancelBookingView

urlpatterns = [
    path("<int:consultation_id>/book/", BookConsultationView.as_view(), name="book-consultation"),
    path("<int:consultation_id>/cancel/", CancelBookingView.as_view(), name="cancel-booking"),
]
