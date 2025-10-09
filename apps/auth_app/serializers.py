from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.core.validators import RegexValidator

from apps.auth_app.models import TeacherApproval
from apps.auth_app.validators import validate_human_name

User = get_user_model()

password_validator = RegexValidator(
    regex=r'^(?=.*[A-Za-z])(?=.*\d)[A-Za-z\d]{8,}$',
    message="Password must be at least 8 characters long, include at least one letter and one number"
)

class RegisterRequestSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(required=False)
    password = serializers.CharField(
        write_only=True,
        validators=[password_validator],
        style={'input_type': 'password'},
        required=False
    )
    telegram_id = serializers.IntegerField(required=False)
    phone_number = serializers.CharField(required=False)
    role = serializers.ChoiceField(choices=User.Role.choices, required=False)

    class Meta:
        model = User
        fields = (
            'username', 'email', 'password',
            'telegram_id', 'phone_number',
            'first_name', 'last_name', 'role'
        )

    def validate(self, attrs):
        role = attrs.get("role", User.Role.STUDENT)

        if role in [User.Role.STUDENT, User.Role.TEACHER]:
            if not attrs.get("telegram_id"):
                raise serializers.ValidationError("Students and teachers require telegram_id.")
            attrs["email"] = attrs.get("email") or None
        elif role == User.Role.ADMIN:
            if not attrs.get("email") or not attrs.get("password"):
                raise serializers.ValidationError("Email and password are required for the administrator.")

        first_name = attrs.get("first_name")
        last_name = attrs.get("last_name")

        if first_name:
            try:
                validate_human_name(first_name, "first_name")
            except ValueError as e:
                raise serializers.ValidationError({"first_name": str(e)})

        if last_name:
            try:
                validate_human_name(last_name, "last_name")
            except ValueError as e:
                raise serializers.ValidationError({"last_name": str(e)})

        return attrs

    def create(self, validated_data):
        role = validated_data.get("role", User.Role.STUDENT)

        if not validated_data.get("email"):
            validated_data["email"] = f"{validated_data['telegram_id']}@telegram.local"

        user = User(**validated_data)

        if role in [User.Role.STUDENT, User.Role.TEACHER]:
            user.set_unusable_password()
            if role == User.Role.TEACHER:
                user.status = User.Status.PENDING
        else:
            user.set_password(validated_data["password"])

        user.save()

        if role == User.Role.TEACHER:
            TeacherApproval.objects.create(user=user)

        return user

class RegisterResponseSerializer(serializers.Serializer):
    access = serializers.CharField()
    refresh = serializers.CharField()


class LoginRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False, allow_blank=True)
    password = serializers.CharField(required=False, allow_blank=True, write_only=True)
    telegram_id = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, data):
        telegram_id = data.get("telegram_id")
        email = data.get("email")
        password = data.get("password")

        if telegram_id:
            return data

        if email and password:
            return data

        raise serializers.ValidationError("Either telegram_id or email and password are required.")

class LoginResponseSerializer(serializers.Serializer):
    access = serializers.CharField()
    refresh = serializers.CharField()


class RefreshRequestSerializer(serializers.Serializer):
    refresh = serializers.CharField()

class RefreshResponseSerializer(serializers.Serializer):
    access = serializers.CharField()


class LogoutRequestSerializer(serializers.Serializer):
    refresh = serializers.CharField()