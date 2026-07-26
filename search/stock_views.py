"""تقرير إجمالي تكلفة المخزون حسب المجموعة والمخزن."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .api_client import ApiClientError, aggregate_group_stock_cost
from .models import ItemBarcode
from .validators import ValidationError, resolve_warehouse


def _warehouses() -> list[dict]:
    return list(settings.EXTERNAL_API.get('WAREHOUSES') or [])


@login_required
@require_http_methods(['GET', 'POST'])
def stock_cost_report(request):
    warehouses = _warehouses()
    default_wh = settings.EXTERNAL_API.get('DEFAULT_WAREHOUSE') or '60'
    wants_json = (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or 'application/json' in (request.headers.get('Accept') or '')
    )

    try:
        warehouse = resolve_warehouse(
            request.POST.get('warehouse') if request.method == 'POST' else request.GET.get('warehouse'),
            warehouses,
            default_wh,
        )
    except ValidationError as exc:
        if wants_json:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
        warehouse = default_wh if default_wh in {w['code'] for w in warehouses} else (
            warehouses[0]['code'] if warehouses else '60'
        )
        return render(
            request,
            'search/stock_cost.html',
            {
                'warehouses': warehouses,
                'warehouse': warehouse,
                'error': str(exc),
                'report': None,
                'index_count': ItemBarcode.objects.count(),
            },
        )

    report = None
    error = ''

    if request.method == 'POST':
        try:
            report = aggregate_group_stock_cost(warehouse)
            if wants_json:
                return JsonResponse({'ok': True, 'report': report})
        except ApiClientError as exc:
            error = str(exc)
            if wants_json:
                return JsonResponse({'ok': False, 'error': error}, status=502)
        except Exception as exc:
            error = f'فشل حساب التكلفة: {exc}'
            if wants_json:
                return JsonResponse({'ok': False, 'error': error}, status=500)

    return render(
        request,
        'search/stock_cost.html',
        {
            'warehouses': warehouses,
            'warehouse': warehouse,
            'error': error,
            'report': report,
            'index_count': ItemBarcode.objects.count(),
        },
    )
