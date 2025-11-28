import logging

from django.db.models import Q
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.auth_app.permissions import IsActive, IsTeacherOrDean
from apps.todo_app.calendar.managers import sync_calendars
from apps.todo_app.models import ToDo
from apps.todo_app.serializers import ToDoRequestSerializer, ToDoResponseSerializer, PaginatedToDosSerializer, \
    ToDoListResponseSerializer
from apps.todo_app.utils import _get_todo
from core.mixins import ErrorResponseMixin
from core.pagination import DefaultPagination
from core.serializers import ErrorResponseSerializer

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

        sync_calendars(todo, request.user, None, True)

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


class ToDoDetailView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsActive, IsTeacherOrDean]

    @swagger_auto_schema(
        tags=["To Do"],
        operation_summary="Детали задачи",
        operation_description="Возвращает задачу по её id. Доступны только создатель или назначенный преподаватель.",
        responses={
            200: openapi.Response(description="Детали задачи", schema=ToDoResponseSerializer),
            400: openapi.Response(description="Некорректные данные", schema=ErrorResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Не найдено", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def get(self, request, todo_id):
        todo, err = _get_todo(request, todo_id)
        if err:
            return err

        if not todo.is_accessible_by(request.user):
            return self.format_error(request, 403, "Forbidden",
                                     "You do not have permission to perform this action.")

        return Response(ToDoResponseSerializer(todo).data, status=200)

    @swagger_auto_schema(
        tags=["To Do"],
        operation_summary="Обновление задачи",
        operation_description="Редактирование задачи доступно только создателю или назначенному преподавателю.",
        request_body=ToDoRequestSerializer,
        responses={
            200: openapi.Response(description="Задача обновлена", schema=ToDoResponseSerializer),
            400: openapi.Response(description="Некорректные данные", schema=ErrorResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Не найдено", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def put(self, request, todo_id):
        todo, err = _get_todo(request, todo_id)
        if err:
            return err

        if not todo.is_accessible_by(request.user):
            return self.format_error(request, 403, "Forbidden",
                                     "You do not have permission to perform this action.")

        serializer = ToDoRequestSerializer(instance=todo, data=request.data, context={"request": request}, partial=True)
        serializer.is_valid(raise_exception=True)

        old_assignee = todo.assignee
        todo = serializer.save()

        sync_calendars(todo, request.user, old_assignee)

        return Response(ToDoResponseSerializer(todo).data, status=200)
