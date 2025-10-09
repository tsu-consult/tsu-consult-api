from django.contrib.auth import get_user_model
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.auth_app.permissions import IsStudent
from apps.teacher_app.serializers import TeacherResponseSerializer, PaginatedTeachersSerializer
from core.mixins import ErrorResponseMixin
from core.pagination import DefaultPagination
from core.serializers import ErrorResponseSerializer

User = get_user_model()


class TeacherListView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudent]
    pagination_class = DefaultPagination

    @swagger_auto_schema(
        tags=['Student'],
        operation_summary="Список подтверждённых преподавателей",
        manual_parameters=[
            openapi.Parameter('page', openapi.IN_QUERY, description="Номер страницы", type=openapi.TYPE_INTEGER, default=1),
            openapi.Parameter('page_size', openapi.IN_QUERY, description="Количество элементов на странице", type=openapi.TYPE_INTEGER, default=10),
        ],
        responses={
            200: openapi.Response(description="Список преподавателей", schema=PaginatedTeachersSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def get(self, request):
        teachers = User.objects.filter(
            role=User.Role.TEACHER,
            teacher_approval__status="approved"
        ).order_by("last_name", "first_name")

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(teachers, request)
        serializer = TeacherResponseSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)
