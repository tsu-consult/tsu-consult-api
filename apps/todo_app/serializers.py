from rest_framework import serializers

from apps.todo_app.models import ToDo
from apps.auth_app.models import User
from apps.profile_app.models import GoogleToken
from apps.todo_app.services import FALLBACK_ALLOWED_MINUTES


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
            raise serializers.ValidationError('Only teachers or the dean can create assignments.')

        assignee = attrs.get('assignee')
        if assignee is not None:
            if getattr(assignee, 'role', None) != 'teacher':
                raise serializers.ValidationError({'assignee_id': 'The performer must be a teacher.'})
            if user.role == 'teacher' and assignee.id != user.id:
                raise serializers.ValidationError({'assignee_id': 'Teachers can only assign tasks to themselves.'})

        reminders = attrs.get('reminders')
        if reminders and not assignee and user.role != 'teacher':
            raise serializers.ValidationError({'reminders': 'Reminders require an assignee (teacher). Dean drafts '
                                                            'cannot have reminders.'})

        calendar_user = assignee or user
        has_calendar = GoogleToken.objects.filter(user=calendar_user).exists()
        if reminders and getattr(calendar_user, 'role', None) != 'teacher':
            raise serializers.ValidationError({'reminders': 'Only a teacher can have reminders.'})

        if not has_calendar and reminders:
            allowed_list_str = ", ".join(str(m) for m in sorted(FALLBACK_ALLOWED_MINUTES))
            for idx, r in enumerate(reminders, start=1):
                method = r.get('method')
                if method != 'popup':
                    raise serializers.ValidationError({'reminders': f'Only method="popup" is allowed for Telegram '
                                                                    f'reminders (item #{idx}).'})
                minutes = r.get('minutes')
                try:
                    minutes_int = int(minutes)
                except (TypeError, ValueError):
                    raise serializers.ValidationError({'reminders': f'Invalid minutes value at item #{idx}.'})
                if minutes_int not in FALLBACK_ALLOWED_MINUTES:
                    raise serializers.ValidationError({'reminders': f'Minutes must be one of: {allowed_list_str} '
                                                                    f'(item #{idx}).'})
        return attrs

    def create(self, validated_data):
        validated_data.pop('reminders', None)
        user = self.context["request"].user
        if getattr(user, 'role', None) == 'teacher' and not validated_data.get('assignee'):
            validated_data['assignee'] = user
        todo = ToDo.objects.create(creator=user, **validated_data)
        return todo


class ToDoResponseSerializer(serializers.ModelSerializer):
    creator = UserSerializer(read_only=True)
    assignee = UserSerializer(read_only=True)

    class Meta:
        model = ToDo
        fields = ["id", "title", "description", "deadline", "status", "creator", "assignee", "created_at", "updated_at"]
