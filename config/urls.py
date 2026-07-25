from django.contrib import admin
from django.urls import include, path
from django.contrib.auth import views as auth_views
from search.debug_auth import auth_debug_dump

urlpatterns = [
    path('__auth_diag_5b001b/', auth_debug_dump, name='auth_debug_dump'),
    path(
        'login/',
        auth_views.LoginView.as_view(
            template_name='registration/login.html',
            redirect_authenticated_user=True,
        ),
        name='login',
    ),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('admin/', admin.site.urls),
    path('', include('search.urls')),
]
