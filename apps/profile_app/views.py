from django.contrib.auth import get_user_model
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.auth_app.models import TeacherApproval
from apps.auth_app.permissions import IsTeacher
from apps.profile_app.serializers import (
    UpdateProfileRequestSerializer,
    ProfileResponseSerializer, ResubmitTeacherApprovalResponseSerializer,
)
from core.mixins import ErrorResponseMixin
from core.serializers import ErrorResponseSerializer

User = get_user_model()


class ProfileView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        tags=['Profile'],
        operation_summary="Получение профиля текущего пользователя",
        operation_description="Возвращает информацию о текущем пользователе, включая роль и статус",
        responses={
            200: openapi.Response(description="Данные пользователя успешно получены", schema=ProfileResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        }
    )
    def get(self, request):
        user = request.user
        data = {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "phone_number": user.phone_number,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "role": user.role,
            "status": user.status,
        }
        return Response(data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        tags=['Profile'],
        operation_summary="Редактирование профиля пользователя",
        operation_description="Позволяет обновить только `first_name` и `last_name` текущего пользователя.",
        request_body=UpdateProfileRequestSerializer,
        responses={
            200: openapi.Response(description="Профиль успешно обновлён", schema=ProfileResponseSerializer),
            400: openapi.Response(description="Ошибка валидации данных", schema=ErrorResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def put(self, request):
        user = request.user
        serializer = UpdateProfileRequestSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        data = {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "phone_number": user.phone_number,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "role": user.role,
            "status": user.status,
        }
        return Response(data, status=status.HTTP_200_OK)



class ResubmitTeacherApprovalView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    @swagger_auto_schema(
        tags=['Profile'],
        operation_summary="Повторная отправка заявки на подтверждение преподавателя",
        operation_description=(
            "Позволяет преподавателю повторно отправить заявку на подтверждение, "
            "если его предыдущая заявка была отклонена. "
        ),
        responses={
            201: openapi.Response(description="Заявка успешно повторно отправлена", schema=ResubmitTeacherApprovalResponseSerializer),
            400: openapi.Response(description="Невозможно повторно отправить заявку", schema=ErrorResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        }
    )
    def post(self, request):
        user = request.user

        last_approval = TeacherApproval.objects.filter(user=user).order_by("-created_at").first()
        if not last_approval or last_approval.status != TeacherApproval.Status.REJECTED:
            return self.format_error(request, 400, "Bad Request", "You can resubmit your approval request only after the previous one has been rejected.")

        new_approval = TeacherApproval.objects.create(user=user)

        return Response(
            ResubmitTeacherApprovalResponseSerializer({
                "message": "The approval request has been resubmitted and is awaiting confirmation from the administrator.",
                "approval_id": new_approval.id,
            }).data,
            status=201
        )