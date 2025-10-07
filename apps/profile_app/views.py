from django.contrib.auth import get_user_model
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.profile_app.serializers import UpdateProfileRequestSerializer
from apps.profile_app.serializers import ProfileResponseSerializer
from core.mixins import ErrorResponseMixin
from core.serializers import ErrorResponseSerializer

User = get_user_model()


class ProfileView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        tags=['profile'],
        operation_summary="Получение данных текущего пользователя",
        operation_description="Возвращает профиль авторизованного пользователя",
        responses={
            200: openapi.Response(
                description="Данные пользователя успешно получены",
                schema=ProfileResponseSerializer
            ),
            401: openapi.Response(
                description="Неавторизован",
                schema=ErrorResponseSerializer
            ),
            500: openapi.Response(
                description="Внутренняя ошибка сервера",
                schema=ErrorResponseSerializer
            ),
        }
    )
    def get(self, request):
        user = request.user
        data = {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
        }
        return Response(data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        tags=['profile'],
        operation_summary="Редактирование профиля пользователя",
        operation_description="Позволяет обновить username, email, first_name, last_name текущего пользователя",
        request_body=UpdateProfileRequestSerializer,
        responses={
            200: openapi.Response(
                description="Профиль успешно обновлён",
                schema=ProfileResponseSerializer
            ),
            400: openapi.Response(
                description="Ошибка валидации данных",
                schema=ErrorResponseSerializer
            ),
            401: openapi.Response(
                description="Неавторизован",
                schema=ErrorResponseSerializer
            ),
            500: openapi.Response(
                description="Внутренняя ошибка сервера",
                schema=ErrorResponseSerializer
            ),
        }
    )
    def put(self, request):
        user = request.user
        serializer = UpdateProfileRequestSerializer(user, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()

        data = {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
        }
        return Response(data, status=status.HTTP_200_OK)
