from django.contrib.auth import get_user_model
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.auth_app.permissions import IsStudent
from apps.teacher_app.models import Subscription
from apps.teacher_app.serializers import TeacherResponseSerializer, PaginatedTeachersSerializer
from core.mixins import ErrorResponseMixin
from core.pagination import DefaultPagination
from core.serializers import ErrorResponseSerializer

User = get_user_model()


class TeacherListView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudent]
    pagination_class = DefaultPagination

    @swagger_auto_schema(
        tags=['Teachers'],
        operation_summary="Список подтверждённых преподавателей",
        manual_parameters=[
            openapi.Parameter('page', openapi.IN_QUERY, description="Номер страницы", type=openapi.TYPE_INTEGER, default=1),
            openapi.Parameter('page_size', openapi.IN_QUERY, description="Количество элементов на странице", type=openapi.TYPE_INTEGER, default=10),
        ],
        responses={
            200: openapi.Response(description="Список преподавателей", schema=PaginatedTeachersSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def get(self, request):
        teachers = User.objects.filter(
            role=User.Role.TEACHER,
            teacher_approvals__status="approved"
        ).order_by("last_name", "first_name")

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(teachers, request)
        serializer = TeacherResponseSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class TeacherSubscribeView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudent]

    @swagger_auto_schema(
        tags=["Teachers"],
        operation_summary="Подписка на преподавателя",
        responses={
            201: openapi.Response(description="Подписка оформлена успешно"),
            400: openapi.Response(description="Подписка уже существует", schema=ErrorResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Преподаватель не найден", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def post(self, request, teacher_id):
        try:
            teacher = User.objects.get(
                id=teacher_id,
                role=User.Role.TEACHER,
                teacher_approvals__status="approved",
            )
        except User.DoesNotExist:
            raise NotFound("Teacher not found")

        if Subscription.objects.filter(student=request.user, teacher=teacher).exists():
            return self.format_error(
                request,
                400,
                "Bad Request",
                "You are already subscribed to this teacher."
            )

        Subscription.objects.create(student=request.user, teacher=teacher)
        return Response(status=201)


class TeacherUnsubscribeView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudent]

    @swagger_auto_schema(
        tags=["Teachers"],
        operation_summary="Отписка от преподавателя",
        responses={
            204: openapi.Response(description="Подписка успешно удалена"),
            400: openapi.Response(description="Подписка не существует", schema=ErrorResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Преподаватель не найден", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def delete(self, request, teacher_id):
        try:
            teacher = User.objects.get(
                id=teacher_id,
                role=User.Role.TEACHER,
                teacher_approvals__status="approved",
            )
        except User.DoesNotExist:
            raise NotFound("Teacher not found")

        subscription = Subscription.objects.filter(student=request.user, teacher=teacher).first()
        if not subscription:
            return self.format_error(
                request,
                400,
                "Bad Request",
                "You are not subscribed to this teacher."
            )

        subscription.delete()
        return Response(status=204)


class TeacherSubscribedListView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudent]
    pagination_class = DefaultPagination

    @swagger_auto_schema(
        tags=["Teachers"],
        operation_summary="Список преподавателей, на которых подписан студент",
        manual_parameters=[
            openapi.Parameter('page', openapi.IN_QUERY, description="Номер страницы", type=openapi.TYPE_INTEGER, default=1),
            openapi.Parameter('page_size', openapi.IN_QUERY, description="Количество элементов на странице", type=openapi.TYPE_INTEGER, default=10),
        ],
        responses={
            200: openapi.Response(description="Список подписанных преподавателей", schema=PaginatedTeachersSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def get(self, request):
        subscriptions = Subscription.objects.filter(student=request.user).select_related('teacher')
        teachers = [sub.teacher for sub in subscriptions]

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(teachers, request)
        serializer = TeacherResponseSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)
