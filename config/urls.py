from django.contrib import admin
from django.urls import include, path
from django.contrib.auth import views as auth_views
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie


@method_decorator(ensure_csrf_cookie, name='dispatch')
class AppLoginView(auth_views.LoginView):
    """يضمن إرسال كوكي CSRF مع صفحة الدخول (مهم بعد إعادة تشغيل السيرفر)."""

    template_name = 'registration/login.html'
    redirect_authenticated_user = True


urlpatterns = [
    path(
        'login/',
        AppLoginView.as_view(),
        name='login',
    ),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('admin/', admin.site.urls),
    path('', include('search.urls')),
]
