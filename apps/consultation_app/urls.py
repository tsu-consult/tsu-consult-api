from django.urls import path

from apps.consultation_app.views import BookConsultationView, CancelBookingView, ConsultationRequestView, \
    ConsultationRequestsListView, ConsultationRequestSubscribeView, ConsultationRequestUnsubscribeView, \
    ConsultationCreateView, ConsultationUpdateView, MyConsultationsView, ConsultationStudentsView, CloseConsultationView

urlpatterns = [
    path("<int:consultation_id>/book/", BookConsultationView.as_view(), name="book-consultation"),
    path("<int:consultation_id>/cancel/", CancelBookingView.as_view(), name="cancel-booking"),
    path("request/", ConsultationRequestView.as_view(), name="consultation-request"),
    path("requests/", ConsultationRequestsListView.as_view(), name="consultation-requests"),
    path("requests/<int:request_id>/subscribe/", ConsultationRequestSubscribeView.as_view(), name="consultation-request-subscribe"),
    path("requests/<int:request_id>/unsubscribe/", ConsultationRequestUnsubscribeView.as_view(), name="consultation-request-unsubscribe"),
    path("", ConsultationCreateView.as_view(), name="create-consultation"),
    path("<int:consultation_id>/", ConsultationUpdateView.as_view(), name="update-consultation"),
    path("my/", MyConsultationsView.as_view(), name="my-consultations"),
    path("<int:consultation_id>/students/", ConsultationStudentsView.as_view(), name="consultation-students"),
    path("<int:consultation_id>/close/", CloseConsultationView.as_view(), name="close-consultation"),
]
