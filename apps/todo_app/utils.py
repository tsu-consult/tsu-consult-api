from typing import Any, Dict, List, Optional
import logging

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


def merge_reminders(r1: Optional[List[Dict[str, Any]]],
                    r2: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """
    Merge two reminders lists removing duplicates by (method, minutes).

    :param r1: First reminders list or ``None``.
    :type r1: Optional[List[Dict[str, Any]]]
    :param r2: Second reminders list or ``None``.
    :type r2: Optional[List[Dict[str, Any]]]

    :return: Merged list without duplicate (method, minutes) pairs, or ``None`` if both inputs are ``None``.
    :rtype: Optional[List[Dict[str, Any]]]
    """
    if r1 is None and r2 is None:
        return None

    combined = (r1 or []) + (r2 or [])
    merged = _normalize_and_sort_reminders(combined)
    return merged


def resolve_assignee_reminders(initial_data: Optional[Dict[str, Any]],
                               provided_reminders: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    Return reminders for assignee.

    If the request payload contains the `reminders` field, its value is used (``None`` means an explicit
    empty list). Otherwise, the default teacher reminders are returned.

    :param initial_data: Raw request data used to detect presence of the `reminders` field.
    :type initial_data: Optional[Dict[str, Any]]
    :param provided_reminders: Parsed reminders from the serializer or ``None``.
    :type provided_reminders: Optional[List[Dict[str, Any]]]

    :return: Reminders list for the assignee (maybe empty).
    :rtype: List[Dict[str, Any]]
    """
    if 'reminders' in (initial_data or {}):
        base = provided_reminders if provided_reminders is not None else []
        return _normalize_and_sort_reminders(base)
    return _normalize_and_sort_reminders(TEACHER_DEFAULT_REMINDERS)


def resolve_creator_reminders(initial_data: Optional[Dict[str, Any]],
                              creator_provided_reminders: Optional[List[Dict[str, Any]]],
                              request_user) -> Optional[List[Dict[str, Any]]]:
    """
    Determine reminders for the creator.

    If the request payload contains the `creator_reminders` field, its value is returned (``None`` means an
    explicit empty list). Otherwise, role-based defaults are returned: dean -> ``DEAN_DEFAULT_REMINDERS``,
    teacher -> ``TEACHER_DEFAULT_REMINDERS``. For other roles ``None`` is returned (no reminders).

    :param initial_data: Raw request data used to detect presence of the `creator_reminders` field.
    :type initial_data: Optional[Dict[str, Any]]
    :param creator_provided_reminders: Parsed creator_reminders from the serializer or ``None``.
    :type creator_provided_reminders: Optional[List[Dict[str, Any]]]
    :param request_user: Request user object (used to inspect the role attribute).
    :type request_user: Any

    :return: Reminders for the creator, or ``None`` if no reminders should be created.
    :rtype: Optional[List[Dict[str, Any]]]
    """
    if 'creator_reminders' in (initial_data or {}):
        return _normalize_and_sort_reminders(creator_provided_reminders
                                             if creator_provided_reminders is not None else [])

    role = getattr(request_user, 'role', None)
    if role == 'dean':
        return _normalize_and_sort_reminders(DEAN_DEFAULT_REMINDERS)
    if role == 'teacher':
        return _normalize_and_sort_reminders(TEACHER_DEFAULT_REMINDERS)
    return None


def sync_and_handle_event(todo: Any,
                          calendar_svc: Any,
                          reminders: Optional[List[Dict[str, Any]]],
                          target_user: Any,
                          for_creator: bool = False) -> Optional[str]:
    """
    Sync a ToDo event to a calendar and schedule fallback reminders on failure.

    :param todo: ToDo instance to sync. Must have a ``deadline`` attribute.
    :type todo: apps.todo_app.models.ToDo
    :param calendar_svc: Calendar service instance (e.g. ``GoogleCalendarService``) which exposes ``.service``.
    :type calendar_svc: Any
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
    from apps.todo_app.services import schedule_fallback_reminders

    if getattr(calendar_svc, 'service', None):
        try:
            event_id = todo.sync_calendar_event(calendar_svc, reminders=reminders, for_creator=for_creator)
        except Exception as exc:
            logger.exception("Calendar sync failed for todo id=%s: %s", getattr(todo, 'id', '<unknown>'), exc)
            event_id = None

    if ((not getattr(calendar_svc, 'service', None) or event_id is None) and getattr(todo, 'deadline', None)
            and reminders):
        try:
            schedule_fallback_reminders(todo, reminders, target_user=target_user)
        except Exception as exc:
            logger.exception("Failed to schedule fallback reminders for todo id=%s: %s",
                             getattr(todo, 'id', '<unknown>'), exc)
            raise

    return event_id
