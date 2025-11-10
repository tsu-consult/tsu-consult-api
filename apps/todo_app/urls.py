from django.urls import path
from apps.todo_app.views import ToDoCreateView

urlpatterns = [
    path('', ToDoCreateView.as_view(), name='todo-create'),
]
