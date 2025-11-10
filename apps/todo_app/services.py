class GoogleCalendarService:
    def __init__(self, user=None):
        self.user = user

    def create_event(self, todo):
        if not todo.deadline:
            return None
        return f"gcal-{todo.id or 'new'}"

    def delete_event(self, event_id):
        return True
