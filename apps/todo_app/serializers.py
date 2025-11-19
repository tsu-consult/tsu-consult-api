from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from apps.auth_app.models import User
from apps.todo_app.config import MAX_DESCRIPTION_LENGTH, MAX_TITLE_LENGTH, \
    MIN_DEADLINE_DELTA
from apps.todo_app.models import ToDo
from apps.todo_app.utils import get_user_reminders, normalize_reminders_permissive


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "phone_number"]


class ReminderSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=["popup", "email"])
    minutes = serializers.IntegerField(min_value=1)


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
    reminders = ReminderSerializer(many=True, required=False, allow_null=True, write_only=True)

    class Meta:
        model = ToDo
        fields = ["title", "description", "deadline", "assignee_id", "reminders"]

    def validate(self, attrs):
        user = self.context["request"].user
        if getattr(user, 'role', None) not in ('teacher', 'dean'):
            raise serializers.ValidationError('Only teachers or the dean can create tasks.')

        title = attrs.get("title")
        if not title:
            raise serializers.ValidationError({"title": "The title is required."})
        if len(title) > MAX_TITLE_LENGTH:
            raise serializers.ValidationError({
                "title": f"The title must be at most {MAX_TITLE_LENGTH} characters long."
            })

        description = attrs.get("description")
        if description and len(description) > MAX_DESCRIPTION_LENGTH:
            raise serializers.ValidationError({
                "description": f"The description must be at most {MAX_DESCRIPTION_LENGTH} characters long."
            })

        deadline = attrs.get("deadline")
        now = timezone.now()
        if deadline:
            if deadline < now:
                raise serializers.ValidationError({
                    "deadline": "The deadline should be later than the current time."
                })

            if deadline - now < MIN_DEADLINE_DELTA:
                raise serializers.ValidationError({
                    "deadline": f"The deadline must be at "
                                f"least {int(MIN_DEADLINE_DELTA.total_seconds() / 60)} minute(s) from now."
                })

        assignee = attrs.get('assignee')
        if getattr(user, 'role', None) == 'dean' and assignee is None:
            raise serializers.ValidationError({
                'assignee_id': 'The dean must assign the task to a teacher.'
            })

        if assignee is not None:
            if getattr(assignee, 'role', None) != 'teacher':
                raise serializers.ValidationError({'assignee_id': 'The assignee must be a teacher.'})
            if getattr(user, 'role', None) == 'teacher' and getattr(assignee, 'id', None) != getattr(user, 'id', None):
                raise serializers.ValidationError({'assignee_id': 'Teachers can only assign tasks to themselves.'})
            if getattr(user, 'role', None) == 'dean' and getattr(assignee, 'id', None) == getattr(user, 'id', None):
                raise serializers.ValidationError({'assignee_id': 'Deans cannot assign tasks to themselves.'})

        raw_initial = getattr(self, 'initial_data', {})
        raw_reminders = attrs.get('reminders', None)
        if 'reminders' in raw_initial:
            try:
                normalized = normalize_reminders_permissive(raw_reminders)
            except ValidationError as exc:
                raise ValidationError(exc.detail)
        else:
            normalized = None

        attrs['reminders'] = normalized

        return attrs

    def create(self, validated_data):
        user = self.context["request"].user
        if getattr(user, 'role', None) == 'teacher' and not validated_data.get('assignee'):
            validated_data['assignee'] = user

        reminders = get_user_reminders(user, self.initial_data, validated_data.get('reminders'))
        assignee_reminders = get_user_reminders(validated_data.get('assignee'), self.initial_data)

        validated_data['reminders'] = reminders
        validated_data['assignee_reminders'] = assignee_reminders

        todo = ToDo.objects.create(creator=user, **validated_data)
        return todo


class ToDoResponseSerializer(serializers.ModelSerializer):
    creator = UserSerializer(read_only=True)
    assignee = UserSerializer(read_only=True)
    reminders = ReminderSerializer(many=True, read_only=True)
    assignee_reminders = ReminderSerializer(many=True, read_only=True)

    class Meta:
        model = ToDo
        fields = ["id", "title", "description", "deadline", "status", "creator", "assignee", "reminders",
                  "assignee_reminders", "created_at", "updated_at"]
