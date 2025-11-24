from django.urls import path
from apps.todo_app.views import ToDoCreateView, ToDoListView, ToDoDetailView

urlpatterns = [
    path('', ToDoCreateView.as_view(), name='todo-create'),
    path('all/', ToDoListView.as_view(), name='todo-list'),
    path('<str:todo_id>/', ToDoDetailView.as_view(), name='todo-detail'),
]
