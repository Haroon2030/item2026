"""تقرير إجمالي تكلفة المخزون حسب المجموعة والمخزن."""

from __future__ import annotations

import json
import re
import time
import traceback
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .api_client import ApiClientError, aggregate_group_stock_cost
from .models import ItemBarcode, ItemGroup, ItemStockValue
from .validators import ValidationError, resolve_warehouse

# #region agent log
_DEBUG_LOG = Path(settings.BASE_DIR) / 'debug-5b001b.log'


def _dbg(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    try:
        payload = {
            'sessionId': '5b001b',
            'runId': 'pre-fix',
            'hypothesisId': hypothesis_id,
            'location': location,
            'message': message,
            'data': data or {},
            'timestamp': int(time.time() * 1000),
        }
        with _DEBUG_LOG.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + '\n')
    except Exception:
        pass


# #endregion

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


def _page_context(*, warehouses, groups, warehouse, g_code, error='', report=None):
    cache_hint = ''
    latest = (
        ItemStockValue.objects.filter(warehouse=warehouse)
        .order_by('-updated_at')
        .values_list('updated_at', flat=True)
        .first()
    )
    if latest:
        cache_hint = latest.strftime('%Y-%m-%d %H:%M')
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


@login_required
@require_http_methods(['GET', 'POST'])
def stock_cost_report(request):
    warehouses = _warehouses()
    groups = _groups()
    default_wh = settings.EXTERNAL_API.get('DEFAULT_WAREHOUSE') or '60'
    wants_json = (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or 'application/json' in (request.headers.get('Accept') or '')
    )
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
        # #region agent log
        _dbg(
            'C',
            'stock_views.py:POST',
            'stock_cost_post_enter',
            {
                'warehouse': warehouse,
                'g_code': g_code,
                'refresh': refresh,
                'wants_json': wants_json,
                'action': str(data.get('action') or ''),
            },
        )
        # #endregion
        try:
            if refresh and not g_code:
                raise ApiClientError(
                    'لتحديث من النظام اختر مجموعة واحدة أولاً. '
                    'بعدها يمكن عرض كل المجموعات من التخزين بسرعة.'
                )
            started = time.monotonic()
            report = aggregate_group_stock_cost(
                warehouse,
                g_code=g_code or None,
                refresh=refresh,
            )
            # #region agent log
            _dbg(
                'D',
                'stock_views.py:success',
                'stock_cost_ok',
                {
                    'elapsed': round(time.monotonic() - started, 2),
                    'source': (report or {}).get('source'),
                    'rows': len((report or {}).get('rows') or []),
                    'grand_total': (report or {}).get('grand_total'),
                },
            )
            # #endregion
            if wants_json:
                return JsonResponse({'ok': True, 'report': report})
        except ApiClientError as exc:
            error = str(exc)
            # #region agent log
            _dbg('B', 'stock_views.py:ApiClientError', 'api_client_error', {'error': error})
            # #endregion
            if wants_json:
                return JsonResponse({'ok': False, 'error': error}, status=502)
        except Exception as exc:
            error = f'فشل حساب التكلفة: {exc}'
            # #region agent log
            _dbg(
                'E',
                'stock_views.py:Exception',
                'unhandled_exception',
                {
                    'error': str(exc),
                    'type': type(exc).__name__,
                    'trace': traceback.format_exc()[-1500:],
                },
            )
            # #endregion
            if wants_json:
                return JsonResponse({'ok': False, 'error': error}, status=500)

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
