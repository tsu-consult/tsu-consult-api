from rest_framework import serializers

from apps.auth_app.models import User
from apps.profile_app.models import GoogleToken
from apps.todo_app.models import ToDo
from apps.todo_app.services import FALLBACK_ALLOWED_MINUTES


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "phone_number"]


class ReminderSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=["popup", "email"])
    minutes = serializers.IntegerField(min_value=15)


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

        assignee = attrs.get('assignee')
        if assignee is not None:
            if getattr(assignee, 'role', None) != 'teacher':
                raise serializers.ValidationError({'assignee_id': 'The assignee must be a teacher.'})
            if getattr(user, 'role', None) == 'teacher' and getattr(assignee, 'id', None) != getattr(user, 'id', None):
                raise serializers.ValidationError({'assignee_id': 'Teachers can only assign tasks to themselves.'})

        reminders = attrs.get('reminders')
        if reminders:
            has_calendar = GoogleToken.objects.filter(user=user).exists()
            for idx, r in enumerate(reminders, start=1):
                method = r.get('method')
                if method not in ('popup', 'email'):
                    raise serializers.ValidationError({
                        'reminders': f'Invalid method at item #{idx}. Only "popup" or "email" allowed.'
                    })
                minutes = r.get('minutes')
                try:
                    minutes_int = int(minutes)
                except (TypeError, ValueError):
                    raise serializers.ValidationError({'reminders': f'Invalid minutes at item #{idx}.'})
                if not has_calendar and method != 'popup':
                    raise serializers.ValidationError({
                        'reminders': f'Only method="popup" is allowed for Telegram reminders (item #{idx}).'
                    })
                if not has_calendar and minutes_int not in FALLBACK_ALLOWED_MINUTES:
                    allowed_list_str = ", ".join(str(m) for m in sorted(FALLBACK_ALLOWED_MINUTES))
                    raise serializers.ValidationError({
                        'reminders': f'Minutes must be one of: {allowed_list_str} (item #{idx}).'
                    })

        return attrs

    def create(self, validated_data):
        user = self.context["request"].user
        if getattr(user, 'role', None) == 'teacher' and not validated_data.get('assignee'):
            validated_data['assignee'] = user

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
