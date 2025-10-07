from rest_framework.exceptions import NotFound, ValidationError, NotAuthenticated, APIException, PermissionDenied, \
    AuthenticationFailed
from rest_framework import status
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from core.mixins import ErrorResponseMixin


def custom_exception_handler(exc, context):
    request = context.get('request')

    if isinstance(exc, NotAuthenticated):
        return ErrorResponseMixin.format_error(
            request,
            status.HTTP_401_UNAUTHORIZED,
            "Unauthorized",
            "User is not authenticated"
        )

    if isinstance(exc, (InvalidToken, TokenError, AuthenticationFailed)):
        return ErrorResponseMixin.format_error(
            request,
            status.HTTP_401_UNAUTHORIZED,
            "Unauthorized",
            "Token is invalid or expired"
        )

    if isinstance(exc, PermissionDenied):
        return ErrorResponseMixin.format_error(
            request,
            status.HTTP_403_FORBIDDEN,
            "Forbidden",
            str(exc.detail)
        )

    if isinstance(exc, NotFound):
        return ErrorResponseMixin.format_error(
            request,
            status.HTTP_404_NOT_FOUND,
            "Not Found",
            str(exc.detail)
        )

    if isinstance(exc, ValidationError):
        return ErrorResponseMixin.format_error(
            request,
            status.HTTP_400_BAD_REQUEST,
            "Bad Request",
            exc.detail
        )

    if isinstance(exc, APIException):
        return ErrorResponseMixin.format_error(
            request,
            exc.status_code,
            exc.default_detail,
            str(exc.detail)
        )

    return ErrorResponseMixin.format_error(
        request,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "Internal Server Error",
        str(exc)
    )