from rest_framework import serializers

from apps.consultation_app.models import Consultation, Booking, ConsultationRequest


class ConsultationCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Consultation
        fields = ["title", "date", "start_time", "end_time", "max_students"]

    def validate(self, attrs):
        if attrs["start_time"] >= attrs["end_time"]:
            raise serializers.ValidationError("The end time must be later than the start time.")
        return attrs

class ConsultationUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Consultation
        fields = ["title", "date", "start_time", "end_time", "max_students"]

    def validate(self, attrs):
        start_time = attrs.get("start_time", getattr(self.instance, "start_time", None))
        end_time = attrs.get("end_time", getattr(self.instance, "end_time", None))
        if start_time and end_time and start_time >= end_time:
            raise serializers.ValidationError("The end time must be later than the start time.")
        return attrs


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

class StudentSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    first_name = serializers.CharField(allow_blank=True)
    last_name = serializers.CharField(allow_blank=True)

class PaginatedStudentsSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    total_pages = serializers.IntegerField()
    current_page = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = StudentSerializer(many=True)


class ConsultationRequestResponseSerializer(serializers.ModelSerializer):
    student = StudentSerializer(source="creator", read_only=True)

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


class ConsultationFromRequestCreateSerializer(serializers.ModelSerializer):
    title = serializers.CharField(required=False, allow_blank=True)
    max_students = serializers.IntegerField(required=False, default=5)

    class Meta:
        model = Consultation
        fields = ["title", "date", "start_time", "end_time", "max_students"]

    def validate(self, attrs):
        start_time = attrs.get("start_time")
        end_time = attrs.get("end_time")
        if start_time and end_time and start_time >= end_time:
            raise serializers.ValidationError("The end time must be later than the start time.")
        return attrs
