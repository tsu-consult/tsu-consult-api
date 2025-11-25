import logging

from django.db import DatabaseError
from django.db.models import Q
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from googleapiclient.errors import HttpError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.auth_app.permissions import IsActive, IsTeacherOrDean
from apps.notification_app.models import Notification
from apps.todo_app.models import ToDo
from apps.todo_app.serializers import ToDoRequestSerializer, ToDoResponseSerializer, PaginatedToDosSerializer, \
    ToDoListResponseSerializer
from apps.todo_app.services import (
    GoogleCalendarService
)
from apps.todo_app.utils import (sync_and_handle_event)
from core.exceptions import GoogleCalendarAuthRequired
from core.mixins import ErrorResponseMixin
from core.pagination import DefaultPagination
from core.serializers import ErrorResponseSerializer

logger = logging.getLogger(__name__)


def _get_todo_or_response(request, todo_id):
    try:
        tid = int(todo_id)
    except (ValueError, TypeError):
        return None, ErrorResponseMixin.format_error(request, 400, "Bad Request", f"Invalid todo id: {todo_id}")

    try:
        todo = ToDo.objects.get(id=tid)
    except ToDo.DoesNotExist:
        return None, ErrorResponseMixin.format_error(request, 404, "Not Found", f"ToDo with id={tid} not found.")

    return todo, None


def _sync_calendars_and_notify(todo, actor_user, old_assignee=None, notify_assignee_on_create=False):
    try:
        if getattr(actor_user, 'id', None) == getattr(todo.creator, 'id', None):
            calendar_service = GoogleCalendarService(user=actor_user)
            sync_and_handle_event(todo, calendar_service, todo.reminders, target_user=actor_user, for_creator=True)
        elif getattr(actor_user, 'id', None) == getattr(getattr(todo, 'assignee', None), 'id', None):
            assignee_calendar_service = GoogleCalendarService(user=actor_user)
            sync_and_handle_event(todo, assignee_calendar_service, todo.assignee_reminders,
                                  target_user=actor_user, for_creator=False)
        else:
            calendar_service = GoogleCalendarService(user=actor_user)
            sync_and_handle_event(todo, calendar_service, todo.reminders, target_user=actor_user, for_creator=True)
    except (HttpError, GoogleCalendarAuthRequired, ValueError, TypeError, RuntimeError) as exc:
        logger.exception("Failed to sync calendar after creating/updating todo id=%s: %s",
                         getattr(todo, 'id', None), exc)

    if todo.assignee and getattr(todo.assignee, 'id', None) != getattr(actor_user, 'id', None):
        try:
            assignee_calendar_service = GoogleCalendarService(user=todo.assignee)
            sync_and_handle_event(todo, assignee_calendar_service, todo.assignee_reminders, target_user=todo.assignee)
        except (HttpError, GoogleCalendarAuthRequired, ValueError, TypeError, RuntimeError) as exc:
            logger.exception("Failed to sync calendar for assignee after creating/updating todo id=%s: %s",
                             getattr(todo, 'id', None), exc)

    if (notify_assignee_on_create and todo.assignee and getattr(todo.assignee, 'id', None) !=
            getattr(actor_user, 'id', None)):
        try:
            Notification.objects.create(
                user=todo.assignee,
                title="Новая задача",
                message=f'Вам назначена задача: "{todo.title}".\n\nЧтобы просмотреть детали, перейдите в раздел "📝 '
                        f'Мои задачи".',
                type=Notification.Type.TELEGRAM,
            )
        except DatabaseError as exc:
            logger.exception("Failed to create notification for assignee on create for todo id=%s: %s",
                             getattr(todo, 'id', None), exc)

    if old_assignee is not None:
        new_assignee = getattr(todo, 'assignee', None)
        if new_assignee and (old_assignee is None or getattr(old_assignee, 'id', None) !=
                             getattr(new_assignee, 'id', None)):
            try:
                Notification.objects.create(
                    user=new_assignee,
                    title="Вас назначили на задачу",
                    message=f'Вам назначена задача: "{todo.title}".\n\nЧтобы просмотреть детали, перейдите в раздел '
                            f'"📝 Мои задачи".',
                    type=Notification.Type.TELEGRAM,
                )
            except DatabaseError as exc:
                logger.exception("Failed to create notification for new assignee for todo id=%s: %s",
                                 getattr(todo, 'id', None), exc)


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

        _sync_calendars_and_notify(todo, request.user, old_assignee=None, notify_assignee_on_create=True)

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
        todo, err = _get_todo_or_response(request, todo_id)
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
        todo, err = _get_todo_or_response(request, todo_id)
        if err:
            return err

        if not todo.is_accessible_by(request.user):
            return self.format_error(request, 403, "Forbidden",
                                     "You do not have permission to perform this action.")

        serializer = ToDoRequestSerializer(instance=todo, data=request.data, context={"request": request}, partial=True)
        serializer.is_valid(raise_exception=True)

        old_assignee = todo.assignee
        todo = serializer.save()

        _sync_calendars_and_notify(todo, request.user, old_assignee)

        return Response(ToDoResponseSerializer(todo).data, status=200)
