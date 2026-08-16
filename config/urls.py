from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import HttpResponse
from django.urls import include, path
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET


@method_decorator(ensure_csrf_cookie, name='dispatch')
class AppLoginView(auth_views.LoginView):
    """يضمن إرسال كوكي CSRF مع صفحة الدخول (مهم بعد إعادة تشغيل السيرفر)."""

    template_name = 'registration/login.html'
    redirect_authenticated_user = True


class AppLogoutView(auth_views.LogoutView):
    """يحفظ مفتاح الجلسة قبل flush حتى يُغلق صف النشاط الصحيح."""

    def dispatch(self, request, *args, **kwargs):
        session = getattr(request, 'session', None)
        request._activity_session_key = (session.session_key if session else '') or ''
        return super().dispatch(request, *args, **kwargs)


@never_cache
@require_GET
def client_version(_request):
    """رقم إصدار الواجهة الحالي — بدون كاش، لفرض التحديث بعد النشر."""
    resp = HttpResponse(
        str(getattr(settings, 'APP_CLIENT_VERSION', '') or ''),
        content_type='text/plain; charset=utf-8',
    )
    resp['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp['Pragma'] = 'no-cache'
    resp['Expires'] = '0'
    return resp


urlpatterns = [
    path('client-version/', client_version, name='client_version'),
    path(
        'login/',
        AppLoginView.as_view(),
        name='login',
    ),
    path('logout/', AppLogoutView.as_view(), name='logout'),
    path('admin/', admin.site.urls),
    path('', include('search.urls')),
]
