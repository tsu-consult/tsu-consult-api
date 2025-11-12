from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.auth_app.permissions import IsActive, IsTeacherOrDean
from apps.notification_app.models import Notification
from apps.todo_app.serializers import ToDoRequestSerializer, ToDoResponseSerializer
from apps.todo_app.services import GoogleCalendarService
from core.mixins import ErrorResponseMixin
from core.serializers import ErrorResponseSerializer


class ToDoCreateView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsActive, IsTeacherOrDean]

    @swagger_auto_schema(
        tags=["To Do"],
        operation_summary="Создание новой задачи",
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
        todo = serializer.save()

        calendar_service = GoogleCalendarService(user=request.user)
        todo.sync_calendar_event(calendar_service, reminders=reminders)

        if todo.assignee and todo.assignee_id != todo.creator_id:
            Notification.objects.create(
                user=todo.assignee,
                title="Новая задача",
                message=f'Вам назначена задача: "{todo.title}".\n\nЧтобы просмотреть детали, перейдите в раздел "📝 Мои '
                        f'задачи".',
                type=Notification.Type.TELEGRAM,
            )

        return Response(ToDoResponseSerializer(todo).data, status=201)
