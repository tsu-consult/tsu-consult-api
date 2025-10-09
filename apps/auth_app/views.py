from django.contrib.auth import get_user_model
from django.db import IntegrityError
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from core.mixins import ErrorResponseMixin
from core.serializers import ErrorResponseSerializer
from .serializers import (
    RegisterRequestSerializer, RegisterResponseSerializer,
    LoginRequestSerializer, LoginResponseSerializer,
    RefreshRequestSerializer, RefreshResponseSerializer,
    LogoutRequestSerializer
)

User = get_user_model()

class RegisterView(ErrorResponseMixin, APIView):
    permission_classes = [AllowAny]
    @swagger_auto_schema(
        tags=['Auth'],
        operation_summary="Регистрация пользователя (Telegram или админ)",
        request_body=RegisterRequestSerializer,
        responses={
            200: openapi.Response(description="Успешная регистрация", schema=RegisterResponseSerializer),
            400: openapi.Response(description="Некорректные данные или пользователь уже существует", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def post(self, request):
        serializer = RegisterRequestSerializer(data=request.data)
        try:
            if serializer.is_valid(raise_exception=True):
                user = serializer.save()
                refresh = RefreshToken.for_user(user)
                return Response({
                    'access': str(refresh.access_token),
                    'refresh': str(refresh)
                }, 200)
        except IntegrityError:
            return ErrorResponseMixin.format_error(
                request,
                400,
                "Bad Request",
                "User with this email or telegram_id already exists."
            )


class LoginView(ErrorResponseMixin, APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        tags=['Auth'],
        operation_summary="Авторизация (email/password или telegram_id)",
        request_body=LoginRequestSerializer,
        responses={
            200: openapi.Response(description="Успешная авторизация", schema=LoginResponseSerializer),
            400: openapi.Response(description="Неверный email или пароль", schema=ErrorResponseSerializer),
            401: openapi.Response(description="Неверные учетные данные", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Пользователь не найден", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def post(self, request):
        serializer = LoginRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return ErrorResponseMixin.format_error(
                request,
                400,
                "Bad Request",
                serializer.errors
            )

        email = serializer.validated_data.get('email')
        password = serializer.validated_data.get('password')
        telegram_id = serializer.validated_data.get('telegram_id')

        if telegram_id:
            user = User.objects.filter(telegram_id=telegram_id).first()
            if not user:
                return ErrorResponseMixin.format_error(request, 404, "Not Found", "User not found")
            refresh = RefreshToken.for_user(user)
            return Response({
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            })

        if email and password:
            try:
                user = User.objects.get(email=email)
            except User.DoesNotExist:
                return ErrorResponseMixin.format_error(request, 404, "Not Found", "User not found")

            if not user.check_password(password):
                return ErrorResponseMixin.format_error(request, 401, "Unauthorized", "Invalid email or password")

            refresh = RefreshToken.for_user(user)
            return Response({
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            }, 200)

        return ErrorResponseMixin.format_error(request, 400, "Bad Request", "Invalid login data")


class RefreshView(ErrorResponseMixin, APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        tags=['Auth'],
        operation_summary="Обновление access-токена",
        request_body=RefreshRequestSerializer,
        responses={
            200: openapi.Response(description="Успешное обновление токена", schema=RefreshResponseSerializer),
            400: openapi.Response(description="Недействительный или просроченный refresh-токен", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Пользователь не найден", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def post(self, request):
        serializer = RefreshRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return ErrorResponseMixin.format_error(request, 400, "Bad Request", serializer.errors)

        refresh_token = serializer.validated_data["refresh"]

        try:
            refresh = RefreshToken(refresh_token)
            user_id = refresh.get("user_id")

            try:
                User.objects.get(id=user_id)
            except User.DoesNotExist:
                return ErrorResponseMixin.format_error(request, 404,
                                                       "Not Found", f"User with id={user_id} not found")

            access_token = str(refresh.access_token)
            return Response({"access": access_token}, 200)

        except TokenError:
            return ErrorResponseMixin.format_error(request, 400,
                                                   "Bad Request","Invalid or expired refresh token")


class LogoutView(ErrorResponseMixin, APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        tags=['Auth'],
        operation_summary="Выход (блокировка refresh-токена)",
        request_body=LogoutRequestSerializer,
        responses={
            200: openapi.Response(description="Успешный выход"),
            400: openapi.Response(description="Недействительный или уже отозванный refresh-токен", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def post(self, request):
        serializer = LogoutRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return ErrorResponseMixin.format_error(request, 400, "Bad Request", serializer.errors)

        refresh_token = serializer.validated_data["refresh"]

        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
            return Response(status=200)
        except TokenError:
            return ErrorResponseMixin.format_error(request, 400,
                                                   "Bad Request", "Invalid or expired refresh token")
