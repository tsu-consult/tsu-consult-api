from rest_framework import serializers

from apps.todos_app.models import ToDo
from apps.auth_app.models import User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "phone_number"]


class ToDoCreateSerializer(serializers.ModelSerializer):
    assignee = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(role='teacher'),
        allow_null=True,
        required=False
    )

    class Meta:
        model = ToDo
        fields = ["title", "description", "deadline", "assignee"]

    def validate(self, attrs):
        user = self.context["request"].user
        if getattr(user, 'role', None) not in ('teacher', 'dean'):
            raise serializers.ValidationError('Only teachers or the dean can create assignments.')

        assignee = attrs.get('assignee')
        if not assignee and user.role != 'dean':
            raise serializers.ValidationError('Only the dean can create a task without an assignee.')

        if assignee and user.role != 'dean' and getattr(assignee, 'id', None) != user.id:
            raise serializers.ValidationError('Only the dean can assign tasks to other users.')
        return attrs

    def create(self, validated_data):
        user = self.context["request"].user
        return ToDo.objects.create(creator=user, **validated_data)


class ToDoResponseSerializer(serializers.ModelSerializer):
    creator = UserSerializer(source="creator", read_only=True)
    assignee = UserSerializer(source="assignee", read_only=True)

    class Meta:
        model = ToDo
        fields = ["id", "title", "description", "deadline", "status", "creator", "assignee", "created_at", "updated_at"]
