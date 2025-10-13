from django.contrib.auth import get_user_model
from rest_framework import serializers

User = get_user_model()

class TeacherResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    phone_number = serializers.CharField(allow_null=True, allow_blank=True)

class PaginatedTeachersSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    total_pages = serializers.IntegerField()
    current_page = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = serializers.ListField(child=TeacherResponseSerializer())
