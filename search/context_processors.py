"""معالجات سياق القوالب."""


import time

from django.conf import settings


_STAMP_TTL = 30.0
_stamp_cache: tuple[float, str] = (0.0, "")


def _css_stamp() -> str:
    """آخر تعديل لملفات CSS/JS حتى يتغيّر رابطها فيُحدَّث المتصفح دون Ctrl+Shift+R.

    يُخزَّن 30 ثانية: كان يمسح مجلد static ويعمل stat لكل ملف في كل طلب."""
    global _stamp_cache
    now = time.monotonic()
    if _stamp_cache[1] and now - _stamp_cache[0] < _STAMP_TTL:
        return _stamp_cache[1]
    value = _compute_css_stamp()
    _stamp_cache = (now, value)
    return value


def _compute_css_stamp() -> str:
    latest = 0
    static = settings.BASE_DIR / "static"
    for pattern in ("css/*.css", "js/*.js"):
        for path in static.glob(pattern):
            try:
                latest = max(latest, path.stat().st_mtime_ns)
            except OSError:
                continue
    return format(latest // 1_000_000_000, "x")


def client_version_string() -> str:
    """نفس الرقم في الصفحة وفي /client-version/ وإلا يعيد force-refresh.js التحميل في كل زيارة."""
    version = str(getattr(settings, "APP_CLIENT_VERSION", "") or "1").strip() or "1"
    return f"{version}-{_css_stamp()}"


def app_client(request):
    """رقم إصدار الواجهة + إشارة تحديث إجباري بعد تسجيل الدخول."""
    version = client_version_string()
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


def nav_access(request):
    """صلاحيات أقسام/شاشات الشريط الجانبي."""
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return {"nav_access": {"is_full": False, "sections": {}, "screens": {}}}
    from .nav_permissions import build_nav_access

    return {"nav_access": build_nav_access(user)}
