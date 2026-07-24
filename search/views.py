from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .api_client import (
    ApiClientError,
    get_item_group,
    index_meta_incomplete,
    lookup_by_barcode,
    lookup_by_item_code,
    search_item_details,
    sync_barcode_index,
)
from .models import ItemBarcode
from .validators import ValidationError, resolve_warehouse, sanitize_search_query


def _warehouses() -> list[dict]:
    return list(settings.EXTERNAL_API.get('WAREHOUSES') or [])


def _items_from_prices(prices: list[dict], group_info: dict) -> list[dict]:
    """احتياطي: بناء صفوف الربط من نتائج الأسعار إن الفهرس المحلي ناقص."""
    seen = set()
    items = []
    for row in prices:
        unit = row.get('unit') or ''
        key = (row.get('code'), unit, row.get('barcode'))
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                'barcode': row.get('barcode') or '',
                'code': row.get('code') or '',
                'name': row.get('name') or '',
                'unit': unit,
                'pack_size': row.get('pack_size') or '',
                'g_code': group_info.get('g_code', ''),
                'g_name': group_info.get('g_name', ''),
                'price': '',
                'quantity': '',
            }
        )
    return items


def item_search(request):
    raw_query = request.GET.get('q')
    items = []
    prices = []
    error = ''
    searched = False
    match_type = ''
    group_info = {'g_code': '', 'g_name': ''}
    cache_count = ItemBarcode.objects.count()
    meta_incomplete = index_meta_incomplete()
    warehouses = _warehouses()
    default_wh = settings.EXTERNAL_API.get('DEFAULT_WAREHOUSE') or '60'

    try:
        query = sanitize_search_query(raw_query)
        warehouse = resolve_warehouse(
            request.GET.get('warehouse'),
            warehouses,
            default_wh,
        )
    except ValidationError as exc:
        query = (raw_query or '').strip()[:64]
        warehouse = default_wh if default_wh in {w['code'] for w in warehouses} else '60'
        searched = bool(query)
        error = str(exc)
        return render(
            request,
            'search/item_search.html',
            {
                'query': query,
                'items': items,
                'prices': prices,
                'error': error,
                'searched': searched,
                'match_type': match_type,
                'cache_count': cache_count,
                'meta_incomplete': False,
                'warehouses': warehouses,
                'warehouse': warehouse,
                'g_code': '',
                'g_name': '',
                'sync_secret_required': bool(settings.SYNC_SECRET) or not settings.DEBUG,
            },
        )

    if query:
        searched = True
        try:
            barcode_hits = lookup_by_barcode(query)
            if barcode_hits:
                match_type = 'barcode'
                item_code = barcode_hits[0]['code']
                items = lookup_by_item_code(item_code) or barcode_hits
                group_info = {
                    'g_code': items[0].get('g_code', '') or barcode_hits[0].get('g_code', ''),
                    'g_name': items[0].get('g_name', '') or barcode_hits[0].get('g_name', ''),
                }
                try:
                    prices = search_item_details(item_code, warehouse=warehouse)
                except ApiClientError:
                    prices = []
                if not any(i.get('barcode') or i.get('unit') or i.get('pack_size') for i in items):
                    items = _items_from_prices(prices, group_info) or items
            else:
                prices = search_item_details(query, warehouse=warehouse)
                if prices:
                    match_type = 'code'
                    item_code = prices[0]['code']
                    group_info = get_item_group(item_code)
                    items = lookup_by_item_code(item_code)
                    if not items:
                        items = _items_from_prices(prices, group_info)
                elif cache_count == 0:
                    error = (
                        'فهرس الباركود فارغ. اضغط «مزامنة الفهرس» مرة واحدة ثم أعد البحث.'
                    )
        except ApiClientError as exc:
            error = str(exc)

    return render(
        request,
        'search/item_search.html',
        {
            'query': query,
            'items': items,
            'prices': prices,
            'error': error,
            'searched': searched,
            'match_type': match_type,
            'cache_count': cache_count,
            'meta_incomplete': meta_incomplete,
            'warehouses': warehouses,
            'warehouse': warehouse,
            'g_code': group_info.get('g_code', ''),
            'g_name': group_info.get('g_name', ''),
            'sync_secret_required': bool(settings.SYNC_SECRET) or not settings.DEBUG,
        },
    )


@require_POST
def sync_barcodes(request):
    wants_json = (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or 'application/json' in (request.headers.get('Accept') or '')
    )

    def respond_error(message: str, status: int = 400):
        if wants_json:
            return JsonResponse({'ok': False, 'error': message}, status=status)
        messages.error(request, message)
        return redirect('item_search')

    def respond_ok(count: int):
        msg = f'تمت مزامنة {count} سجل (باركود + مجموعات) بنجاح.'
        if wants_json:
            return JsonResponse({'ok': True, 'count': count, 'message': msg})
        messages.success(request, msg)
        return redirect('item_search')

    expected = (settings.SYNC_SECRET or '').strip()
    if not expected and not settings.DEBUG:
        return respond_error('المزامنة معطّلة: عيّن SYNC_SECRET في ملف .env', status=403)

    if expected:
        provided = (request.POST.get('sync_secret') or '').strip()
        if provided != expected:
            return respond_error('رمز المزامنة غير صحيح.', status=403)

    try:
        count = sync_barcode_index()
        return respond_ok(count)
    except ApiClientError as exc:
        return respond_error(str(exc), status=502)
