"""صفحة تطبيق الموبايل على /app/ — HTML خفيف يعمل على Safari."""

from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@never_cache
@require_GET
def mobile_app_page(request, asset: str = ""):
    """واجهة المبيعات للجوال — بلا Flutter/WASM حتى يفتح الآيفون."""
    resp = render(request, "search/mobile_app.html")
    resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp["Pragma"] = "no-cache"
    return resp
