import logging
import json
from rest_framework.test import APIRequestFactory
from rest_framework.test import force_authenticate
from apps.todo_app.views import ToDoCreateView, ToDoDetailView
from apps.todo_app.models import ToDo

logger = logging.getLogger(__name__)


class ToDoAdminAPIService:
    def __init__(self, user):
        self.user = user
        self.factory = APIRequestFactory()

    def create_todo(self, data: dict) -> ToDo:
        request = self.factory.post('/todo/', data=json.dumps(data), content_type='application/json')
        force_authenticate(request, user=self.user)

        view = ToDoCreateView.as_view()
        response = view(request)

        if response.status_code != 201:
            raise Exception(f"Failed to create ToDo via API: {response.data}")

        todo_id = response.data.get('id')
        return ToDo.objects.get(id=todo_id)

    def update_todo(self, todo_id: int, data: dict) -> ToDo:
        request = self.factory.patch(f'/todo/{todo_id}/', data=json.dumps(data), content_type='application/json')
        force_authenticate(request, user=self.user)

        view = ToDoDetailView.as_view()
        response = view(request, todo_id=str(todo_id))

        if response.status_code != 200:
            raise Exception(f"Failed to update ToDo via API: {response.data}")

        return ToDo.objects.get(id=todo_id)

    def delete_todo(self, todo_id: int) -> None:
        request = self.factory.delete(f'/todo/{todo_id}/')
        force_authenticate(request, user=self.user)

        view = ToDoDetailView.as_view()
        response = view(request, todo_id=str(todo_id))

        if response.status_code not in [200, 204]:
            raise Exception(f"Failed to delete ToDo via API: {response.data}")
