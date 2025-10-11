from django.urls import path

from apps.consultation_app.views import BookConsultationView, CancelBookingView, ConsultationRequestView, \
    ConsultationRequestsListView, ConsultationRequestSubscribeView

urlpatterns = [
    path("<int:consultation_id>/book/", BookConsultationView.as_view(), name="book-consultation"),
    path("<int:consultation_id>/cancel/", CancelBookingView.as_view(), name="cancel-booking"),
    path("request", ConsultationRequestView.as_view(), name="consultation-request"),
    path("requests/", ConsultationRequestsListView.as_view(), name="consultation-requests"),
    path("requests/<int:request_id>/subscribe/", ConsultationRequestSubscribeView.as_view(), name="consultation-request-subscribe"),
]
