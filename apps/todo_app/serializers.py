from rest_framework import serializers

from apps.todo_app.models import ToDo
from apps.auth_app.models import User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "phone_number"]


class ToDoRequestSerializer(serializers.ModelSerializer):
    assignee_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(role='teacher'),
        allow_null=True,
        required=False,
        write_only=True,
        source='assignee',
        error_messages={
            'does_not_exist': 'User with id={pk_value} not found.',
            'incorrect_type': 'Invalid type for the assignee field. An integer is expected.'
        }
    )

    class Meta:
        model = ToDo
        fields = ["title", "description", "deadline", "assignee_id"]

    def validate(self, attrs):
        user = self.context["request"].user
        if getattr(user, 'role', None) not in ('teacher', 'dean'):
            raise serializers.ValidationError('Only teachers or the dean can create assignments.')

        assignee = attrs.get('assignee')
        if assignee is not None:
            if getattr(assignee, 'role', None) != 'teacher':
                raise serializers.ValidationError({'assignee_id': 'The performer must be a teacher.'})
            if user.role == 'teacher' and assignee.id != user.id:
                raise serializers.ValidationError({'assignee_id': 'Teachers can only assign tasks to themselves.'})
        return attrs

    def create(self, validated_data):
        user = self.context["request"].user
        if getattr(user, 'role', None) == 'teacher' and not validated_data.get('assignee'):
            validated_data['assignee'] = user
        return ToDo.objects.create(creator=user, **validated_data)


class ToDoResponseSerializer(serializers.ModelSerializer):
    creator = UserSerializer(read_only=True)
    assignee = UserSerializer(read_only=True)

    class Meta:
        model = ToDo
        fields = ["id", "title", "description", "deadline", "status", "creator", "assignee", "created_at", "updated_at"]
