"""معالج فشل CSRF يعيد JSON لطلبات AJAX."""

from __future__ import annotations

from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import render


def csrf_failure(request, reason=''):
    wants_json = (
        request.method == 'POST'
        or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or 'application/json' in (request.headers.get('Accept') or '')
    )
    message = 'انتهت صلاحية الجلسة أو رمز الحماية. حدّث الصفحة ثم أعد المحاولة.'
    if wants_json:
        return JsonResponse({'ok': False, 'error': message}, status=403)
    try:
        return render(
            request,
            'registration/login.html',
            {'error': message},
            status=403,
        )
    except Exception:
        return HttpResponseForbidden(message, content_type='text/plain; charset=utf-8')
