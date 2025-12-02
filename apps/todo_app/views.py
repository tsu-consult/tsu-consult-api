import logging

from django.db.models import Q
from django.utils import timezone
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.auth_app.permissions import IsActive, IsTeacherOrDean
from apps.todo_app.calendar.managers import sync_calendars
from apps.todo_app.calendar.services import GoogleCalendarService
from apps.todo_app.models import ToDo
from apps.todo_app.serializers import ToDoRequestSerializer, ToDoResponseSerializer, PaginatedToDosSerializer, \
    ToDoListResponseSerializer
from apps.todo_app.services import ToDoUpdateService
from apps.todo_app.utils import get_todo, cancel_pending_notifications_for_user
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

        todos = ToDo.objects.filter(
            Q(creator=user) | Q(assignee=user),
            deleted_at__isnull=True
        ).order_by('-created_at')

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

        if todo.is_deleted():
            return self.format_error(request, 404, "Not Found",
                                     "Task has been deleted.")

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

        if todo.is_deleted():
            return self.format_error(request, 404, "Not Found",
                                     "Task has been deleted.")

        if not todo.is_accessible_by(request.user):
            return self.format_error(request, 403, "Forbidden",
                                     "You do not have permission to perform this action.")

        update_service = ToDoUpdateService(todo, request.user)
        update_service.save_old_state()

        serializer = ToDoRequestSerializer(instance=todo, data=request.data, context={"request": request}, partial=True)
        serializer.is_valid(raise_exception=True)
        todo = serializer.save()

        raw_request = getattr(request, 'data', {}) or {}
        reminders_in_request = 'reminders' in raw_request
        reminders_value = raw_request.get('reminders') if reminders_in_request else None

        update_service.restore_reminders_if_needed(reminders_in_request)
        update_service.handle_deadline_removed()
        update_service.handle_deadline_changed()
        update_service.handle_reminders_update(reminders_value)
        update_service.sync_calendars()

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

    @swagger_auto_schema(
        tags=["To Do"],
        operation_summary="Удаление задачи",
        operation_description="Удаление задачи доступно только создателю. Задача помечается как удалённая (tombstone), "
                              "все pending уведомления для создателя и назначенного преподавателя отменяются.",
        responses={
            204: openapi.Response(description="Задача успешно удалена"),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Не найдено", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def delete(self, request, todo_id):
        todo, err = get_todo(request, todo_id)
        if err:
            return err

        if todo.creator_id != request.user.id:
            return self.format_error(request, 403, "Forbidden",
                                     "Only the creator can delete this task.")

        if todo.is_deleted():
            return self.format_error(request, 404, "Not Found",
                                     "Task has already been deleted.")

        if todo.creator:
            cancel_pending_notifications_for_user(todo, todo.creator, reason='Task deleted', only_deadline=False)

        if todo.assignee:
            cancel_pending_notifications_for_user(todo, todo.assignee, reason='Task deleted', only_deadline=False)

        if todo.creator and (todo.calendar_event_id or todo.calendar_event_active):
            try:
                creator_service = GoogleCalendarService(todo.creator)
                if creator_service.service:
                    creator_service.delete_event(todo)
            except Exception as exc:
                logger.exception("Failed to delete calendar event for creator during task deletion "
                                 "todo id=%s: %s", todo.id, exc)

        if todo.assignee and (todo.assignee_calendar_event_id or todo.assignee_calendar_event_active):
            try:
                assignee_service = GoogleCalendarService(todo.assignee)
                if assignee_service.service:
                    assignee_service.delete_event(todo)
            except Exception as exc:
                logger.exception("Failed to delete calendar event for assignee during task deletion "
                                 "todo id=%s: %s", todo.id, exc)

        todo.deleted_at = timezone.now()
        todo.save(update_fields=['deleted_at'])

        return Response(status=204)
