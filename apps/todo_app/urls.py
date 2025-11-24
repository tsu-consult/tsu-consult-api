from django.urls import path
from apps.todo_app.views import ToDoCreateView, ToDoListView

urlpatterns = [
    path('', ToDoCreateView.as_view(), name='todo-create'),
    path('all/', ToDoListView.as_view(), name='todo-list'),
]
