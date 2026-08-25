"""معالجات سياق القوالب."""


from django.conf import settings


def app_client(request):
    """رقم إصدار الواجهة + إشارة تحديث إجباري بعد تسجيل الدخول."""
    version = str(getattr(settings, "APP_CLIENT_VERSION", "") or "1").strip() or "1"
    force_hard_refresh = False
    user = getattr(request, "user", None)
    session = getattr(request, "session", None)
    if user is not None and getattr(user, "is_authenticated", False) and session is not None:
        try:
            if session.pop("force_hard_refresh", None) == "1":
                force_hard_refresh = True
                session.modified = True
        except Exception:
            force_hard_refresh = False
    return {
        "app_client_version": version,
        "force_hard_refresh": force_hard_refresh,
    }
