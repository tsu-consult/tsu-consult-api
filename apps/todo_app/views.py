import logging

from celery.exceptions import CeleryError
from django.db import DatabaseError
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
from apps.todo_app.utils import get_todo, cancel_pending_notifications_for_user, has_calendar_integration
from core.mixins import ErrorResponseMixin
from core.pagination import DefaultPagination
from core.serializers import ErrorResponseSerializer

logger = logging.getLogger(__name__)

TODO_UPDATE_RESPONSES = {
    200: openapi.Response(description="Задача обновлена", schema=ToDoResponseSerializer),
    400: openapi.Response(description="Некорректные данные", schema=ErrorResponseSerializer),
    401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
    403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
    404: openapi.Response(description="Не найдено", schema=ErrorResponseSerializer),
    500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
}


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
        todo, err = get_todo(request, todo_id)
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
        responses=TODO_UPDATE_RESPONSES,
    )
    def put(self, request, todo_id):
        todo, err = get_todo(request, todo_id)
        if err:
            return err

        if not todo.is_accessible_by(request.user):
            return self.format_error(request, 403, "Forbidden",
                                     "You do not have permission to perform this action.")

        serializer = ToDoRequestSerializer(instance=todo, data=request.data, context={"request": request}, partial=True)
        serializer.is_valid(raise_exception=True)

        old_assignee = todo.assignee
        old_deadline = getattr(todo, 'deadline', None)
        old_creator_reminders = getattr(todo, 'reminders', None)
        old_assignee_reminders = getattr(todo, 'assignee_reminders', None)

        todo = serializer.save()

        raw_request = (getattr(request, 'data', {}) or {})
        if 'reminders' not in raw_request:
            fields_to_update = []
            if old_creator_reminders is not None and getattr(todo, 'reminders', None) is None:
                todo.reminders = old_creator_reminders
                fields_to_update.append('reminders')
            if old_assignee_reminders is not None and getattr(todo, 'assignee_reminders', None) is None:
                todo.assignee_reminders = old_assignee_reminders
                fields_to_update.append('assignee_reminders')
            if fields_to_update:
                try:
                    todo.save(update_fields=fields_to_update)
                except Exception as exc:
                    logger.exception("Failed to restore reminders fields for todo id=%s: %s",
                                     getattr(todo, 'id', None), exc)

        reminders_in_request = 'reminders' in raw_request
        reminders = reminders_in_request and raw_request.get('reminders') is not None
        reminders_value = raw_request.get('reminders') if reminders_in_request else None

        deadline_changed = (old_deadline is not None and getattr(todo, 'deadline', None) is not None and old_deadline !=
                            getattr(todo, 'deadline', None))

        if deadline_changed:
            potential_users = []
            creator_user = getattr(todo, 'creator', None)
            assignee_user = getattr(todo, 'assignee', None)
            if creator_user:
                potential_users.append(creator_user)
            if assignee_user and getattr(assignee_user, 'id', None) != getattr(creator_user, 'id', None):
                potential_users.append(assignee_user)

            for u in potential_users:
                if has_calendar_integration(u):
                    continue

                try:
                    cancel_pending_notifications_for_user(todo, u, reason='Deadline changed')
                except (DatabaseError, CeleryError, RuntimeError, ValueError) as exc:
                    logger.exception("Failed to cancel pending notifications during deadline change for todo "
                                     "id=%s user=%s: %s", getattr(todo, 'id', None), getattr(u, 'id', None), exc)

        if reminders:
            if isinstance(reminders_value, list) and len(reminders_value) == 0:
                try:
                    cancel_pending_notifications_for_user(todo, request.user, reason='Reminders cleared via PUT')
                except (DatabaseError, CeleryError, RuntimeError, ValueError) as exc:
                    logger.exception("Failed to cancel pending notifications on explicit empty "
                                     "reminders for todo id=%s: %s", getattr(todo, 'id', None), exc)
            else:
                try:
                    cancel_pending_notifications_for_user(todo, request.user, reason='Reminders updated via PUT')
                except (DatabaseError, CeleryError, RuntimeError, ValueError) as exc:
                    logger.exception("Failed to cancel pending notifications on reminders update for todo id=%s: %s",
                                     getattr(todo, 'id', None), exc)

                if not has_calendar_integration(request.user):
                    logger.debug("Actor has no calendar integration; fallback scheduling delegated to "
                                 "sync_calendars for todo id=%s", getattr(todo, 'id', None))

        sync_calendars(todo, request.user, old_assignee)

        return Response(ToDoResponseSerializer(todo).data, status=200)

    @swagger_auto_schema(
        tags=["To Do"],
        operation_summary="Частичное обновление задачи",
        operation_description=("Частичное редактирование задачи (PATCH) доступно только создателю или "
                               "назначенному преподавателю."),
        request_body=ToDoRequestSerializer,
        responses=TODO_UPDATE_RESPONSES,
    )
    def patch(self, request, todo_id):
        return self.put(request, todo_id)
