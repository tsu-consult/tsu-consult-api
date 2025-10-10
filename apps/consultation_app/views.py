from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.auth_app.permissions import IsStudent
from apps.consultation_app.models import Consultation
from apps.consultation_app.serializers import PaginatedConsultationsSerializer, ConsultationSerializer
from core.pagination import DefaultPagination
from core.serializers import ErrorResponseSerializer
from core.mixins import ErrorResponseMixin


class ConsultationsView(ErrorResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudent]
    pagination_class = DefaultPagination

    @swagger_auto_schema(
        tags=["Student"],
        operation_summary="Просмотр расписания выбранного преподавателя",
        manual_parameters=[
            openapi.Parameter("page", openapi.IN_QUERY, description="Номер страницы", type=openapi.TYPE_INTEGER, default=1,
            ),
            openapi.Parameter("page_size", openapi.IN_QUERY, description="Количество элементов на странице", type=openapi.TYPE_INTEGER, default=10),
        ],
        responses={
            200: openapi.Response(description="Список консультаций преподавателя", schema=PaginatedConsultationsSerializer),
            401: openapi.Response(description="Неавторизован", schema=ErrorResponseSerializer),
            403: openapi.Response(description="Нет доступа", schema=ErrorResponseSerializer),
            404: openapi.Response(description="Преподаватель не найден", schema=ErrorResponseSerializer),
            500: openapi.Response(description="Внутренняя ошибка сервера", schema=ErrorResponseSerializer),
        },
    )
    def get(self, request, teacher_id):
        consultations = Consultation.objects.filter(
            teacher_id=teacher_id,
            status=Consultation.Status.ACTIVE,
        ).order_by("date", "start_time")

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(consultations, request)
        serializer = ConsultationSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)
