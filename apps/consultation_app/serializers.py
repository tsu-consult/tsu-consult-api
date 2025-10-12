from rest_framework import serializers

from apps.consultation_app.models import Consultation, Booking, ConsultationRequest


class ConsultationResponseSerializer(serializers.ModelSerializer):
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
    results = ConsultationResponseSerializer(many=True)


class ConsultationRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConsultationRequest
        fields = ["id", "title", "description", "status", "created_at", "updated_at"]
        read_only_fields = ["id", "status", "created_at", "updated_at"]

    def create(self, validated_data):
        user = self.context["request"].user
        return ConsultationRequest.objects.create(creator=user, **validated_data)

class ConsultationRequestStudentSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    first_name = serializers.CharField(allow_blank=True)
    last_name = serializers.CharField(allow_blank=True)

class ConsultationRequestResponseSerializer(serializers.ModelSerializer):
    student = ConsultationRequestStudentSerializer(source="creator", read_only=True)

    class Meta:
        model = ConsultationRequest
        fields = ["id", "title", "description", "status", "student", "created_at", "updated_at"]
        read_only_fields = ["id", "status", "student", "created_at", "updated_at"]

    def create(self, validated_data):
        user = self.context["request"].user
        return ConsultationRequest.objects.create(creator=user, **validated_data)


class BookingRequestSerializer(serializers.Serializer):
    message = serializers.CharField(required=True, allow_blank=False)

class BookingResponseSerializer(serializers.ModelSerializer):
    consultation_title = serializers.CharField(source="consultation.title", read_only=True)
    teacher_name = serializers.CharField(source="consultation.teacher.get_full_name", read_only=True)

    class Meta:
        model = Booking
        fields = [
            "id", "consultation_id", "consultation_title",
            "teacher_name", "message", "created_at"
        ]