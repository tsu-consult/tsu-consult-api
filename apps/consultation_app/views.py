from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.auth_app.permissions import IsStudent, IsTeacher, IsActive
from apps.consultation_app.models import Consultation, Booking, ConsultationRequest, ConsultationRequestSubscription, \
    StudentWithMessageSerializer
from apps.consultation_app.serializers import PaginatedConsultationsSerializer, ConsultationResponseSerializer, \
    BookingRequestSerializer, BookingResponseSerializer, ConsultationRequestSerializer, \
    ConsultationRequestResponseSerializer, ConsultationCreateSerializer, ConsultationUpdateSerializer, \
    StudentSerializer, PaginatedStudentsSerializer, ConsultationFromRequestCreateSerializer
from apps.notification_app.models import Notification
from core.mixins import ErrorResponseMixin
from core.pagination import DefaultPagination
from core.serializers import ErrorResponseSerializer


class ConsultationsView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudent]
    pagination_class = DefaultPagination

    @swagger_auto_schema(
        tags=["Teachers"],
        operation_summary="Просмотр расписания выбранного преподавателя",
        manual_parameters=[
            openapi.Parameter("page", openapi.IN_QUERY, description="Номер страницы", type=openapi.TYPE_INTEGER, default=1),
            openapi.Parameter("page_size", openapi.IN_QUERY, description="Количество элементов на странице", type=openapi.TYPE_INTEGER, default=10),
            openapi.Parameter("is_closed", openapi.IN_QUERY, description="Фильтр по закрытым консультациям (true / false)", type=openapi.TYPE_BOOLEAN),
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

        is_closed_param = request.query_params.get("is_closed")
        if is_closed_param is not None:
            if is_closed_param.lower() in ["true", "1"]:
                consultations = consultations.filter(is_closed=True)
            elif is_closed_param.lower() in ["false", "0"]:
                consultations = consultations.filter(is_closed=False)

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(consultations, request)
        serializer = ConsultationResponseSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

class MyConsultationsView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsActive]
    pagination_class = DefaultPagination

    @swagger_auto_schema(
        tags=["Consultations"],
        operation_summary="Просмотр своих консультаций",
        operation_description=(
            "Если пользователь — преподаватель, возвращаются созданные им консультации.\n"
            "Если пользователь — студент, возвращаются консультации, на которые он записан."
        ),
        manual_parameters=[
            openapi.Parameter("page", openapi.IN_QUERY, description="Номер страницы", type=openapi.TYPE_INTEGER, default=1),
            openapi.Parameter("page_size", openapi.IN_QUERY, description="Количество элементов на странице", type=openapi.TYPE_INTEGER, default=10),
            openapi.Parameter("is_closed", openapi.IN_QUERY, description="Фильтр по закрытым консультациям (true / false)", type=openapi.TYPE_BOOLEAN),
        ],
        responses={
            200: openapi.Response(description="Список консультаций пользователя", schema=PaginatedConsultationsSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def get(self, request):
        user = request.user

        if user.is_teacher:
            consultations = Consultation.objects.filter(teacher=user).order_by("-date", "-start_time")
        else:
            consultations = Consultation.objects.filter(bookings__student=user).order_by("-date", "-start_time")

        is_closed_param = request.query_params.get("is_closed")
        if is_closed_param is not None:
            if is_closed_param.lower() in ["true", "1"]:
                consultations = consultations.filter(is_closed=True)
            elif is_closed_param.lower() in ["false", "0"]:
                consultations = consultations.filter(is_closed=False)

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(consultations, request)
        serializer = ConsultationResponseSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

class ConsultationStudentsView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsTeacher, IsActive]
    pagination_class = DefaultPagination

    @swagger_auto_schema(
        tags=["Consultations"],
        operation_summary="Список студентов, записанных на консультацию",
        manual_parameters=[
            openapi.Parameter( "page", openapi.IN_QUERY, description="Номер страницы", type=openapi.TYPE_INTEGER, default=1),
            openapi.Parameter( "page_size", openapi.IN_QUERY, description="Количество элементов на странице", type=openapi.TYPE_INTEGER, default=10),
        ],
        responses={
            200: openapi.Response(description="Список записанных студентов", schema=PaginatedStudentsSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Консультация не найдена", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def get(self, request, consultation_id):
        user = request.user
        try:
            consultation = Consultation.objects.get(id=consultation_id)
        except Consultation.DoesNotExist:
            raise NotFound("Consultation not found")

        if consultation.teacher != user:
            return self.format_error(request, 403, "Forbidden", "You are not the owner of this consultation.")

        bookings = Booking.objects.filter(consultation=consultation).select_related("student")

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(bookings, request)
        serializer = StudentWithMessageSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class ConsultationCreateView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher, IsActive]

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
                message=f"Преподаватель {request.user.get_full_name()} опубликовал(-a) консультацию «{consultation.title}».",
                type=Notification.Type.TELEGRAM,
            )

        return Response(ConsultationResponseSerializer(consultation).data, status=201)

class ConsultationUpdateView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsTeacher, IsActive]

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
        try:
            consultation = Consultation.objects.get(id=consultation_id, teacher=request.user)
        except Consultation.DoesNotExist:
            raise NotFound("Consultation not found")

        if consultation.status == Consultation.Status.CANCELLED:
            return self.format_error(request, 400, "Bad Request", "Cancelled consultations cannot be modified.")

        serializer = ConsultationUpdateSerializer(
            consultation, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        consultation = serializer.save()

        current_booked = consultation.bookings.count()

        if consultation.max_students <= current_booked and not consultation.is_closed:
            consultation.close_registration(by_teacher=False)
        elif consultation.max_students > current_booked and consultation.is_closed and not consultation.closed_by_teacher:
            consultation.is_closed = False
            consultation.save(update_fields=["is_closed"])

        remaining_slots = max(0, consultation.max_students - current_booked)

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

class CloseConsultationView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsTeacher, IsActive]

    @swagger_auto_schema(
        tags=["Consultations"],
        operation_summary="Закрытие записи на консультацию",
        responses={
            200: openapi.Response(description="Запись на консультацию успешно закрыта", schema=ConsultationResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Консультация не найдена", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def post(self, request, consultation_id):
        try:
            consultation = Consultation.objects.get(id=consultation_id, teacher=request.user)
        except Consultation.DoesNotExist:
            raise NotFound("Consultation not found")

        if consultation.status != Consultation.Status.ACTIVE:
            return self.format_error(request, 400, "Bad Request", "Only active consultations can be closed.")

        if consultation.is_closed:
            return self.format_error(request, 400, "Bad Request", "Registration is already closed for this consultation.")

        consultation.close_registration(by_teacher=True)

        return Response(ConsultationResponseSerializer(consultation).data, status=200)

class CancelConsultationView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsTeacher, IsActive]

    @swagger_auto_schema(
        tags=["Consultations"],
        operation_summary="Отмена консультации преподавателем",
        responses={
            204: openapi.Response(description="Консультация успешно отменена", schema=ConsultationResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Консультация не найдена", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def delete(self, request, consultation_id):
        try:
            consultation = Consultation.objects.get(id=consultation_id, teacher=request.user)
        except Consultation.DoesNotExist:
            raise NotFound("Consultation not found")

        if consultation.status == Consultation.Status.CANCELLED:
            return self.format_error(request, 400, "Bad Request", "Consultation is already cancelled.")

        consultation.cancel()

        booked_students = [booking.student for booking in consultation.bookings.select_related("student")]
        for student in booked_students:
            Notification.objects.create(
                user=student,
                title="Консультация отменена",
                message=(
                    f"Консультация «{consultation.title}» преподавателя "
                    f"{consultation.teacher.get_full_name()} была отменена."
                ),
                type=Notification.Type.TELEGRAM,
            )

        return Response(status=204)


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
        try:
            consultation = Consultation.objects.get(id=consultation_id, status=Consultation.Status.ACTIVE)
        except Consultation.DoesNotExist:
            raise NotFound("Consultation not found")

        if consultation.is_closed:
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

        if consultation.bookings.count() >= consultation.max_students:
            consultation.close_registration(by_teacher=False)

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
        try:
            consultation = Consultation.objects.get(id=consultation_id, status=Consultation.Status.ACTIVE)
        except Consultation.DoesNotExist:
            raise NotFound("Consultation not found")

        booking = Booking.objects.filter(consultation=consultation, student=request.user).first()
        if not booking:
            return self.format_error(request, 404, "Not Found", "You are not registered for this consultation.")

        booking.delete()

        consultation.open_registration_if_needed()

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
    permission_classes = [IsAuthenticated, IsActive]
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
        try:
            consultation_request = ConsultationRequest.objects.get(id=request_id)
        except ConsultationRequest.DoesNotExist:
            raise NotFound("Consultation request not found")

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
        try:
            consultation_request = ConsultationRequest.objects.get(id=request_id)
        except ConsultationRequest.DoesNotExist:
            raise NotFound("Consultation request not found")

        subscription = ConsultationRequestSubscription.objects.filter(
            request=consultation_request,
            student=request.user
        ).first()

        if not subscription:
            return self.format_error(request, 400, "Bad Request", "You are not subscribed to this request.")

        subscription.delete()
        return Response(status=204)

class ConsultationRequestSubscribedListView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudent]
    pagination_class = DefaultPagination

    @swagger_auto_schema(
        tags=["Consultations"],
        operation_summary="Список запросов, на которые подписан студент",
        manual_parameters=[
            openapi.Parameter('page', openapi.IN_QUERY, description="Номер страницы", type=openapi.TYPE_INTEGER, default=1),
            openapi.Parameter('page_size', openapi.IN_QUERY, description="Количество элементов на странице", type=openapi.TYPE_INTEGER, default=10),
        ],
        responses={
            200: openapi.Response(description="Список подписанных преподавателей", schema=ConsultationRequestResponseSerializer(many=True)),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def get(self, request):
        subscriptions = ConsultationRequestSubscription.objects.filter(
            student=request.user
        ).select_related('request', 'request__creator')
        requests = [sub.request for sub in subscriptions]

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(requests, request)
        serializer = ConsultationRequestResponseSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class ConsultationFromRequestView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsTeacher, IsActive]

    @swagger_auto_schema(
        tags=["Consultations"],
        operation_summary="Создание консультации на основе запроса студентов",
        request_body=ConsultationFromRequestCreateSerializer,
        responses={
            201: openapi.Response(description="Консультация успешно создана", schema=ConsultationResponseSerializer),
            400: openapi.Response(description="Некорректные данные", schema=ErrorResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Запрос не найден", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def post(self, request, request_id):
        try:
            consultation_request = ConsultationRequest.objects.get(id=request_id)
        except ConsultationRequest.DoesNotExist:
            raise NotFound("Consultation request not found")

        if consultation_request.status != ConsultationRequest.Status.OPEN:
            return self.format_error(
                request, 400, "Bad Request", "This request is already closed or accepted."
            )

        serializer = ConsultationFromRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        title = serializer.validated_data.get("title") or consultation_request.title

        consultation = Consultation.objects.create(
            teacher=request.user,
            title=title,
            date=serializer.validated_data["date"],
            start_time=serializer.validated_data["start_time"],
            end_time=serializer.validated_data["end_time"],
            max_students=serializer.validated_data["max_students"],
        )

        consultation_request.status = ConsultationRequest.Status.ACCEPTED
        consultation_request.save(update_fields=["status"])

        subscribed_students = consultation_request.subscriptions.select_related("student")
        for sub in subscribed_students:
            student = sub.student
            if not Booking.objects.filter(consultation=consultation, student=student).exists():
                Booking.objects.create(
                    consultation=consultation,
                    student=student,
                    message="Автоматическая запись по подписке на запрос"
                )

                Notification.objects.create(
                    user=student,
                    title=f"Вы записаны на консультацию «{consultation.title}»",
                    message=(
                        f"Вы были автоматически записаны на консультацию «{consultation.title}» преподавателя "
                        f"{consultation.teacher.get_full_name()} "
                        f"по вашему запросу «{consultation_request.title}»."
                    ),
                    type=Notification.Type.TELEGRAM,
                )

        if consultation.bookings.count() >= consultation.max_students:
            consultation.close_registration(by_teacher=False)

        teacher_subscribers = request.user.subscribers.select_related("student")
        for sub in teacher_subscribers:
            student = sub.student
            if student not in [s.student for s in subscribed_students]:
                Notification.objects.create(
                    user=student,
                    title="Новое время консультации",
                    message=f"Преподаватель {request.user.get_full_name()} опубликовал(-a) консультацию «{consultation.title}».",
                    type=Notification.Type.TELEGRAM,
                )

        return Response(ConsultationResponseSerializer(consultation).data, status=201)
