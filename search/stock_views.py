"""تقرير إجمالي تكلفة المخزون حسب المجموعة والمخزن."""

from __future__ import annotations

import re

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .api_client import ApiClientError, aggregate_group_stock_cost
from .models import ItemBarcode, ItemGroup, ItemStockValue
from .validators import ValidationError, resolve_warehouse

_GROUP_RE = re.compile(r'^[0-9A-Za-z_\-]{1,64}$')


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
    """POST من الواجهة يجب أن يبقى JSON حتى لو حُذفت بعض الهيدرز على البروكسي."""
    if request.method == 'POST':
        return True
    return (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or 'application/json' in (request.headers.get('Accept') or '')
    )


def _page_context(*, warehouses, groups, warehouse, g_code, error='', report=None):
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
    }


@require_http_methods(['GET', 'POST'])
def stock_cost_report(request):
    wants_json = _wants_json(request)
    if not request.user.is_authenticated:
        if wants_json:
            return JsonResponse(
                {'ok': False, 'error': 'انتهت الجلسة. أعد تسجيل الدخول ثم حاول مجدداً.'},
                status=401,
            )
        return redirect_to_login(request.get_full_path())

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
        ),
    )
