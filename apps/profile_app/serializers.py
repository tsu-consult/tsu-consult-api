from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.auth_app.validators import validate_human_name
from apps.auth_app.serializers import password_validator

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


class ChangeEmailRequestSerializer(serializers.Serializer):
    new_email = serializers.EmailField(required=True)

    def validate_new_email(self, value):
        user = self.context.get('user')
        if User.objects.filter(email=value).exclude(id=user.id).exists():
            raise serializers.ValidationError("This email is already in use by another user.")
        return value


class ChangePasswordRequestSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})
    new_password = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'},
        validators=[password_validator]
    )

    def validate_current_password(self, value):
        user = self.context.get('user')
        if not user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
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
