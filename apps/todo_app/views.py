import logging

from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Q

from apps.auth_app.permissions import IsActive, IsTeacherOrDean
from apps.notification_app.models import Notification
from apps.todo_app.serializers import ToDoRequestSerializer, ToDoResponseSerializer, PaginatedToDosSerializer, \
    ToDoListResponseSerializer
from apps.todo_app.services import (
    GoogleCalendarService
)
from apps.todo_app.utils import (sync_and_handle_event)
from core.mixins import ErrorResponseMixin
from core.serializers import ErrorResponseSerializer
from core.pagination import DefaultPagination
from apps.todo_app.models import ToDo

logger = logging.getLogger(__name__)


class ToDoCreateView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsActive, IsTeacherOrDean]

    @swagger_auto_schema(
        tags=["To Do"],
        operation_summary="Создание новой задачи",
        operation_description="Поле `reminders` используется для указания напоминаний создателя задачи и является "
                              "необязательным.\n\nЕсли не указано, будут использованы значения "
                              "по умолчанию в зависимости от роли пользователя.",
        request_body=ToDoRequestSerializer,
        responses={
            201: openapi.Response(description="Задача создана", schema=ToDoResponseSerializer),
            400: openapi.Response(description="Некорректные данные", schema=ErrorResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def post(self, request):
        serializer = ToDoRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        todo = serializer.save()

        calendar_service = GoogleCalendarService(user=request.user)
        sync_and_handle_event(todo, calendar_service, todo.reminders, target_user=request.user, for_creator=True)

        if todo.assignee and todo.assignee != request.user:
            assignee_calendar_service = GoogleCalendarService(user=todo.assignee)
            sync_and_handle_event(todo, assignee_calendar_service, todo.assignee_reminders, todo.assignee)

            Notification.objects.create(
                user=todo.assignee,
                title="Новая задача",
                message=f'Вам назначена задача: "{todo.title}".\n\nЧтобы просмотреть детали, перейдите в раздел "📝 Мои '
                        f'задачи".',
                type=Notification.Type.TELEGRAM,
            )

        return Response(ToDoResponseSerializer(todo).data, status=201)


class ToDoListView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsActive, IsTeacherOrDean]
    pagination_class = DefaultPagination

    @swagger_auto_schema(
        tags=["To Do"],
        operation_summary="Список задач",
        manual_parameters=[
            openapi.Parameter('page', openapi.IN_QUERY, description="Номер страницы",
                              type=openapi.TYPE_INTEGER, default=1),
            openapi.Parameter('page_size', openapi.IN_QUERY, description="Количество элементов на странице",
                              type=openapi.TYPE_INTEGER, default=10),
            openapi.Parameter('status', openapi.IN_QUERY, description="Фильтр по статусу задачи (in progress / done)",
                              type=openapi.TYPE_STRING),
        ],
        responses={
            200: openapi.Response(description="Список задач", schema=PaginatedToDosSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def get(self, request):
        user = request.user

        todos = ToDo.objects.filter(Q(creator=user) | Q(assignee=user)).order_by('-created_at')

        status_param = request.query_params.get('status')
        if status_param:
            todos = todos.filter(status=status_param)

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(todos, request)
        serializer = ToDoListResponseSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)
