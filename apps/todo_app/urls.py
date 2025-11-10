from django.urls import path
from apps.todo_app.views import ToDoListCreateView

urlpatterns = [
    path('', ToDoListCreateView.as_view(), name='todo-create'),
]
