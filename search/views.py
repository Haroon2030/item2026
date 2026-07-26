from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from .api_client import (
    ApiClientError,
    get_item_group,
    index_meta_incomplete,
    lookup_by_barcode,
    lookup_by_item_code,
    lookup_by_name,
    search_item_details,
    sync_barcode_index,
)
from .models import ItemBarcode
from .validators import ValidationError, looks_like_item_code, resolve_warehouse, sanitize_search_query


def _warehouses() -> list[dict]:
    return list(settings.EXTERNAL_API.get('WAREHOUSES') or [])


def _enrich_prices_with_match(prices: list[dict], items: list[dict], query: str) -> tuple[list[dict], dict | None]:
    """يربط باركود الوحدات من الفهرس ويختار الصف المطابق (باركود أو وحدة الرصيد/التكلفة)."""
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

    matched = next((r for r in enriched if r.get('is_matched')), None)

    # بحث برقم الصنف (بدون باركود وحدة): فضّل الوحدة المخزنية الحقيقية من GetItemQtyCost
    # (هي التي يرجع بها النظام الرصيد/التكلفة مباشرة = كيلو للأصناف الوزنية)
    # الرصيد محوّل على كل الوحدات، لذا لا نعتمد على وجود الرصيد بل على is_stock_unit
    if not matched and enriched:
        preferred = None
        # 1) الوحدة المخزنية الحقيقية (رصيد + تكلفة من الـ API مباشرة)
        for row in enriched:
            if row.get('is_stock_unit'):
                preferred = row
                break
        # 2) وحدة عليها تكلفة فعلية (تكلفة تظهر فقط للوحدة المخزنية)
        if not preferred:
            for row in enriched:
                if str(row.get('avg_cost') or '').strip():
                    preferred = row
                    break
        # 3) وحدة كيلو بالاسم
        if not preferred:
            for row in enriched:
                unit = str(row.get('unit') or '')
                if 'كيلو' in unit or unit.lower() in {'kg', 'kilo'}:
                    preferred = row
                    break
        if preferred:
            for row in enriched:
                row['is_matched'] = row is preferred
            matched = preferred

    enriched.sort(key=lambda r: (0 if r.get('is_matched') else 1, str(r.get('unit') or '')))
    matched = next((r for r in enriched if r.get('is_matched')), matched)

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


@login_required
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
            code_hits = [] if barcode_hits else lookup_by_item_code(query)
            name_hits = [] if barcode_hits or code_hits else lookup_by_name(query)

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
            elif code_hits:
                # بحث مباشر برقم الصنف من الفهرس المحلي
                match_type = 'code'
                item_code = code_hits[0]['code']
                items = code_hits
                group_info = {
                    'g_code': items[0].get('g_code', ''),
                    'g_name': items[0].get('g_name', ''),
                }
                try:
                    prices = search_item_details(item_code, warehouse=warehouse)
                except ApiClientError:
                    prices = []
                if not any(i.get('barcode') or i.get('unit') or i.get('pack_size') for i in items):
                    items = _items_from_prices(prices, group_info) or items
            elif len(name_hits) == 1:
                # اسم واحد مطابق → اعرض تفاصيله كصنف محدد
                match_type = 'name'
                item_code = name_hits[0]['code']
                items = lookup_by_item_code(item_code) or name_hits
                group_info = {
                    'g_code': items[0].get('g_code', ''),
                    'g_name': items[0].get('g_name', ''),
                }
                try:
                    prices = search_item_details(item_code, warehouse=warehouse)
                except ApiClientError:
                    prices = []
                if not any(i.get('barcode') or i.get('unit') or i.get('pack_size') for i in items):
                    items = _items_from_prices(prices, group_info) or items
            elif len(name_hits) > 1:
                # عدة أسماء → قائمة اختيار
                match_type = 'name_list'
                items = name_hits
                prices = []
            elif looks_like_item_code(query):
                # احتياطي: إرسال النص للنظام كرقم صنف فقط
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
            elif cache_count == 0:
                error = (
                    'فهرس الباركود فارغ. اضغط «مزامنة» أولاً ثم ابحث بالاسم أو الباركود.'
                )
        except ApiClientError as exc:
            error = str(exc)

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


@login_required
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
    except Exception as exc:
        # أي فشل قاعدة بيانات/مزامنة يجب أن يعود JSON للواجهة لا صفحة HTML
        return respond_error(f'فشلت المزامنة: {exc}', status=500)
