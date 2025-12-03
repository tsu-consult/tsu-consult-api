from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.auth_app.validators import validate_human_name

User = get_user_model()


class ProfileResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.EmailField()
    phone_number = serializers.CharField(allow_null=True, allow_blank=True)
    first_name = serializers.CharField(allow_blank=True)
    last_name = serializers.CharField(allow_blank=True)
    role = serializers.CharField()
    status = serializers.CharField()


class UpdateProfileRequestSerializer(serializers.ModelSerializer):
    first_name = serializers.CharField(required=False, allow_blank=True)
    last_name = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = User
        fields = ['first_name', 'last_name']

    @staticmethod
    def validate_first_name(value):
        if value:
            try:
                validate_human_name(value, "first_name")
            except ValueError as e:
                raise serializers.ValidationError(str(e))
        return value

    @staticmethod
    def validate_last_name(value):
        if value:
            try:
                validate_human_name(value, "last_name")
            except ValueError as e:
                raise serializers.ValidationError(str(e))
        return value


class ResubmitTeacherApprovalResponseSerializer(serializers.Serializer):
    message = serializers.CharField(read_only=True)
    approval_id = serializers.IntegerField(read_only=True)


class GoogleCalendarInitResponseSerializer(serializers.Serializer):
    authorization_url = serializers.URLField()


class GoogleCalendarRedirectResponseSerializer(serializers.Serializer):
    status = serializers.CharField()


class GoogleCalendarDisconnectResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
