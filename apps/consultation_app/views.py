from django.shortcuts import get_object_or_404
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.auth_app.permissions import IsStudent, IsTeacher
from apps.consultation_app.models import Consultation, Booking, ConsultationRequest, ConsultationRequestSubscription
from apps.consultation_app.serializers import PaginatedConsultationsSerializer, ConsultationResponseSerializer, \
    BookingRequestSerializer, BookingResponseSerializer, ConsultationRequestSerializer, \
    ConsultationRequestResponseSerializer, ConsultationCreateSerializer, ConsultationUpdateSerializer
from apps.notification_app.models import Notification
from core.pagination import DefaultPagination
from core.serializers import ErrorResponseSerializer
from core.mixins import ErrorResponseMixin


class ConsultationsView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudent]
    pagination_class = DefaultPagination

    @swagger_auto_schema(
        tags=["Teachers"],
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


class ConsultationCreateView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    @swagger_auto_schema(
        tags=["Consultations"],
        operation_summary="Создание консультации преподавателем",
        request_body=ConsultationCreateSerializer,
        responses={
            201: openapi.Response(description="Консультация успешно создана", schema=ConsultationResponseSerializer),
            400: openapi.Response(description="Некорректные данные", schema=ErrorResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def post(self, request):
        serializer = ConsultationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        consultation = serializer.save(teacher=request.user)

        for sub in request.user.subscribers.select_related("student"):
            Notification.objects.create(
                user=sub.student,
                title="Новое время консультации",
                message=f"Преподаватель {request.user.get_full_name()} опубликовал консультацию «{consultation.title}».",
                type=Notification.Type.TELEGRAM,
            )

        return Response(ConsultationResponseSerializer(consultation).data, status=201)

class ConsultationUpdateView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    @swagger_auto_schema(
        tags=["Consultations"],
        operation_summary="Изменение консультации преподавателем",
        request_body=ConsultationUpdateSerializer,
        responses={
            200: openapi.Response(description="Консультация успешно обновлена", schema=ConsultationResponseSerializer),
            400: openapi.Response(description="Некорректные данные", schema=ErrorResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Консультация не найдена", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def patch(self, request, consultation_id):
        consultation = get_object_or_404(Consultation, id=consultation_id, teacher=request.user)
        serializer = ConsultationUpdateSerializer(
            consultation, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        consultation = serializer.save()

        remaining_slots = max(0, consultation.max_students - consultation.bookings.count())

        subscribers = set(sub.student for sub in consultation.teacher.subscribers.all())
        booked_students = set(booking.student for booking in consultation.bookings.all())
        all_students_to_notify = subscribers.union(booked_students)

        for student in all_students_to_notify:
            Notification.objects.create(
                user=student,
                title=f"Изменение в расписании преподавателя {consultation.teacher.get_full_name()}",
                message=(
                    f"Консультация '{consultation.title}' была обновлена.\n"
                    f"Дата: {consultation.date}, время: {consultation.start_time}-{consultation.end_time}.\n"
                    f"Доступные места: {remaining_slots}"
                ),
                type=Notification.Type.TELEGRAM,
            )

        return Response(ConsultationResponseSerializer(consultation).data, status=200)


class BookConsultationView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudent]

    @swagger_auto_schema(
        tags=["Consultations"],
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

class CancelBookingView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudent]

    @swagger_auto_schema(
        tags=["Consultations"],
        operation_summary="Отмена записи на консультацию",
        operation_description="Позволяет студенту отменить свою запись на выбранную консультацию.",
        responses={
            204: openapi.Response(description="Запись успешно отменена"),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Запись не найдена", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def delete(self, request, consultation_id):
        consultation = get_object_or_404(
            Consultation,
            id=consultation_id,
            status=Consultation.Status.ACTIVE
        )

        booking = Booking.objects.filter(
            consultation=consultation,
            student=request.user
        ).first()

        if not booking:
            return self.format_error(
                request, 404, "Not Found",
                "You are not registered for this consultation."
            )

        booking.delete()
        return Response(status=204)


class ConsultationRequestView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudent]

    @swagger_auto_schema(
        tags=["Consultations"],
        operation_summary="Создание запроса на консультацию",
        request_body=ConsultationRequestSerializer,
        responses={
            201: openapi.Response(description="Запрос успешно создан", schema=ConsultationRequestResponseSerializer),
            400: openapi.Response(description="Некорректные данные", schema=ErrorResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def post(self, request):
        serializer = ConsultationRequestSerializer(data=request.data, context={"request": request})
        if serializer.is_valid():
            consultation_request = serializer.save()
            return Response(ConsultationRequestResponseSerializer(consultation_request).data, status=201)
        return ErrorResponseMixin.format_error(request, 400, "Bad Request", serializer.errors)



class ConsultationRequestsListView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated]
    pagination_class = DefaultPagination

    @swagger_auto_schema(
        tags=["Consultations"],
        operation_summary="Просмотр всех запросов на консультацию",
        manual_parameters=[
            openapi.Parameter( "page", openapi.IN_QUERY, description="Номер страницы", type=openapi.TYPE_INTEGER, default=1),
            openapi.Parameter( "page_size", openapi.IN_QUERY, description="Количество элементов на странице", type=openapi.TYPE_INTEGER, default=10),
            openapi.Parameter( "status", openapi.IN_QUERY, description="Фильтр по статусу (open, accepted, closed)", type=openapi.TYPE_STRING),
        ],
        responses={
            200: openapi.Response(description="Список запросов на консультацию", schema=ConsultationRequestResponseSerializer(many=True)),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        }
    )
    def get(self, request):
        requests = ConsultationRequest.objects.all().order_by("-created_at")

        status_filter = request.query_params.get("status")
        if status_filter:
            requests = requests.filter(status=status_filter)

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(requests, request)
        serializer = ConsultationRequestResponseSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class ConsultationRequestSubscribeView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudent]

    @swagger_auto_schema(
        tags=["Consultations"],
        operation_summary="Подписка под запросом на консультацию",
        operation_description="Студент подписывается под запросом, чтобы получить уведомление и быть автоматически записанным, если преподаватель создаст консультацию по этому запросу.",
        responses={
            201: openapi.Response(description="Подписка успешно создана"),
            400: openapi.Response(description="Некорректные данные", schema=ErrorResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Запрос не найден", schema=ErrorResponseSerializer),
            409: openapi.Response(description="Студент уже подписан под этим запросом", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        }
    )
    def post(self, request, request_id):
        consultation_request = get_object_or_404(ConsultationRequest, id=request_id)

        if consultation_request.status != ConsultationRequest.Status.OPEN:
            return self.format_error(request, 400, "Bad Request", "This consultation request is not open for subscriptions.")

        if ConsultationRequestSubscription.objects.filter(request=consultation_request, student=request.user).exists():
            return self.format_error(request, 409, "Conflict", "You are already subscribed to this request.")

        ConsultationRequestSubscription.objects.create(request=consultation_request, student=request.user)

        return Response(status=201)

class ConsultationRequestUnsubscribeView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudent]

    @swagger_auto_schema(
        tags=["Consultations"],
        operation_summary="Отмена подписки под запросом на консультацию",
        responses={
            204: openapi.Response(description="Подписка успешно отменена"),
            400: openapi.Response(description="Вы не подписаны на этот запрос", schema=ErrorResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Запрос не найден", schema=ErrorResponseSerializer),
        }
    )
    def delete(self, request, request_id):
        consultation_request = get_object_or_404(ConsultationRequest, id=request_id)

        subscription = ConsultationRequestSubscription.objects.filter(
            request=consultation_request,
            student=request.user
        ).first()

        if not subscription:
            return self.format_error(request, 400, "Bad Request", "You are not subscribed to this request.")

        subscription.delete()
        return Response(status=204)