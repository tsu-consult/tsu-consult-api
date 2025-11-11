import os
from django.core.cache import cache
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from google_auth_oauthlib.flow import Flow
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from config import settings
from core.mixins import ErrorResponseMixin
from core.serializers import ErrorResponseSerializer
from .models import GoogleToken
from .serializers import GoogleCalendarInitResponseSerializer, GoogleCalendarRedirectResponseSerializer
from ..auth_app.permissions import IsActive

if settings.DEBUG:
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'


def _get_redirect_uri():
    base_uri = settings.SWAGGER_SETTINGS.get("DEFAULT_API_URL")
    if not base_uri.endswith('/'):
        base_uri += '/'
    return f"{base_uri}profile/calendar/redirect/"


def _get_client_config():
    return {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "project_id": settings.GOOGLE_PROJECT_ID,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://accounts.google.com/o/oauth2/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
        }
    }


class GoogleCalendarInitView(ErrorResponseMixin, APIView):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated, IsActive]

    @swagger_auto_schema(
        tags=['Profile'],
        operation_summary="Инициализация интеграции с Google Calendar",
        operation_description="Возвращает URL для авторизации пользователя в Google и предоставления доступа к "
                              "календарю.",
        responses={
            200: openapi.Response(description="URL для авторизации", schema=GoogleCalendarInitResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        }
    )
    def get(self, request):
        redirect_uri = _get_redirect_uri()
        flow = Flow.from_client_config(
            _get_client_config(),
            scopes=['https://www.googleapis.com/auth/calendar'],
            redirect_uri=redirect_uri
        )
        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true'
        )

        cache.set(f"state:{state}", request.user.id, timeout=600)
        return Response({'authorization_url': authorization_url})


class GoogleCalendarRedirectView(APIView):

    @swagger_auto_schema(
        tags=['Profile'],
        operation_summary="Обработка редиректа от Google Calendar",
        operation_description="Сохраняет токен доступа после успешной авторизации пользователя в Google. Этот "
                              "эндпоинт используется только для редиректа.",
        responses={
            200: openapi.Response(description="Успешная авторизация", schema=GoogleCalendarRedirectResponseSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        }
    )
    def get(self, request):
        state = request.query_params.get('state')
        user_id = cache.get(f"state:{state}")
        if not user_id:
            return Response({'error': 'Invalid or expired state'}, status=400)

        redirect_uri = _get_redirect_uri()
        flow = Flow.from_client_config(
            _get_client_config(),
            scopes=['https://www.googleapis.com/auth/calendar'],
            state=state,
            redirect_uri=redirect_uri
        )

        authorization_response = request.build_absolute_uri()
        flow.fetch_token(authorization_response=authorization_response)

        credentials = flow.credentials
        GoogleToken.objects.update_or_create(
            user_id=user_id,
            defaults={'credentials': credentials.to_json()}
        )
        return Response({'status': 'Ok'})
