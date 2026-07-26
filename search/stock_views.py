"""تقرير إجمالي تكلفة المخزون حسب المجموعة والمخزن."""

from __future__ import annotations

import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.views import redirect_to_login
from django.core import signing
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .api_client import ApiClientError, aggregate_group_stock_cost
from .models import ItemBarcode, ItemGroup, ItemStockValue
from .validators import ValidationError, resolve_warehouse

_GROUP_RE = re.compile(r'^[0-9A-Za-z_\-]{1,64}$')
_TOKEN_SALT = 'stock-cost-v1'


def _warehouses() -> list[dict]:
    return list(settings.EXTERNAL_API.get('WAREHOUSES') or [])


def _groups() -> list[dict]:
    return [
        {'code': g.g_code, 'name': g.g_name or g.g_code}
        for g in ItemGroup.objects.order_by('g_code').only('g_code', 'g_name')
    ]


def _resolve_group(raw: str | None) -> str:
    selected = (raw or '').strip()
    if not selected:
        return ''
    if not _GROUP_RE.match(selected):
        raise ValidationError('المجموعة المحددة غير صالحة.')
    return selected


def _wants_refresh(data) -> bool:
    action = str(data.get('action') or '').strip().lower()
    if action in {'refresh', 'live', 'sync'}:
        return True
    refresh = str(data.get('refresh') or '').strip().lower()
    return refresh in {'1', 'true', 'yes', 'on'}


def _wants_json(request) -> bool:
    if request.method == 'POST':
        return True
    return (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or 'application/json' in (request.headers.get('Accept') or '')
    )


def _make_stock_token(user) -> str:
    return signing.dumps(
        {'uid': int(user.pk), 'u': str(user.get_username())},
        salt=_TOKEN_SALT,
    )


def _user_from_stock_token(raw: str | None):
    token = (raw or '').strip()
    if not token:
        return None
    max_age = int(getattr(settings, 'SESSION_COOKIE_AGE', 28800) or 28800)
    try:
        payload = signing.loads(token, salt=_TOKEN_SALT, max_age=max_age)
        uid = int(payload.get('uid'))
    except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError, AttributeError):
        return None
    return get_user_model().objects.filter(pk=uid, is_active=True).first()


def _resolve_stock_user(request):
    """جلسة عادية، أو رمز صفحة موقّع إن لم تُرسل كعكة الجلسة مع fetch."""
    if request.user.is_authenticated:
        return request.user
    if request.method == 'POST':
        return _user_from_stock_token(request.POST.get('stock_token'))
    return None


def _page_context(*, warehouses, groups, warehouse, g_code, error='', report=None, stock_token=''):
    cache_hint = ''
    try:
        latest = (
            ItemStockValue.objects.filter(warehouse=warehouse)
            .order_by('-updated_at')
            .values_list('updated_at', flat=True)
            .first()
        )
        if latest:
            cache_hint = latest.strftime('%Y-%m-%d %H:%M')
    except Exception:
        cache_hint = ''
    return {
        'warehouses': warehouses,
        'groups': groups,
        'warehouse': warehouse,
        'g_code': g_code,
        'error': error,
        'report': report,
        'index_count': ItemBarcode.objects.count(),
        'cache_updated_display': cache_hint,
        'stock_token': stock_token,
    }


@require_http_methods(['GET', 'POST'])
def stock_cost_report(request):
    wants_json = _wants_json(request)
    user = _resolve_stock_user(request)
    if user is None:
        if wants_json:
            return JsonResponse(
                {
                    'ok': False,
                    'error': 'تعذّر التحقق من الهوية. حدّث الصفحة ثم أعد المحاولة.',
                },
                status=401,
            )
        return redirect_to_login(request.get_full_path())

    stock_token = _make_stock_token(user)
    warehouses = _warehouses()
    groups = _groups()
    default_wh = settings.EXTERNAL_API.get('DEFAULT_WAREHOUSE') or '60'
    data = request.POST if request.method == 'POST' else request.GET

    try:
        warehouse = resolve_warehouse(
            data.get('warehouse'),
            warehouses,
            default_wh,
        )
        g_code = _resolve_group(data.get('g_code'))
    except ValidationError as exc:
        if wants_json:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
        warehouse = default_wh if default_wh in {w['code'] for w in warehouses} else (
            warehouses[0]['code'] if warehouses else '60'
        )
        return render(
            request,
            'search/stock_cost.html',
            _page_context(
                warehouses=warehouses,
                groups=groups,
                warehouse=warehouse,
                g_code='',
                error=str(exc),
                stock_token=stock_token,
            ),
        )

    report = None
    error = ''

    if request.method == 'POST':
        refresh = _wants_refresh(data)
        try:
            if refresh and not g_code:
                raise ApiClientError(
                    'لتحديث من النظام اختر مجموعة واحدة أولاً. '
                    'بعدها يمكن عرض كل المجموعات من التخزين بسرعة.'
                )
            report = aggregate_group_stock_cost(
                warehouse,
                g_code=g_code or None,
                refresh=refresh,
            )
            return JsonResponse({'ok': True, 'report': report})
        except ApiClientError as exc:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=502)
        except Exception as exc:
            return JsonResponse(
                {'ok': False, 'error': f'فشل حساب التكلفة: {exc}'},
                status=500,
            )

    return render(
        request,
        'search/stock_cost.html',
        _page_context(
            warehouses=warehouses,
            groups=groups,
            warehouse=warehouse,
            g_code=g_code,
            error=error,
            report=report,
            stock_token=stock_token,
        ),
    )
