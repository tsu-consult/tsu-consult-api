from datetime import timedelta

MAX_TITLE_LENGTH = 255
MAX_DESCRIPTION_LENGTH = 1000
MIN_DEADLINE_DELTA = timedelta(minutes=1)
ALLOWED_MINUTES = {15, 30, 60, 1440}
MAX_REMINDERS = 5
TEACHER_DEFAULT_REMINDERS = [{"method": "popup", "minutes": 15}]
DEAN_DEFAULT_REMINDERS = [{"method": "popup", "minutes": 15}]
