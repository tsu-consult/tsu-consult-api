from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from apps.auth_app.models import User
from apps.todo_app.config import MAX_DESCRIPTION_LENGTH, MAX_TITLE_LENGTH, \
    MIN_DEADLINE_DELTA, TEACHER_DEFAULT_REMINDERS
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
        fields = ["title", "description", "deadline", "assignee_id", "reminders", "status"]

    def validate_assignee_edit_permissions(self, attrs):
        instance = getattr(self, "instance", None)
        if not instance:
            return attrs

        user = self.context["request"].user
        raw_initial = getattr(self, "initial_data", {}) or {}

        is_assignee = getattr(instance.assignee, "id", None) == getattr(user, "id", None)
        creator_role = getattr(instance.creator, "role", None)

        if is_assignee and creator_role == "dean":
            allowed_keys = {"reminders", "status"}
            forbidden = {k for k in raw_initial.keys() if k not in allowed_keys}
            if forbidden:
                raise serializers.ValidationError(
                    "Assignee may only edit reminders and status for tasks assigned by a dean."
                )

        return attrs

    def validate(self, attrs):
        user = self.context["request"].user
        raw_initial = getattr(self, 'initial_data', {})

        if getattr(user, 'role', None) not in ('teacher', 'dean'):
            raise serializers.ValidationError('Only teachers or the dean can create or edit tasks.')

        title = attrs.get("title")
        if (not getattr(self, 'instance', None)) or ('title' in raw_initial):
            if not title:
                raise serializers.ValidationError({"title": "The title is required."})
        if title and len(title) > MAX_TITLE_LENGTH:
            raise serializers.ValidationError({"title": f"The title must be at most {MAX_TITLE_LENGTH} "
                                                        f"characters long."})

        instance = getattr(self, 'instance', None)
        if instance is not None and 'status' in (raw_initial or {}):
            assignee_id = getattr(getattr(instance, 'assignee', None), 'id', None)
            user_id = getattr(user, 'id', None)
            if assignee_id != user_id:
                raise serializers.ValidationError({'status': 'Only assignee may change status.'})

        description = attrs.get("description")
        if description and len(description) > MAX_DESCRIPTION_LENGTH:
            raise serializers.ValidationError({
                "description": f"The description must be at most {MAX_DESCRIPTION_LENGTH} characters long."
            })

        deadline = attrs.get("deadline")
        now = timezone.now()
        if deadline:
            if deadline < now:
                raise serializers.ValidationError({"deadline": "The deadline should be later than the current time."})
            if deadline - now < MIN_DEADLINE_DELTA:
                raise serializers.ValidationError({"deadline": f"The deadline must be at least "
                                                               f"{int(MIN_DEADLINE_DELTA.total_seconds() / 60)} "
                                                               f"minute(s) from now."})

        assignee = attrs.get("assignee")
        instance = getattr(self, "instance", None)

        is_create = instance is None
        is_assignee_provided = "assignee" in raw_initial

        if getattr(user, 'role', None) == 'dean' and (is_assignee_provided or is_create) and assignee is None:
            raise serializers.ValidationError({'assignee_id': 'The dean must assign the task to a teacher.'})

        if is_assignee_provided or is_create:
            if assignee is not None:
                if getattr(assignee, 'role', None) != 'teacher':
                    raise serializers.ValidationError({'assignee_id': 'The assignee must be a teacher.'})
                if (getattr(user, 'role', None) == 'teacher' and getattr(assignee, 'id', None) !=
                        getattr(user, 'id', None)):
                    raise serializers.ValidationError({'assignee_id': 'Teachers can only assign tasks to themselves.'})
                if getattr(user, 'role', None) == 'dean' and getattr(assignee, 'id', None) == getattr(user, 'id', None):
                    raise serializers.ValidationError({'assignee_id': 'Deans cannot assign tasks to themselves.'})

        attrs = self.validate_assignee_edit_permissions(attrs)

        raw_reminders = attrs.get("reminders", None)
        if 'reminders' in raw_initial and raw_initial.get('reminders') is None:
            if 'reminders' in attrs:
                attrs.pop('reminders', None)
        elif 'reminders' in raw_initial and raw_initial.get('reminders') is not None:
            try:
                normalized = normalize_reminders_permissive(raw_reminders)
            except ValidationError as exc:
                raise ValidationError(exc.detail)
            attrs['reminders'] = normalized

        return attrs

    def create(self, validated_data):
        user = self.context["request"].user
        if getattr(user, 'role', None) == 'teacher' and not validated_data.get('assignee'):
            validated_data['assignee'] = user

        reminders = get_user_reminders(user, self.initial_data, validated_data.get('reminders'))
        assignee_reminders = TEACHER_DEFAULT_REMINDERS if getattr(user, 'role', None) == 'dean' else []

        validated_data['reminders'] = reminders
        validated_data['assignee_reminders'] = assignee_reminders

        todo = ToDo.objects.create(creator=user, **validated_data)
        return todo

    def update(self, instance, validated_data):
        user = self.context["request"].user

        old_assignee = getattr(instance, 'assignee', None)

        if getattr(user, 'role', None) == 'teacher' and not validated_data.get('assignee'):
            validated_data['assignee'] = user

        raw_initial = getattr(self, 'initial_data', {}) or {}
        deadline_passed = getattr(instance, 'deadline', None) and getattr(instance, 'deadline') < timezone.now()
        if deadline_passed and 'reminders' in raw_initial:
            raise serializers.ValidationError({'reminders': 'Cannot modify reminders for overdue tasks.'})
        if 'reminders' in raw_initial and raw_initial.get('reminders') is not None:
            creator = getattr(instance, 'creator', None)
            creator_role = getattr(creator, 'role', None)
            is_assignee = getattr(user, 'id', None) == getattr(getattr(instance, 'assignee', None), 'id', None)
            reminders = get_user_reminders(user, raw_initial, validated_data.get('reminders'))

            creator_id = getattr(creator, 'id', None)
            assignee_id = getattr(getattr(instance, 'assignee', None), 'id', None)
            if creator_id is not None and creator_id == assignee_id and is_assignee:
                validated_data['reminders'] = reminders
            else:
                if is_assignee and creator_role == 'dean':
                    validated_data['assignee_reminders'] = reminders
                    if 'reminders' in validated_data:
                        validated_data.pop('reminders', None)
                else:
                    validated_data['reminders'] = reminders

        new_assignee = validated_data.get('assignee', getattr(instance, 'assignee', None))
        if getattr(user, 'role', None) == 'dean':
            if ('assignee_reminders' not in validated_data) and (
                    old_assignee is None or getattr(old_assignee, 'id', None) != getattr(new_assignee, 'id', None)):
                validated_data['assignee_reminders'] = TEACHER_DEFAULT_REMINDERS

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        new_assignee = getattr(instance, 'assignee', None)
        if old_assignee is not None and getattr(old_assignee, 'id', None) != getattr(new_assignee, 'id', None):
            instance.assignee_calendar_event_id = None
            instance.assignee_calendar_event_active = False

        instance.save()
        return instance


class ToDoResponseSerializer(serializers.ModelSerializer):
    creator = UserSerializer(read_only=True)
    assignee = UserSerializer(read_only=True)
    reminders = ReminderSerializer(many=True, read_only=True)
    assignee_reminders = ReminderSerializer(many=True, read_only=True)

    class Meta:
        model = ToDo
        fields = ["id", "title", "description", "deadline", "status", "creator", "assignee", "reminders",
                  "assignee_reminders", "created_at", "updated_at"]


class ToDoListResponseSerializer(serializers.ModelSerializer):
    creator = UserSerializer(read_only=True)
    assignee = UserSerializer(read_only=True, required=False)

    class Meta:
        model = ToDo
        fields = ["id", "title", "status", "deadline", "creator", "assignee", "created_at", "updated_at"]


class PaginatedToDosSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    total_pages = serializers.IntegerField()
    current_page = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = ToDoListResponseSerializer(many=True)
