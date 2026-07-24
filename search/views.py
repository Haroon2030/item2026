from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .api_client import (
    ApiClientError,
    get_item_group,
    index_meta_incomplete,
    lookup_by_barcode,
    lookup_by_item_code,
    search_item_details,
    sync_barcode_index,
)
from .debug_agent import agent_log, agent_logs_dump
from .models import ItemBarcode
from .validators import ValidationError, resolve_warehouse, sanitize_search_query


def _warehouses() -> list[dict]:
    return list(settings.EXTERNAL_API.get('WAREHOUSES') or [])


def _enrich_prices_with_match(prices: list[dict], items: list[dict], query: str) -> tuple[list[dict], dict | None]:
    """يربط باركود الوحدات من الفهرس ويضع الصف المطابق للبحث أولاً."""
    q = (query or '').strip()
    barcode_by_unit = {}
    unit_by_barcode = {}
    for row in items or []:
        unit = str(row.get('unit') or '').strip()
        barcode = str(row.get('barcode') or '').strip()
        if unit and barcode:
            barcode_by_unit.setdefault(unit, barcode)
            unit_by_barcode.setdefault(barcode, unit)

    matched_unit = unit_by_barcode.get(q, '')
    enriched = []
    for row in prices or []:
        item = dict(row)
        unit = str(item.get('unit') or '').strip()
        # فضّل باركود الفهرس المحلي لكل وحدة (أدق من GetAllPrice)
        barcode = barcode_by_unit.get(unit, '') or str(item.get('barcode') or '').strip()
        item['barcode'] = barcode
        if matched_unit:
            item['is_matched'] = unit == matched_unit
        else:
            item['is_matched'] = bool(q) and barcode == q
        enriched.append(item)

    # إن تطابقت عدة صفوف بنفس الباركود من الـ API، أبقِ الأول فقط كمطلوب
    if not matched_unit:
        seen_match = False
        for item in enriched:
            if item.get('is_matched'):
                if seen_match:
                    item['is_matched'] = False
                else:
                    seen_match = True

    enriched.sort(key=lambda r: (0 if r.get('is_matched') else 1, str(r.get('unit') or '')))
    matched = next((r for r in enriched if r.get('is_matched')), None)
    return enriched, matched


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
            # #region agent log
            agent_log(
                'A',
                'views.py:item_search',
                'lookup_start',
                {
                    'query_len': len(query),
                    'warehouse': warehouse,
                    'cache_count': cache_count,
                    'barcode_hits': len(barcode_hits),
                    'meta_incomplete': meta_incomplete,
                },
            )
            # #endregion
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
                    item_code = str(prices[0].get('code') or '').strip() or query
                    match_type = 'barcode' if item_code != query else 'code'
                    group_info = get_item_group(item_code)
                    items = lookup_by_item_code(item_code)
                    if not items:
                        items = _items_from_prices(prices, group_info)
                elif cache_count == 0:
                    error = (
                        'فهرس الباركود فارغ. اضغط «مزامنة» أو انتظر المزامنة التلقائية بعد النشر ثم أعد البحث.'
                    )
            # #region agent log
            agent_log(
                'A',
                'views.py:item_search',
                'lookup_result',
                {
                    'match_type': match_type,
                    'items_n': len(items),
                    'prices_n': len(prices),
                    'sample_item': {
                        'pack': (items[0].get('pack_size') if items else ''),
                        'g_code': (items[0].get('g_code') if items else ''),
                        'barcode': (items[0].get('barcode') if items else ''),
                        'unit': (items[0].get('unit') if items else ''),
                    }
                    if items
                    else {},
                    'sample_price': {
                        'pack': (prices[0].get('pack_size') if prices else ''),
                        'qty': (prices[0].get('quantity') if prices else ''),
                        'unit': (prices[0].get('unit') if prices else ''),
                        'barcode': (prices[0].get('barcode') if prices else ''),
                    }
                    if prices
                    else {},
                    'qty_filled': sum(1 for p in prices if str(p.get('quantity') or '').strip()),
                    'pack_filled': sum(1 for p in prices if str(p.get('pack_size') or '').strip()),
                },
            )
            # #endregion
        except ApiClientError as exc:
            error = str(exc)
            # #region agent log
            agent_log('C', 'views.py:item_search', 'api_error', {'error': str(exc)[:200]})
            # #endregion

    matched_price = None
    if prices:
        prices, matched_price = _enrich_prices_with_match(prices, items, query)
    selected_price = matched_price or (prices[0] if prices else None)

    return render(
        request,
        'search/item_search.html',
        {
            'query': query,
            'items': items,
            'prices': prices,
            'matched_price': matched_price,
            'selected_price': selected_price,
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
        # #region agent log
        with_pack = ItemBarcode.objects.exclude(pack_size='').count()
        with_g = ItemBarcode.objects.exclude(g_code='').count()
        agent_log(
            'A',
            'views.py:sync_barcodes',
            'sync_done',
            {'count': count, 'with_pack': with_pack, 'with_g': with_g},
        )
        # #endregion
        return respond_ok(count)
    except ApiClientError as exc:
        # #region agent log
        agent_log('A', 'views.py:sync_barcodes', 'sync_error', {'error': str(exc)[:200]})
        # #endregion
        return respond_error(str(exc), status=502)


@csrf_exempt
@require_POST
def agent_debug_ingest(request):
    """Same-origin ingest for browser debug logs (CSP connect-src self)."""
    import json

    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        body = {}
    agent_log(
        str(body.get('hypothesisId') or 'D'),
        str(body.get('location') or 'browser'),
        str(body.get('message') or 'event'),
        body.get('data') if isinstance(body.get('data'), dict) else {},
    )
    return JsonResponse({'ok': True})


@require_GET
def agent_debug_dump(request):
    return JsonResponse({'ok': True, 'logs': agent_logs_dump()})
