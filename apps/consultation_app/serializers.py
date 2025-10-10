from rest_framework import serializers

from apps.consultation_app.models import Consultation


class ConsultationSerializer(serializers.ModelSerializer):
    teacher_name = serializers.CharField(source="teacher.get_full_name", read_only=True)

    class Meta:
        model = Consultation
        fields = [
            "id",
            "title",
            "date",
            "start_time",
            "end_time",
            "max_students",
            "is_closed",
            "status",
            "teacher_id",
            "teacher_name",
            "created_at",
            "updated_at",
        ]

class PaginatedConsultationsSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    total_pages = serializers.IntegerField()
    current_page = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = ConsultationSerializer(many=True)
