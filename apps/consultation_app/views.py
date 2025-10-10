from django.shortcuts import get_object_or_404
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.auth_app.permissions import IsStudent
from apps.consultation_app.models import Consultation, Booking
from apps.consultation_app.serializers import PaginatedConsultationsSerializer, ConsultationResponseSerializer, \
    BookingRequestSerializer, BookingResponseSerializer
from core.pagination import DefaultPagination
from core.serializers import ErrorResponseSerializer
from core.mixins import ErrorResponseMixin


class ConsultationsView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudent]
    pagination_class = DefaultPagination

    @swagger_auto_schema(
        tags=["Student"],
        operation_summary="Просмотр расписания выбранного преподавателя",
        manual_parameters=[
            openapi.Parameter("page", openapi.IN_QUERY, description="Номер страницы", type=openapi.TYPE_INTEGER, default=1,
            ),
            openapi.Parameter("page_size", openapi.IN_QUERY, description="Количество элементов на странице", type=openapi.TYPE_INTEGER, default=10),
        ],
        responses={
            200: openapi.Response(description="Список консультаций преподавателя", schema=PaginatedConsultationsSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Преподаватель не найден", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def get(self, request, teacher_id):
        consultations = Consultation.objects.filter(
            teacher_id=teacher_id,
            status=Consultation.Status.ACTIVE,
        ).order_by("date", "start_time")

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(consultations, request)
        serializer = ConsultationResponseSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)



class BookConsultationView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudent]

    @swagger_auto_schema(
        tags=["Student"],
        operation_summary="Запись на консультацию",
        operation_description="Студент записывается на консультацию, указав сообщение (обязательно).",
        request_body=BookingRequestSerializer,
        responses={
            201: openapi.Response(description="Запись успешно создана", schema=BookingResponseSerializer),
            400: openapi.Response(description="Некорректные данные", schema=ErrorResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Консультация не найдена", schema=ErrorResponseSerializer),
            409: openapi.Response(description="Студент уже записан на консультацию", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def post(self, request, consultation_id):
        consultation = get_object_or_404(Consultation, id=consultation_id, status=Consultation.Status.ACTIVE)

        if consultation.is_closed or consultation.bookings.count() >= consultation.max_students:
            return self.format_error(request, 400, "Bad Request", "Registration for this consultation is closed.")

        if Booking.objects.filter(consultation=consultation, student=request.user).exists():
            return self.format_error(request, 409, "Conflict", "You are already registered for this consultation.")

        serializer = BookingRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        booking = Booking.objects.create(
            consultation=consultation,
            student=request.user,
            message=serializer.validated_data["message"],
        )

        response_serializer = BookingResponseSerializer(booking)
        return Response(response_serializer.data, status=201)