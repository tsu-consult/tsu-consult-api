from typing import Any, Dict, List, Optional
import logging

from rest_framework.exceptions import ValidationError
from googleapiclient.errors import HttpError
from celery.exceptions import CeleryError

from core.exceptions import GoogleCalendarAuthRequired

from apps.todo_app.config import MAX_REMINDERS, TEACHER_DEFAULT_REMINDERS, DEAN_DEFAULT_REMINDERS, ALLOWED_MINUTES

logger = logging.getLogger(__name__)


def validate_and_normalize_reminders(reminders: Optional[List[Dict[str, Any]]]) -> List[Dict[str, int]]:
    """
    Validate and normalize a list of reminder dicts.

    This function validates each item in ``reminders``, converts the ``minutes`` value to an integer,
    filters invalid or duplicate entries, sorts the remaining reminders by ascending minutes and limits
    the result to ``MAX_REMINDERS``.

    :param reminders: List of reminder dicts to validate and normalize.
    :type reminders: Optional[List[Dict[str, Any]]]
    :return: A list of normalized reminder dicts (each with keys ``method`` and ``minutes``).
    :rtype: List[Dict[str, int]]
    :raises ValidationError: If no valid reminders remain after validation; the error payload
        contains per-item error messages when available.
    """
    if not reminders:
        return []

    normalized: List[Dict[str, int]] = []
    seen = set()
    errors = []

    for idx, r in enumerate(reminders):
        if not isinstance(r, dict):
            errors.append(f"reminders[{idx}] is not an object")
            logger.debug("Skipping reminder at index %s: not a dict (%r)", idx, r)
            continue

        method = r.get('method')
        minutes = r.get('minutes')

        if method not in ('popup', 'email'):
            errors.append(f"reminders[{idx}].method invalid: {method!r}")
            logger.debug("Skipping reminder at index %s: invalid method (%r)", idx, method)
            continue

        try:
            minutes_int = int(minutes)
        except (TypeError, ValueError) as exc:
            errors.append(f"reminders[{idx}].minutes is not a valid integer: {minutes!r}")
            logger.debug("Skipping reminder at index %s: cannot convert minutes (%r): %s", idx, minutes, exc)
            continue

        if minutes_int <= 0:
            errors.append(f"reminders[{idx}].minutes must be > 0, got {minutes_int}")
            logger.debug("Skipping reminder at index %s: non-positive minutes (%d)", idx, minutes_int)
            continue

        pair = (method, minutes_int)
        if pair in seen:
            logger.debug("Skipping duplicate reminder at index %s: %s", idx, pair)
            continue

        seen.add(pair)
        normalized.append({'method': method, 'minutes': minutes_int})

        if len(normalized) >= MAX_REMINDERS * 2:
            logger.debug("Reached buffer limit while normalizing reminders")
            break

    if not normalized:
        raise ValidationError({"reminders": errors or ["All provided reminders are invalid."]})

    normalized.sort(key=lambda x: x['minutes'])
    return normalized[:MAX_REMINDERS]


def normalize_reminders_permissive(reminders: Optional[List[Dict[str, any]]]) -> List[Dict[str, int]]:
    """
    Permissively normalize a list of reminder dicts.

    This function converts reminder entries to a uniform form (``{'method': str, 'minutes': int}``),
    silently skips invalid or non-dict entries and removes duplicates, sorts by minutes ascending and
    limits the result to ``MAX_REMINDERS``.

    :param reminders: List of reminder dicts to normalize.
    :type reminders: Optional[List[Dict[str, Any]]]
    :return: A list of normalized reminder dicts (each with keys ``method`` and ``minutes``).
    :rtype: List[Dict[str, int]]
    :raises ValidationError: If no valid reminders remain after filtering.
    """
    if not reminders:
        return []

    normalized: List[Dict[str, int]] = []
    seen = set()

    for idx, r in enumerate(reminders):
        if not isinstance(r, dict):
            continue

        method = r.get('method')
        minutes = r.get('minutes')

        try:
            minutes_int = int(minutes)
        except (TypeError, ValueError):
            continue

        pair = (method, minutes_int)
        if pair in seen:
            continue

        seen.add(pair)
        normalized.append({'method': method, 'minutes': minutes_int})

        if len(normalized) >= MAX_REMINDERS * 2:
            break

    if not normalized:
        raise ValidationError({'reminders': 'All provided reminders are invalid.'})

    normalized.sort(key=lambda x: x['minutes'])
    return normalized[:MAX_REMINDERS]


def get_user_reminders(user: Any,
                       initial: Optional[Dict[str, Any]],
                       reminders: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """
    Determine which reminders should be used for a user's To Do.

    :param user: User object which is expected to have a ``role`` attribute.
    :param initial: Raw request data used to determine whether the ``reminders`` field was provided.
    :param reminders: Optional list of reminders provided by the caller. Returned unchanged when
                      ``reminders`` key is present in ``initial`` (or as an empty list when falsy).
    :return: A list of reminders ready to be used by the application (maybe empty).
    """

    if 'reminders' in (initial or {}):
        return reminders or []

    role = getattr(user, 'role', None)
    if role == 'teacher':
        return TEACHER_DEFAULT_REMINDERS
    if role == 'dean':
        return DEAN_DEFAULT_REMINDERS

    return []


def sync_and_handle_event(td: Any,
                          calendar_service: Any,
                          reminders: Optional[List[Dict[str, Any]]],
                          target_user: Any,
                          for_creator: bool = False) -> Optional[str]:
    """
    Sync a To Do event to a calendar and schedule fallback reminders on failure.

    :param td: To Do instance to sync. Must have a ``deadline`` attribute.
    :type td: apps.todo_app.models.ToDo
    :param calendar_service: Calendar service instance (e.g. ``GoogleCalendarService``) which exposes ``.service``.
    :type calendar_service: Any
    :param reminders: Reminders to apply for the calendar event. If ``None``, defaults are used by the service.
    :type reminders: Optional[List[Dict[str, Any]]]
    :param target_user: User who should receive fallback (Telegram) notifications if calendar sync fails.
    :type target_user: Any
    :param for_creator: If True, the event is intended for the creator and the created event id will be
        saved into the to do's ``creator_calendar_event_id`` via ``todo.sync_calendar_event``.
    :type for_creator: bool

    :return: The created calendar event id when successful, otherwise ``None``.
    :rtype: Optional[str]

    :raises Exception: If scheduling fallback reminders or other unexpected errors occur (may be propagated).
    """

    if not getattr(td, 'deadline', None):
        return None

    event_id = None

    if getattr(calendar_service, 'service', None):
        try:
            event_id = td.create_calendar_event(calendar_service, reminders=reminders, for_creator=for_creator)
        except (HttpError, GoogleCalendarAuthRequired) as exc:
            logger.exception("Calendar sync failed for todo id=%s with Google API error: %s",
                             getattr(td, 'id', '<unknown>'), exc)
            if getattr(td, 'deadline', None) and reminders:
                try:
                    from apps.todo_app.services import FallbackReminderService
                    FallbackReminderService().schedule_fallback_reminders(td, reminders, target_user=target_user)
                except (CeleryError, RuntimeError) as exc2:
                    logger.exception("Failed to schedule fallback reminders after Google API error for todo id=%s: %s",
                                     getattr(td, 'id', '<unknown>'), exc2)
        except (ValueError, TypeError, AttributeError) as exc:
            logger.exception("Calendar sync failed for todo id=%s: %s", getattr(td, 'id', '<unknown>'), exc)
            event_id = None

    if ((not getattr(calendar_service, 'service', None) or event_id is None) and getattr(td, 'deadline', None)
            and reminders):
        try:
            from apps.todo_app.services import FallbackReminderService
            FallbackReminderService().schedule_fallback_reminders(td, reminders, target_user=target_user)
        except (CeleryError, RuntimeError) as exc:
            logger.exception("Failed to schedule fallback reminders for todo id=%s: %s",
                             getattr(td, 'id', '<unknown>'), exc)
            raise

    return event_id


def normalize_reminders_for_fallback(reminders: Optional[List[Dict[str, Any]]],
                                     allowed_minutes: Optional[List[int]] = None,
                                     limit: int = 5) -> List[int]:
    """
    Normalize and filter reminder dicts for fallback notification scheduling.

    This function extracts the 'minutes' value from each reminder dict, ensures it is an integer,
    filters out invalid or duplicate values, and only includes those minutes present in the allowed_minutes set.
    The result is a list of unique, valid reminder times (in minutes), up to the specified limit.

    :param reminders: List of reminder dicts, each expected to have a 'minutes' key.
    :type reminders: Optional[List[Dict[str, Any]]]
    :param allowed_minutes: List of allowed minute values. Only reminders with 'minutes' in this list are included.
        If None, uses the default ALLOWED_MINUTES from config.
    :type allowed_minutes: Optional[List[int]]
    :param limit: Maximum number of reminders to return.
    :type limit: int
    :return: List of unique, valid reminder times (in minutes), filtered and limited.
    :rtype: List[int]
    """
    if not reminders:
        return []

    allowed = set(allowed_minutes) if allowed_minutes is not None else set(ALLOWED_MINUTES)
    seen = set()
    out: List[int] = []

    for idx, r in enumerate(reminders if isinstance(reminders, list) else []):
        if not isinstance(r, dict):
            logger.debug("Skipping non-dict reminder at index %s: %r", idx, r)
            continue

        minutes = r.get('minutes')
        try:
            minutes_int = int(minutes)
        except (TypeError, ValueError):
            logger.debug("Skipping reminder at index %s due to invalid minutes: %r", idx, minutes)
            continue

        if minutes_int <= 0 or minutes_int not in allowed:
            logger.debug("Skipping reminder at index %s due to disallowed minutes: %s", idx, minutes_int)
            continue

        if minutes_int in seen:
            logger.debug("Skipping duplicate reminder minutes at index %s: %s", idx, minutes_int)
            continue

        seen.add(minutes_int)
        out.append(minutes_int)

        if len(out) >= limit:
            break

    return out
