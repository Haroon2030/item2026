"""طبقات أمان: هيدرز + حد معدل الطلبات."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from django.conf import settings
from django.http import HttpResponseForbidden, JsonResponse


class SecurityHeadersMiddleware:
    """هيدرز أمان إضافية فوق إعدادات Django الافتراضية."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Referrer-Policy', 'same-origin')
        response.headers.setdefault(
            'Permissions-Policy',
            'geolocation=(), microphone=(), camera=(self), payment=()',
        )
        # CSP محافظة: السماح بخطوط Google فقط مع منع السكربتات المضمّنة
        response.headers.setdefault(
            'Content-Security-Policy',
            (
                "default-src 'self'; "
                "img-src 'self' data:; "
                "style-src 'self' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; "
                "script-src 'self'; "
                "connect-src 'self'; "
                "frame-ancestors 'none'; "
                "base-uri 'self'; "
                "form-action 'self'"
            ),
        )
        if not settings.DEBUG:
            response.headers.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
        return response


class RateLimitMiddleware:
    """
    حد بسيط لمعدل الطلبات في الذاكرة (مناسب لتطبيق داخلي/جهاز واحد).
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _client_ip(self, request) -> str:
        # لا نثق بـ X-Forwarded-For إلا خلف بروكسي موثوق (غير مفعّل هنا)
        return request.META.get('REMOTE_ADDR') or 'unknown'

    def _allow(self, key: str, limit: int, window_sec: int) -> bool:
        now = time.monotonic()
        bucket = self._hits[key]
        while bucket and now - bucket[0] > window_sec:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    def __call__(self, request):
        path = request.path
        ip = self._client_ip(request)

        if path.rstrip('/').endswith('sync-barcodes') and request.method == 'POST':
            limit = int(getattr(settings, 'RATE_LIMIT_SYNC_PER_HOUR', 3))
            if not self._allow(f'sync:{ip}', limit, 3600):
                return HttpResponseForbidden(
                    'تم تجاوز حد المزامنة. حاول لاحقًا.',
                    content_type='text/plain; charset=utf-8',
                )

        if request.method == 'GET' and request.GET.get('q'):
            limit = int(getattr(settings, 'RATE_LIMIT_SEARCH_PER_MINUTE', 60))
            if not self._allow(f'search:{ip}', limit, 60):
                if request.headers.get('Accept', '').startswith('application/json'):
                    return JsonResponse({'error': 'rate_limited'}, status=429)
                return HttpResponseForbidden(
                    'طلبات بحث كثيرة. انتظر دقيقة ثم أعد المحاولة.',
                    content_type='text/plain; charset=utf-8',
                )

        return self.get_response(request)
