from django.urls import path
from .views import RegisterView, LoginView, RefreshView, LogoutView

urlpatterns = [
    path('register/', RegisterView.as_view(), name='auth-register'),
    path('login/', LoginView.as_view(), name='auth-login'),
    path('refresh/', RefreshView.as_view(), name='auth-refresh'),
    path("logout/", LogoutView.as_view(), name="auth-logout"),
]
