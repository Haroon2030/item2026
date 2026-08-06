from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .api_client import (
    ApiClientError,
    compare_item_across_warehouses,
    compute_inventory_stock_cost,
    enrich_group_browse,
    get_item_group,
    index_meta_incomplete,
    list_groups,
    lookup_by_barcode,
    lookup_by_group,
    lookup_by_item_code,
    lookup_by_name,
    search_item_details,
    sync_barcode_index,
)
from .models import ItemBarcode
from .validators import ValidationError, looks_like_item_code, resolve_group, resolve_warehouse, sanitize_search_query

logger = logging.getLogger(__name__)


def _fetch_suppliers_safe(item_code: str) -> list[dict]:
    """جلب موردي الصنف من أوراكل (قراءة فقط) دون تعطيل نتيجة البحث عند الفشل."""
    code = str(item_code or '').strip()
    if not code:
        return []
    try:
        from .oracle_stock import fetch_item_suppliers, oracle_enabled

        if not oracle_enabled():
            return []
        return fetch_item_suppliers(code)
    except Exception as exc:  # noqa: BLE001
        logger.warning('Item suppliers skipped: %s', exc)
        return []


def _warehouses() -> list[dict]:
    """قائمة المخازن: من أوراكل بالاسم الحقيقي، مع الرجوع لإعدادات WAREHOUSES عند التعذر."""
    fallback = [
        {
            'code': str(w.get('code') or '').strip(),
            'name': str(w.get('name') or w.get('code') or '').strip(),
        }
        for w in (settings.EXTERNAL_API.get('WAREHOUSES') or [])
        if str(w.get('code') or '').strip()
    ]
    try:
        from .oracle_stock import fetch_warehouse_options, oracle_enabled

        if not oracle_enabled():
            return fallback
        rows = fetch_warehouse_options(active_only=True)
        if not rows:
            return fallback
        out: list[dict] = []
        for w in rows:
            code = str(w.get('code') or '').strip()
            if not code:
                continue
            raw_name = str(w.get('name') or '').strip()
            if raw_name and raw_name != code:
                # أزل تكرار رقم المخزن إن وُجد داخل الاسم
                suffix = f'({code})'
                if raw_name.endswith(suffix):
                    raw_name = raw_name[: -len(suffix)].strip()
                label = f'{raw_name} - {code}'
            else:
                label = f'مخزن {code}'
            out.append({'code': code, 'name': label})
        return out or fallback
    except Exception as exc:  # noqa: BLE001
        logger.warning('Warehouse list from Oracle failed: %s', exc)
        return fallback


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
    warehouse_compare: list[dict] = []
    scope_raw = str(request.GET.get('scope') or 'one').strip().lower()
    raw_wh = str(request.GET.get('warehouse') or '').strip()
    # توافق مع الروابط القديمة warehouse=all
    compare_mode = scope_raw in ('compare', 'all') or raw_wh == 'all'
    if raw_wh == 'all':
        raw_wh = ''

    try:
        query = sanitize_search_query(raw_query)
        warehouse = resolve_warehouse(
            raw_wh or None,
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
                'detail_warehouse': warehouse,
                'compare_mode': compare_mode,
                'warehouse_compare': [],
                'g_code': '',
                'g_name': '',
                'sync_secret_required': bool(settings.SYNC_SECRET) or not settings.DEBUG,
            },
        )

    detail_wh = warehouse
    if detail_wh not in {w['code'] for w in warehouses}:
        detail_wh = next((w['code'] for w in warehouses), '60')

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
                if not compare_mode:
                    try:
                        prices = search_item_details(item_code, warehouse=detail_wh)
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
                if not compare_mode:
                    try:
                        prices = search_item_details(item_code, warehouse=detail_wh)
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
                if not compare_mode:
                    try:
                        prices = search_item_details(item_code, warehouse=detail_wh)
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
                if compare_mode:
                    # في وضع المقارنة نكتفي برقم الاستعلام ونترك المقارنة تجلب التفاصيل
                    match_type = 'code'
                    item_code = query
                    group_info = get_item_group(item_code)
                    items = lookup_by_item_code(item_code) or [
                        {'code': item_code, 'name': '', 'barcode': '', 'unit': ''}
                    ]
                else:
                    prices = search_item_details(query, warehouse=detail_wh)
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

    if compare_mode and match_type != 'name_list' and not error:
        compare_code = ''
        if selected_price:
            compare_code = str(selected_price.get('code') or '').strip()
        if not compare_code and items:
            compare_code = str(items[0].get('code') or '').strip()
        if not compare_code and query:
            compare_code = str(query).strip()
        if compare_code:
            name_map = {str(w['code']): str(w['name']) for w in warehouses}
            try:
                warehouse_compare = compare_item_across_warehouses(
                    compare_code,
                    warehouse_names=name_map,
                )
                logger.info(
                    'warehouse compare code=%s rows=%s ok=%s',
                    compare_code,
                    len(warehouse_compare),
                    sum(1 for r in warehouse_compare if r.get('ok')),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning('Warehouse compare failed: %s', exc)
                warehouse_compare = []

    suppliers: list[dict] = []
    if match_type != 'name_list' and (selected_price or items or warehouse_compare):
        supplier_code = ''
        if selected_price:
            supplier_code = str(selected_price.get('code') or '').strip()
        if not supplier_code and items:
            supplier_code = str(items[0].get('code') or '').strip()
        if not supplier_code and warehouse_compare:
            supplier_code = str(warehouse_compare[0].get('code') or '').strip()
        suppliers = _fetch_suppliers_safe(supplier_code)

    return render(
        request,
        'search/item_search.html',
        {
            'query': query,
            'items': items,
            'prices': prices,
            'matched_price': matched_price,
            'selected_price': selected_price,
            'suppliers': suppliers,
            'error': error,
            'searched': searched,
            'match_type': match_type,
            'cache_count': cache_count,
            'meta_incomplete': meta_incomplete,
            'warehouses': warehouses,
            'warehouse': warehouse,
            'detail_warehouse': detail_wh,
            'compare_mode': compare_mode,
            'warehouse_compare': warehouse_compare,
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


def _parse_qty(value) -> float | None:
    if value is None or value == '':
        return None
    try:
        return float(str(value).replace(',', '').strip())
    except ValueError:
        return None


def _priced_items_for_group(
    warehouse: str, group_code: str, all_items: list[dict]
) -> tuple[list[dict], dict[str, int], str]:
    """
    مسار التصفح الدقيق: أصناف بكمية > 0 + كاش فقط عند اكتمال الجلب.
    """
    qty_src = (getattr(settings, 'STOCK_QTY_SOURCE', 'api') or 'api').strip().lower()
    cache_key = f'browse_stocked:v14:{qty_src}:{warehouse}:{group_code}:{len(all_items)}'
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and 'stocked' in cached and cached.get('counts', {}).get('complete'):
        return cached['stocked'], cached.get('counts') or {}, ''

    error = ''
    stocked: list[dict] = []
    counts: dict[str, int] = {
        'catalog_count': len(all_items),
        'stocked_count': 0,
        'zero_count': 0,
        'fetch_failed': 0,
        'complete': False,
    }
    try:
        stocked, counts = enrich_group_browse(
            all_items,
            warehouse,
            max_workers=20,
            group_code=group_code,
        )
        if counts.get('complete') and (
            counts.get('stocked_count', 0) > 0 or counts.get('zero_count', 0) > 0
        ):
            cache.set(cache_key, {'stocked': stocked, 'counts': counts}, 900)
        elif counts.get('fetch_failed', 0) > 0:
            error = (
                f'الجلب غير مكتمل: تعذّر {counts["fetch_failed"]} صنف — '
                'الإجمالي أدناه غير معتمد حتى يكتمل الجلب. أعد التحميل.'
            )
        elif all_items and counts.get('stocked_count', 0) == 0 and counts.get('zero_count', 0) == 0:
            error = 'نظام أونكس لا يستجيب حالياً — الكميات غير متاحة مؤقتاً. أعد المحاولة بعد قليل.'
    except Exception as exc:  # noqa: BLE001
        error = f'تعذّر جلب الكميات: {exc}'
    return stocked, counts, error


@login_required
@require_GET
@never_cache
def browse_groups(request):
    """اختيار مجموعة ومخزن ثم عرض أصناف المجموعة (بكمية فقط)."""
    warehouses = _warehouses()
    default_wh = settings.EXTERNAL_API.get('DEFAULT_WAREHOUSE') or '60'
    groups = list_groups()
    items = []
    page_obj = None
    error = ''
    browsed = False
    group_code = ''
    group_name = ''
    total_count = 0
    catalog_count = 0
    zero_excluded = 0
    missing_qty = 0
    fetch_failed = 0
    stock_cost_total = ''
    stock_cost_used = 0
    stock_cost_skipped = 0
    fetch_complete = False
    qty_source = ''

    empty_ctx = {
        'warehouses': warehouses,
        'groups': groups,
        'items': items,
        'page_obj': page_obj,
        'total_count': 0,
        'catalog_count': 0,
        'zero_excluded': 0,
        'missing_qty': 0,
        'fetch_failed': 0,
        'stock_cost_total': '',
        'stock_cost_used': 0,
        'stock_cost_skipped': 0,
        'fetch_complete': False,
        'qty_source': '',
        'browsed': False,
    }

    try:
        warehouse = resolve_warehouse(
            request.GET.get('warehouse'),
            warehouses,
            default_wh,
        )
        submitted = 'group' in request.GET
        group_code = resolve_group(request.GET.get('group'), groups, required=submitted)
    except ValidationError as exc:
        warehouse = default_wh if default_wh in {w['code'] for w in warehouses} else '60'
        group_code = (request.GET.get('group') or '').strip()[:64]
        return render(
            request,
            'search/browse_groups.html',
            {
                **empty_ctx,
                'warehouse': warehouse,
                'group': group_code,
                'group_name': '',
                'error': str(exc),
            },
        )

    if group_code:
        browsed = True
        group_name = next(
            (g['g_name'] for g in groups if g['g_code'] == group_code),
            group_code,
        )
        all_items = lookup_by_group(group_code)
        stocked, counts, error = _priced_items_for_group(warehouse, group_code, all_items)

        catalog_count = counts.get('catalog_count', len(all_items))
        total_count = counts.get('stocked_count', len(stocked))
        zero_excluded = counts.get('zero_count', 0)
        fetch_failed = counts.get('fetch_failed', 0)
        missing_qty = fetch_failed
        fetch_complete = bool(counts.get('complete'))
        qty_source = str(counts.get('qty_source') or '')

        stock = compute_inventory_stock_cost(stocked)
        stock_cost_total = stock['total']
        stock_cost_used = stock['used_count']
        stock_cost_skipped = stock['skipped_count']
        if fetch_failed and not error:
            error = (
                f'الجلب غير مكتمل: تعذّر {fetch_failed} صنف — '
                'الإجمالي غير معتمد. أعد التحميل.'
            )

        paginator = Paginator(stocked, 10)
        try:
            page_obj = paginator.page(request.GET.get('page') or 1)
        except PageNotAnInteger:
            page_obj = paginator.page(1)
        except EmptyPage:
            page_obj = paginator.page(paginator.num_pages)
        items = list(page_obj.object_list)

    return render(
        request,
        'search/browse_groups.html',
        {
            'warehouses': warehouses,
            'warehouse': warehouse,
            'groups': groups,
            'group': group_code,
            'group_name': group_name,
            'items': items,
            'page_obj': page_obj,
            'total_count': total_count,
            'catalog_count': catalog_count,
            'zero_excluded': zero_excluded,
            'missing_qty': missing_qty,
            'fetch_failed': fetch_failed,
            'stock_cost_total': stock_cost_total,
            'stock_cost_used': stock_cost_used,
            'stock_cost_skipped': stock_cost_skipped,
            'fetch_complete': fetch_complete,
            'qty_source': qty_source,
            'error': error,
            'browsed': browsed,
        },
    )


def _parse_sales_dates(raw_from: str | None, raw_to: str | None):
    """يحوّل تواريخ النموذج إلى date؛ الافتراضي الشهر الحالي."""
    from datetime import date, datetime

    today = date.today()
    month_start = today.replace(day=1)

    def parse_one(raw: str | None, fallback: date) -> date:
        text = (raw or '').strip()
        if not text:
            return fallback
        try:
            return datetime.strptime(text[:10], '%Y-%m-%d').date()
        except ValueError as exc:
            raise ValidationError('صيغة التاريخ غير صحيحة. استخدم YYYY-MM-DD.') from exc

    d_from = parse_one(raw_from, month_start)
    d_to = parse_one(raw_to, today)
    if d_from > d_to:
        raise ValidationError('تاريخ البداية يجب أن يكون قبل تاريخ النهاية أو مساوياً له.')
    # حد أقصى معقول لتفادي استعلام ضخم
    if (d_to - d_from).days > 366:
        raise ValidationError('الفترة القصوى سنة واحدة.')
    return d_from, d_to


def _format_sales_metric_rows(rows):
    for row in rows:
        row['net_total_display'] = f"{float(row.get('net_total') or 0):,.2f}"
        row['vat_total_display'] = f"{float(row.get('vat_total') or 0):,.2f}"
        row['sales_total_display'] = f"{float(row.get('sales_total') or 0):,.2f}"
        row['gross_total_display'] = f"{float(row.get('gross_total') or 0):,.2f}"
        row['avg_basket_display'] = f"{float(row.get('avg_basket') or 0):,.2f}"
        row['invoice_count_display'] = f"{int(row.get('invoice_count') or 0):,}"
        row['return_count_display'] = f"{int(row.get('return_count') or 0):,}"


def _build_sales_system(
    key,
    label,
    date_from,
    date_to,
    requested_user,
    branch_code='',
    *,
    light=False,
):
    """يجمع إجماليات الفروع (أو يومي لفرع) وأعلى المستخدمين لنظام بيع واحد."""
    from .oracle_stock import (
        fetch_branch_daily_sales_totals,
        fetch_sales_mst_bundle,
        fetch_user_invoice_details,
    )

    bundle = fetch_sales_mst_bundle(
        date_from,
        date_to,
        system=key,
        light=light,
        top_users_limit=8,
    )
    branch_rows = bundle.get('branches') or []
    branches = [
        {
            'code': str(r.get('branch_code') or ''),
            'name': str(r.get('branch_name') or ''),
        }
        for r in branch_rows
        if r.get('branch_code')
    ]
    branches_by_code = {b['code']: b for b in branches}
    selected_branch = str(branch_code or '').strip()
    if selected_branch and selected_branch not in branches_by_code:
        selected_branch = ''

    daily_mode = bool(selected_branch) and not light
    if daily_mode:
        rows = fetch_branch_daily_sales_totals(
            date_from,
            date_to,
            selected_branch,
            system=key,
        )
        selected_branch_name = branches_by_code[selected_branch]['name']
    else:
        rows = branch_rows
        selected_branch_name = ''

    _format_sales_metric_rows(rows)
    top_users: list[dict] = []
    if not light:
        if selected_branch:
            from .oracle_stock import fetch_top_sales_users

            top_users = fetch_top_sales_users(
                date_from,
                date_to,
                system=key,
                branch_code=selected_branch,
                limit=8,
            )
        else:
            top_users = list(bundle.get('top_users') or [])
        for user in top_users:
            user['net_total_display'] = f"{float(user.get('net_total') or 0):,.2f}"
            user['sales_total_display'] = f"{float(user.get('sales_total') or 0):,.2f}"

    users_by_code = {str(u.get('user_code') or ''): u for u in top_users}
    selected_code = requested_user if requested_user in users_by_code else ''
    selected_name = ''
    invoice_groups: list[dict] = []
    if selected_code and not light:
        selected_name = str(users_by_code[selected_code].get('user_name') or '')
        invoice_groups = fetch_user_invoice_details(
            date_from,
            date_to,
            selected_code,
            system=key,
        )
        for group in invoice_groups:
            group['sales_total_display'] = (
                f"{float(group.get('sales_total') or 0):,.2f}"
            )
            for invoice in group.get('invoices') or []:
                invoice['bill_total_display'] = (
                    f"{float(invoice.get('bill_total') or 0):,.2f}"
                )

    grand_net = sum(float(r.get('net_total') or 0) for r in rows)
    grand_vat = sum(float(r.get('vat_total') or 0) for r in rows)
    grand_sales = sum(float(r.get('sales_total') or 0) for r in rows)
    grand_gross = sum(float(r.get('gross_total') or 0) for r in rows)
    grand_invoices = sum(int(r.get('invoice_count') or 0) for r in rows)
    grand_returns = sum(int(r.get('return_count') or 0) for r in rows)
    grand_avg = round(grand_sales / grand_invoices, 2) if grand_invoices else 0.0

    top_visit_branch = ''
    top_visit_invoices = 0
    top_visit_invoices_display = '0'
    if branch_rows:
        best = max(branch_rows, key=lambda r: int(r.get('invoice_count') or 0))
        top_visit_invoices = int(best.get('invoice_count') or 0)
        if top_visit_invoices > 0:
            top_visit_branch = str(
                best.get('branch_name') or best.get('branch_code') or ''
            )
            top_visit_invoices_display = f'{top_visit_invoices:,}'

    seller_count = int(bundle.get('seller_count') or 0)
    device_count = int(bundle.get('device_count') or 0)

    chart_branches: list[dict] = []
    if not light and branch_rows:
        ranked = sorted(
            branch_rows,
            key=lambda r: float(r.get('return_total') or 0),
            reverse=True,
        )
        ranked = [r for r in ranked if float(r.get('return_total') or 0) > 0][:15]
        total = sum(float(r.get('return_total') or 0) for r in ranked)
        assigned = 0.0
        for idx, row in enumerate(ranked):
            amount = float(row.get('return_total') or 0)
            inv = int(row.get('return_count') or 0)
            if not total:
                pct = 0.0
            elif idx == len(ranked) - 1:
                pct = round(max(0.0, 100.0 - assigned), 1)
            else:
                pct = round((amount / total) * 100.0, 1)
                assigned += pct
            chart_branches.append(
                {
                    'branch_code': str(row.get('branch_code') or ''),
                    'branch_name': str(
                        row.get('branch_name') or row.get('branch_code') or ''
                    ),
                    'sales_total': amount,
                    'sales_total_display': f'{amount:,.2f}',
                    'invoice_count': inv,
                    'invoice_count_display': f'{inv:,}',
                    'share_pct': pct,
                }
            )

    return {
        'key': key,
        'label': label,
        'rows': rows if not light else [],
        'branches': branches,
        'chart_branches': chart_branches,
        'daily_mode': daily_mode,
        'selected_branch': selected_branch if not light else '',
        'selected_branch_name': selected_branch_name,
        'top_users': top_users,
        'invoice_groups': invoice_groups,
        'selected_user_code': selected_code,
        'selected_user_name': selected_name,
        'branch_count': len(branch_rows),
        'day_count': len(rows) if daily_mode else 0,
        'seller_count': seller_count,
        'seller_count_display': f'{seller_count:,}',
        'device_count': device_count,
        'device_count_display': f'{device_count:,}',
        'grand_invoices': grand_invoices,
        'grand_returns': grand_returns,
        'grand_invoices_display': f'{grand_invoices:,}',
        'grand_returns_display': f'{grand_returns:,}',
        'grand_gross': f'{grand_gross:,.2f}',
        'grand_net': f'{grand_net:,.2f}',
        'grand_vat': f'{grand_vat:,.2f}',
        'grand_sales': f'{grand_sales:,.2f}',
        'grand_avg_basket': f'{grand_avg:,.2f}',
        'grand_sales_num': grand_sales,
        'top_user_name': str(top_users[0].get('user_name') or '') if top_users else '',
        'top_user_sales': top_users[0]['sales_total_display'] if top_users else '0.00',
        'top_visit_branch': top_visit_branch,
        'top_visit_invoices': top_visit_invoices,
        'top_visit_invoices_display': top_visit_invoices_display,
    }


def _empty_group_panel(selected_branch='', selected_group='', groups=None):
    return {
        'rows': [],
        'groups': groups or [],
        'selected_group': selected_group,
        'selected_group_name': '',
        'selected_branch': selected_branch,
        'by_branch': bool(selected_group),
        'grand_invoices_display': '0',
        'grand_qty_display': '0.00',
        'grand_sales': '0.00',
        'grand_gross': '0.00',
        'grand_net': '0.00',
        'grand_vat': '0.00',
        'grand_avg_basket': '0.00',
        'fast_mode': False,
        'loading': True,
    }


def _build_group_panel(
    date_from,
    date_to,
    active_system,
    branches,
    selected_group='',
    selected_branch='',
):
    from .oracle_stock import (
        fetch_group_sales_totals,
        fetch_sales_group_options,
        sales_fast_mode,
    )

    group_options = fetch_sales_group_options()
    group_codes = {g['code'] for g in group_options}
    if selected_group and selected_group not in group_codes:
        selected_group = ''
    by_branch = bool(selected_group)
    group_rows = fetch_group_sales_totals(
        date_from,
        date_to,
        system=active_system,
        branch_code=selected_branch,
        group_code=selected_group,
        by_branch=by_branch,
    )
    _format_sales_metric_rows(group_rows)
    for row in group_rows:
        row['qty_total_display'] = f"{float(row.get('qty_total') or 0):,.2f}"
    if not by_branch and selected_branch:
        branch_label = next(
            (
                str(b.get('name') or b.get('code') or '')
                for b in (branches or [])
                if str(b.get('code') or '') == selected_branch
            ),
            selected_branch,
        )
        for row in group_rows:
            row['branch_code'] = selected_branch
            row['branch_name'] = branch_label
    g_net = sum(float(r.get('net_total') or 0) for r in group_rows)
    g_vat = sum(float(r.get('vat_total') or 0) for r in group_rows)
    g_sales = sum(float(r.get('sales_total') or 0) for r in group_rows)
    g_gross = sum(float(r.get('gross_total') or 0) for r in group_rows)
    g_inv = sum(int(r.get('invoice_count') or 0) for r in group_rows)
    g_qty = sum(float(r.get('qty_total') or 0) for r in group_rows)
    g_avg = round(g_sales / g_inv, 2) if g_inv else 0.0
    selected_group_name = ''
    if by_branch and group_rows:
        selected_group_name = str(group_rows[0].get('group_name') or selected_group)
    elif selected_group:
        selected_group_name = next(
            (g['name'] for g in group_options if g['code'] == selected_group),
            selected_group,
        )
    return {
        'rows': group_rows,
        'groups': group_options,
        'selected_group': selected_group,
        'selected_group_name': selected_group_name,
        'selected_branch': selected_branch,
        'by_branch': by_branch,
        'grand_invoices_display': f'{g_inv:,}',
        'grand_qty_display': f'{g_qty:,.2f}',
        'grand_sales': f'{g_sales:,.2f}',
        'grand_gross': f'{g_gross:,.2f}',
        'grand_net': f'{g_net:,.2f}',
        'grand_vat': f'{g_vat:,.2f}',
        'grand_avg_basket': f'{g_avg:,.2f}',
        'fast_mode': sales_fast_mode(date_from, date_to),
        'loading': False,
    }


def _build_sales_panels_payload(
    date_from,
    date_to,
    active_system,
    selected_group='',
    selected_branch='',
    item_limit=20,
):
    """مجموعات + أصناف مبيعات + أصناف مرتجعة في جلسة أوراكل واحدة."""
    panel = _build_group_panel(
        date_from,
        date_to,
        active_system,
        branches=[],
        selected_group=selected_group,
        selected_branch=selected_branch,
    )
    gcode = panel.get('selected_group') or ''
    items, fast_mode = _build_top_items_list(
        date_from,
        date_to,
        active_system,
        selected_branch=selected_branch,
        selected_group=gcode,
        limit=item_limit,
    )
    return_items = _build_top_return_items_list(
        date_from,
        date_to,
        active_system,
        selected_branch=selected_branch,
        selected_group=gcode,
        limit=item_limit,
    )
    return panel, items, return_items, fast_mode


def _build_top_items_list(
    date_from,
    date_to,
    active_system,
    selected_branch='',
    selected_group='',
    limit=20,
):
    from .oracle_stock import fetch_top_sales_items, sales_fast_mode

    top_items = fetch_top_sales_items(
        date_from,
        date_to,
        system=active_system,
        branch_code=selected_branch,
        group_code=selected_group,
        limit=limit,
    )
    for item in top_items:
        item['sales_total_display'] = f"{float(item.get('sales_total') or 0):,.2f}"
        item['qty_total_display'] = f"{float(item.get('qty_total') or 0):,.2f}"
    return top_items, sales_fast_mode(date_from, date_to)


def _build_top_users_list(
    date_from,
    date_to,
    active_system,
    selected_branch='',
    limit=8,
):
    from .oracle_stock import fetch_top_sales_users

    top_users = fetch_top_sales_users(
        date_from,
        date_to,
        system=active_system,
        branch_code=selected_branch,
        limit=limit,
    )
    for user in top_users:
        user['net_total_display'] = f"{float(user.get('net_total') or 0):,.2f}"
        user['sales_total_display'] = f"{float(user.get('sales_total') or 0):,.2f}"
    return top_users


def _build_top_return_items_list(
    date_from,
    date_to,
    active_system,
    selected_branch='',
    selected_group='',
    limit=20,
):
    from .oracle_stock import fetch_top_returned_items

    rows = fetch_top_returned_items(
        date_from,
        date_to,
        system=active_system,
        branch_code=selected_branch,
        group_code=selected_group,
        limit=limit,
    )
    for item in rows:
        total = float(item.get('return_total') or item.get('sales_total') or 0)
        item['sales_total_display'] = f'{total:,.2f}'
        item['return_total_display'] = f'{total:,.2f}'
        item['qty_total_display'] = f"{float(item.get('qty_total') or 0):,.2f}"
    return rows


def _build_chart_branches_list(
    date_from,
    date_to,
    active_system,
    selected_branch='',
    selected_group='',
    limit=15,
):
    """فروع مرتبة حسب قيمة المرتجع — مع فلتر مجموعة/فرع اختياري."""
    from .oracle_stock import fetch_branch_return_totals

    ranked = fetch_branch_return_totals(
        date_from,
        date_to,
        system=active_system,
        branch_code=selected_branch,
        group_code=selected_group,
        limit=limit,
    )
    total = sum(float(r.get('return_total') or r.get('sales_total') or 0) for r in ranked)
    out: list[dict] = []
    assigned = 0.0
    for idx, row in enumerate(ranked):
        amount = float(row.get('return_total') or row.get('sales_total') or 0)
        inv = int(row.get('return_count') or row.get('invoice_count') or 0)
        if not total:
            pct = 0.0
        elif idx == len(ranked) - 1:
            pct = round(max(0.0, 100.0 - assigned), 1)
        else:
            pct = round((amount / total) * 100.0, 1)
            assigned += pct
        out.append(
            {
                'branch_code': str(row.get('branch_code') or ''),
                'branch_name': str(
                    row.get('branch_name') or row.get('branch_code') or ''
                ),
                'sales_total': amount,
                'sales_total_display': f'{amount:,.2f}',
                'invoice_count': inv,
                'invoice_count_display': f'{inv:,}',
                'share_pct': pct,
            }
        )
    return out


@login_required
@require_GET
@never_cache
def browse_sales(request):
    """داشبورد مبيعات: نقاط البيع والجملة بأرقام صحيحة دون تكرار."""
    error = ''
    systems: list[dict] = []
    active_system = str(request.GET.get('sys') or 'pos').strip().lower()
    if active_system not in ('pos', 'wholesale'):
        active_system = 'pos'

    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from'),
            request.GET.get('date_to'),
        )
    except ValidationError as exc:
        from datetime import date as date_cls

        today = date_cls.today()
        return render(
            request,
            'search/browse_sales.html',
            {
                'date_from': (request.GET.get('date_from') or '')[:10],
                'date_to': (request.GET.get('date_to') or '')[:10],
                'systems': [],
                'active': None,
                'active_system': active_system,
                'kpi': None,
                'browsed': False,
                'error': str(exc),
                'default_from': today.replace(day=1).isoformat(),
                'default_to': today.isoformat(),
            },
        )

    selected_group = str(request.GET.get('group') or '').strip()
    active = None
    systems_by_key: dict = {}
    top_items: list[dict] = []
    group_panel = _empty_group_panel()
    defer_heavy = True
    try:
        from .oracle_stock import (
            SALES_SYSTEMS,
            oracle_enabled,
            oracle_session,
        )

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن جلب المبيعات.'
        else:
            requested_branch = str(request.GET.get('branch') or '').strip()

            def _job_system(key, label, is_active):
                with oracle_session():
                    return _build_sales_system(
                        key,
                        label,
                        date_from,
                        date_to,
                        str(request.GET.get('user_id') or '').strip() if is_active else '',
                        requested_branch if is_active else '',
                        light=not is_active,
                    )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {
                    pool.submit(
                        _job_system,
                        key,
                        conf['label'],
                        key == active_system,
                    ): key
                    for key, conf in SALES_SYSTEMS.items()
                }
                built = {}
                for fut in as_completed(futures):
                    built[futures[fut]] = fut.result()
                systems = [
                    built[key] for key in SALES_SYSTEMS.keys() if key in built
                ]

            systems_by_key = {s['key']: s for s in systems}
            active = systems_by_key.get(active_system) or (
                systems[0] if systems else None
            )
            if active:
                active_system = active['key']

            if active:
                from .oracle_stock import fetch_sales_group_options

                group_options = fetch_sales_group_options()
                group_codes = {g['code'] for g in group_options}
                if selected_group and selected_group not in group_codes:
                    selected_group = ''
                group_panel = _empty_group_panel(
                    selected_branch=active.get('selected_branch') or '',
                    selected_group=selected_group,
                    groups=group_options,
                )
                with oracle_session():
                    active['chart_branches'] = _build_chart_branches_list(
                        date_from,
                        date_to,
                        active_system,
                        limit=15,
                    )
                    from .oracle_stock import fetch_sales_item_highlights

                    active['item_highlights'] = fetch_sales_item_highlights(
                        date_from,
                        date_to,
                        branch_code=active.get('selected_branch') or '',
                        group_code=selected_group,
                        system=active_system,
                    )
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_sales failed: %s', exc)
        error = f'تعذّر جلب المبيعات: {exc}'
        systems = []
        active = None
        systems_by_key = {}
        top_items = []
        defer_heavy = False

    kpi = None
    if systems:
        if not systems_by_key:
            systems_by_key = {s['key']: s for s in systems}
        pos = systems_by_key.get('pos') or {}
        wholesale = systems_by_key.get('wholesale') or {}
        total_sales = float(pos.get('grand_sales_num') or 0) + float(
            wholesale.get('grand_sales_num') or 0
        )
        total_invoices = int(pos.get('grand_invoices') or 0) + int(
            wholesale.get('grand_invoices') or 0
        )
        highlights = (active or {}).get('item_highlights') or {}
        kpi = {
            'pos_sales': pos.get('grand_sales') or '0.00',
            'pos_invoices': pos.get('grand_invoices_display') or '0',
            'wholesale_sales': wholesale.get('grand_sales') or '0.00',
            'wholesale_invoices': wholesale.get('grand_invoices_display') or '0',
            'total_sales': f'{total_sales:,.2f}',
            'total_invoices': f'{total_invoices:,}',
            'branch_count': max(
                int(pos.get('branch_count') or 0),
                int(wholesale.get('branch_count') or 0),
            ),
            'seller_count': (active or {}).get('seller_count_display')
            or f"{int((active or {}).get('seller_count') or 0):,}",
            'device_count': (active or {}).get('device_count_display')
            or f"{int((active or {}).get('device_count') or 0):,}",
            'seller_system_label': (active or {}).get('label') or '',
            'top_visit_branch': (
                pos.get('top_visit_branch')
                or (active or {}).get('top_visit_branch')
                or '—'
            ),
            'top_visit_invoices': (
                pos.get('top_visit_invoices_display')
                or (active or {}).get('top_visit_invoices_display')
                or '0'
            ),
            'top_item_amount_name': highlights.get('top_amount_name') or '—',
            'top_item_amount_code': highlights.get('top_amount_code') or '',
            'top_item_amount_value': highlights.get('top_amount_value') or '0.00',
            'top_item_qty_name': highlights.get('top_qty_name') or '—',
            'top_item_qty_code': highlights.get('top_qty_code') or '',
            'top_item_qty_value': highlights.get('top_qty_value') or '0.00',
            'top_item_return_name': highlights.get('top_return_name') or '—',
            'top_item_return_code': highlights.get('top_return_code') or '',
            'top_item_return_value': highlights.get('top_return_value') or '0.00',
        }

    return render(
        request,
        'search/browse_sales.html',
        {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'systems': systems,
            'active': active,
            'active_system': active_system,
            'selected_branch': (active or {}).get('selected_branch') or '',
            'selected_group': selected_group,
            'group_panel': group_panel,
            'top_items': top_items,
            'defer_heavy': defer_heavy and bool(active) and not error,
            'kpi': kpi,
            'browsed': bool(systems) and not error,
            'error': error,
            'default_from': date_from.isoformat(),
            'default_to': date_to.isoformat(),
        },
    )


@login_required
@require_GET
@never_cache
def browse_sales_groups_api(request):
    """تحميل لاحق لجدول المجموعات (ثقيل على الفترات الطويلة)."""
    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from'),
            request.GET.get('date_to'),
        )
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

    active_system = str(request.GET.get('sys') or 'pos').strip().lower()
    if active_system not in ('pos', 'wholesale'):
        active_system = 'pos'
    selected_group = str(request.GET.get('group') or '').strip()
    selected_branch = str(request.GET.get('branch') or '').strip()
    try:
        from .oracle_stock import oracle_enabled, oracle_session

        if not oracle_enabled():
            return JsonResponse({'ok': False, 'error': 'أوراكل غير مفعّل.'}, status=400)
        with oracle_session():
            panel = _build_group_panel(
                date_from,
                date_to,
                active_system,
                branches=[],
                selected_group=selected_group,
                selected_branch=selected_branch,
            )
        return JsonResponse({'ok': True, 'panel': panel})
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_sales_groups_api failed: %s', exc)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)


@login_required
@require_GET
@never_cache
def browse_sales_items_api(request):
    """تحميل لاحق للأصناف الأكثر مبيعاً."""
    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from'),
            request.GET.get('date_to'),
        )
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

    active_system = str(request.GET.get('sys') or 'pos').strip().lower()
    if active_system not in ('pos', 'wholesale'):
        active_system = 'pos'
    selected_group = str(request.GET.get('group') or '').strip()
    selected_branch = str(request.GET.get('branch') or '').strip()
    try:
        from .oracle_stock import oracle_enabled, oracle_session

        if not oracle_enabled():
            return JsonResponse({'ok': False, 'error': 'أوراكل غير مفعّل.'}, status=400)
        with oracle_session():
            items, fast_mode = _build_top_items_list(
                date_from,
                date_to,
                active_system,
                selected_branch=selected_branch,
                selected_group=selected_group,
                limit=20,
            )
        return JsonResponse({'ok': True, 'items': items, 'fast_mode': fast_mode})
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_sales_items_api failed: %s', exc)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)


@login_required
@require_GET
@never_cache
def browse_sales_users_api(request):
    """أكثر المستخدمين مبيعاً مع فلتر فرع."""
    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from'),
            request.GET.get('date_to'),
        )
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

    active_system = str(request.GET.get('sys') or 'pos').strip().lower()
    if active_system not in ('pos', 'wholesale'):
        active_system = 'pos'
    selected_branch = str(request.GET.get('branch') or '').strip()
    try:
        from .oracle_stock import oracle_enabled, oracle_session

        if not oracle_enabled():
            return JsonResponse({'ok': False, 'error': 'أوراكل غير مفعّل.'}, status=400)
        with oracle_session():
            users = _build_top_users_list(
                date_from,
                date_to,
                active_system,
                selected_branch=selected_branch,
                limit=8,
            )
        return JsonResponse({'ok': True, 'users': users})
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_sales_users_api failed: %s', exc)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)


@login_required
@require_GET
@never_cache
def browse_sales_panels_api(request):
    """طلب واحد: مجموعات + أعلى أصناف (جلسة أوراكل واحدة)."""
    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from'),
            request.GET.get('date_to'),
        )
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

    active_system = str(request.GET.get('sys') or 'pos').strip().lower()
    if active_system not in ('pos', 'wholesale'):
        active_system = 'pos'
    selected_group = str(request.GET.get('group') or '').strip()
    selected_branch = str(request.GET.get('branch') or '').strip()
    try:
        from .oracle_stock import oracle_enabled, oracle_session

        if not oracle_enabled():
            return JsonResponse({'ok': False, 'error': 'أوراكل غير مفعّل.'}, status=400)
        with oracle_session():
            panel, items, return_items, fast_mode = _build_sales_panels_payload(
                date_from,
                date_to,
                active_system,
                selected_group=selected_group,
                selected_branch=selected_branch,
                item_limit=20,
            )
        return JsonResponse(
            {
                'ok': True,
                'panel': panel,
                'items': items,
                'return_items': return_items,
                'fast_mode': fast_mode,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_sales_panels_api failed: %s', exc)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)


@login_required
@require_GET
@never_cache
def browse_sales_charts_api(request):
    """رسوم الفروع + الأصناف الأكثر إرجاعاً مع فلتر فرع/مجموعة."""
    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from'),
            request.GET.get('date_to'),
        )
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

    active_system = str(request.GET.get('sys') or 'pos').strip().lower()
    if active_system not in ('pos', 'wholesale'):
        active_system = 'pos'
    selected_group = str(request.GET.get('group') or '').strip()
    selected_branch = str(request.GET.get('branch') or '').strip()
    try:
        from .oracle_stock import oracle_enabled, oracle_session

        if not oracle_enabled():
            return JsonResponse({'ok': False, 'error': 'أوراكل غير مفعّل.'}, status=400)
        with oracle_session():
            chart_branches = _build_chart_branches_list(
                date_from,
                date_to,
                active_system,
                selected_branch=selected_branch,
                selected_group=selected_group,
                limit=15,
            )
            return_items = _build_top_return_items_list(
                date_from,
                date_to,
                active_system,
                selected_branch=selected_branch,
                selected_group=selected_group,
                limit=20,
            )
        return JsonResponse(
            {
                'ok': True,
                'chart_branches': chart_branches,
                'return_items': return_items,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_sales_charts_api failed: %s', exc)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)


@login_required
@require_GET
@never_cache
def browse_sales_margins_api(request):
    """هامش ربح الفروع والمجموعات — أعلى 15 من الأفضل إلى الأسوأ."""
    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from'),
            request.GET.get('date_to'),
        )
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

    active_system = str(request.GET.get('sys') or 'pos').strip().lower()
    if active_system not in ('pos', 'wholesale'):
        active_system = 'pos'
    selected_group = str(request.GET.get('group') or '').strip()
    selected_branch = str(request.GET.get('branch') or '').strip()
    try:
        from .oracle_stock import fetch_margin_ranks, oracle_enabled, oracle_session

        if not oracle_enabled():
            return JsonResponse({'ok': False, 'error': 'أوراكل غير مفعّل.'}, status=400)
        with oracle_session():
            payload = fetch_margin_ranks(
                date_from,
                date_to,
                system=active_system,
                branch_code=selected_branch,
                group_code=selected_group,
                limit=15,
            )
        branches = []
        for row in payload.get('branches') or []:
            branch = dict(row)
            branch['sales_net_display'] = f"{float(branch.get('sales_net') or 0):,.2f}"
            branch['cost_total_display'] = f"{float(branch.get('cost_total') or 0):,.2f}"
            branch['profit_display'] = f"{float(branch.get('profit') or 0):,.2f}"
            branch['qty_total_display'] = f"{float(branch.get('qty_total') or 0):,.2f}"
            margin = branch.get('margin_pct')
            branch['margin_pct_display'] = (
                f"{float(margin):.2f}%" if margin is not None else '—'
            )
            branches.append(branch)
        groups = []
        for row in payload.get('groups') or []:
            group = dict(row)
            group['sales_net_display'] = f"{float(group.get('sales_net') or 0):,.2f}"
            group['cost_total_display'] = f"{float(group.get('cost_total') or 0):,.2f}"
            group['profit_display'] = f"{float(group.get('profit') or 0):,.2f}"
            group['qty_total_display'] = f"{float(group.get('qty_total') or 0):,.2f}"
            margin = group.get('margin_pct')
            group['margin_pct_display'] = (
                f"{float(margin):.2f}%" if margin is not None else '—'
            )
            groups.append(group)
        return JsonResponse({'ok': True, 'branches': branches, 'groups': groups})
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_sales_margins_api failed: %s', exc)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)


@login_required
@require_GET
@never_cache
def browse_performance(request):
    """قياس الأداء — فلترة حقيقية ومقارنة فترتين."""
    from datetime import date as date_cls
    from datetime import datetime
    from datetime import timedelta

    error = ''
    insights = None
    branches: list[dict] = []
    groups: list[dict] = []
    active_system = str(request.GET.get('sys') or 'pos').strip().lower()
    if active_system not in ('pos', 'wholesale'):
        active_system = 'pos'
    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_group = str(request.GET.get('group') or '').strip()
    compare_mode = str(request.GET.get('compare') or 'auto').strip().lower()
    if compare_mode not in ('auto', 'custom'):
        compare_mode = 'auto'

    systems = [
        {'key': 'pos', 'label': 'نقاط البيع'},
        {'key': 'wholesale', 'label': 'الآجل'},
    ]

    def _parse_one(raw: str | None, fallback: date_cls) -> date_cls:
        text = (raw or '').strip()
        if not text:
            return fallback
        try:
            return datetime.strptime(text[:10], '%Y-%m-%d').date()
        except ValueError as exc:
            raise ValidationError('صيغة التاريخ غير صحيحة.') from exc

    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from'),
            request.GET.get('date_to'),
        )
    except ValidationError as exc:
        today = date_cls.today()
        return render(
            request,
            'search/browse_performance.html',
            {
                'date_from': (request.GET.get('date_from') or '')[:10],
                'date_to': (request.GET.get('date_to') or '')[:10],
                'compare_from': (request.GET.get('compare_from') or '')[:10],
                'compare_to': (request.GET.get('compare_to') or '')[:10],
                'default_from': today.replace(day=1).isoformat(),
                'default_to': today.isoformat(),
                'active_system': active_system,
                'selected_branch': selected_branch,
                'selected_group': selected_group,
                'compare_mode': compare_mode,
                'systems': systems,
                'branches': [],
                'groups': [],
                'insights': None,
                'error': str(exc),
            },
        )

    span_days = (date_to - date_from).days
    default_b_to = date_from - timedelta(days=1)
    default_b_from = default_b_to - timedelta(days=span_days)
    compare_from = None
    compare_to = None
    if compare_mode == 'custom':
        try:
            compare_from = _parse_one(request.GET.get('compare_from'), default_b_from)
            compare_to = _parse_one(request.GET.get('compare_to'), default_b_to)
            if compare_from > compare_to:
                raise ValidationError('فترة المقارنة: تاريخ البداية بعد النهاية.')
            if (compare_to - compare_from).days > 366:
                raise ValidationError('فترة المقارنة القصوى سنة واحدة.')
        except ValidationError as exc:
            error = str(exc)

    try:
        from .oracle_stock import (
            SALES_SYSTEMS,
            fetch_branch_sales_totals,
            fetch_sales_group_options,
            oracle_enabled,
            oracle_session,
        )
        from .sales_insights import build_performance_insights

        systems = [
            {'key': key, 'label': conf['label']}
            for key, conf in SALES_SYSTEMS.items()
        ]
        if not oracle_enabled():
            error = error or 'أوراكل غير مفعّل — لا يمكن قياس الأداء.'
        elif not error:
            with oracle_session():
                branches = [
                    {
                        'code': str(r.get('branch_code') or ''),
                        'name': str(r.get('branch_name') or r.get('branch_code') or ''),
                    }
                    for r in fetch_branch_sales_totals(
                        date_from, date_to, system=active_system
                    )
                    if r.get('branch_code')
                ]
                groups = fetch_sales_group_options()
                group_codes = {g['code'] for g in groups}
                if selected_group and selected_group not in group_codes:
                    selected_group = ''
                insights = build_performance_insights(
                    date_from,
                    date_to,
                    system=active_system,
                    branch_code=selected_branch,
                    group_code=selected_group,
                    compare_from=compare_from if compare_mode == 'custom' else None,
                    compare_to=compare_to if compare_mode == 'custom' else None,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_performance failed: %s', exc)
        error = f'تعذّر حساب قياس الأداء: {exc}'
        insights = None

    if compare_mode == 'custom' and compare_from and compare_to:
        compare_from_s = compare_from.isoformat()
        compare_to_s = compare_to.isoformat()
    else:
        compare_from_s = default_b_from.isoformat()
        compare_to_s = default_b_to.isoformat()

    return render(
        request,
        'search/browse_performance.html',
        {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'compare_from': compare_from_s,
            'compare_to': compare_to_s,
            'default_from': date_from.isoformat(),
            'default_to': date_to.isoformat(),
            'active_system': active_system,
            'selected_branch': selected_branch,
            'selected_group': selected_group,
            'compare_mode': compare_mode,
            'systems': systems,
            'branches': branches,
            'groups': groups,
            'insights': insights,
            'error': error,
        },
    )


@login_required
@require_GET
@never_cache
def browse_inventory(request):
    """تحليل المخزون — إجماليات حسب المخازن والمجموعات والفروع."""
    error = ''
    insights = None
    warehouses: list[dict] = []
    groups: list[dict] = []
    branches: list[dict] = []

    selected_warehouse = str(request.GET.get('warehouse') or '').strip()
    selected_group = str(request.GET.get('group') or '').strip()
    selected_branch = str(request.GET.get('branch') or '').strip()

    try:
        from .inventory_insights import build_inventory_insights
        from .oracle_stock import (
            fetch_sales_group_options,
            fetch_warehouse_options,
            oracle_enabled,
            oracle_session,
        )

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن تحليل المخزون.'
        else:
            with oracle_session():
                warehouses = fetch_warehouse_options(active_only=True)
                groups = fetch_sales_group_options()
                wh_codes = {w['code'] for w in warehouses}
                group_codes = {g['code'] for g in groups}
                if selected_warehouse and selected_warehouse not in wh_codes:
                    selected_warehouse = ''
                if selected_group and selected_group not in group_codes:
                    selected_group = ''

                branch_map: dict[str, str] = {}
                for w in warehouses:
                    brn = str(w.get('branch_code') or '').strip()
                    if brn:
                        branch_map[brn] = str(w.get('branch_name') or brn)
                branches = [
                    {'code': code, 'name': name}
                    for code, name in sorted(
                        branch_map.items(), key=lambda x: (x[1], x[0])
                    )
                ]
                if selected_branch and selected_branch not in branch_map:
                    selected_branch = ''

                # عند اختيار فرع: اعرض مخازنه فقط في القائمة
                warehouse_choices = warehouses
                if selected_branch:
                    warehouse_choices = [
                        w
                        for w in warehouses
                        if str(w.get('branch_code') or '') == selected_branch
                    ]
                    if (
                        selected_warehouse
                        and selected_warehouse
                        not in {w['code'] for w in warehouse_choices}
                    ):
                        selected_warehouse = ''

                insights = build_inventory_insights(
                    warehouse=selected_warehouse,
                    group_code=selected_group,
                    branch_code=selected_branch,
                )
                warehouses = warehouse_choices
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_inventory failed: %s', exc)
        error = f'تعذّر تحليل المخزون: {exc}'
        insights = None

    return render(
        request,
        'search/browse_inventory.html',
        {
            'selected_warehouse': selected_warehouse,
            'selected_group': selected_group,
            'selected_branch': selected_branch,
            'warehouses': warehouses,
            'groups': groups,
            'branches': branches,
            'insights': insights,
            'error': error,
        },
    )


@login_required
@require_GET
@never_cache
def browse_purchases(request):
    """تحليل فواتير المشتريات حسب الفرع والمجموعة والمورد."""
    from datetime import date

    today = date.today()
    month_start = today.replace(day=1)
    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_group = str(request.GET.get('group') or '').strip()
    selected_vendor = str(request.GET.get('vendor') or '').strip()
    dashboard = None
    error = ''
    branches: list[dict] = []
    groups: list[dict] = []
    vendors: list[dict] = []

    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from'),
            request.GET.get('date_to'),
        )
    except ValidationError as exc:
        return render(
            request,
            'search/browse_purchases.html',
            {
                'date_from': (request.GET.get('date_from') or '')[:10],
                'date_to': (request.GET.get('date_to') or '')[:10],
                'default_from': month_start.isoformat(),
                'default_to': today.isoformat(),
                'selected_branch': selected_branch,
                'selected_group': selected_group,
                'selected_vendor': selected_vendor,
                'branches': [],
                'groups': [],
                'vendors': [],
                'dashboard': None,
                'error': str(exc),
            },
        )

    try:
        from .oracle_income import fetch_income_branches
        from .oracle_purchases import (
            build_purchase_dashboard,
            fetch_purchase_vendor_options,
        )
        from .oracle_stock import (
            fetch_sales_group_options,
            oracle_enabled,
            oracle_session,
        )

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن تحليل المشتريات.'
        else:
            with oracle_session():
                branches = fetch_income_branches()
                groups = fetch_sales_group_options()
                vendors = fetch_purchase_vendor_options(date_from, date_to)
                if selected_branch not in {row['code'] for row in branches}:
                    selected_branch = ''
                if selected_group not in {row['code'] for row in groups}:
                    selected_group = ''
                if selected_vendor not in {row['code'] for row in vendors}:
                    selected_vendor = ''
                dashboard = build_purchase_dashboard(
                    date_from,
                    date_to,
                    branch_code=selected_branch,
                    group_code=selected_group,
                    vendor_code=selected_vendor,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_purchases failed: %s', exc)
        error = f'تعذّر تحليل المشتريات: {exc}'
        dashboard = None

    return render(
        request,
        'search/browse_purchases.html',
        {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'default_from': month_start.isoformat(),
            'default_to': today.isoformat(),
            'selected_branch': selected_branch,
            'selected_group': selected_group,
            'selected_vendor': selected_vendor,
            'branches': branches,
            'groups': groups,
            'vendors': vendors,
            'dashboard': dashboard,
            'error': error,
        },
    )


@login_required
@require_GET
@never_cache
def browse_income(request):
    """قائمة الدخل — أرصدة مع حركة من قيود أوراكل حسب الفرع ومركز التكلفة."""
    from datetime import date as date_cls
    from datetime import datetime

    error = ''
    statement = None
    branches: list[dict] = []
    cost_centers: list[dict] = []
    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_cc = str(request.GET.get('cc') or '').strip()
    # افتراضي: كل القيود (مرحّل + غير مرحّل) — أرصدة كلية
    posted_raw = request.GET.get('posted')
    if posted_raw is None:
        posted_only = False
    else:
        posted_only = str(posted_raw).strip() in ('1', 'true', 'yes', 'on')

    today = date_cls.today()
    year_start = today.replace(month=1, day=1)

    def parse_one(raw: str | None, fallback: date_cls) -> date_cls:
        text = (raw or '').strip()
        if not text:
            return fallback
        try:
            return datetime.strptime(text[:10], '%Y-%m-%d').date()
        except ValueError as exc:
            raise ValidationError('صيغة التاريخ غير صحيحة. استخدم YYYY-MM-DD.') from exc

    try:
        date_from = parse_one(request.GET.get('date_from'), year_start)
        date_to = parse_one(request.GET.get('date_to'), today)
        if date_from > date_to:
            raise ValidationError('تاريخ البداية يجب أن يكون قبل تاريخ النهاية أو مساوياً له.')
        if (date_to - date_from).days > 366:
            raise ValidationError('الفترة القصوى سنة واحدة.')
    except ValidationError as exc:
        return render(
            request,
            'search/browse_income.html',
            {
                'date_from': (request.GET.get('date_from') or '')[:10],
                'date_to': (request.GET.get('date_to') or '')[:10],
                'default_from': year_start.isoformat(),
                'default_to': today.isoformat(),
                'selected_branch': selected_branch,
                'selected_cc': selected_cc,
                'posted_only': posted_only,
                'branches': [],
                'cost_centers': [],
                'statement': None,
                'error': str(exc),
            },
        )

    try:
        from .oracle_income import (
            build_income_statement,
            fetch_income_branches,
            fetch_income_cost_centers,
        )
        from .oracle_stock import oracle_enabled, oracle_session

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن عرض قائمة الدخل.'
        else:
            with oracle_session():
                branches = fetch_income_branches()
                cost_centers = fetch_income_cost_centers()
                branch_codes = {b['code'] for b in branches}
                cc_codes = {c['code'] for c in cost_centers}
                if selected_branch and selected_branch not in branch_codes:
                    selected_branch = ''
                if selected_cc and selected_cc not in cc_codes:
                    selected_cc = ''
                statement = build_income_statement(
                    date_from,
                    date_to,
                    branch_code=selected_branch,
                    cc_code=selected_cc,
                    posted_only=posted_only,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_income failed: %s', exc)
        error = f'تعذّر تحميل قائمة الدخل: {exc}'
        statement = None

    return render(
        request,
        'search/browse_income.html',
        {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'default_from': year_start.isoformat(),
            'default_to': today.isoformat(),
            'selected_branch': selected_branch,
            'selected_cc': selected_cc,
            'posted_only': posted_only,
            'branches': branches,
            'cost_centers': cost_centers,
            'statement': statement,
            'error': error,
        },
    )
