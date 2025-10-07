from rest_framework.response import Response
from django.utils import timezone

class ErrorResponseMixin:
    @staticmethod
    def format_error(request, status_code, error, message):
        return Response({
            "timestamp": timezone.now().isoformat(),
            "status": status_code,
            "error": error,
            "message": message,
            "path": request.path
        }, status=status_code)
