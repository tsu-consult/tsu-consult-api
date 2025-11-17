from typing import Any, Dict, List, Optional
import logging

from apps.todo_app.services import schedule_fallback_reminders

logger = logging.getLogger(__name__)

TEACHER_DEFAULT_REMINDERS = [
    {"method": "popup", "minutes": 15},
]

DEAN_DEFAULT_REMINDERS = [
    {"method": "popup", "minutes": 15}
]

MAX_REMINDERS = 5


def _normalize_and_sort_reminders(reminders: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    Convert reminders to uniform dicts with int minutes, filter invalid and sort by minutes ascending.

    :param reminders: Input reminders (may contain string minutes or invalid entries).
    :type reminders: Optional[List[Dict[str, Any]]]
    :return: Sorted and filtered reminders with integer minutes limited to MAX_REMINDERS.
    :rtype: List[Dict[str, Any]]
    """
    if not reminders:
        return []
    normalized: List[Dict[str, Any]] = []
    seen = set()
    for idx, r in enumerate(reminders):
        if not isinstance(r, dict):
            logger.debug("Skipping reminder at index %s: not a dict (%r)", idx, r)
            continue
        method = r.get('method')
        minutes = r.get('minutes')
        if not isinstance(method, str) or method not in ('popup', 'email'):
            logger.debug("Skipping reminder at index %s: invalid method (%r)", idx, method)
            continue
        try:
            minutes_int = int(minutes)
        except Exception as exc:
            logger.debug("Skipping reminder at index %s: cannot convert minutes (%r): %s", idx, minutes, exc)
            continue
        if minutes_int <= 0:
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
    normalized.sort(key=lambda x: x['minutes'])
    return normalized[:MAX_REMINDERS]


def get_user_reminders(user: Any,
                       initial: Optional[Dict[str, Any]],
                       reminders: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """
    Return normalized reminders for a user, using role-based defaults if none are provided.

    :param user: User object with `role` attribute.
    :param initial: Raw request data used to detect presence of the `reminders` field.
    :type initial: Optional[Dict[str, Any]]
    :param reminders: Optional list of reminders provided by the user.
    :return: List of normalized reminders, limited to MAX_REMINDERS.
    """

    if 'reminders' in (initial or {}):
        try:
            return _normalize_and_sort_reminders(reminders)
        except (ValueError, TypeError) as exc:
            logger.warning("Invalid reminders provided by user id=%s: %s", getattr(user, 'id', None), exc)

    role = getattr(user, 'role', None) if user else None
    if role == 'teacher':
        return _normalize_and_sort_reminders(TEACHER_DEFAULT_REMINDERS)
    if role == 'dean':
        return _normalize_and_sort_reminders(DEAN_DEFAULT_REMINDERS)

    return []


def sync_and_handle_event(todo: Any,
                          calendar_service: Any,
                          reminders: Optional[List[Dict[str, Any]]],
                          target_user: Any,
                          for_creator: bool = False) -> Optional[str]:
    """
    Sync a ToDo event to a calendar and schedule fallback reminders on failure.

    :param todo: ToDo instance to sync. Must have a ``deadline`` attribute.
    :type todo: apps.todo_app.models.ToDo
    :param calendar_service: Calendar service instance (e.g. ``GoogleCalendarService``) which exposes ``.service``.
    :type calendar_service: Any
    :param reminders: Reminders to apply for the calendar event. If ``None``, defaults are used by the service.
    :type reminders: Optional[List[Dict[str, Any]]]
    :param target_user: User who should receive fallback (Telegram) notifications if calendar sync fails.
    :type target_user: Any
    :param for_creator: If True, the event is intended for the creator and the created event id will be
        saved into the todo's ``creator_calendar_event_id`` via ``todo.sync_calendar_event``.
    :type for_creator: bool

    :return: The created calendar event id when successful, otherwise ``None``.
    :rtype: Optional[str]

    :raises Exception: If scheduling fallback reminders or other unexpected errors occur (may be propagated).
    """
    if not getattr(todo, 'deadline', None):
        return None

    event_id = None

    if getattr(calendar_service, 'service', None):
        try:
            event_id = todo.create_calendar_event(calendar_service, reminders=reminders, for_creator=for_creator)
        except Exception as exc:
            logger.exception("Calendar sync failed for todo id=%s: %s", getattr(todo, 'id', '<unknown>'), exc)
            event_id = None

    if ((not getattr(calendar_service, 'service', None) or event_id is None) and getattr(todo, 'deadline', None)
            and reminders):
        try:
            schedule_fallback_reminders(todo, reminders, target_user=target_user)
        except Exception as exc:
            logger.exception("Failed to schedule fallback reminders for todo id=%s: %s",
                             getattr(todo, 'id', '<unknown>'), exc)
            raise

    return event_id
