"""طبقات أمان: هيدرز + حد معدل الطلبات."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse

from .validators import scan_request_for_sql_injection


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
        # السماح بـ media من نفس المصدر للكاميرا
        response.headers.setdefault(
            'Content-Security-Policy',
            (
                "default-src 'self'; "
                "img-src 'self' data: blob:; "
                "media-src 'self' blob:; "
                "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
                "font-src 'self' https://fonts.gstatic.com; "
                "script-src 'self'; "
                "connect-src 'self'; "
                "worker-src 'self' blob:; "
                "frame-ancestors 'none'; "
                "base-uri 'self'; "
                "form-action 'self'"
            ),
        )
        if not settings.DEBUG:
            response.headers.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
        return response


class SqlInjectionGuardMiddleware:
    """يرفض الطلبات التي تحمل أنماط حقن SQL في المدخلات (دفاع إضافي فوق ORM)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        bad_field = scan_request_for_sql_injection(request)
        if bad_field:
            wants_json = (
                request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                or 'application/json' in (request.headers.get('Accept') or '')
            )
            message = 'طلب مرفوض: محتوى غير مسموح.'
            if wants_json:
                return JsonResponse({'ok': False, 'error': message}, status=400)
            return HttpResponseForbidden(
                message,
                content_type='text/plain; charset=utf-8',
            )
        return self.get_response(request)


class RateLimitMiddleware:
    """
    حد بسيط لمعدل الطلبات في الذاكرة (مناسب لتطبيق داخلي/جهاز واحد).
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _client_ip(self, request) -> str:
        # خلف Dokploy/Nginx: أول IP في X-Forwarded-For هو العميل الحقيقي
        if getattr(settings, 'USE_X_FORWARDED_HOST', False) or settings.SECURE_PROXY_SSL_HEADER:
            forwarded = (request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',')
            if forwarded and forwarded[0].strip():
                return forwarded[0].strip()
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

    def _login_blocked_response(self, request):
        wants_json = 'application/json' in (request.headers.get('Accept') or '')
        message = 'محاولات دخول كثيرة. انتظر دقيقة ثم أعد المحاولة.'
        if wants_json:
            return JsonResponse({'error': message}, status=429)
        html = (
            '<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>تم الإيقاف مؤقتاً</title>'
            '<style>body{font-family:Tahoma,sans-serif;background:#eef3f9;display:grid;'
            'place-items:center;min-height:100vh;margin:0}'
            '.card{background:#fff;border:1px solid #c9d8ea;border-radius:14px;'
            'padding:1.5rem;max-width:420px;text-align:center;box-shadow:0 8px 24px rgba(29,79,145,.08)}'
            'a{color:#1d4f91;font-weight:700}</style></head><body><div class="card">'
            '<h1>تم الإيقاف مؤقتاً</h1>'
            f'<p>{message}</p>'
            '<p><a href="/login/">العودة لتسجيل الدخول</a></p>'
            '</div></body></html>'
        )
        return HttpResponse(html, status=429, content_type='text/html; charset=utf-8')

    def __call__(self, request):
        path = request.path
        ip = self._client_ip(request)

        if path.rstrip('/').endswith('login') and request.method == 'POST':
            # حد مرن: 20 محاولة / 2 دقيقة
            limit = int(getattr(settings, 'RATE_LIMIT_LOGIN_PER_10_MINUTES', 20))
            window = int(getattr(settings, 'RATE_LIMIT_LOGIN_WINDOW_SECONDS', 120) or 120)
            if not self._allow(f'login:{ip}', limit, window):
                return self._login_blocked_response(request)

        if path.rstrip('/').endswith('sync-barcodes') and request.method == 'POST':
            limit = int(getattr(settings, 'RATE_LIMIT_SYNC_PER_HOUR', 10))
            if not self._allow(f'sync:{ip}', limit, 3600):
                wants_json = (
                    request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                    or 'application/json' in (request.headers.get('Accept') or '')
                )
                if wants_json:
                    return JsonResponse(
                        {'ok': False, 'error': 'تم تجاوز حد المزامنة. حاول لاحقاً.'},
                        status=429,
                    )
                return HttpResponseForbidden(
                    'تم تجاوز حد المزامنة. حاول لاحقًا.',
                    content_type='text/plain; charset=utf-8',
                )

        if path.rstrip('/').endswith('stock-cost') and request.method == 'POST':
            # لا تعتمد على request.POST هنا؛ الهيدر كافٍ لتمييز التحديث
            action_hdr = str(request.headers.get('X-Stock-Cost-Action') or '').strip().lower()
            is_refresh = action_hdr in {'refresh', 'live', 'sync'}
            wants_json = (
                request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                or 'application/json' in (request.headers.get('Accept') or '')
                or True
            )
            if is_refresh:
                limit = int(getattr(settings, 'RATE_LIMIT_STOCK_COST_PER_HOUR', 8))
                key, window, message = (
                    f'stockcost-refresh:{ip}',
                    3600,
                    'تم تجاوز حد تحديث تكلفة المخزون من النظام. حاول لاحقاً.',
                )
            else:
                limit = int(getattr(settings, 'RATE_LIMIT_STOCK_COST_VIEW_PER_MINUTE', 60))
                key, window, message = (
                    f'stockcost-view:{ip}',
                    60,
                    'طلبات عرض كثيرة. انتظر قليلاً ثم أعد المحاولة.',
                )
            if not self._allow(key, limit, window):
                if wants_json:
                    return JsonResponse({'ok': False, 'error': message}, status=429)
                return HttpResponseForbidden(
                    message,
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
