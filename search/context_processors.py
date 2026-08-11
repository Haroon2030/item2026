"""معالجات سياق القوالب."""


from django.conf import settings


def app_client(request):
    """رقم إصدار الواجهة لإجبار التحديث بعد النشر."""
    version = str(getattr(settings, "APP_CLIENT_VERSION", "") or "1").strip() or "1"
    return {"app_client_version": version}
