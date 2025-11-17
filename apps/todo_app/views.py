import logging

from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.auth_app.permissions import IsActive, IsTeacherOrDean
from apps.notification_app.models import Notification
from apps.todo_app.serializers import ToDoRequestSerializer, ToDoResponseSerializer
from apps.todo_app.services import (
    GoogleCalendarService
)
from apps.todo_app.utils import (sync_and_handle_event, get_user_reminders)
from core.mixins import ErrorResponseMixin
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

        reminders = serializer.validated_data.get('reminders', None)
        initial = serializer.initial_data or {}

        todo = serializer.save()

        reminders = get_user_reminders(request.user, initial, reminders)
        assignee_reminders = get_user_reminders(todo.assignee, initial)

        todo.reminders = reminders
        todo.save(update_fields=["reminders"])

        calendar_service = GoogleCalendarService(user=request.user)

        sync_and_handle_event(todo, calendar_service, reminders, target_user=request.user, for_creator=True)

        if todo.assignee and todo.assignee != request.user:
            assignee_calendar_service = GoogleCalendarService(user=todo.assignee)
            sync_and_handle_event(todo, assignee_calendar_service, assignee_reminders,
                                  target_user=todo.assignee)

            Notification.objects.create(
                user=todo.assignee,
                title="Новая задача",
                message=f'Вам назначена задача: "{todo.title}".\n\nЧтобы просмотреть детали, перейдите в раздел "📝 Мои '
                        f'задачи".',
                type=Notification.Type.TELEGRAM,
            )

        return Response(ToDoResponseSerializer(todo).data, status=201)
