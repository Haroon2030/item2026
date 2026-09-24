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
    build_browse_groups_excel,
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
    lookup_by_name_in_codes,
    search_item_details,
    sync_barcode_index,
)
from .models import ItemBarcode
from .validators import ValidationError, looks_like_item_code, resolve_group, resolve_warehouse, sanitize_search_query

logger = logging.getLogger(__name__)


def _welcome_cards_for_user(user) -> list[dict]:
    """بطاقات الترحيب حسب الشاشات المسموح بها."""
    from django.urls import reverse

    from .nav_permissions import user_can_access_screen

    catalog = (
        (
            'browse_income',
            'قائمة الدخل',
            'النتيجة المالية للفترة',
            'welcome-card--income',
            'M12 1v22M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6',
        ),
        (
            'browse_sales',
            'المبيعات',
            'صافي الفروع والقنوات بعد المرتجع',
            'welcome-card--sales',
            'M4 19V5M4 19h16M8 16V9M13 16V6M18 16v-4',
        ),
        (
            'browse_performance',
            'الأداء',
            'مقارنة الفترات ومؤشرات التشغيل',
            'welcome-card--perf',
            'M12 20V10M18 20V4M6 20v-4M4 20h16',
        ),
        (
            'browse_inventory',
            'تحليل المخزون',
            'الأرصدة وحركة المجموعات',
            'welcome-card--inv',
            'M3 7l9-4 9 4-9 4-9-4zM3 12l9 4 9-4M3 17l9 4 9-4',
        ),
        (
            'browse_purchases',
            'تحليل المشتريات',
            'التوريد ودورة الموردين',
            'welcome-card--purch',
            'M6 6h15l-1.5 9H8L6 6zM9 20a1.5 1.5 0 1 0 0-0.01M18 20a1.5 1.5 0 1 0 0-0.01M6 6L5 3H2',
        ),
        (
            'browse_assets',
            'الأصول',
            'الأصول المسجّلة على الفروع',
            'welcome-card--assets',
            'M3 21h18M5 21V8l7-4 7 4v13M9 21v-6h6v6',
        ),
    )
    cards: list[dict] = []
    for key, title, hint, css, path_d in catalog:
        if not user_can_access_screen(user, key):
            continue
        try:
            href = reverse(key)
        except Exception:  # noqa: BLE001
            continue
        cards.append(
            {
                'key': key,
                'title': title,
                'hint': hint,
                'css': css,
                'href': href,
                'path_d': path_d,
            }
        )
    return cards


def _welcome_display_name(user) -> str:
    """اسم الشخص للعرض — يتجاهل الأرقام الصرفة (رقم الدخول) لصالح الاسم الحقيقي."""
    profile = getattr(user, 'profile', None)
    candidates = [
        ((profile.display_name if profile else '') or '').strip(),
        (user.get_full_name() or '').strip(),
        (user.first_name or '').strip(),
        (user.last_name or '').strip(),
        (user.username or '').strip(),
    ]
    for name in candidates:
        if not name:
            continue
        # تجاهل رقم الدخول/الهاتف إن وُجد اسم نصّي لاحقاً
        if name.isdigit():
            continue
        return name
    for name in candidates:
        if name:
            return name
    return 'مستخدم'


def _welcome_user_context(user) -> dict:
    """اسم العرض واسم الدور ونص الترحيب حسب الدور."""
    from django.utils.html import format_html

    from .nav_permissions import (
        EXECUTIVE_ROLE_NAMES,
        SECTION_MANAGER_ROLES,
        has_full_app_access,
        user_role_name,
    )

    display_name = _welcome_display_name(user)
    role_name = user_role_name(user)
    if not role_name:
        role_name = 'مدير النظام' if user.is_staff else 'مستخدم'

    name_html = format_html('<span class="welcome-sub-name">{}</span>', display_name)
    role_html = format_html('<span class="welcome-sub-role">{}</span>', role_name)

    role_copy = {
        'مدير مبيعات': (
            'لإدارة المبيعات',
            'تتاح لكم لوحة المبيعات: صافي الفروع، بحث المبيعات، وتحليل الأداء ضمن صلاحياتكم.',
        ),
        'مدير مشتريات': (
            'لإدارة المشتريات',
            'تتاح لكم لوحة المشتريات: بحث الأصناف، تحليل التوريد، ودوران الموردين وطلبات الشراء.',
        ),
        'مدير تسعيرة': (
            'لإدارة التسعيرة',
            'تتاح لكم لوحة التسعير: حد الربح، الأصناف غير المسعّرة، ومقارنة أسعار الموردين.',
        ),
        'مدير مخازن': (
            'لإدارة المخزون',
            'تتاح لكم لوحة المخزون: الأرصدة، المجموعات، والرصيد بلا مبيعات ضمن صلاحياتكم.',
        ),
        'مدير مستودع': (
            'لإدارة المستودعات',
            'تتاح لكم لوحة المستودعات: التحويلات، مقارنة الأرصدة، ومتابعة الحركة الصادرة.',
        ),
    }

    if has_full_app_access(user) or role_name in EXECUTIVE_ROLE_NAMES:
        kicker = 'للإدارة العليا'
        subtitle = format_html(
            'نرحّب بك بصفتكم {} نقدّم قراءة تنفيذية لقائمة الدخل والمبيعات ومؤشرات الأداء، '
            'ثم المخزون والمشتريات والأصول — لدعم قرار الإدارة.',
            role_html,
        )
    elif role_name in role_copy:
        kicker, detail = role_copy[role_name]
        subtitle = format_html(
            'نرحّب بكم أستاذ {} في منصة التحليل. بصفتكم {}، {}',
            name_html,
            role_html,
            detail,
        )
    elif role_name in SECTION_MANAGER_ROLES:
        kicker = f'لدور {role_name}'
        subtitle = format_html(
            'نرحّب بكم أستاذ {}. بصفتكم {}، '
            'البطاقات أدناه تعرض الشاشات المتاحة حسب صلاحيات دوركم.',
            name_html,
            role_html,
        )
    else:
        kicker = 'حسب صلاحياتك'
        subtitle = format_html(
            'نرحّب بكم أستاذ {} في منصة التحليل. '
            'البطاقات أدناه تعرض الأقسام والشاشات المسموح لكم بفتحها فقط.',
            name_html,
        )

    return {
        'display_name': display_name,
        'role_name': role_name,
        'is_staff': bool(user.is_staff),
        'welcome_kicker': kicker,
        'welcome_subtitle': subtitle,
        'welcome_cards': _welcome_cards_for_user(user),
    }


@login_required
@never_cache
def home(request):
    """الصفحة الرئيسية بعد الدخول — ترحيب وبطاقات حسب الدور والصلاحيات."""
    ctx = _welcome_user_context(request.user)
    return render(
        request,
        'search/home.html',
        {
            'display_name': ctx['display_name'],
            'role_name': ctx['role_name'],
            'is_staff_user': ctx['is_staff'],
            'welcome_kicker': ctx['welcome_kicker'],
            'welcome_subtitle': ctx['welcome_subtitle'],
            'welcome_cards': ctx['welcome_cards'],
        },
    )


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
            'branch_code': str(w.get('branch_code') or '').strip(),
            'branch_name': str(w.get('branch_name') or '').strip(),
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
            out.append(
                {
                    'code': code,
                    'name': label,
                    'branch_code': str(w.get('branch_code') or '').strip(),
                    'branch_name': str(w.get('branch_name') or '').strip(),
                }
            )
        return out or fallback
    except Exception as exc:  # noqa: BLE001
        logger.warning('Warehouse list from Oracle failed: %s', exc)
        return fallback


def _branches_from_warehouses(warehouses: list[dict]) -> list[dict]:
    """فروع فريدة من ربط المخازن (CONN_BRN_NO)."""
    seen: dict[str, str] = {}
    for row in warehouses or []:
        code = str(row.get('branch_code') or '').strip()
        if not code or code in seen:
            continue
        name = str(row.get('branch_name') or '').strip() or code
        seen[code] = name
    return [
        {'code': code, 'name': name}
        for code, name in sorted(seen.items(), key=lambda item: (item[1], item[0]))
    ]


def _warehouses_for_branch(warehouses: list[dict], branch_code: str) -> list[dict]:
    brn = str(branch_code or '').strip()
    if not brn:
        return list(warehouses or [])
    return [
        row
        for row in (warehouses or [])
        if str(row.get('branch_code') or '').strip() == brn
    ]


def _clean_warehouse_name(code: str, raw_name: str) -> str:
    """يفصل اسم المخزن عن رقمه إن كان ملتصقاً في W_NAME مثل 64(مرتجعات اسكاي)."""
    code = str(code or '').strip()
    name = str(raw_name or '').strip() or code
    if not code or name == code:
        return name
    suffix = f'({code})'
    if name.endswith(suffix):
        name = name[: -len(suffix)].strip()
    wrapped = f'{code}('
    if name.startswith(wrapped) and name.endswith(')'):
        name = name[len(wrapped) : -1].strip()
    return name or code


def _low_margin_branches(warehouses: list[dict]) -> list[dict]:
    """فروع S_BRN التي لها مخازن نشطة — أسماء رسمية بدون تكرار."""
    from .oracle_income import fetch_income_branches

    wh_brn = {
        str(w.get('branch_code') or '').strip()
        for w in (warehouses or [])
        if str(w.get('branch_code') or '').strip()
    }
    if not wh_brn:
        return []
    official = {b['code']: b['name'] for b in fetch_income_branches()}
    out = [
        {'code': code, 'name': official.get(code) or code}
        for code in wh_brn
    ]
    out.sort(key=lambda row: (row['name'], row['code']))
    return out


def _low_margin_wh_name_map(warehouses: list[dict]) -> dict[str, str]:
    return {
        str(w.get('code') or '').strip(): str(w.get('name') or '').strip()
        or str(w.get('code') or '').strip()
        for w in (warehouses or [])
        if str(w.get('code') or '').strip()
    }


def _parse_qty_loose(value) -> float | None:
    text = str(value or '').strip().replace(',', '')
    if not text or text in {'—', '-'}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_decimal_param(raw: str, *, default: float | None = None) -> float:
    """تحويل نص رقمي — يقبل الفاصلة العربية/الإنجليزية كنقطة عشرية."""
    text = str(raw or '').strip().replace('،', ',').replace(',', '.')
    if not text:
        if default is None:
            raise ValueError('empty')
        return float(default)
    return float(text)


def _decimal_input_str(value: float) -> str:
    """قيمة حقل إدخال — نقطة عشرية دائماً (متوافقة مع type=number)."""
    n = float(value)
    if abs(n - round(n)) < 1e-9:
        return str(int(round(n)))
    return f'{n:.4f}'.rstrip('0').rstrip('.')


def _item_sales_turnover(
    sold_qty: float,
    stock_qty: float | None,
    *,
    unit: str = '',
) -> dict:
    """دوران المبيعات = المباع ÷ (المباع + الرصيد الحالي)."""
    sold = max(0.0, float(sold_qty or 0))
    stock = max(0.0, float(stock_qty or 0)) if stock_qty is not None else None
    unit_label = str(unit or '').strip()

    def _fmt(n: float) -> str:
        if abs(n - round(n)) < 1e-9:
            return f'{int(round(n)):,}'
        return f'{n:,.2f}'

    if stock is None:
        return {
            'sold_qty': round(sold, 2),
            'sold_qty_display': _fmt(sold) if sold else '0',
            'stock_qty': None,
            'stock_qty_display': '—',
            'turnover_pct': None,
            'turnover_display': '—',
            'unit': unit_label,
        }
    base = sold + stock
    pct = round((sold / base) * 100.0, 1) if base > 0 else 0.0

    return {
        'sold_qty': round(sold, 2),
        'sold_qty_display': _fmt(sold),
        'stock_qty': round(stock, 2),
        'stock_qty_display': _fmt(stock),
        'turnover_pct': pct,
        'turnover_display': f'{pct:.1f}%',
        'unit': unit_label,
    }


def _qty_to_max_pack(qty: float, max_pack: float) -> float:
    """حوّل كمية وحدة المخزون/الأساس إلى أكبر عبوة."""
    pack = float(max_pack or 0)
    if pack <= 1:
        return float(qty or 0)
    return float(qty or 0) / pack


def _scale_sales_bundle_to_max_pack(bundle: dict | None, max_pack: float) -> dict | None:
    """أعد عرض كميات المبيعات بوحدة أكبر عبوة (الأرقام المالية دون تغيير)."""
    if not bundle or float(max_pack or 0) <= 1:
        return bundle
    pack = float(max_pack)

    def _fmt(n: float) -> str:
        if abs(n - round(n)) < 1e-9:
            return f'{int(round(n)):,}'
        return f'{n:,.2f}'

    for row in bundle.get('rows') or []:
        try:
            qty = float(row.get('qty') or 0) / pack
            ret_qty = float(row.get('return_qty') or 0) / pack
        except (TypeError, ValueError):
            continue
        row['qty'] = round(qty, 4)
        row['qty_display'] = _fmt(qty)
        row['return_qty'] = round(ret_qty, 4)
        row['return_qty_display'] = _fmt(ret_qty)
    totals = bundle.get('totals') or {}
    try:
        t_qty = float(totals.get('qty') or 0) / pack
        t_ret = float(totals.get('return_qty') or 0) / pack
    except (TypeError, ValueError):
        return bundle
    totals['qty'] = round(t_qty, 4)
    totals['qty_display'] = _fmt(t_qty)
    totals['return_qty'] = round(t_ret, 4)
    totals['return_qty_display'] = _fmt(t_ret)
    bundle['totals'] = totals
    bundle['qty_unit'] = 'max_pack'
    return bundle

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
    from datetime import date as date_cls

    from .oracle_stock import (
        OracleStockError,
        fetch_posted_item_sales_by_warehouses,
        oracle_enabled,
    )

    raw_query = request.GET.get('q')
    items = []
    prices = []
    error = ''
    searched = False
    match_type = ''
    group_info = {'g_code': '', 'g_name': ''}
    cache_count = ItemBarcode.objects.count()
    meta_incomplete = index_meta_incomplete()
    all_warehouses = _warehouses()
    branches = _branches_from_warehouses(all_warehouses)
    selected_branch = str(request.GET.get('branch') or '').strip()
    vendor_q = str(request.GET.get('vendor') or '').strip()[:120]
    company_vendors: list[dict] = []
    try:
        from .oracle_stock import fetch_company_vendor_options, oracle_enabled as _oe

        if _oe():
            company_vendors = fetch_company_vendor_options()
    except Exception as exc:  # noqa: BLE001
        logger.warning('Company vendor list skipped: %s', exc)
        company_vendors = []
    vendor_codes = {str(v.get('code') or '').strip() for v in company_vendors}
    selected_vendor = vendor_q if vendor_q in vendor_codes else ''
    # توافق: إن وصل اسم قديم بدل الكود نطابقه بالاسم
    if vendor_q and not selected_vendor:
        needle = vendor_q.casefold()
        for row in company_vendors:
            if needle == str(row.get('name') or '').casefold() or needle == str(row.get('code') or '').casefold():
                selected_vendor = str(row.get('code') or '').strip()
                break
    vendor_q = selected_vendor
    selected_vendor_name = next(
        (str(v.get('name') or '') for v in company_vendors if v.get('code') == selected_vendor),
        '',
    )
    vendor_item_count = 0
    if selected_vendor:
        try:
            from .oracle_stock import fetch_vendor_item_count

            vendor_item_count = fetch_vendor_item_count(selected_vendor)
        except Exception:  # noqa: BLE001
            vendor_item_count = 0
    default_wh = settings.EXTERNAL_API.get('DEFAULT_WAREHOUSE') or '60'
    warehouse_compare: list[dict] = []
    sales_bundle: dict | None = None
    sales_turnover: dict | None = None
    scope_raw = str(request.GET.get('scope') or 'one').strip().lower()
    raw_wh = str(request.GET.get('warehouse') or '').strip()
    # توافق مع الروابط القديمة warehouse=all
    warehouse_all_selected = raw_wh.lower() == 'all' or (
        not raw_wh and scope_raw in ('compare', 'all')
    )
    compare_mode = scope_raw in ('compare', 'all') or raw_wh.lower() == 'all'
    if raw_wh.lower() == 'all':
        raw_wh = ''

    branch_codes = {b['code'] for b in branches}
    if selected_branch and selected_branch not in branch_codes:
        selected_branch = ''
    # مخزن واحد: يلزم فرع محدد (بدون «كل الفروع»)
    if not compare_mode and not selected_branch and branches:
        selected_branch = str(branches[0].get('code') or '').strip()

    warehouses = (
        _warehouses_for_branch(all_warehouses, selected_branch)
        if selected_branch
        else list(all_warehouses)
    )
    if not warehouses:
        warehouses = list(all_warehouses)

    today = date_cls.today()
    month_start = today.replace(day=1)
    try:
        query = sanitize_search_query(raw_query)
        warehouse = resolve_warehouse(
            raw_wh or None,
            warehouses,
            default_wh if any(w['code'] == default_wh for w in warehouses) else (
                warehouses[0]['code'] if warehouses else default_wh
            ),
        )
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from') or month_start.isoformat(),
            request.GET.get('date_to') or today.isoformat(),
        )
    except ValidationError as exc:
        query = (raw_query or '').strip()[:64]
        warehouse = default_wh if default_wh in {w['code'] for w in warehouses} else (
            warehouses[0]['code'] if warehouses else '60'
        )
        date_from, date_to = month_start, today
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
                'all_warehouses': all_warehouses,
                'branches': branches,
                'selected_branch': selected_branch,
                'vendor_q': vendor_q,
                'company_vendors': company_vendors,
                'selected_vendor_name': selected_vendor_name,
                'vendor_item_count': vendor_item_count,
                'warehouse': warehouse,
                'detail_warehouse': warehouse,
                'compare_mode': compare_mode,
                'warehouse_all_selected': warehouse_all_selected,
                'warehouse_compare': [],
                'date_from': date_from.isoformat(),
                'date_to': date_to.isoformat(),
                'sales_bundle': None,
                'sales_turnover': None,
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
        vendor_item_codes: set[str] | None = None
        if selected_vendor:
            try:
                from .oracle_stock import fetch_vendor_item_codes

                vendor_item_codes = fetch_vendor_item_codes(selected_vendor)
            except Exception as exc:  # noqa: BLE001
                logger.warning('Vendor item codes skipped: %s', exc)
                vendor_item_codes = set()
            if not vendor_item_codes:
                error = 'لا أصناف مربوطة بهذا المورد في أونكس.'

        try:
            if error:
                barcode_hits = []
                code_hits = []
                name_hits = []
            else:
                barcode_hits = lookup_by_barcode(query)
                code_hits = [] if barcode_hits else lookup_by_item_code(query)
                if barcode_hits or code_hits:
                    name_hits = []
                elif vendor_item_codes is not None:
                    name_hits = lookup_by_name_in_codes(query, vendor_item_codes, limit=80)
                else:
                    name_hits = lookup_by_name(query)

            def _allowed(code: str) -> bool:
                if vendor_item_codes is None:
                    return True
                c = str(code or '').strip()
                return bool(c) and c in vendor_item_codes

            if barcode_hits:
                item_code = str(barcode_hits[0].get('code') or '').strip()
                if not _allowed(item_code):
                    error = 'هذا الصنف غير من أصناف المورد المختار.'
                    items = []
                    prices = []
                    match_type = ''
                else:
                    match_type = 'barcode'
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
                item_code = str(code_hits[0].get('code') or '').strip()
                if not _allowed(item_code):
                    error = 'هذا الصنف غير من أصناف المورد المختار.'
                    items = []
                    prices = []
                    match_type = ''
                else:
                    match_type = 'code'
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
                match_type = 'name_list'
                items = name_hits
                prices = []
            elif looks_like_item_code(query) and (vendor_item_codes is None or _allowed(query)):
                if compare_mode:
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
                        if not _allowed(item_code):
                            error = 'هذا الصنف غير من أصناف المورد المختار.'
                            items = []
                            prices = []
                            match_type = ''
                        else:
                            match_type = 'barcode' if item_code != query else 'code'
                            group_info = get_item_group(item_code)
                            items = lookup_by_item_code(item_code)
                            if not items:
                                items = _items_from_prices(prices, group_info)
                    elif cache_count == 0:
                        error = (
                            'فهرس الباركود فارغ. اضغط «مزامنة» أو انتظر المزامنة التلقائية بعد النشر ثم أعد البحث.'
                        )
            elif looks_like_item_code(query) and vendor_item_codes is not None:
                error = 'هذا الصنف غير من أصناف المورد المختار.'
            elif not error and cache_count == 0:
                error = (
                    'فهرس الباركود فارغ. اضغط «مزامنة» أولاً ثم ابحث بالاسم أو الباركود.'
                )
            elif not error and selected_vendor and not (barcode_hits or code_hits or name_hits):
                error = 'لا صنف بهذا الاسم ضمن أصناف المورد المختار.'
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
            name_map = {str(w['code']): str(w['name']) for w in all_warehouses}
            compare_list = (
                [w['code'] for w in warehouses]
                if selected_branch
                else _compare_warehouse_codes(all_warehouses)
            )
            try:
                warehouse_compare = compare_item_across_warehouses(
                    compare_code,
                    compare_list,
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
        if selected_vendor:
            for row in suppliers:
                code = str(row.get('code') or '').strip()
                row['is_matched'] = code == selected_vendor
            suppliers.sort(key=lambda r: (0 if r.get('is_matched') else 1, str(r.get('name') or '')))

    # دوران المبيعات للفترة ضمن المخزن/مخازن الفرع
    if searched and not error and match_type != 'name_list':
        sales_code = ''
        if selected_price:
            sales_code = str(selected_price.get('code') or '').strip()
        if not sales_code and items:
            sales_code = str(items[0].get('code') or '').strip()
        if not sales_code and warehouse_compare:
            sales_code = str(warehouse_compare[0].get('code') or '').strip()
        if sales_code and oracle_enabled():
            name_map = {str(w['code']): str(w['name']) for w in all_warehouses}
            if compare_mode:
                wh_codes = (
                    [w['code'] for w in warehouses]
                    if selected_branch
                    else _compare_warehouse_codes(all_warehouses)
                )
            else:
                wh_codes = [detail_wh]
            try:
                sales_bundle = fetch_posted_item_sales_by_warehouses(
                    sales_code,
                    wh_codes,
                    date_from,
                    date_to,
                    warehouse_names=name_map,
                    system='pos',
                )
                if not sales_bundle.get('item_name') and items:
                    sales_bundle['item_name'] = str(items[0].get('name') or sales_code)
                sold_qty = float((sales_bundle.get('totals') or {}).get('qty') or 0)
                stock_qty = None
                if compare_mode and warehouse_compare:
                    stock_sum = 0.0
                    any_stock = False
                    for row in warehouse_compare:
                        parsed = _parse_qty_loose(row.get('quantity'))
                        if parsed is None:
                            continue
                        any_stock = True
                        stock_sum += max(0.0, parsed)
                    if any_stock:
                        stock_qty = stock_sum
                elif selected_price is not None:
                    stock_qty = _parse_qty_loose(selected_price.get('quantity'))
                    if stock_qty is not None:
                        stock_qty = max(0.0, stock_qty)

                max_pack = 0.0
                max_unit = ''
                try:
                    from .oracle_stock import fetch_item_max_pack_map

                    pack_info = (fetch_item_max_pack_map([sales_code]) or {}).get(sales_code) or {}
                    max_pack = float(pack_info.get('pack') or 0)
                    max_unit = str(pack_info.get('unit') or '').strip()
                except Exception as exc:  # noqa: BLE001
                    logger.warning('Item max pack skipped: %s', exc)

                if max_pack > 1:
                    sold_qty = _qty_to_max_pack(sold_qty, max_pack)
                    if stock_qty is not None:
                        sel_unit = str((selected_price or {}).get('unit') or '').strip()
                        sel_pack = _parse_qty_loose((selected_price or {}).get('pack_size'))
                        if (
                            not compare_mode
                            and selected_price is not None
                            and (
                                (sel_unit and max_unit and sel_unit == max_unit)
                                or (
                                    sel_pack is not None
                                    and abs(float(sel_pack) - max_pack) < 1e-9
                                )
                            )
                        ):
                            # الرصيد معروض أصلاً بأكبر عبوة
                            pass
                        elif (
                            not compare_mode
                            and selected_price is not None
                            and sel_pack is not None
                            and sel_pack > 0
                            and abs(float(sel_pack) - 1.0) > 1e-9
                            and abs(float(sel_pack) - max_pack) > 1e-9
                        ):
                            # كمية بوحدة وسيطة → أساس ثم أكبر عبوة
                            stock_qty = (float(stock_qty) * float(sel_pack)) / max_pack
                        else:
                            stock_qty = _qty_to_max_pack(stock_qty, max_pack)
                    sales_bundle = _scale_sales_bundle_to_max_pack(sales_bundle, max_pack)

                sales_turnover = _item_sales_turnover(
                    sold_qty,
                    stock_qty,
                    unit=max_unit if max_pack > 1 else '',
                )
            except OracleStockError as exc:
                logger.warning('Item sales turnover skipped: %s', exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning('Item sales turnover failed: %s', exc)

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
            'all_warehouses': all_warehouses,
            'branches': branches,
            'selected_branch': selected_branch,
            'vendor_q': vendor_q,
            'company_vendors': company_vendors,
            'selected_vendor_name': selected_vendor_name,
            'vendor_item_count': vendor_item_count,
            'warehouse': warehouse,
            'detail_warehouse': detail_wh,
            'compare_mode': compare_mode,
            'warehouse_all_selected': warehouse_all_selected,
            'warehouse_compare': warehouse_compare,
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'sales_bundle': sales_bundle,
            'sales_turnover': sales_turnover,
            'g_code': group_info.get('g_code', ''),
            'g_name': group_info.get('g_name', ''),
            'sync_secret_required': bool(settings.SYNC_SECRET) or not settings.DEBUG,
        },
    )


@login_required
@require_GET
def item_vendor_item_count(request):
    """عدد أصناف المورد المختار — لمقترح البحث الفوري."""
    vendor = str(request.GET.get('vendor') or '').strip()[:40]
    if not vendor:
        return JsonResponse({'ok': True, 'vendor': '', 'count': 0})
    try:
        from .oracle_stock import (
            fetch_company_vendor_options,
            fetch_vendor_item_count,
            oracle_enabled,
        )

        if not oracle_enabled():
            return JsonResponse({'ok': False, 'error': 'oracle_disabled', 'count': 0}, status=503)
        allowed = {str(v.get('code') or '').strip() for v in fetch_company_vendor_options()}
        if vendor not in allowed:
            return JsonResponse({'ok': False, 'error': 'vendor_not_allowed', 'count': 0}, status=400)
        count = fetch_vendor_item_count(vendor)
        return JsonResponse({'ok': True, 'vendor': vendor, 'count': int(count)})
    except Exception as exc:  # noqa: BLE001
        return JsonResponse({'ok': False, 'error': str(exc), 'count': 0}, status=502)


def _compare_warehouse_codes(warehouses: list[dict]) -> list[str]:
    """قائمة مخازن المقارنة من الإعدادات مع الإبقاء على الموجود فعلياً."""
    cfg = settings.EXTERNAL_API or {}
    raw = [
        str(c).strip()
        for c in (cfg.get('COMPARE_WAREHOUSES') or [])
        if str(c).strip()
    ]
    if not raw:
        raw = ['1201', '1', '30', '1901', '2001', '1801', '60', '701']
    known = {str(w.get('code') or '').strip() for w in warehouses}
    if known:
        filtered = [c for c in raw if c in known]
        if filtered:
            return filtered
    return raw

@login_required
@require_GET
@never_cache
def sales_search(request):
    """البحث عن مبيعات صنف حسب مخزن واحد أو مخازن المقارنة (فواتير أونكس أو نقاط البيع)."""
    from .oracle_stock import (
        OracleStockError,
        fetch_posted_item_sales_by_warehouses,
        oracle_enabled,
    )

    raw_query = request.GET.get('q')
    items: list[dict] = []
    error = ''
    searched = False
    match_type = ''
    sales_bundle: dict | None = None
    warehouses = _warehouses()
    default_wh = settings.EXTERNAL_API.get('DEFAULT_WAREHOUSE') or '60'
    scope_raw = str(request.GET.get('scope') or 'one').strip().lower()
    raw_wh = str(request.GET.get('warehouse') or '').strip()
    compare_mode = scope_raw in ('compare', 'all') or raw_wh == 'all'
    if raw_wh == 'all':
        raw_wh = ''
    active_system = str(request.GET.get('sys') or 'bill').strip().lower()
    if active_system not in ('bill', 'pos'):
        active_system = 'bill'

    try:
        query = sanitize_search_query(raw_query)
        warehouse = resolve_warehouse(
            raw_wh or None,
            warehouses,
            default_wh,
        )
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from'),
            request.GET.get('date_to'),
        )
    except ValidationError as exc:
        query = (raw_query or '').strip()[:64]
        warehouse = default_wh if default_wh in {w['code'] for w in warehouses} else '60'
        from datetime import date as date_cls

        today = date_cls.today()
        date_from, date_to = today, today
        searched = bool(query)
        error = str(exc)
        return render(
            request,
            'search/sales_search.html',
            {
                'query': query,
                'items': [],
                'error': error,
                'searched': searched,
                'match_type': '',
                'warehouses': warehouses,
                'warehouse': warehouse,
                'compare_mode': compare_mode,
                'active_system': active_system,
                'date_from': date_from.isoformat(),
                'date_to': date_to.isoformat(),
                'sales_bundle': None,
                'compare_warehouses': _compare_warehouse_codes(warehouses),
            },
        )

    item_code = ''
    if query:
        searched = True
        try:
            barcode_hits = lookup_by_barcode(query)
            code_hits = [] if barcode_hits else lookup_by_item_code(query)
            name_hits = [] if barcode_hits or code_hits else lookup_by_name(query)

            if barcode_hits:
                match_type = 'barcode'
                item_code = str(barcode_hits[0].get('code') or '').strip()
                items = lookup_by_item_code(item_code) or barcode_hits
            elif code_hits:
                match_type = 'code'
                item_code = str(code_hits[0].get('code') or '').strip()
                items = code_hits
            elif len(name_hits) == 1:
                match_type = 'name'
                item_code = str(name_hits[0].get('code') or '').strip()
                items = lookup_by_item_code(item_code) or name_hits
            elif len(name_hits) > 1:
                match_type = 'name_list'
                items = name_hits
            elif looks_like_item_code(query):
                match_type = 'code'
                item_code = query
                items = lookup_by_item_code(item_code) or [
                    {'code': item_code, 'name': '', 'barcode': '', 'unit': ''}
                ]
            else:
                error = 'لا يوجد صنف مطابق.'
        except ApiClientError as exc:
            error = str(exc)

    if searched and not error and match_type != 'name_list' and item_code:
        if not oracle_enabled():
            error = 'اتصال أوراكل غير مفعّل.'
        else:
            name_map = {str(w['code']): str(w['name']) for w in warehouses}
            if compare_mode:
                wh_codes = _compare_warehouse_codes(warehouses)
            else:
                wh_codes = [warehouse]
            try:
                sales_bundle = fetch_posted_item_sales_by_warehouses(
                    item_code,
                    wh_codes,
                    date_from,
                    date_to,
                    warehouse_names=name_map,
                    system=active_system,
                )
                if not sales_bundle.get('item_name') and items:
                    sales_bundle['item_name'] = str(items[0].get('name') or item_code)
            except OracleStockError as exc:
                error = str(exc)
            except Exception as exc:  # noqa: BLE001
                logger.exception('sales_search failed: %s', exc)
                error = 'تعذر جلب مبيعات الصنف.'

    return render(
        request,
        'search/sales_search.html',
        {
            'query': query,
            'items': items,
            'error': error,
            'searched': searched,
            'match_type': match_type,
            'warehouses': warehouses,
            'warehouse': warehouse,
            'compare_mode': compare_mode,
            'active_system': active_system,
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'sales_bundle': sales_bundle,
            'compare_warehouses': _compare_warehouse_codes(warehouses),
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
    cache_key = f'browse_stocked:v16:{qty_src}:{warehouse}:{group_code}:{len(all_items)}'
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
            cache.set(cache_key, {'stocked': stocked, 'counts': counts}, 1800)
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

        want_excel = str(request.GET.get('export') or '').strip().lower() in {
            'xls',
            'excel',
            'xlsx',
            '1',
            'true',
        }
        if want_excel and stocked:
            wh_name = next(
                (w['name'] for w in warehouses if w['code'] == warehouse),
                warehouse,
            )
            return build_browse_groups_excel(
                items=stocked,
                warehouse=warehouse,
                warehouse_name=wh_name,
                group_code=group_code,
                group_name=group_name,
                stock_cost_total=stock_cost_total,
                catalog_count=catalog_count,
                qty_source=qty_source,
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
    """يحوّل تواريخ النموذج إلى date؛ الافتراضي يوم اليوم (من وإلى)."""
    from datetime import date, datetime

    today = date.today()

    def parse_one(raw: str | None, fallback: date) -> date:
        text = (raw or '').strip()
        if not text:
            return fallback
        try:
            return datetime.strptime(text[:10], '%Y-%m-%d').date()
        except ValueError as exc:
            raise ValidationError('صيغة التاريخ غير صحيحة. استخدم YYYY-MM-DD.') from exc

    d_from = parse_one(raw_from, today)
    d_to = parse_one(raw_to, today)
    if d_from > d_to:
        raise ValidationError('تاريخ البداية يجب أن يكون قبل تاريخ النهاية أو مساوياً له.')
    # حد أقصى معقول لتفادي استعلام ضخم
    if (d_to - d_from).days > 366:
        raise ValidationError('الفترة القصوى سنة واحدة.')
    return d_from, d_to

@login_required

@require_GET

@never_cache

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
                'default_from': today.isoformat(),
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
def browse_sales(request):
    """تحليل المبيعات — الفروع فوراً، والمجموعات تُحمَّل لاحقاً عبر API."""
    from datetime import date
    from urllib.parse import urlencode

    from django.urls import reverse

    today = date.today()
    error = ''
    dashboard = None
    branches: list[dict] = []
    groups: list[dict] = []
    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_group = str(request.GET.get('group') or '').strip()

    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from'),
            request.GET.get('date_to'),
        )
    except ValidationError as exc:
        return render(
            request,
            'search/browse_sales.html',
            {
                'date_from': (request.GET.get('date_from') or '')[:10],
                'date_to': (request.GET.get('date_to') or '')[:10],
                'default_from': today.isoformat(),
                'default_to': today.isoformat(),
                'selected_branch': selected_branch,
                'selected_group': selected_group,
                'branches': [],
                'groups': [],
                'dashboard': None,
                'error': str(exc),
                'browsed': False,
                'groups_api_url': '',
                'groups_month_api_url': '',
                'items_api_url': '',
                'activity_api_url': '',
            },
        )

    try:
        from .oracle_stock import (
            fetch_sales_group_options,
            fetch_warehouse_options,
            oracle_enabled,
            oracle_session,
        )
        from .sales_dashboard import build_sales_branches

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن تحليل المبيعات.'
        else:
            with oracle_session():
                warehouses = fetch_warehouse_options(active_only=True)
                groups = fetch_sales_group_options()
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
                branch_codes = {b['code'] for b in branches}
                group_codes = {g['code'] for g in groups}
                if selected_branch and selected_branch not in branch_codes:
                    selected_branch = ''
                if selected_group and selected_group not in group_codes:
                    selected_group = ''

            dashboard = build_sales_branches(
                date_from,
                date_to,
                branch_code=selected_branch,
                group_code=selected_group,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_sales failed: %s', exc)
        from .sales_dashboard import build_sales_branches_from_cache

        dashboard = build_sales_branches_from_cache(
            date_from,
            date_to,
            branch_code=selected_branch,
            group_code=selected_group,
        )
        if dashboard is not None:
            error = (
                'تعذّر الاتصال بأوراكل — عرض أرقام محفوظة مسبقاً (قد تكون أقل من أونكس الحي). '
                'فعّل VPN حتى يصل الجهاز إلى المنفذ 1521 ثم حدّث الصفحة.'
            )
        else:
            error = f'تعذّر تحليل المبيعات: {exc}'
            dashboard = None

    groups_api_url = ''
    groups_month_api_url = ''
    items_api_url = ''
    users_api_url = ''
    groups_seed = None
    if dashboard is not None:
        qs = {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
        }
        if selected_branch:
            qs['branch'] = selected_branch
        if selected_group:
            qs['group'] = selected_group
        # بدون partial: عند اكتمال الشهور تُطابق المجموعات مع إجمالي جدول الفروع
        groups_api_url = f"{reverse('browse_sales_groups_api')}?{urlencode(qs)}"
        month_qs = {}
        if selected_branch:
            month_qs['branch'] = selected_branch
        if selected_group:
            month_qs['group'] = selected_group
        groups_month_api_url = reverse('browse_sales_groups_month_api')
        if month_qs:
            groups_month_api_url = (
                f"{groups_month_api_url}?{urlencode(month_qs)}"
            )
        items_api_url = f"{reverse('browse_sales_top_items_api')}?{urlencode(qs)}"
        users_api_url = f"{reverse('browse_sales_top_users_api')}?{urlencode(qs)}"
        # زرع فوري من الكاش — بلا حلقة شهور في الواجهة
        try:
            from .sales_dashboard import peek_sales_groups

            peeked = peek_sales_groups(
                date_from,
                date_to,
                branch_code=selected_branch,
                group_code=selected_group,
            )
            if peeked and peeked.get('rows'):
                groups_seed = {'ok': True, 'groups': peeked}
                dashboard['groups'] = {
                    'rows': peeked['rows'],
                    'totals': peeked['totals'],
                }
                dashboard['groups_pending'] = False
                dashboard['kpis']['group_sales'] = peeked['totals'][
                    'sales_total_display'
                ]
                dashboard['kpis']['group_count'] = peeked['totals'][
                    'group_count_display'
                ]
        except Exception:  # noqa: BLE001
            groups_seed = None

    return render(
        request,
        'search/browse_sales.html',
        {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'default_from': today.isoformat(),
            'default_to': today.isoformat(),
            'selected_branch': selected_branch,
            'selected_group': selected_group,
            'branches': branches,
            'groups': groups,
            'dashboard': dashboard,
            'error': error,
            'browsed': dashboard is not None,
            'groups_api_url': groups_api_url,
            'groups_month_api_url': groups_month_api_url,
            'items_api_url': items_api_url,
            'users_api_url': users_api_url,
            'groups_seed': groups_seed,
        },
    )


@login_required
@require_GET
@never_cache
def browse_sales_groups_api(request):
    """تحميل لاحق لمبيعات المجموعات (تفاصيل الأصناف)."""
    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from'),
            request.GET.get('date_to'),
        )
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

    branch_code = str(request.GET.get('branch') or '').strip()
    group_code = str(request.GET.get('group') or '').strip()

    try:
        from .oracle_stock import oracle_enabled
        from .sales_dashboard import build_sales_groups

        if not oracle_enabled():
            return JsonResponse(
                {'ok': False, 'error': 'أوراكل غير مفعّل.'},
                status=400,
            )
        # مطابقة إجمالي المجموعات مع صافي نقاط البيع (جدول الفروع)
        payload = build_sales_groups(
            date_from,
            date_to,
            branch_code=branch_code,
            group_code=group_code,
            reconcile=True,
        )
        return JsonResponse({'ok': True, 'groups': payload})
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_sales_groups_api failed: %s', exc)
        # 200 بدل 5xx حتى لا يحجب البروكسي الرسالة كـ HTTP 502
        return JsonResponse(
            {
                'ok': True,
                'groups': {
                    'rows': [],
                    'totals': {
                        'invoice_count_display': '0',
                        'qty_display': '0',
                        'sales_total_display': '0.00',
                        'group_count_display': '0',
                    },
                    'warning': str(exc)
                    or 'تعذّر جلب مبيعات المجموعات. أعد المحاولة بعد لحظات.',
                },
            }
        )


@login_required
@require_GET
@never_cache
def browse_sales_groups_month_api(request):
    """SQL لشهر واحد من مبيعات المجموعات → JSON (للجلب المتوازي من الواجهة)."""
    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from'),
            request.GET.get('date_to'),
        )
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

    branch_code = str(request.GET.get('branch') or '').strip()
    group_code = str(request.GET.get('group') or '').strip()

    try:
        from .oracle_stock import oracle_enabled
        from .sales_dashboard import build_sales_groups_month

        if not oracle_enabled():
            return JsonResponse(
                {'ok': False, 'error': 'أوراكل غير مفعّل.'},
                status=400,
            )
        payload = build_sales_groups_month(
            date_from,
            date_to,
            branch_code=branch_code,
            group_code=group_code,
        )
        return JsonResponse({'ok': True, 'month': payload, 'source': 'sql'})
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_sales_groups_month_api failed: %s', exc)
        return JsonResponse(
            {
                'ok': False,
                'error': str(exc)
                or 'تعذّر جلب مبيعات مجموعات الشهر من أوراكل.',
            },
            status=200,
        )


@login_required
@require_GET
@never_cache
def browse_sales_top_items_api(request):
    """تحميل لاحق لأعلى أصناف الإرجاع من نقاط البيع."""
    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from'),
            request.GET.get('date_to'),
        )
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

    branch_code = str(request.GET.get('branch') or '').strip()
    group_code = str(request.GET.get('group') or '').strip()

    try:
        from .oracle_stock import oracle_enabled
        from .sales_dashboard import build_sales_top_items

        if not oracle_enabled():
            return JsonResponse(
                {'ok': False, 'error': 'أوراكل غير مفعّل.'},
                status=400,
            )
        payload = build_sales_top_items(
            date_from,
            date_to,
            branch_code=branch_code,
            group_code=group_code,
            limit=20,
        )
        return JsonResponse({'ok': True, 'items': payload})
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_sales_top_items_api failed: %s', exc)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)


@login_required
@require_GET
@never_cache
def browse_sales_top_users_api(request):
    """تحميل لاحق لأكثر المستخدمين مبيعاً."""
    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from'),
            request.GET.get('date_to'),
        )
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

    branch_code = str(request.GET.get('branch') or '').strip()
    try:
        limit = int(request.GET.get('limit') or 15)
    except (TypeError, ValueError):
        limit = 15

    try:
        from .oracle_stock import oracle_enabled
        from .sales_dashboard import build_sales_top_users

        if not oracle_enabled():
            return JsonResponse(
                {'ok': False, 'error': 'أوراكل غير مفعّل.'},
                status=400,
            )
        payload = build_sales_top_users(
            date_from,
            date_to,
            branch_code=branch_code,
            limit=limit,
        )
        return JsonResponse({'ok': True, 'users': payload})
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_sales_top_users_api failed: %s', exc)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)



@login_required
@require_GET
@never_cache
def browse_suppliers(request):
    """الموردون — فواتير، رصيد مخزون أصنافهم، والسداد."""
    from datetime import date

    today = date.today()
    month_start = today.replace(day=1)
    selected_branch = str(request.GET.get('branch') or '').strip()
    q = str(request.GET.get('q') or '').strip()
    scope = str(request.GET.get('scope') or 'all').strip().lower()
    if scope not in {'all', 'both', 'inv_only', 'pay_only'}:
        scope = 'all'
    try:
        limit = int(request.GET.get('limit') or 5000)
    except (TypeError, ValueError):
        limit = 5000
    want_excel = str(request.GET.get('export') or '').strip().lower() in {
        '1',
        'excel',
        'xls',
    }
    report = None
    error = ''
    branches: list[dict] = []

    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from') or month_start.isoformat(),
            request.GET.get('date_to') or today.isoformat(),
        )
    except ValidationError as exc:
        return render(
            request,
            'search/browse_suppliers.html',
            {
                'date_from': (request.GET.get('date_from') or '')[:10],
                'date_to': (request.GET.get('date_to') or '')[:10],
                'default_from': month_start.isoformat(),
                'default_to': today.isoformat(),
                'selected_branch': selected_branch,
                'q': q,
                'scope': scope,
                'branches': [],
                'report': None,
                'error': str(exc),
            },
        )

    try:
        from .oracle_income import fetch_income_branches
        from .oracle_stock import oracle_enabled, oracle_session
        from .oracle_suppliers import build_suppliers_excel, build_suppliers_report

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن عرض الموردين.'
        else:
            with oracle_session():
                branches = fetch_income_branches()
                if selected_branch not in {row['code'] for row in branches}:
                    selected_branch = ''
                report = build_suppliers_report(
                    date_from,
                    date_to,
                    branch_code=selected_branch,
                    q=q,
                    scope=scope,
                    limit=limit,
                )
                if want_excel and report is not None:
                    return build_suppliers_excel(report)
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_suppliers failed: %s', exc)
        error = f'تعذّر تحميل تقرير الموردين: {exc}'

    return render(
        request,
        'search/browse_suppliers.html',
        {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'default_from': month_start.isoformat(),
            'default_to': today.isoformat(),
            'selected_branch': selected_branch,
            'q': q,
            'scope': scope,
            'branches': branches,
            'report': report,
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
                raw_warehouses = fetch_warehouse_options(active_only=True)
                groups = fetch_sales_group_options()
                wh_codes = {w['code'] for w in raw_warehouses}
                group_codes = {g['code'] for g in groups}
                if selected_warehouse and selected_warehouse not in wh_codes:
                    selected_warehouse = ''
                if selected_group and selected_group not in group_codes:
                    selected_group = ''

                branch_map: dict[str, str] = {}
                warehouses = []
                for w in raw_warehouses:
                    code = str(w.get('code') or '').strip()
                    if not code:
                        continue
                    brn = str(w.get('branch_code') or '').strip()
                    if brn:
                        branch_map[brn] = str(w.get('branch_name') or brn)
                    clean = _clean_warehouse_name(code, str(w.get('name') or ''))
                    warehouses.append(
                        {
                            'code': code,
                            'name': clean,
                            'branch_code': brn,
                            'branch_name': str(w.get('branch_name') or brn or '—'),
                        }
                    )
                branches = [
                    {'code': code, 'name': name}
                    for code, name in sorted(
                        branch_map.items(), key=lambda x: (x[1], x[0])
                    )
                ]
                if selected_branch and selected_branch not in branch_map:
                    selected_branch = ''

                # إن وُجد فرع محدد ومخزن خارج نطاقه — ألغِ المخزن
                if selected_branch and selected_warehouse:
                    allowed = {
                        w['code']
                        for w in warehouses
                        if str(w.get('branch_code') or '') == selected_branch
                    }
                    if selected_warehouse not in allowed:
                        selected_warehouse = ''

            # خارج الجلسة: الاستعلامات الثقيلة تعمل بالتوازي بجلسات مستقلة
            insights = build_inventory_insights(
                warehouse=selected_warehouse,
                group_code=selected_group,
                branch_code=selected_branch,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_inventory failed: %s', exc)
        msg = str(exc or '').lower()
        if 'interpreter shutdown' in msg or 'cannot schedule' in msg:
            # غالباً بعد حفظ ملف وإعادة تحميل runserver أثناء الطلب الطويل
            error = (
                'أُعيد تحميل الخادم أثناء التحليل — حدّث الصفحة وأعد المحاولة.'
            )
        else:
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
def browse_inventory_pack_errors(request):
    """كشف اختلاف P_SIZE — فلتر من فرع/مخزن إلى فرع/مخزن."""
    from datetime import date as date_cls

    error = ''
    report = None

    today = date_cls.today()
    month_start = today.replace(day=1)

    # من / إلى — مع توافق الروابط القديمة (branch / warehouse)
    selected_branch_from = str(
        request.GET.get('branch_from') or request.GET.get('branch') or ''
    ).strip()
    selected_branch_to = str(request.GET.get('branch_to') or '').strip()
    selected_warehouse_from = str(
        request.GET.get('warehouse_from')
        or request.GET.get('warehouse')
        or request.GET.get('wh')
        or ''
    ).strip()
    selected_warehouse_to = str(
        request.GET.get('warehouse_to') or request.GET.get('wh_to') or ''
    ).strip()
    warehouses_legacy = str(request.GET.get('warehouses') or '').strip()
    if not selected_warehouse_from and warehouses_legacy:
        legacy_parts = [
            p.strip()
            for p in warehouses_legacy.replace('،', ',').replace('-', ',').split(',')
            if p.strip()
        ]
        if len(legacy_parts) == 1:
            selected_warehouse_from = legacy_parts[0]
            warehouses_legacy = ''

    limit_raw = str(request.GET.get('limit') or '120').strip()

    date_from_raw = str(request.GET.get('date_from') or month_start.isoformat()).strip()
    date_to_raw = str(request.GET.get('date_to') or today.isoformat()).strip()

    # ثابتان داخلياً — أُلغيا من واجهة الفلاتر
    min_pack_size = 2.0
    tolerance_pct = 0.1
    try:
        limit = int(limit_raw or 120)
    except ValueError:
        error = 'قيمة عدد النتائج غير صحيحة.'
        limit = 120

    try:
        date_from, date_to = _parse_sales_dates(date_from_raw, date_to_raw)
        if (date_to - date_from).days > 90:
            raise ValidationError('الفترة القصوى لهذا التقرير 90 يوم.')
    except ValidationError as exc:
        error = str(exc)
        date_from, date_to = month_start, today

    branches: list[dict] = []
    all_warehouses: list[dict] = []
    warehouses_from: list[dict] = []
    warehouses_to: list[dict] = []
    wh_codes_raw = ''

    try:
        from .oracle_stock import fetch_warehouse_options, oracle_enabled, oracle_session

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن عرض تقرير أخطاء الوحدات.'
        else:
            with oracle_session():
                wh_rows = fetch_warehouse_options(active_only=True) or []
                all_warehouses = [
                    {
                        'code': str(w.get('code') or '').strip(),
                        'name': str(w.get('name') or '').strip()
                        or str(w.get('code') or '').strip(),
                        'branch_code': str(w.get('branch_code') or '').strip(),
                        'branch_name': str(w.get('branch_name') or '').strip(),
                    }
                    for w in wh_rows
                    if str(w.get('code') or '').strip()
                ]
                branch_map: dict[str, str] = {}
                for w in all_warehouses:
                    brn = w['branch_code']
                    if brn:
                        branch_map[brn] = w['branch_name'] or brn
                branches = [
                    {'code': code, 'name': name}
                    for code, name in sorted(
                        branch_map.items(), key=lambda x: (x[1], x[0])
                    )
                ]

                if selected_branch_from and selected_branch_from not in branch_map:
                    selected_branch_from = ''
                if selected_branch_to and selected_branch_to not in branch_map:
                    selected_branch_to = ''

                warehouses_from = [
                    w
                    for w in all_warehouses
                    if not selected_branch_from
                    or w['branch_code'] == selected_branch_from
                ]
                warehouses_from.sort(key=lambda w: (w['name'], w['code']))
                warehouses_to = [
                    w
                    for w in all_warehouses
                    if not selected_branch_to
                    or w['branch_code'] == selected_branch_to
                ]
                warehouses_to.sort(key=lambda w: (w['name'], w['code']))

                from_allowed = {w['code'] for w in warehouses_from}
                to_allowed = {w['code'] for w in warehouses_to}
                if (
                    selected_warehouse_from
                    and selected_warehouse_from not in from_allowed
                ):
                    selected_warehouse_from = ''
                if (
                    selected_warehouse_to
                    and selected_warehouse_to not in to_allowed
                ):
                    selected_warehouse_to = ''

                if selected_warehouse_from:
                    wh_codes_raw = selected_warehouse_from
                elif warehouses_legacy:
                    parsed = [
                        p.strip()
                        for p in warehouses_legacy.replace('،', ',')
                        .replace('-', ',')
                        .split(',')
                        if p.strip() and p.strip() in from_allowed
                    ]
                    wh_codes_raw = ','.join(parsed)
                elif selected_branch_from:
                    # فرع مصدر فقط: أوراكل يفلتر عبر CONN_BRN_NO
                    wh_codes_raw = ''
                else:
                    # كل المصادر: بدون قائمة مخازن إن وُجدت وجهة؛ وإلا كل المخازن
                    if selected_branch_to or selected_warehouse_to:
                        wh_codes_raw = ''
                    else:
                        wh_codes_raw = ','.join(
                            sorted({w['code'] for w in all_warehouses})
                        )

                from .oracle_unit_pack_mismatch import build_unit_pack_mismatch_report

                report = build_unit_pack_mismatch_report(
                    date_from,
                    date_to,
                    warehouse_codes=wh_codes_raw,
                    branch_from=selected_branch_from,
                    branch_to=selected_branch_to,
                    warehouse_to=selected_warehouse_to,
                    min_pack_size=min_pack_size,
                    tolerance_pct=tolerance_pct,
                    limit=limit,
                )

                wh_name_map = {w['code']: w['name'] for w in all_warehouses}
                for r in report.get('rows') or []:
                    r['wh_name'] = (
                        wh_name_map.get(str(r.get('wh_code') or '').strip())
                        or '—'
                    )
    except ValidationError as exc:
        error = str(exc)
        report = None
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_inventory_pack_errors failed: %s', exc)
        error = f'تعذّر تحميل تقرير أخطاء الوحدات: {exc}'
        report = None

    return render(
        request,
        'search/browse_inventory_pack_errors.html',
        {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'default_from': month_start.isoformat(),
            'default_to': today.isoformat(),
            'branches': branches,
            'selected_branch_from': selected_branch_from,
            'selected_branch_to': selected_branch_to,
            'selected_warehouse_from': selected_warehouse_from,
            'selected_warehouse_to': selected_warehouse_to,
            'all_warehouses': all_warehouses,
            'warehouses_from': warehouses_from,
            'warehouses_to': warehouses_to,
            'warehouses_raw': wh_codes_raw,
            'limit': limit,
            'report': report,
            'error': error,
        },
    )


@login_required
@require_GET
@never_cache
def browse_inventory_pack_errors_detail(request):
    """فرق الشد للتحويلات الصادرة المخالفة — مجمّع حسب فرع الوجهة."""
    from datetime import date as date_cls
    from urllib.parse import urlencode

    from django.urls import reverse

    error = ''
    detail = None
    today = date_cls.today()
    month_start = today.replace(day=1)

    date_from_raw = str(request.GET.get('date_from') or month_start.isoformat()).strip()
    date_to_raw = str(request.GET.get('date_to') or today.isoformat()).strip()
    item_code = str(request.GET.get('item') or '').strip()
    wh_code = str(request.GET.get('wh') or '').strip()
    unit = str(request.GET.get('unit') or '').strip()
    buy_ps_raw = str(request.GET.get('buy_ps') or '').strip()
    out_ps_raw = str(request.GET.get('out_ps') or '').strip()

    back_parts = []
    for key, val in (
        ('date_from', date_from_raw),
        ('date_to', date_to_raw),
        ('branch_from', str(request.GET.get('branch_from') or request.GET.get('branch') or '').strip()),
        ('branch_to', str(request.GET.get('branch_to') or '').strip()),
        ('warehouse_from', str(request.GET.get('warehouse_from') or request.GET.get('warehouse') or '').strip()),
        ('warehouse_to', str(request.GET.get('warehouse_to') or '').strip()),
        ('warehouses', str(request.GET.get('warehouses') or '').strip()),
        ('limit', str(request.GET.get('limit') or '').strip()),
    ):
        if val:
            back_parts.append((key, val))
    back_url = reverse('browse_inventory_pack_errors')
    if back_parts:
        back_url = f"{back_url}?{urlencode(back_parts)}"

    try:
        date_from, date_to = _parse_sales_dates(date_from_raw, date_to_raw)
        buy_ps = float(buy_ps_raw)
        out_ps = float(out_ps_raw)
    except (ValidationError, ValueError, TypeError) as exc:
        error = str(exc) if isinstance(exc, ValidationError) else 'معاملات التفصيل غير مكتملة.'
        date_from, date_to = month_start, today
        buy_ps = out_ps = 0.0

    if not error and (not item_code or not wh_code or not unit):
        error = 'يلزم الصنف والمخزن والوحدة لعرض التفصيل.'

    if not error:
        try:
            from .oracle_stock import oracle_enabled, oracle_session
            from .oracle_unit_pack_mismatch import build_pack_mismatch_branch_detail

            if not oracle_enabled():
                error = 'أوراكل غير مفعّل.'
            else:
                with oracle_session():
                    detail = build_pack_mismatch_branch_detail(
                        date_from,
                        date_to,
                        item_code=item_code,
                        wh_code=wh_code,
                        unit=unit,
                        purchase_p_size=buy_ps,
                        transfer_p_size=out_ps,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning('browse_inventory_pack_errors_detail failed: %s', exc)
            error = f'تعذّر تحميل تفصيل فرق الشد: {exc}'
            detail = None

    return render(
        request,
        'search/browse_inventory_pack_errors_detail.html',
        {
            'detail': detail,
            'error': error,
            'back_url': back_url,
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
        },
    )


@login_required
@require_GET
@never_cache
def browse_vendor_turnover(request):
    """دوران مخزون الموردين — كمية واردة مقابل مباعة واستحقاق السداد."""
    from datetime import date
    from urllib.parse import urlencode

    from django.urls import reverse

    today = date.today()
    month_start = today.replace(day=1)
    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_vendor = str(request.GET.get('vendor') or '').strip()
    selected_decision = str(request.GET.get('decision') or '').strip().lower()
    vendor_q = str(request.GET.get('q') or '').strip()[:80]
    view_mode = str(request.GET.get('view') or '').strip().lower()
    want_excel = str(request.GET.get('export') or '').strip().lower() in {
        '1',
        'excel',
        'xls',
        'xlsx',
    }
    if selected_decision not in {'', 'settle', 'partial', 'hold'}:
        selected_decision = ''
    report = None
    item_detail = None
    error = ''
    branches: list[dict] = []
    vendors: list[dict] = []

    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from'),
            request.GET.get('date_to'),
        )
    except ValidationError as exc:
        return render(
            request,
            'search/browse_vendor_turnover.html',
            {
                'date_from': (request.GET.get('date_from') or '')[:10],
                'date_to': (request.GET.get('date_to') or '')[:10],
                'default_from': month_start.isoformat(),
                'default_to': today.isoformat(),
                'selected_branch': selected_branch,
                'selected_vendor': selected_vendor,
                'selected_decision': selected_decision,
                'vendor_q': vendor_q,
                'view_mode': view_mode,
                'branches': [],
                'vendors': [],
                'report': None,
                'item_detail': None,
                'back_list_url': '',
                'error': str(exc),
            },
        )

    try:
        from .oracle_income import fetch_income_branches
        from .oracle_purchases import fetch_purchase_vendor_options
        from .oracle_stock import (
            _friendly_connect_error,
            _is_connect_timeout,
            _is_disconnect_error,
            oracle_enabled,
            oracle_session,
        )
        from .oracle_vendor_turnover import (
            apply_decision_filter,
            apply_vendor_query,
            build_vendor_item_detail,
            build_vendor_item_detail_excel,
            build_vendor_turnover,
            build_vendor_turnover_excel,
            peek_vendor_item_detail,
            peek_vendor_turnover,
        )


        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن حساب دوران الموردين.'
        elif view_mode == 'items':
            if not selected_vendor:
                error = 'اختر مورداً من الجدول لعرض أصنافه.'
            else:
                try:
                    item_detail = build_vendor_item_detail(
                        date_from,
                        date_to,
                        vendor_code=selected_vendor,
                        branch_code=selected_branch,
                    )
                except Exception as item_exc:  # noqa: BLE001
                    cached_items = peek_vendor_item_detail(
                        date_from,
                        date_to,
                        vendor_code=selected_vendor,
                        branch_code=selected_branch,
                    )
                    if cached_items is not None and (
                        _is_connect_timeout(item_exc)
                        or _is_disconnect_error(item_exc)
                    ):
                        item_detail = cached_items
                        error = (
                            'تعذّر الاتصال بأوراكل — عرض أصناف محفوظة مسبقاً. '
                            'تحقق من VPN/الإنترنت ثم حدّث الصفحة.'
                        )
                    else:
                        raise
                if want_excel and item_detail is not None:
                    return build_vendor_item_detail_excel(item_detail)
            try:
                with oracle_session():
                    branches = fetch_income_branches()
                    vendors = fetch_purchase_vendor_options(date_from, date_to)
                    if selected_branch not in {row['code'] for row in branches}:
                        selected_branch = ''
            except Exception as opt_exc:  # noqa: BLE001
                logger.warning(
                    'browse_vendor_turnover options failed: %s', opt_exc
                )
        else:
            # التقرير يعمل باستعلامات متوازية (اتصالات مستقلة) — خارج جلسة واحدة
            try:
                report = build_vendor_turnover(
                    date_from,
                    date_to,
                    branch_code=selected_branch,
                    vendor_code=selected_vendor,
                )
            except Exception as turn_exc:  # noqa: BLE001
                cached = peek_vendor_turnover(
                    date_from,
                    date_to,
                    branch_code=selected_branch,
                    vendor_code=selected_vendor,
                )
                if cached is not None and (
                    _is_connect_timeout(turn_exc)
                    or _is_disconnect_error(turn_exc)
                ):
                    report = cached
                    error = (
                        'تعذّر الاتصال بأوراكل — عرض أرقام محفوظة مسبقاً. '
                        'تحقق من VPN/الإنترنت ثم حدّث الصفحة.'
                    )
                elif _is_connect_timeout(turn_exc) or _is_disconnect_error(
                    turn_exc
                ):
                    # إعادة محاولة واحدة بعد تصفير المجمّع — الشبكة غالباً متقطعة
                    import time

                    from .oracle_stock import _reset_pool

                    _reset_pool()
                    time.sleep(1.5)
                    try:
                        report = build_vendor_turnover(
                            date_from,
                            date_to,
                            branch_code=selected_branch,
                            vendor_code=selected_vendor,
                        )
                    except Exception as retry_exc:  # noqa: BLE001
                        raise retry_exc from turn_exc
                else:
                    raise
            if selected_decision and report:
                report = apply_decision_filter(report, selected_decision)
            if vendor_q and report:
                report = apply_vendor_query(report, vendor_q)
            if want_excel and report is not None:
                return build_vendor_turnover_excel(report)
            try:
                with oracle_session():
                    branches = fetch_income_branches()
                    vendors = fetch_purchase_vendor_options(date_from, date_to)
                    if selected_branch not in {row['code'] for row in branches}:
                        selected_branch = ''
                    if selected_vendor not in {row['code'] for row in vendors}:
                        selected_vendor = ''
            except Exception as opt_exc:  # noqa: BLE001
                logger.warning(
                    'browse_vendor_turnover options failed: %s', opt_exc
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_vendor_turnover failed: %s', exc)
        error = f'تعذّر حساب دوران الموردين: {exc}'

    back_qs = {
        'date_from': date_from.isoformat(),
        'date_to': date_to.isoformat(),
    }
    if selected_branch:
        back_qs['branch'] = selected_branch
    if selected_decision:
        back_qs['decision'] = selected_decision
    if vendor_q:
        back_qs['q'] = vendor_q
    back_list_url = f"{reverse('browse_vendor_turnover')}?{urlencode(back_qs)}"

    return render(
        request,
        'search/browse_vendor_turnover.html',
        {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'default_from': month_start.isoformat(),
            'default_to': today.isoformat(),
            'selected_branch': selected_branch,
            'selected_vendor': selected_vendor,
            'selected_decision': selected_decision,
            'vendor_q': vendor_q,
            'view_mode': view_mode,
            'branches': branches,
            'vendors': vendors,
            'report': report,
            'item_detail': item_detail,
            'back_list_url': back_list_url,
            'error': error,
        },
    )


@login_required
@require_GET
@never_cache
def browse_vendor_price_compare(request):
    """مقارنة أسعار شراء المورد بين الفروع/المخازن لكشف فروقات التسعير."""
    from datetime import date as date_cls

    today = date_cls.today()
    month_start = today.replace(day=1)
    date_from_raw = str(request.GET.get('date_from') or month_start.isoformat()).strip()
    date_to_raw = str(request.GET.get('date_to') or today.isoformat()).strip()
    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_warehouse = str(request.GET.get('warehouse') or '').strip()
    selected_vendor = str(request.GET.get('vendor') or '').strip()
    vendor_q = str(request.GET.get('q') or '').strip()[:80]
    # افتراضي: كل أسعار الشراء من الفواتير (بدون حد فرق)
    diffs_only = False
    diffs_vals = request.GET.getlist('diffs')
    if diffs_vals:
        diffs_only = str(diffs_vals[-1]).strip() not in ('0', 'false', 'no', 'off')
    want_excel = str(request.GET.get('export') or '').strip().lower() in {
        '1',
        'excel',
        'xls',
        'xlsx',
    }
    run_raw = str(request.GET.get('run') or '').strip().lower()
    want_run = run_raw in ('1', 'true', 'yes') or want_excel

    report = None
    error = ''
    hint = ''
    branches: list[dict] = []
    vendors: list[dict] = []
    all_warehouses: list[dict] = []
    branch_warehouses: list[dict] = []
    selected_vendor_name = ''

    try:
        date_from, date_to = _parse_sales_dates(date_from_raw, date_to_raw)
    except ValidationError as exc:
        return render(
            request,
            'search/browse_vendor_price_compare.html',
            {
                'date_from': (date_from_raw or '')[:10],
                'date_to': (date_to_raw or '')[:10],
                'default_from': month_start.isoformat(),
                'default_to': today.isoformat(),
                'selected_branch': selected_branch,
                'selected_warehouse': selected_warehouse,
                'selected_vendor': selected_vendor,
                'vendor_q': vendor_q,
                'diffs_only': diffs_only,
                'branches': [],
                'vendors': [],
                'all_warehouses': [],
                'branch_warehouses': [],
                'report': None,
                'error': str(exc),
                'hint': '',
            },
        )

    try:
        from .oracle_stock import (
            fetch_company_vendor_options,
            fetch_warehouse_options,
            oracle_enabled,
            oracle_session,
        )
        from .oracle_vendor_price_compare import (
            build_vendor_price_compare,
            build_vendor_price_compare_excel,
        )

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن مقارنة أسعار الشراء.'
        else:
            with oracle_session():
                wh_rows = fetch_warehouse_options(active_only=True) or []
                all_warehouses = [
                    {
                        'code': str(w.get('code') or '').strip(),
                        'name': str(w.get('name') or '').strip()
                        or str(w.get('code') or '').strip(),
                        'branch_code': str(w.get('branch_code') or '').strip(),
                        'branch_name': str(w.get('branch_name') or '').strip(),
                    }
                    for w in wh_rows
                    if str(w.get('code') or '').strip()
                ]
                branch_map: dict[str, str] = {}
                for w in all_warehouses:
                    brn = w['branch_code']
                    if brn:
                        branch_map[brn] = w['branch_name'] or brn
                branches = [
                    {'code': code, 'name': name}
                    for code, name in sorted(
                        branch_map.items(), key=lambda x: (x[1], x[0])
                    )
                ]
                if selected_branch and selected_branch not in branch_map:
                    selected_branch = ''

                branch_warehouses = [
                    w
                    for w in all_warehouses
                    if not selected_branch or w['branch_code'] == selected_branch
                ]
                branch_warehouses.sort(key=lambda w: (w['name'], w['code']))
                wh_allowed = {w['code'] for w in branch_warehouses}
                if selected_warehouse and selected_warehouse not in wh_allowed:
                    selected_warehouse = ''

                # موردو مجموعة 23 / «موردين الشركة» — نفس مصدر بحث الأصناف
                vendors = fetch_company_vendor_options() or []
                all_vendor_codes = {
                    str(v.get('code') or '').strip() for v in vendors if v.get('code')
                }
                if selected_vendor and selected_vendor not in all_vendor_codes:
                    selected_vendor = ''
                selected_vendor_name = ''
                for v in vendors:
                    if str(v.get('code') or '').strip() == selected_vendor:
                        selected_vendor_name = str(v.get('name') or '').strip()
                        break

                if want_run and not error:
                    if not selected_vendor:
                        raise ValidationError('اختر مورداً لمقارنة أسعار الشراء بين الفروع.')
                    report = build_vendor_price_compare(
                        date_from,
                        date_to,
                        vendor_code=selected_vendor,
                        branch_code=selected_branch,
                        warehouse_code=selected_warehouse,
                        min_spread_pct=0.0,
                        diffs_only=diffs_only,
                    )
                    if want_excel and report is not None:
                        return build_vendor_price_compare_excel(report)
                elif not error:
                    hint = 'اختر المورد ثم اضغط «عرض الأسعار» لجلب آخر سعر شراء من الفواتير.'
    except ValidationError as exc:
        error = str(exc)
        report = None
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_vendor_price_compare failed: %s', exc)
        error = f'تعذّر تحميل مقارنة أسعار الموردين: {exc}'
        report = None

    return render(
        request,
        'search/browse_vendor_price_compare.html',
        {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'default_from': month_start.isoformat(),
            'default_to': today.isoformat(),
            'selected_branch': selected_branch,
            'selected_warehouse': selected_warehouse,
            'selected_vendor': selected_vendor,
            'selected_vendor_name': selected_vendor_name,
            'vendor_q': vendor_q,
            'diffs_only': diffs_only,
            'branches': branches,
            'vendors': vendors,
            'all_warehouses': all_warehouses,
            'branch_warehouses': branch_warehouses,
            'report': report,
            'error': error,
            'hint': hint,
        },
    )


@login_required
@require_GET
@never_cache
def browse_low_margin_prices(request):
    """أصناف مسعّرة بنسبة ربح أقل من حد معيّن (افتراضي 15%)."""
    from django.urls import reverse

    error = ''
    hint = ''
    report = None

    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_group = str(request.GET.get('group') or '').strip()
    wh_codes_raw = str(request.GET.get('warehouses') or '').strip()
    item_q = str(request.GET.get('q') or '').strip()
    min_prft_raw = str(request.GET.get('min_profit') or '').strip()
    max_prft_raw = str(request.GET.get('max_profit') or '').strip()
    lev_raw = str(request.GET.get('lev') or '1').strip()
    want_excel = str(request.GET.get('export') or '').strip().lower() in {
        '1',
        'excel',
        'xls',
        'xlsx',
    }
    include_negative = str(request.GET.get('include_neg') or '1').strip() not in (
        '0',
        'false',
        'no',
    )
    submitted = str(request.GET.get('run') or '').strip() in ('1', 'true', 'yes') or want_excel

    min_profit: float | None = None
    min_profit_input = ''
    max_profit = 15.0
    max_profit_input = _decimal_input_str(15.0)
    lev_no = 1
    if submitted and not max_prft_raw:
        error = 'حد الربح «إلى» مطلوب — اكتب النسبة العليا (الافتراضي 15).'
    else:
        try:
            if max_prft_raw:
                max_profit = _parse_decimal_param(max_prft_raw)
                max_profit_input = _decimal_input_str(max_profit)
            elif not submitted:
                max_profit_input = _decimal_input_str(15.0)
            if min_prft_raw:
                min_profit = _parse_decimal_param(min_prft_raw)
                min_profit_input = _decimal_input_str(min_profit)
            lev_no = int(lev_raw or 1)
            if max_profit < 0:
                error = 'حد الربح «إلى» يجب أن يكون صفراً أو أكثر.'
            if min_profit is not None and min_profit >= max_profit:
                error = '«من» يجب أن يكون أقل من «إلى».'
        except ValueError:
            error = 'قيم نطاق الربح / المستوى غير صحيحة — استخدم نقطة للكسور (مثل 5 أو 15).'
            min_profit = None
            min_profit_input = ''
            max_profit = 15.0
            max_profit_input = _decimal_input_str(15.0)
            lev_no = 1

    branches: list[dict] = []
    warehouses: list[dict] = []
    groups: list[dict] = []
    wh_name_map: dict[str, str] = {}

    try:
        from .oracle_stock import (
            fetch_sales_group_options,
            fetch_warehouse_options,
            oracle_enabled,
            oracle_session,
        )

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن عرض الأسعار منخفضة الربح.'
        else:
            with oracle_session():
                wh_rows = fetch_warehouse_options(active_only=True) or []
                groups = fetch_sales_group_options() or []
            warehouses = wh_rows
            wh_name_map = _low_margin_wh_name_map(warehouses)
            branches = _low_margin_branches(warehouses)

            if selected_group and selected_group not in {
                str(g.get('code') or '') for g in groups
            }:
                selected_group = ''

            if submitted and not error:
                with oracle_session():
                    wh_allowed = {w.get('code') for w in warehouses if w.get('code')}
                if selected_branch and selected_branch not in {
                    b['code'] for b in branches
                }:
                    selected_branch = ''

                if selected_branch:
                    wh_allowed = {
                        w.get('code')
                        for w in warehouses
                        if str(w.get('branch_code') or '') == selected_branch
                        and w.get('code')
                    }

                if wh_codes_raw:
                    parsed = [
                        p.strip()
                        for p in wh_codes_raw.replace('،', ',')
                        .replace('-', ',')
                        .split(',')
                        if p.strip()
                    ]
                    if wh_allowed:
                        keep = [p for p in parsed if p in wh_allowed]
                        if not keep and not selected_branch:
                            keep = parsed
                        parsed = keep
                    wh_codes_raw = ','.join(parsed)

                if not wh_codes_raw and not selected_branch:
                    error = (
                        'اختر مخزناً محدداً قبل العرض — '
                        'نفس اتصال أوراكل المستخدم في باقي الموقع.'
                    )
                elif want_excel:
                    from .oracle_low_margin_prices import build_low_margin_excel

                    return build_low_margin_excel(
                        min_profit_pct=min_profit,
                        max_profit_pct=max_profit,
                        lev_no=lev_no,
                        warehouse_codes=wh_codes_raw or None,
                        item_q=item_q,
                        include_negative=include_negative,
                        wh_name_map=wh_name_map,
                        branch_code=selected_branch if not wh_codes_raw else '',
                        group_code=selected_group,
                    )
                else:
                    from .oracle_low_margin_prices import fetch_low_margin_priced_items

                    # مخزن واحد: جلب كل الأصناف دفعة واحدة (حتى 100 ألف)
                    is_single_wh = bool(
                        wh_codes_raw and ',' not in wh_codes_raw
                    )
                    fetch_limit = 100_000 if is_single_wh else 200
                    report = fetch_low_margin_priced_items(
                        min_profit_pct=min_profit,
                        max_profit_pct=max_profit,
                        lev_no=lev_no,
                        warehouse_codes=wh_codes_raw or None,
                        item_q=item_q,
                        limit=fetch_limit,
                        offset=0,
                        include_negative=include_negative,
                        with_total=True,
                        branch_code=selected_branch if not wh_codes_raw else '',
                        group_code=selected_group,
                    )
                    for r in report.get('rows') or []:
                        r['wh_name'] = (
                            wh_name_map.get(str(r.get('wh_code') or '').strip())
                            or '—'
                        )
            elif not branches:
                hint = 'قائمة الفروع غير متاحة حالياً — أعد المحاولة بعد اتصال أوراكل.'
            else:
                hint = (
                    'اختر فرعاً ومخزناً والمجموعة اختيارياً، '
                    'حدّد نطاق الربح % (من → إلى، افتراضي إلى 15)، ثم اضغط عرض.'
                )
    except ValidationError as exc:
        error = str(exc)
        report = None
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_low_margin_prices failed: %s', exc)
        msg = str(exc)
        low = msg.lower()
        if any(t in msg for t in ('12170', '12541', 'Cannot connect', 'مهلة الشبكة')):
            error = (
                'تعذّر الاتصال بأوراكل حالياً (انقطاع الشبكة). '
                'أعد المحاولة بعد عودة الاتصال.'
            )
        elif 'مهلة جلب' in msg or 'call timeout' in low or 'dpy-4011' in low:
            error = (
                'الاستعلام طال على أوراكل — اختر مخزناً واحداً أو صنفاً وحاول مجدداً.'
            )
        else:
            error = f'تعذّر تحميل الأصناف منخفضة الربح: {exc}'
        report = None

    api_url = reverse('browse_low_margin_prices_api')

    selected_warehouse = ''
    if wh_codes_raw and ',' not in wh_codes_raw:
        selected_warehouse = wh_codes_raw.strip()
    elif wh_codes_raw:
        selected_warehouse = wh_codes_raw.split(',')[0].strip()

    branch_warehouses: list[dict] = []
    if selected_branch:
        branch_warehouses = [
            {
                'code': str(w.get('code') or '').strip(),
                'name': str(w.get('name') or w.get('code') or '').strip(),
                'branch_code': str(w.get('branch_code') or '').strip(),
            }
            for w in warehouses
            if str(w.get('branch_code') or '').strip() == selected_branch
            and str(w.get('code') or '').strip()
        ]
    else:
        # كل الفروع → كل المخازن في القائمة
        branch_warehouses = [
            {
                'code': str(w.get('code') or '').strip(),
                'name': str(w.get('name') or w.get('code') or '').strip(),
                'branch_code': str(w.get('branch_code') or '').strip(),
            }
            for w in warehouses
            if str(w.get('code') or '').strip()
        ]
    branch_warehouses.sort(key=lambda w: (w['name'], w['code']))

    all_warehouses = [
        {
            'code': str(w.get('code') or '').strip(),
            'name': str(w.get('name') or w.get('code') or '').strip(),
            'branch_code': str(w.get('branch_code') or '').strip(),
        }
        for w in warehouses
        if str(w.get('code') or '').strip()
    ]

    return render(
        request,
        'search/browse_low_margin_prices.html',
        {
            'branches': branches,
            'selected_branch': selected_branch,
            'groups': groups,
            'selected_group': selected_group,
            'warehouses': warehouses,
            'all_warehouses': all_warehouses,
            'branch_warehouses': branch_warehouses,
            'selected_warehouse': selected_warehouse,
            'warehouses_raw': wh_codes_raw,
            'item_q': item_q,
            'min_profit': min_profit,
            'min_profit_input': min_profit_input,
            'max_profit': max_profit,
            'max_profit_input': max_profit_input,
            'lev_no': lev_no,
            'limit': 20,
            'include_negative': include_negative,
            'report': report,
            'error': error,
            'hint': hint,
            'api_url': api_url,
            'page_size': 200,
            'scroll_max': (report or {}).get('meta', {}).get('scroll_max', 500000) if report else 500000,
        },
    )


@login_required
@require_GET
@never_cache
def browse_low_margin_prices_api(request):
    """صفحة إضافية من الأصناف منخفضة الربح (تمرير لا نهائي)."""
    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_group = str(request.GET.get('group') or '').strip()
    wh_codes_raw = str(request.GET.get('warehouses') or '').strip()
    item_q = str(request.GET.get('q') or '').strip()
    min_prft_raw = str(request.GET.get('min_profit') or '').strip()
    max_prft_raw = str(request.GET.get('max_profit') or '').strip()
    lev_raw = str(request.GET.get('lev') or '1').strip()
    include_negative = str(request.GET.get('include_neg') or '1').strip() not in (
        '0',
        'false',
        'no',
    )

    if not max_prft_raw:
        return JsonResponse({'ok': False, 'error': 'حد الربح «إلى» مطلوب.'}, status=400)

    try:
        max_profit = _parse_decimal_param(max_prft_raw)
        min_profit = (
            _parse_decimal_param(min_prft_raw) if min_prft_raw else None
        )
        if min_profit is not None and min_profit >= max_profit:
            return JsonResponse(
                {'ok': False, 'error': '«من» يجب أن يكون أقل من «إلى».'},
                status=400,
            )
        lev_no = int(lev_raw or 1)
        offset = max(0, int(request.GET.get('offset') or 0))
        limit = min(max(1, int(request.GET.get('limit') or 200)), 500)
    except (TypeError, ValueError):
        return JsonResponse({'ok': False, 'error': 'معاملات غير صحيحة.'}, status=400)

    rows: list = []
    has_more = False
    total_matching = 0
    try:
        from .oracle_low_margin_prices import fetch_low_margin_priced_items
        from .oracle_stock import fetch_warehouse_options, oracle_enabled, oracle_session

        if not oracle_enabled():
            return JsonResponse({'ok': False, 'error': 'أوراكل غير مفعّل.'}, status=400)

        with oracle_session():
            warehouses = fetch_warehouse_options(active_only=True)
            wh_allowed = {w.get('code') for w in warehouses if w.get('code')}
            if selected_branch:
                warehouses = [
                    w
                    for w in warehouses
                    if str(w.get('branch_code') or '') == selected_branch
                ]
                wh_allowed = {w.get('code') for w in warehouses if w.get('code')}
            if wh_codes_raw:
                parsed = [
                    p.strip()
                    for p in wh_codes_raw.replace('،', ',').replace('-', ',').split(',')
                    if p.strip()
                ]
                if wh_allowed:
                    keep = [p for p in parsed if p in wh_allowed]
                    if not keep and not selected_branch:
                        keep = parsed
                    parsed = keep
                wh_codes_raw = ','.join(parsed)

            wh_name_map = {
                str(w.get('code') or '').strip(): str(w.get('name') or '').strip()
                or str(w.get('code') or '').strip()
                for w in fetch_warehouse_options(active_only=True)
            }

            report = fetch_low_margin_priced_items(
                min_profit_pct=min_profit,
                max_profit_pct=max_profit,
                lev_no=lev_no,
                warehouse_codes=wh_codes_raw or None,
                item_q=item_q,
                limit=limit,
                offset=offset,
                include_negative=include_negative,
                with_total=True,
                branch_code=selected_branch if not wh_codes_raw else '',
                group_code=selected_group,
            )
            rows = report.get('rows') or []
            for r in rows:
                r['wh_name'] = wh_name_map.get(str(r.get('wh_code') or '').strip()) or '—'
            has_more = bool((report.get('kpis') or {}).get('has_more'))
            try:
                total_matching = int((report.get('kpis') or {}).get('total_matching') or 0)
            except (TypeError, ValueError):
                total_matching = 0
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_low_margin_prices_api failed: %s', exc)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)

    return JsonResponse(
        {
            'ok': True,
            'rows': rows,
            'offset': offset,
            'limit': limit,
            'has_more': has_more,
            'total': total_matching,
        }
    )


@login_required
@require_GET
@never_cache
def browse_unpriced_items(request):
    """أصناف بلا سعر بيع على وحدة البيع — فلتر فرع/مخزن/مجموعة."""
    error = ''
    hint = ''
    report = None

    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_warehouse = str(request.GET.get('warehouse') or '').strip()
    selected_group = str(request.GET.get('group') or '').strip()
    item_q = str(request.GET.get('q') or '').strip()
    lev_no = 1
    want_excel = str(request.GET.get('export') or '').strip().lower() in {
        '1',
        'excel',
        'xls',
        'xlsx',
    }
    submitted = str(request.GET.get('run') or '').strip() in ('1', 'true', 'yes') or want_excel

    branches: list[dict] = []
    warehouses: list[dict] = []
    groups: list[dict] = []
    wh_name_map: dict[str, str] = {}

    try:
        from .oracle_income import fetch_income_branches
        from .oracle_stock import (
            fetch_sales_group_options,
            fetch_warehouse_options,
            oracle_enabled,
            oracle_session,
        )

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن عرض الأصناف غير المسعّرة.'
        else:
            with oracle_session():
                warehouses = fetch_warehouse_options(active_only=True) or []
                groups = fetch_sales_group_options() or []
                branches = _low_margin_branches(warehouses) or fetch_income_branches()
            wh_name_map = _low_margin_wh_name_map(warehouses)

            if selected_branch and selected_branch not in {b['code'] for b in branches}:
                selected_branch = ''
            if selected_group and selected_group not in {
                str(g.get('code') or '') for g in groups
            }:
                selected_group = ''

            branch_wh_codes = {
                str(w.get('code') or '').strip()
                for w in warehouses
                if (
                    not selected_branch
                    or str(w.get('branch_code') or '').strip() == selected_branch
                )
                and str(w.get('code') or '').strip()
            }
            if selected_warehouse and selected_warehouse not in branch_wh_codes:
                if not selected_branch:
                    # ابقِ المخزن إن وُجد في القائمة الكاملة
                    if selected_warehouse not in {
                        str(w.get('code') or '').strip() for w in warehouses
                    }:
                        selected_warehouse = ''
                else:
                    selected_warehouse = ''

            if submitted and not error:
                if not selected_warehouse:
                    error = 'اختر مخزناً محدداً قبل العرض.'
                elif want_excel:
                    from .oracle_unpriced_items import build_unpriced_excel

                    with oracle_session():
                        return build_unpriced_excel(
                            warehouse_code=selected_warehouse,
                            lev_no=lev_no,
                            group_code=selected_group,
                            item_q=item_q,
                            wh_name=wh_name_map.get(selected_warehouse) or '',
                        )
                else:
                    from .oracle_unpriced_items import fetch_unpriced_items

                    with oracle_session():
                        report = fetch_unpriced_items(
                            warehouse_code=selected_warehouse,
                            lev_no=lev_no,
                            group_code=selected_group,
                            item_q=item_q,
                            limit=50_000,
                            offset=0,
                            with_total=True,
                        )
                    for r in report.get('rows') or []:
                        r['wh_name'] = (
                            wh_name_map.get(str(r.get('wh_code') or '').strip()) or '—'
                        )
            elif not error:
                hint = (
                    'اختر فرعاً ومخزناً (والمجموعة اختيارياً)، ثم اعرض الأصناف '
                    'ذات الكمية بلا تسعير على وحدة البيع.'
                )
    except ValidationError as exc:
        error = str(exc)
        report = None
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_unpriced_items failed: %s', exc)
        msg = str(exc)
        low = msg.lower()
        if any(t in msg for t in ('12170', '12541', 'Cannot connect', 'مهلة الشبكة')):
            error = (
                'تعذّر الاتصال بأوراكل حالياً (انقطاع الشبكة). '
                'أعد المحاولة بعد عودة الاتصال.'
            )
        elif 'مهلة جلب' in msg or 'call timeout' in low or 'dpy-4011' in low:
            error = 'الاستعلام طال على أوراكل — ضيّق المجموعة أو ابحث بصنف وحاول مجدداً.'
        else:
            error = f'تعذّر تحميل الأصناف غير المسعّرة: {exc}'
        report = None

    branch_warehouses = [
        {
            'code': str(w.get('code') or '').strip(),
            'name': str(w.get('name') or w.get('code') or '').strip(),
            'branch_code': str(w.get('branch_code') or '').strip(),
        }
        for w in warehouses
        if str(w.get('code') or '').strip()
        and (
            not selected_branch
            or str(w.get('branch_code') or '').strip() == selected_branch
        )
    ]
    branch_warehouses.sort(key=lambda w: (w['name'], w['code']))

    all_warehouses = [
        {
            'code': str(w.get('code') or '').strip(),
            'name': str(w.get('name') or w.get('code') or '').strip(),
            'branch_code': str(w.get('branch_code') or '').strip(),
        }
        for w in warehouses
        if str(w.get('code') or '').strip()
    ]

    return render(
        request,
        'search/browse_unpriced_items.html',
        {
            'branches': branches,
            'selected_branch': selected_branch,
            'branch_warehouses': branch_warehouses,
            'all_warehouses': all_warehouses,
            'selected_warehouse': selected_warehouse,
            'groups': groups,
            'selected_group': selected_group,
            'item_q': item_q,
            'lev_no': lev_no,
            'report': report,
            'error': error,
            'hint': hint,
            'wh_name': wh_name_map.get(selected_warehouse) or '',
        },
    )


@login_required
@require_GET
@never_cache
def browse_below_cost_prices(request):
    """أصناف مسعّرة بأقل من متوسط التكلفة — مستوى 1 سعر بيع · كل الوحدات."""
    error = ''
    hint = ''
    report = None

    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_warehouse = str(request.GET.get('warehouse') or '').strip()
    selected_group = str(request.GET.get('group') or '').strip()
    item_q = str(request.GET.get('q') or '').strip()
    want_excel = str(request.GET.get('export') or '').strip().lower() in {
        '1',
        'excel',
        'xls',
        'xlsx',
    }
    submitted = str(request.GET.get('run') or '').strip() in ('1', 'true', 'yes') or want_excel

    branches: list[dict] = []
    warehouses: list[dict] = []
    groups: list[dict] = []
    wh_name_map: dict[str, str] = {}

    try:
        from .oracle_income import fetch_income_branches
        from .oracle_stock import (
            fetch_sales_group_options,
            fetch_warehouse_options,
            oracle_enabled,
            oracle_session,
        )

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن عرض التسعير الأقل من التكلفة.'
        else:
            with oracle_session():
                warehouses = fetch_warehouse_options(active_only=True) or []
                groups = fetch_sales_group_options() or []
                branches = _low_margin_branches(warehouses) or fetch_income_branches()
            wh_name_map = _low_margin_wh_name_map(warehouses)

            if selected_branch and selected_branch not in {b['code'] for b in branches}:
                selected_branch = ''
            if selected_group and selected_group not in {
                str(g.get('code') or '') for g in groups
            }:
                selected_group = ''

            branch_wh_codes = {
                str(w.get('code') or '').strip()
                for w in warehouses
                if (
                    not selected_branch
                    or str(w.get('branch_code') or '').strip() == selected_branch
                )
                and str(w.get('code') or '').strip()
            }
            if selected_warehouse and selected_warehouse not in branch_wh_codes:
                if not selected_branch:
                    if selected_warehouse not in {
                        str(w.get('code') or '').strip() for w in warehouses
                    }:
                        selected_warehouse = ''
                else:
                    selected_warehouse = ''

            if submitted and not error:
                if not selected_warehouse:
                    error = 'اختر مخزناً محدداً قبل العرض.'
                elif want_excel:
                    from .oracle_below_cost_prices import build_below_cost_excel

                    with oracle_session():
                        return build_below_cost_excel(
                            warehouse_code=selected_warehouse,
                            group_code=selected_group,
                            item_q=item_q,
                            wh_name=wh_name_map.get(selected_warehouse) or '',
                        )
                else:
                    from .oracle_below_cost_prices import fetch_below_cost_items

                    with oracle_session():
                        report = fetch_below_cost_items(
                            warehouse_code=selected_warehouse,
                            group_code=selected_group,
                            item_q=item_q,
                            limit=50_000,
                            offset=0,
                            with_total=True,
                        )
                    for r in report.get('rows') or []:
                        r['wh_name'] = (
                            wh_name_map.get(str(r.get('wh_code') or '').strip()) or '—'
                        )
            elif not error:
                hint = (
                    'اختر فرعاً ومخزناً، ثم اعرض الأصناف المسعّرة '
                    'بأقل من متوسط تكلفتها لنفس الوحدة (كأونكس: سعر 0.24 ومتوسط 0.18).'
                )
    except ValidationError as exc:
        error = str(exc)
        report = None
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_below_cost_prices failed: %s', exc)
        msg = str(exc)
        low = msg.lower()
        if any(t in msg for t in ('12170', '12541', 'Cannot connect', 'مهلة الشبكة')):
            error = (
                'تعذّر الاتصال بأوراكل حالياً (انقطاع الشبكة). '
                'أعد المحاولة بعد عودة الاتصال.'
            )
        elif 'مهلة جلب' in msg or 'call timeout' in low or 'dpy-4011' in low:
            error = 'الاستعلام طال على أوراكل — ضيّق المجموعة أو ابحث بصنف وحاول مجدداً.'
        else:
            error = f'تعذّر تحميل التسعير الأقل من التكلفة: {exc}'
        report = None

    branch_warehouses = [
        {
            'code': str(w.get('code') or '').strip(),
            'name': str(w.get('name') or w.get('code') or '').strip(),
            'branch_code': str(w.get('branch_code') or '').strip(),
        }
        for w in warehouses
        if str(w.get('code') or '').strip()
        and (
            not selected_branch
            or str(w.get('branch_code') or '').strip() == selected_branch
        )
    ]
    branch_warehouses.sort(key=lambda w: (w['name'], w['code']))

    all_warehouses = [
        {
            'code': str(w.get('code') or '').strip(),
            'name': str(w.get('name') or w.get('code') or '').strip(),
            'branch_code': str(w.get('branch_code') or '').strip(),
        }
        for w in warehouses
        if str(w.get('code') or '').strip()
    ]

    return render(
        request,
        'search/browse_below_cost_prices.html',
        {
            'branches': branches,
            'selected_branch': selected_branch,
            'branch_warehouses': branch_warehouses,
            'all_warehouses': all_warehouses,
            'selected_warehouse': selected_warehouse,
            'groups': groups,
            'selected_group': selected_group,
            'item_q': item_q,
            'report': report,
            'error': error,
            'hint': hint,
            'wh_name': wh_name_map.get(selected_warehouse) or '',
        },
    )


@login_required
@require_GET
@never_cache
def browse_price_changes(request):
    """أصناف عُدّل سعر بيعها (المستوى 1) خلال يوم محدد."""
    from datetime import date as date_cls

    error = ''
    hint = ''
    report = None
    today = date_cls.today()

    day_raw = str(request.GET.get('day') or request.GET.get('date') or '').strip()
    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_warehouse = str(request.GET.get('warehouse') or '').strip()
    selected_group = str(request.GET.get('group') or '').strip()
    item_q = str(request.GET.get('q') or '').strip()
    changed_only = str(request.GET.get('changed') or '1').strip() not in {
        '0',
        'false',
        'no',
        'off',
    }
    want_excel = str(request.GET.get('export') or '').strip().lower() in {
        '1',
        'excel',
        'xls',
        'xlsx',
    }
    submitted = str(request.GET.get('run') or '').strip() in ('1', 'true', 'yes') or want_excel

    try:
        day, _ = _parse_sales_dates(day_raw or today.isoformat(), day_raw or today.isoformat())
    except ValidationError as exc:
        return render(
            request,
            'search/browse_price_changes.html',
            {
                'day': today.isoformat(),
                'branches': [],
                'selected_branch': selected_branch,
                'branch_warehouses': [],
                'all_warehouses': [],
                'selected_warehouse': selected_warehouse,
                'groups': [],
                'selected_group': selected_group,
                'item_q': item_q,
                'changed_only': changed_only,
                'report': None,
                'error': str(exc),
                'hint': '',
                'wh_name': '',
            },
        )

    branches: list[dict] = []
    warehouses: list[dict] = []
    groups: list[dict] = []
    wh_name_map: dict[str, str] = {}

    try:
        from .oracle_income import fetch_income_branches
        from .oracle_price_changes import (
            _EXCEL_LIMIT,
            _FETCH_LIMIT,
            build_price_changes_excel,
            fetch_price_changes,
        )
        from .oracle_stock import (
            fetch_sales_group_options,
            fetch_warehouse_options,
            oracle_enabled,
            oracle_session,
        )

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن عرض تعديلات الأسعار.'
        else:
            with oracle_session():
                warehouses = fetch_warehouse_options(active_only=True) or []
                groups = fetch_sales_group_options() or []
                branches = _low_margin_branches(warehouses) or fetch_income_branches()
            wh_name_map = _low_margin_wh_name_map(warehouses)

            if selected_branch and selected_branch not in {b['code'] for b in branches}:
                selected_branch = ''
            if selected_group and selected_group not in {
                str(g.get('code') or '') for g in groups
            }:
                selected_group = ''

            branch_wh_codes = {
                str(w.get('code') or '').strip()
                for w in warehouses
                if (
                    not selected_branch
                    or str(w.get('branch_code') or '').strip() == selected_branch
                )
                and str(w.get('code') or '').strip()
            }
            if selected_warehouse and selected_warehouse not in branch_wh_codes:
                if not selected_branch:
                    if selected_warehouse not in {
                        str(w.get('code') or '').strip() for w in warehouses
                    }:
                        selected_warehouse = ''
                else:
                    selected_warehouse = ''

            if submitted and not error:
                with oracle_session():
                    report = fetch_price_changes(
                        day=day,
                        warehouse_code=selected_warehouse,
                        branch_code=selected_branch,
                        group_code=selected_group,
                        item_q=item_q,
                        changed_only=changed_only,
                        limit=_EXCEL_LIMIT if want_excel else _FETCH_LIMIT,
                    )
                for r in report.get('rows') or []:
                    code = str(r.get('wh_code') or '').strip()
                    r['wh_name'] = wh_name_map.get(code) or code or '—'
                if want_excel:
                    return build_price_changes_excel(report)
            elif not error:
                hint = (
                    'اختر اليوم (الافتراضي اليوم) ثم اعرض الأصناف التي تغيّر '
                    'سعر بيعها في المستوى 1.'
                )
    except ValidationError as exc:
        error = str(exc)
        report = None
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_price_changes failed: %s', exc)
        error = f'تعذّر تحميل تعديلات الأسعار: {exc}'
        report = None

    branch_warehouses = [
        {
            'code': str(w.get('code') or '').strip(),
            'name': str(w.get('name') or w.get('code') or '').strip(),
            'branch_code': str(w.get('branch_code') or '').strip(),
        }
        for w in warehouses
        if str(w.get('code') or '').strip()
        and (
            not selected_branch
            or str(w.get('branch_code') or '').strip() == selected_branch
        )
    ]
    branch_warehouses.sort(key=lambda w: (w['name'], w['code']))
    all_warehouses = [
        {
            'code': str(w.get('code') or '').strip(),
            'name': str(w.get('name') or w.get('code') or '').strip(),
            'branch_code': str(w.get('branch_code') or '').strip(),
        }
        for w in warehouses
        if str(w.get('code') or '').strip()
    ]

    return render(
        request,
        'search/browse_price_changes.html',
        {
            'day': day.isoformat(),
            'branches': branches,
            'selected_branch': selected_branch,
            'branch_warehouses': branch_warehouses,
            'all_warehouses': all_warehouses,
            'selected_warehouse': selected_warehouse,
            'groups': groups,
            'selected_group': selected_group,
            'item_q': item_q,
            'changed_only': changed_only,
            'report': report,
            'error': error,
            'hint': hint,
            'wh_name': wh_name_map.get(selected_warehouse) or '',
        },
    )


@login_required
@require_GET
@never_cache
def browse_cost_adjustments(request):
    """تسوية التكاليف — أصناف عُدّلت تكلفتها والحساب المحاسبي المرحّل إليه."""
    from datetime import date as date_cls
    from datetime import timedelta

    error = ''
    hint = ''
    report = None
    today = date_cls.today()

    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_warehouse = str(request.GET.get('warehouse') or '').strip()
    selected_group = str(request.GET.get('group') or '').strip()
    item_q = str(request.GET.get('q') or '').strip()[:80]
    account_q = str(request.GET.get('account') or '').strip()[:80]
    want_excel = str(request.GET.get('export') or '').strip().lower() in {
        '1',
        'excel',
        'xls',
        'xlsx',
    }
    submitted = str(request.GET.get('run') or '').strip() in ('1', 'true', 'yes') or want_excel

    try:
        # الافتراضي: آخر 7 أيام (اليوم و6 أيام قبله)
        raw_from = request.GET.get('date_from') or (today - timedelta(days=6)).isoformat()
        raw_to = request.GET.get('date_to') or today.isoformat()
        date_from, date_to = _parse_sales_dates(raw_from, raw_to)
    except ValidationError as exc:
        return render(
            request,
            'search/browse_cost_adjustments.html',
            {
                'date_from': (request.GET.get('date_from') or '')[:10],
                'date_to': (request.GET.get('date_to') or '')[:10],
                'default_from': (today - timedelta(days=6)).isoformat(),
                'default_to': today.isoformat(),
                'branches': [],
                'selected_branch': selected_branch,
                'branch_warehouses': [],
                'all_warehouses': [],
                'selected_warehouse': selected_warehouse,
                'groups': [],
                'selected_group': selected_group,
                'item_q': item_q,
                'account_q': account_q,
                'report': None,
                'error': str(exc),
                'hint': '',
                'wh_name': '',
            },
        )

    branches: list[dict] = []
    warehouses: list[dict] = []
    groups: list[dict] = []
    wh_name_map: dict[str, str] = {}

    try:
        from .oracle_cost_adjustments import (
            _EXCEL_LIMIT,
            _FETCH_LIMIT,
            build_cost_adjustments_excel,
            fetch_cost_adjustments,
        )
        from .oracle_income import fetch_income_branches
        from .oracle_stock import (
            fetch_sales_group_options,
            fetch_warehouse_options,
            oracle_enabled,
            oracle_session,
        )

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن عرض تسوية التكاليف.'
        else:
            with oracle_session():
                warehouses = fetch_warehouse_options(active_only=True) or []
                groups = fetch_sales_group_options() or []
                branches = _low_margin_branches(warehouses) or fetch_income_branches()
            wh_name_map = _low_margin_wh_name_map(warehouses)

            if selected_branch and selected_branch not in {b['code'] for b in branches}:
                selected_branch = ''
            if selected_group and selected_group not in {
                str(g.get('code') or '') for g in groups
            }:
                selected_group = ''

            branch_wh_codes = {
                str(w.get('code') or '').strip()
                for w in warehouses
                if (
                    not selected_branch
                    or str(w.get('branch_code') or '').strip() == selected_branch
                )
                and str(w.get('code') or '').strip()
            }
            if selected_warehouse and selected_warehouse not in branch_wh_codes:
                if not selected_branch:
                    if selected_warehouse not in {
                        str(w.get('code') or '').strip() for w in warehouses
                    }:
                        selected_warehouse = ''
                else:
                    selected_warehouse = ''

            if submitted and not error:
                with oracle_session():
                    report = fetch_cost_adjustments(
                        date_from,
                        date_to,
                        branch_code=selected_branch,
                        warehouse_code=selected_warehouse,
                        group_code=selected_group,
                        item_q=item_q,
                        account_q=account_q,
                        limit=_EXCEL_LIMIT if want_excel else _FETCH_LIMIT,
                    )
                for r in report.get('rows') or []:
                    code = str(r.get('wh_code') or '').strip()
                    r['wh_name'] = wh_name_map.get(code) or code or '—'
                if want_excel:
                    return build_cost_adjustments_excel(report)
            elif not error:
                hint = (
                    'اختر الفترة (الافتراضي آخر 7 أيام) ثم اعرض الأصناف التي عُدّلت '
                    'تكلفتها والحساب المحاسبي الذي رُحّلت إليه كل تسوية.'
                )
    except ValidationError as exc:
        error = str(exc)
        report = None
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_cost_adjustments failed: %s', exc)
        error = f'تعذّر تحميل تسوية التكاليف: {exc}'
        report = None

    branch_warehouses = [
        {
            'code': str(w.get('code') or '').strip(),
            'name': str(w.get('name') or w.get('code') or '').strip(),
            'branch_code': str(w.get('branch_code') or '').strip(),
        }
        for w in warehouses
        if str(w.get('code') or '').strip()
        and (
            not selected_branch
            or str(w.get('branch_code') or '').strip() == selected_branch
        )
    ]
    branch_warehouses.sort(key=lambda w: (w['name'], w['code']))
    all_warehouses = [
        {
            'code': str(w.get('code') or '').strip(),
            'name': str(w.get('name') or w.get('code') or '').strip(),
            'branch_code': str(w.get('branch_code') or '').strip(),
        }
        for w in warehouses
        if str(w.get('code') or '').strip()
    ]

    return render(
        request,
        'search/browse_cost_adjustments.html',
        {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'default_from': (today - timedelta(days=6)).isoformat(),
            'default_to': today.isoformat(),
            'branches': branches,
            'selected_branch': selected_branch,
            'branch_warehouses': branch_warehouses,
            'all_warehouses': all_warehouses,
            'selected_warehouse': selected_warehouse,
            'groups': groups,
            'selected_group': selected_group,
            'item_q': item_q,
            'account_q': account_q,
            'report': report,
            'error': error,
            'hint': hint,
            'wh_name': wh_name_map.get(selected_warehouse) or '',
        },
    )


@login_required
@require_GET
@never_cache
def browse_name_barcode_conflicts(request):
    """أصناف متشابهة بالاسم ومختلفة بالباركود — فهرس باركود محلي."""
    error = ''
    hint = ''
    report = None

    selected_group = str(request.GET.get('group') or '').strip()
    item_q = str(request.GET.get('q') or '').strip()
    mode = str(request.GET.get('mode') or 'all').strip().lower()
    min_ratio_raw = str(request.GET.get('min_ratio') or '88').strip()
    want_excel = str(request.GET.get('export') or '').strip().lower() in {
        '1',
        'excel',
        'xls',
        'xlsx',
    }
    submitted = str(request.GET.get('run') or '').strip() in ('1', 'true', 'yes') or want_excel

    if mode not in ('all', 'exact', 'similar'):
        mode = 'all'

    try:
        min_ratio = max(0.5, min(1.0, float(min_ratio_raw or 88) / 100.0))
    except ValueError:
        error = 'نسبة التشابه غير صحيحة (0–100).'
        min_ratio = 0.88

    groups: list[dict] = []
    try:
        from search.models import ItemBarcode

        from .name_barcode_conflicts import (
            build_name_barcode_excel,
            fetch_name_barcode_conflicts,
            group_options,
        )

        groups = group_options()
        index_count = ItemBarcode.objects.exclude(item_code='').count()

        if selected_group and selected_group not in {g['code'] for g in groups}:
            selected_group = ''

        if index_count == 0:
            error = (
                'فهرس الباركود فارغ — نفّذ مزامنة الأصناف من الإعدادات '
                'أو من شاشة البحث قبل استخدام هذا التقرير.'
            )
        elif submitted and not error:
            if want_excel:
                return build_name_barcode_excel(
                    group_code=selected_group,
                    item_q=item_q,
                    mode=mode,
                    min_ratio=min_ratio,
                )
            report = fetch_name_barcode_conflicts(
                group_code=selected_group,
                item_q=item_q,
                mode=mode,
                min_ratio=min_ratio,
            )
        elif not error:
            hint = (
                'يعرض أصنافاً مختلفة الأرقام تشترك في اسم متطابق أو مشابه '
                'ولكن بباركود مختلف — من فهرس GetAllItems المحلي. '
                'اختر مجموعة أو ابحث بصنف لتضييق النتائج.'
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_name_barcode_conflicts failed: %s', exc)
        error = f'تعذّر تحميل تقرير الاسم والباركود: {exc}'
        report = None

    return render(
        request,
        'search/browse_name_barcode_conflicts.html',
        {
            'groups': groups,
            'selected_group': selected_group,
            'item_q': item_q,
            'mode': mode,
            'min_ratio_input': min_ratio_raw or '88',
            'report': report,
            'error': error,
            'hint': hint,
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
def browse_unsold(request):
    """رصيد في مخازن محددة بلا حركة مبيعات — فلترة بالفرع والمجموعة والبحث."""
    from datetime import date

    today = date.today()
    month_start = today.replace(day=1)
    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_group = str(request.GET.get('group') or '').strip()
    q = str(request.GET.get('q') or '').strip()[:80]
    report = None
    error = ''
    branches: list[dict] = []
    groups: list[dict] = []

    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from') or month_start.isoformat(),
            request.GET.get('date_to') or today.isoformat(),
        )
    except ValidationError as exc:
        return render(
            request,
            'search/browse_unsold.html',
            {
                'date_from': (request.GET.get('date_from') or '')[:10],
                'date_to': (request.GET.get('date_to') or '')[:10],
                'default_from': month_start.isoformat(),
                'default_to': today.isoformat(),
                'selected_branch': selected_branch,
                'selected_group': selected_group,
                'q': q,
                'branches': [],
                'groups': [],
                'report': None,
                'error': str(exc),
            },
        )

    try:
        from .oracle_income import fetch_income_branches
        from .oracle_stock import (
            fetch_sales_group_options,
            oracle_enabled,
            oracle_session,
        )
        from .oracle_unsold import build_unsold_report, excluded_unsold_group_codes

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن عرض التقرير.'
        else:
            with oracle_session():
                skip_groups = excluded_unsold_group_codes()
                branches = fetch_income_branches()
                groups = [
                    row
                    for row in fetch_sales_group_options()
                    if row['code'] not in skip_groups
                ]
                if selected_branch not in {row['code'] for row in branches}:
                    selected_branch = ''
                if selected_group not in {row['code'] for row in groups}:
                    selected_group = ''
                report = build_unsold_report(
                    date_from,
                    date_to,
                    branch_code=selected_branch,
                    group_code=selected_group,
                    q=q,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_unsold failed: %s', exc)
        error = f'تعذّر تحميل تقرير الرصيد بلا مبيعات: {exc}'
        report = None

    return render(
        request,
        'search/browse_unsold.html',
        {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'default_from': month_start.isoformat(),
            'default_to': today.isoformat(),
            'selected_branch': selected_branch,
            'selected_group': selected_group,
            'q': q,
            'branches': branches,
            'groups': groups,
            'report': report,
            'error': error,
        },
    )


@login_required
@require_GET
def browse_unsold_api(request):
    """صفحة إضافية من أصناف الرصيد بلا مبيعات للتمرير اللانهائي."""
    from datetime import date

    today = date.today()
    month_start = today.replace(day=1)
    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from') or month_start.isoformat(),
            request.GET.get('date_to') or today.isoformat(),
        )
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

    try:
        offset = max(0, int(request.GET.get('offset') or 0))
        limit = min(max(1, int(request.GET.get('limit') or 50)), 200)
    except (TypeError, ValueError):
        offset, limit = 0, 50

    try:
        from .oracle_stock import oracle_enabled, oracle_session
        from .oracle_unsold import fetch_unsold_items

        if not oracle_enabled():
            return JsonResponse(
                {'ok': False, 'error': 'أوراكل غير مفعّل.'}, status=400
            )
        with oracle_session():
            rows = fetch_unsold_items(
                date_from,
                date_to,
                branch_code=str(request.GET.get('branch') or '').strip(),
                group_code=str(request.GET.get('group') or '').strip(),
                q=str(request.GET.get('q') or '').strip()[:80],
                offset=offset,
                limit=limit,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_unsold_api failed: %s', exc)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)

    return JsonResponse({'ok': True, 'rows': rows, 'offset': offset, 'limit': limit})


@login_required
@require_GET
@never_cache
def browse_pr_compare(request):
    """قائمة طلبات الشراء حسب التاريخ لمقارنة الأرصدة حسب الفرع/المخزن."""
    from datetime import date, datetime

    today = date.today()
    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_warehouse = str(request.GET.get('warehouse') or '').strip()
    date_raw = str(request.GET.get('date') or '').strip()[:10]
    search_q = _pr_list_search_filters(request)
    selected_date = today
    if date_raw:
        try:
            selected_date = datetime.strptime(date_raw, '%Y-%m-%d').date()
        except ValueError:
            selected_date = today
    requests_today: list[dict] = []
    request_users: list[dict] = []
    branches: list[dict] = []
    warehouses: list[dict] = []
    all_warehouses: list[dict] = []
    selected_warehouse_name = ''
    error = ''

    try:
        from .oracle_pr_compare import (
            _norm_code,
            fetch_today_purchase_requests,
        )
        from .oracle_stock import (
            _branch_names,
            fetch_warehouse_options,
            oracle_enabled,
            oracle_session,
        )

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن مقارنة طلبات الشراء.'
        else:
            with oracle_session():
                # الفروع من ربط المخازن (CONN_BRN_NO) لضمان تطابق قائمة المخازن
                all_warehouses = fetch_warehouse_options(active_only=True)
                branch_name_map = _branch_names()

                def _branch_label(code: str, fallback: str = '') -> str:
                    raw = str(code or '').strip()
                    if not raw:
                        return fallback or '—'
                    if raw in branch_name_map:
                        return branch_name_map[raw]
                    norm = _norm_code(raw)
                    for key, label in branch_name_map.items():
                        if _norm_code(key) == norm:
                            return label
                    return str(fallback or raw).strip() or raw

                branch_map: dict[str, str] = {}
                for row in all_warehouses:
                    brn = str(row.get('branch_code') or '').strip()
                    if not brn:
                        continue
                    branch_map[brn] = _branch_label(brn, row.get('branch_name') or '')
                branches = [
                    {'code': code, 'name': name}
                    for code, name in sorted(
                        branch_map.items(), key=lambda item: (item[1], item[0])
                    )
                ]
                if selected_branch and selected_branch not in branch_map:
                    # احتياطي للترميز الرقمي
                    matched = next(
                        (
                            code
                            for code in branch_map
                            if _norm_code(code) == _norm_code(selected_branch)
                        ),
                        '',
                    )
                    selected_branch = matched

                warehouses = (
                    [
                        row
                        for row in all_warehouses
                        if str(row.get('branch_code') or '').strip() == selected_branch
                        or _norm_code(row.get('branch_code'))
                        == _norm_code(selected_branch)
                    ]
                    if selected_branch
                    else []
                )
                # فضّل التطابق الحرفي أولاً
                exact = [
                    row
                    for row in warehouses
                    if str(row.get('branch_code') or '').strip() == selected_branch
                ]
                if exact:
                    warehouses = exact

                warehouses = sorted(
                    warehouses,
                    key=lambda row: (
                        str(row.get('name') or '').strip(),
                        str(row.get('code') or '').strip(),
                    ),
                )

                wh_codes = {str(row.get('code') or '').strip() for row in warehouses}
                if selected_warehouse and selected_warehouse not in wh_codes:
                    selected_warehouse = ''
                if selected_warehouse:
                    selected_warehouse_name = next(
                        (
                            str(row.get('name') or selected_warehouse)
                            for row in warehouses
                            if str(row.get('code') or '').strip() == selected_warehouse
                        ),
                        selected_warehouse,
                    )
                if selected_branch:
                    requests_today = fetch_today_purchase_requests(
                        branch_code=selected_branch,
                        day=selected_date,
                        warehouse_code=selected_warehouse,
                    )
                    request_users = _request_list_users(requests_today)
                    requests_today = _filter_request_list_rows(
                        requests_today,
                        company_q=search_q['company_q'],
                        user_q=search_q['user_q'],
                        req_no_q=search_q['req_no_q'],
                        kind='pr',
                    )
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_pr_compare failed: %s', exc)
        error = f'تعذّر تحميل طلبات الشراء: {exc}'
        requests_today = []

    is_today = selected_date == today
    period_label = 'اليوم' if is_today else selected_date.isoformat()

    return render(
        request,
        'search/browse_pr_compare.html',
        {
            'today': today.isoformat(),
            'selected_date': selected_date.isoformat(),
            'period_label': period_label,
            'is_today': is_today,
            'selected_branch': selected_branch,
            'selected_warehouse': selected_warehouse,
            'selected_warehouse_name': selected_warehouse_name,
            'company_q': search_q['company_q'],
            'user_q': search_q['user_q'],
            'req_no_q': search_q['req_no_q'],
            'request_users': request_users,
            'branches': branches,
            'warehouses': warehouses,
            'all_warehouses': all_warehouses,
            'requests_today': requests_today,
            'error': error,
        },
    )

@login_required
@require_GET
@never_cache
def browse_pr_compare_detail(request):
    """مقارنة أصناف طلب شراء مع المخازن ذات الرصيد فقط."""
    pr_type = str(request.GET.get('pr_type') or '').strip()
    pr_no = str(request.GET.get('pr_no') or '').strip()
    pr_ser = str(request.GET.get('pr_ser') or '').strip()
    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_warehouse = str(request.GET.get('warehouse') or '').strip()
    selected_date = str(request.GET.get('date') or '').strip()[:10]
    want_excel = str(request.GET.get('export') or '').strip().lower() in {
        'xls',
        'excel',
        'xlsx',
    }
    compare = None
    error = ''

    if not (pr_type and pr_no and pr_ser):
        error = 'معرّف طلب الشراء غير مكتمل.'
    else:
        try:
            from .oracle_pr_compare import (
                build_purchase_request_compare,
                build_purchase_request_compare_excel,
            )
            from .oracle_stock import oracle_enabled, oracle_session

            if not oracle_enabled():
                error = 'أوراكل غير مفعّل — لا يمكن مقارنة الطلب.'
            else:
                # ورقة المقارنة: كل المخازن ذات الرصيد فقط (بدون تقييد بفرع/مخزن الفلتر)
                with oracle_session():
                    compare = build_purchase_request_compare(
                        pr_type=pr_type,
                        pr_no=pr_no,
                        pr_ser=pr_ser,
                        warehouse_codes=None,
                        branch_code="",
                    )
                if compare is None:
                    error = 'طلب الشراء غير موجود أو غير نشط.'
                elif want_excel and compare is not None:
                    return build_purchase_request_compare_excel(compare)
        except Exception as exc:  # noqa: BLE001
            logger.warning('browse_pr_compare_detail failed: %s', exc)
            error = f'تعذّر مقارنة طلب الشراء: {exc}'
            compare = None

    return render(
        request,
        'search/browse_pr_compare_detail.html',
        {
            'pr_type': pr_type,
            'pr_no': pr_no,
            'pr_ser': pr_ser,
            'selected_branch': selected_branch,
            'selected_warehouse': selected_warehouse,
            'selected_date': selected_date,
            'compare': compare,
            'error': error,
        },
    )


def _pr_compare_filters(request):
    """فلتر تاريخ/فرع/مخزن المشترك بين مقارنات الطلبات."""
    from datetime import date, datetime

    today = date.today()
    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_warehouse = str(request.GET.get('warehouse') or '').strip()
    date_raw = str(request.GET.get('date') or '').strip()[:10]
    selected_date = today
    if date_raw:
        try:
            selected_date = datetime.strptime(date_raw, '%Y-%m-%d').date()
        except ValueError:
            selected_date = today
    return today, selected_date, selected_branch, selected_warehouse


def _pr_list_search_filters(request) -> dict[str, str]:
    """بحث قائمة الطلبات: شركة/مورد · مستخدم · رقم الطلب."""
    return {
        'company_q': str(
            request.GET.get('company')
            or request.GET.get('vendor')
            or request.GET.get('q_company')
            or ''
        ).strip()[:80],
        'user_q': str(
            request.GET.get('user') or request.GET.get('q_user') or ''
        ).strip()[:80],
        'req_no_q': str(
            request.GET.get('req_no')
            or request.GET.get('pr_no')
            or request.GET.get('tr_no')
            or request.GET.get('q_no')
            or ''
        ).strip()[:40],
    }


def _request_list_users(rows: list[dict]) -> list[dict]:
    """مستخدمو الطلبات في المصدر الحالي (تاريخ/فرع/مخزن) لاقتراح البحث."""
    seen: dict[str, dict[str, str]] = {}
    for row in rows or []:
        code = str(row.get('user_code') or '').strip()
        name = str(row.get('user_name') or '').strip()
        if not code and not name:
            continue
        key = code or name.casefold()
        if key in seen:
            continue
        seen[key] = {'code': code, 'name': name or code}
    return sorted(
        seen.values(),
        key=lambda r: (str(r.get('name') or '').casefold(), str(r.get('code') or '')),
    )


def _filter_request_list_rows(
    rows: list[dict],
    *,
    company_q: str = '',
    user_q: str = '',
    req_no_q: str = '',
    kind: str = 'pr',
) -> list[dict]:
    """تصفية صفوف قائمة الطلبات محلياً بعد الجلب."""
    company = str(company_q or '').strip().casefold()
    user = str(user_q or '').strip().casefold()
    req_no = str(req_no_q or '').strip().casefold()
    if not (company or user or req_no):
        return list(rows or [])

    out: list[dict] = []
    for row in rows or []:
        if company:
            if kind == 'pr':
                hay = ' '.join(
                    (
                        str(row.get('vendor_name') or ''),
                        str(row.get('vendor_code') or ''),
                    )
                ).casefold()
            else:
                hay = ' '.join(
                    (
                        str(row.get('from_wh_name') or ''),
                        str(row.get('from_wh_code') or ''),
                        str(row.get('from_wh_label') or ''),
                        str(row.get('to_wh_name') or ''),
                        str(row.get('to_wh_code') or ''),
                        str(row.get('to_wh_label') or ''),
                        str(row.get('main_wh_label') or ''),
                        str(row.get('main_wh_code') or ''),
                        str(row.get('branch_name') or ''),
                        str(row.get('branch_code') or ''),
                    )
                ).casefold()
            if company not in hay:
                continue
        if user:
            hay_u = ' '.join(
                (
                    str(row.get('user_name') or ''),
                    str(row.get('user_code') or ''),
                )
            ).casefold()
            if user not in hay_u:
                continue
        if req_no:
            if kind == 'pr':
                hay_n = ' '.join(
                    (
                        str(row.get('pr_no') or ''),
                        str(row.get('pr_ser') or ''),
                        str(row.get('pr_type') or ''),
                    )
                ).casefold()
            else:
                hay_n = ' '.join(
                    (
                        str(row.get('tr_no') or ''),
                        str(row.get('tr_ser') or ''),
                        str(row.get('tr_type') or ''),
                    )
                ).casefold()
            if req_no not in hay_n:
                continue
        out.append(row)
    return out


def _load_branch_warehouses(selected_branch: str, selected_warehouse: str):
    from .oracle_pr_compare import _norm_code
    from .oracle_stock import _branch_names, fetch_warehouse_options

    all_warehouses = fetch_warehouse_options(active_only=True)
    branch_name_map = _branch_names()

    def _branch_label(code: str, fallback: str = '') -> str:
        raw = str(code or '').strip()
        if not raw:
            return fallback or '—'
        if raw in branch_name_map:
            return branch_name_map[raw]
        norm = _norm_code(raw)
        for key, label in branch_name_map.items():
            if _norm_code(key) == norm:
                return label
        return str(fallback or raw).strip() or raw

    branch_map: dict[str, str] = {}
    for row in all_warehouses:
        brn = str(row.get('branch_code') or '').strip()
        if not brn:
            continue
        branch_map[brn] = _branch_label(brn, row.get('branch_name') or '')
    branches = [
        {'code': code, 'name': name}
        for code, name in sorted(branch_map.items(), key=lambda item: (item[1], item[0]))
    ]
    if selected_branch and selected_branch not in branch_map:
        matched = next(
            (
                code
                for code in branch_map
                if _norm_code(code) == _norm_code(selected_branch)
            ),
            '',
        )
        selected_branch = matched

    warehouses = (
        [
            row
            for row in all_warehouses
            if str(row.get('branch_code') or '').strip() == selected_branch
            or _norm_code(row.get('branch_code')) == _norm_code(selected_branch)
        ]
        if selected_branch
        else []
    )
    exact = [
        row
        for row in warehouses
        if str(row.get('branch_code') or '').strip() == selected_branch
    ]
    if exact:
        warehouses = exact
    warehouses = sorted(
        warehouses,
        key=lambda row: (
            str(row.get('name') or '').strip(),
            str(row.get('code') or '').strip(),
        ),
    )
    wh_codes = {str(row.get('code') or '').strip() for row in warehouses}
    if selected_warehouse and selected_warehouse not in wh_codes:
        selected_warehouse = ''
    selected_warehouse_name = ''
    if selected_warehouse:
        selected_warehouse_name = next(
            (
                str(row.get('name') or selected_warehouse)
                for row in warehouses
                if str(row.get('code') or '').strip() == selected_warehouse
            ),
            selected_warehouse,
        )
    return (
        selected_branch,
        selected_warehouse,
        selected_warehouse_name,
        branches,
        warehouses,
        all_warehouses,
    )


@login_required
@require_GET
@never_cache
def browse_tr_compare(request):
    """قائمة طلبات التحويل حسب التاريخ لمقارنة المخزن المطلوب مع المخزن الرئيسي للفرع."""
    today, selected_date, selected_branch, selected_warehouse = _pr_compare_filters(
        request
    )
    search_q = _pr_list_search_filters(request)
    requests_today: list[dict] = []
    request_users: list[dict] = []
    branches: list[dict] = []
    warehouses: list[dict] = []
    all_warehouses: list[dict] = []
    selected_warehouse_name = ''
    error = ''

    try:
        from .oracle_stock import oracle_enabled, oracle_session
        from .oracle_tr_compare import fetch_today_transfer_requests

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن مقارنة طلبات التحويل.'
        else:
            with oracle_session():
                (
                    selected_branch,
                    selected_warehouse,
                    selected_warehouse_name,
                    branches,
                    warehouses,
                    all_warehouses,
                ) = _load_branch_warehouses(selected_branch, selected_warehouse)
                if selected_branch:
                    requests_today = fetch_today_transfer_requests(
                        branch_code=selected_branch,
                        day=selected_date,
                        warehouse_code=selected_warehouse,
                    )
                    request_users = _request_list_users(requests_today)
                    requests_today = _filter_request_list_rows(
                        requests_today,
                        company_q=search_q['company_q'],
                        user_q=search_q['user_q'],
                        req_no_q=search_q['req_no_q'],
                        kind='tr',
                    )
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_tr_compare failed: %s', exc)
        error = f'تعذّر تحميل طلبات التحويل: {exc}'
        requests_today = []

    is_today = selected_date == today
    period_label = 'اليوم' if is_today else selected_date.isoformat()

    return render(
        request,
        'search/browse_tr_compare.html',
        {
            'today': today.isoformat(),
            'selected_date': selected_date.isoformat(),
            'period_label': period_label,
            'is_today': is_today,
            'selected_branch': selected_branch,
            'selected_warehouse': selected_warehouse,
            'selected_warehouse_name': selected_warehouse_name,
            'company_q': search_q['company_q'],
            'user_q': search_q['user_q'],
            'req_no_q': search_q['req_no_q'],
            'request_users': request_users,
            'branches': branches,
            'warehouses': warehouses,
            'all_warehouses': all_warehouses,
            'requests_today': requests_today,
            'error': error,
        },
    )


@login_required
@require_GET
@never_cache
def browse_tr_compare_detail(request):
    """مقارنة أصناف طلب تحويل مع المخزن المطلوب والمخزن الرئيسي للفرع بعد الترحيل."""
    tr_type = str(request.GET.get('tr_type') or '').strip()
    tr_no = str(request.GET.get('tr_no') or '').strip()
    tr_ser = str(request.GET.get('tr_ser') or '').strip()
    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_warehouse = str(request.GET.get('warehouse') or '').strip()
    selected_date = str(request.GET.get('date') or '').strip()[:10]
    short_only = str(request.GET.get('short_only') or '').strip() in (
        '1',
        'true',
        'yes',
        'on',
    )
    pr_match = str(request.GET.get('pr_match') or '').strip() in (
        '1',
        'true',
        'yes',
        'on',
    )
    want_excel = str(request.GET.get('export') or '').strip().lower() in {
        'xls',
        'excel',
        'xlsx',
        'no_pr',
    }
    compare = None
    error = ''

    if not (tr_type and tr_no and tr_ser):
        error = 'معرّف طلب التحويل غير مكتمل.'
    else:
        try:
            from .oracle_stock import oracle_enabled, oracle_session
            from .oracle_tr_compare import (
                build_transfer_request_compare,
                build_transfer_short_no_pr_excel,
            )

            if not oracle_enabled():
                error = 'أوراكل غير مفعّل — لا يمكن مقارنة الطلب.'
            else:
                with oracle_session():
                    compare = build_transfer_request_compare(
                        tr_type=tr_type,
                        tr_no=tr_no,
                        tr_ser=tr_ser,
                    )
                if compare is None:
                    error = 'طلب التحويل غير موجود أو غير نشط.'
                elif want_excel and compare is not None:
                    # تصدير غير المتوفر بلا طلب شراء (من كامل نتيجة المقارنة)
                    return build_transfer_short_no_pr_excel(compare)
                elif (short_only or pr_match) and compare:
                    all_items = list(compare.get('items') or [])
                    short_items = [row for row in all_items if not row.get('can_cover')]
                    # pr_match: كل غير المتوفر مع قائمة طلبات الشراء (أو «لا يوجد»)
                    compare = {
                        **compare,
                        'items': short_items,
                        'shown_item_count': len(short_items),
                        'short_only': True,
                        'pr_match': bool(pr_match),
                        'all_item_count': len(all_items),
                        'short_item_count': len(short_items),
                        'short_pr_hit_count': sum(
                            1
                            for row in short_items
                            if row.get('recent_prs') or row.get('recent_pr_nos')
                        ),
                        'short_no_pr_count': sum(
                            1
                            for row in short_items
                            if not (row.get('recent_prs') or row.get('recent_pr_nos'))
                        ),
                    }
        except Exception as exc:  # noqa: BLE001
            logger.warning('browse_tr_compare_detail failed: %s', exc)
            error = f'تعذّر مقارنة طلب التحويل: {exc}'
            compare = None

    return render(
        request,
        'search/browse_tr_compare_detail.html',
        {
            'tr_type': tr_type,
            'tr_no': tr_no,
            'tr_ser': tr_ser,
            'selected_branch': selected_branch,
            'selected_warehouse': selected_warehouse,
            'selected_date': selected_date,
            'short_only': short_only or pr_match,
            'pr_match': pr_match,
            'compare': compare,
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


@login_required
@require_GET
@never_cache
def browse_trial_balance(request):
    """ميزان المراجعة — أرصدة نهائية / تفصيلي تحليلي من قيود أوراكل."""
    from datetime import date as date_cls
    from datetime import datetime

    error = ''
    report = None
    branches: list[dict] = []
    selected_branch = str(request.GET.get('branch') or '').strip()
    view_raw = str(request.GET.get('view') or 'summary').strip().lower()
    if view_raw in {
        'detail',
        'detailed',
        'movement',
        'movements',
        'حركة',
        'تفصيلي',
    }:
        view_mode = 'detail'
    elif view_raw in {
        'analytic',
        'analytical',
        'تحليلي',
    }:
        view_mode = 'analytic'
    else:
        view_mode = 'summary'
    posted_raw = request.GET.get('posted')
    if posted_raw is None:
        posted_only = False
    else:
        posted_only = str(posted_raw).strip() in ('1', 'true', 'yes', 'on')
    hide_zero = str(request.GET.get('hide_zero') or '1').strip() not in (
        '0',
        'false',
        'no',
        'off',
    )
    want_excel = str(request.GET.get('export') or '').strip().lower() in {
        '1',
        'excel',
        'xls',
        'xlsx',
    }

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
            'search/browse_trial_balance.html',
            {
                'date_from': (request.GET.get('date_from') or '')[:10],
                'date_to': (request.GET.get('date_to') or '')[:10],
                'default_from': year_start.isoformat(),
                'default_to': today.isoformat(),
                'selected_branch': selected_branch,
                'posted_only': posted_only,
                'hide_zero': hide_zero,
                'view_mode': view_mode,
                'branches': [],
                'report': None,
                'error': str(exc),
            },
        )

    try:
        from .oracle_stock import (
            _friendly_connect_error,
            _is_connect_timeout,
            _is_disconnect_error,
            oracle_enabled,
            oracle_session,
        )
        from .oracle_trial_balance import (
            build_trial_balance,
            build_trial_balance_excel,
            fetch_income_branches,
            peek_trial_balance,
        )

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن عرض ميزان المراجعة.'
        else:
            with oracle_session():
                branches = fetch_income_branches()
                branch_codes = {b['code'] for b in branches}
                if selected_branch and selected_branch not in branch_codes:
                    selected_branch = ''
                try:
                    report = build_trial_balance(
                        date_from,
                        date_to,
                        branch_code=selected_branch,
                        posted_only=posted_only,
                        hide_zero=hide_zero,
                        mode=view_mode,
                        use_cache=False,
                    )
                except Exception as tb_exc:  # noqa: BLE001
                    cached = peek_trial_balance(
                        date_from,
                        date_to,
                        branch_code=selected_branch,
                        posted_only=posted_only,
                        hide_zero=hide_zero,
                        mode=view_mode,
                    )
                    if cached is not None and (
                        _is_connect_timeout(tb_exc)
                        or _is_disconnect_error(tb_exc)
                    ):
                        report = cached
                        error = (
                            'تعذّر الاتصال بأوراكل — عرض ميزان محفوظ مسبقاً. '
                            'تحقق من VPN/الإنترنت ثم حدّث الصفحة.'
                        )
                    else:
                        raise
            if want_excel and report is not None:
                return build_trial_balance_excel(report)
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_trial_balance failed: %s', exc)
        try:
            from .oracle_stock import _friendly_connect_error

            error = f'تعذّر تحميل ميزان المراجعة: {_friendly_connect_error(exc)}'
        except Exception:
            error = f'تعذّر تحميل ميزان المراجعة: {exc}'
        report = None

    return render(
        request,
        'search/browse_trial_balance.html',
        {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'default_from': year_start.isoformat(),
            'default_to': today.isoformat(),
            'selected_branch': selected_branch,
            'posted_only': posted_only,
            'hide_zero': hide_zero,
            'view_mode': view_mode,
            'branches': branches,
            'report': report,
            'error': error,
        },
    )


@login_required
@require_GET
@never_cache
def browse_assets(request):
    """الأصول الثابتة المسجّلة على الفروع مع إحصائيات كل فرع."""
    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_group = str(request.GET.get('group') or '').strip()
    q = str(request.GET.get('q') or '').strip()[:80]
    want_excel = str(request.GET.get('export') or '').strip().lower() in {
        'xls',
        'excel',
        'xlsx',
    }
    report = None
    error = ''
    branches: list[dict] = []
    groups: list[dict] = []

    try:
        from .oracle_assets import (
            build_assets_excel,
            build_assets_report,
            excluded_asset_branch_codes,
            fetch_asset_groups,
        )
        from .oracle_income import fetch_income_branches
        from .oracle_stock import oracle_enabled, oracle_session

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن عرض الأصول.'
        else:
            with oracle_session():
                skip_brn = excluded_asset_branch_codes()
                branches = [
                    row
                    for row in fetch_income_branches()
                    if row['code'] not in skip_brn
                ]
                groups = fetch_asset_groups()
                if selected_branch not in {row['code'] for row in branches}:
                    selected_branch = ''
                if selected_group not in {row['code'] for row in groups}:
                    selected_group = ''
                report = build_assets_report(
                    branch_code=selected_branch,
                    group_code=selected_group,
                    q=q,
                )
                if want_excel and report is not None:
                    return build_assets_excel(report)
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_assets failed: %s', exc)
        error = f'تعذّر تحميل الأصول: {exc}'
        report = None

    return render(
        request,
        'search/browse_assets.html',
        {
            'selected_branch': selected_branch,
            'selected_group': selected_group,
            'q': q,
            'branches': branches,
            'groups': groups,
            'report': report,
            'error': error,
        },
    )


@login_required
@require_GET
@never_cache
def browse_warehouse_expense(request):
    """توزيع مصاريف المستودع على الفروع حسب تحويلات المخازن المصدر."""
    from datetime import date

    today = date.today()
    month_start = today.replace(day=1)
    date_from_raw = request.GET.get('date_from') or month_start.isoformat()
    date_to_raw = request.GET.get('date_to') or today.isoformat()
    src_wh_raw = str(request.GET.get('src_wh') or '3,901,902,401').strip()[:120]
    src_filter_raw = str(request.GET.get('src_one') or '').strip()[:32]
    cc_code_raw = str(request.GET.get('cc') or '103').strip()[:32] or '103'
    expense_raw = str(request.GET.get('expense') or '0').replace(',', '').strip()
    posted_raw = request.GET.get('posted')
    if posted_raw is None:
        posted_only = True
    else:
        posted_only = str(posted_raw).strip() in ('1', 'true', 'yes', 'on')

    report = None
    error = ''
    expense_value = 0.0
    export_raw = str(request.GET.get('export') or '').strip().lower()
    want_expense_excel = export_raw in ('expense', 'expense_excel', 'cc_excel')
    show_cc = str(request.GET.get('show_cc') or '').strip() in ('1', 'true', 'yes', 'on')
    try:
        expense_value = round(float(expense_raw or 0), 2)
    except ValueError:
        error = 'قيمة المصروف غير صحيحة.'
    try:
        date_from, date_to = _parse_sales_dates(date_from_raw, date_to_raw)
    except ValidationError as exc:
        return render(
            request,
            'search/browse_warehouse_expense.html',
            {
                'date_from': (date_from_raw or '')[:10],
                'date_to': (date_to_raw or '')[:10],
                'default_from': month_start.isoformat(),
                'default_to': today.isoformat(),
                'src_wh': src_wh_raw,
                'src_one': src_filter_raw,
                'cc_code': cc_code_raw,
                'expense': expense_raw,
                'posted_only': posted_only,
                'show_cc': show_cc or want_expense_excel,
                'report': None,
                'error': str(exc),
            },
        )
    if not error:
        try:
            from .oracle_stock import oracle_enabled, oracle_session
            from .oracle_warehouse_expense import (
                build_warehouse_expense_accounts_excel,
                build_warehouse_expense_distribution,
            )

            if not oracle_enabled():
                error = 'أوراكل غير مفعّل — لا يمكن عرض توزيع مصاريف المستودع.'
            else:
                with oracle_session():
                    report = build_warehouse_expense_distribution(
                        date_from,
                        date_to,
                        source_warehouses=src_wh_raw,
                        expense_total=expense_value,
                        posted_only=posted_only,
                        source_wh_filter=src_filter_raw,
                        cc_code=cc_code_raw,
                    )
                    if want_expense_excel and report is not None:
                        return build_warehouse_expense_accounts_excel(report)
        except Exception as exc:  # noqa: BLE001
            logger.warning('browse_warehouse_expense failed: %s', exc)
            error = f'تعذّر تحميل توزيع مصاريف المستودع: {exc}'
            report = None

    return render(
        request,
        'search/browse_warehouse_expense.html',
        {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'default_from': month_start.isoformat(),
            'default_to': today.isoformat(),
            'src_wh': src_wh_raw,
            'src_one': (report or {}).get('filters', {}).get('source_wh_filter')
            or src_filter_raw,
            'cc_code': (report or {}).get('filters', {}).get('cc_code') or cc_code_raw,
            'expense': f'{expense_value:.2f}' if expense_raw else '0',
            'posted_only': posted_only,
            'show_cc': show_cc,
            'report': report,
            'error': error,
        },
    )


@login_required
@require_GET
@never_cache
def browse_wh_outgoing(request):
    """تحويلات صادرة مع فلتر من فرع/مخزن إلى فرع/مخزن ومتابعة الاستلام."""
    from datetime import date, timedelta

    today = date.today()
    default_from = today - timedelta(days=30)
    date_from_raw = request.GET.get('date_from') or default_from.isoformat()
    date_to_raw = request.GET.get('date_to') or today.isoformat()
    selected_branch_from = str(request.GET.get('branch_from') or '').strip()
    selected_branch_to = str(
        request.GET.get('branch_to') or request.GET.get('branch') or ''
    ).strip()
    selected_warehouse_from = str(
        request.GET.get('warehouse_from') or request.GET.get('wh_from') or ''
    ).strip()
    selected_warehouse_to = str(
        request.GET.get('warehouse_to')
        or request.GET.get('warehouse')
        or request.GET.get('wh_to')
        or ''
    ).strip()
    selected_group = str(request.GET.get('group') or '').strip()
    status_raw = str(request.GET.get('status') or 'all').strip().lower()
    if status_raw not in ('all', 'received', 'pending', 'late'):
        status_raw = 'all'
    want_excel = str(request.GET.get('export') or '').strip().lower() in {
        'xls',
        'excel',
        'xlsx',
    }

    report = None
    error = ''
    branches: list[dict] = []
    warehouses_from: list[dict] = []
    warehouses_to: list[dict] = []
    groups: list[dict] = []

    ctx_base = {
        'date_from': (date_from_raw or '')[:10],
        'date_to': (date_to_raw or '')[:10],
        'default_from': default_from.isoformat(),
        'default_to': today.isoformat(),
        'selected_branch_from': selected_branch_from,
        'selected_branch_to': selected_branch_to,
        'selected_branch': selected_branch_to,
        'selected_warehouse_from': selected_warehouse_from,
        'selected_warehouse_to': selected_warehouse_to,
        'selected_warehouse': selected_warehouse_to,
        'selected_group': selected_group,
        'status': status_raw,
        'branches': [],
        'warehouses_from': [],
        'warehouses_to': [],
        'warehouses': [],
        'groups': [],
        'report': None,
    }

    try:
        date_from, date_to = _parse_sales_dates(date_from_raw, date_to_raw)
    except ValidationError as exc:
        ctx_base['error'] = str(exc)
        return render(request, 'search/browse_wh_outgoing.html', ctx_base)

    try:
        from .oracle_stock import (
            fetch_sales_group_options,
            fetch_warehouse_options,
            oracle_enabled,
            oracle_session,
        )
        from .oracle_wh_outgoing import (
            build_outgoing_transfers_excel,
            build_outgoing_transfers_report,
        )

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن عرض تحويلات المستودعات.'
        else:
            with oracle_session():
                all_wh = fetch_warehouse_options(active_only=True) or []
                (
                    selected_branch_to,
                    selected_warehouse_to,
                    _dst_wh_name,
                    branches,
                    warehouses_to,
                    _all_wh_dst,
                ) = _load_branch_warehouses(
                    selected_branch_to, selected_warehouse_to
                )
                (
                    selected_branch_from,
                    selected_warehouse_from,
                    _src_wh_name,
                    branches_src,
                    warehouses_from,
                    _all_wh_src,
                ) = _load_branch_warehouses(
                    selected_branch_from, selected_warehouse_from
                )
                if not branches and branches_src:
                    branches = branches_src

                # بلا فرع مصدر: كل مخازن كل الفروع
                if not selected_branch_from:
                    warehouses_from = sorted(
                        all_wh,
                        key=lambda r: (
                            str(r.get('branch_name') or ''),
                            str(r.get('code') or ''),
                        ),
                    )
                    if selected_warehouse_from:
                        allowed = {
                            str(w.get('code') or '').strip()
                            for w in warehouses_from
                        }
                        if selected_warehouse_from not in allowed:
                            selected_warehouse_from = ''

                # بلا فرع وصول: كل المخازن متاحة للاختيار الاختياري
                if not selected_branch_to:
                    warehouses_to = sorted(
                        all_wh,
                        key=lambda r: (
                            str(r.get('branch_name') or ''),
                            str(r.get('code') or ''),
                        ),
                    )
                    if selected_warehouse_to:
                        allowed = {
                            str(w.get('code') or '').strip()
                            for w in warehouses_to
                        }
                        if selected_warehouse_to not in allowed:
                            selected_warehouse_to = ''

                groups = fetch_sales_group_options()
                group_codes = {str(g.get('code') or '').strip() for g in groups}
                if selected_group and selected_group not in group_codes:
                    selected_group = ''

                if selected_warehouse_from:
                    source_wh = selected_warehouse_from
                else:
                    # فرع فقط أو الكل: بدون قائمة مخازن محددة (كل مخازن النطاق)
                    source_wh = ''

                report = build_outgoing_transfers_report(
                    date_from,
                    date_to,
                    source_warehouses=source_wh,
                    branch_from=selected_branch_from,
                    branch_to=selected_branch_to,
                    warehouse_code=selected_warehouse_to,
                    group_code=selected_group,
                    status=status_raw,
                )
                if want_excel and report is not None:
                    return build_outgoing_transfers_excel(report)
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_wh_outgoing failed: %s', exc)
        error = f'تعذّر تحميل تحويلات المستودعات: {exc}'
        report = None

    return render(
        request,
        'search/browse_wh_outgoing.html',
        {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'default_from': default_from.isoformat(),
            'default_to': today.isoformat(),
            'selected_branch_from': selected_branch_from,
            'selected_branch_to': selected_branch_to,
            'selected_branch': selected_branch_to,
            'selected_warehouse_from': selected_warehouse_from,
            'selected_warehouse_to': selected_warehouse_to,
            'selected_warehouse': selected_warehouse_to,
            'selected_group': selected_group,
            'status': status_raw,
            'branches': branches,
            'warehouses_from': warehouses_from,
            'warehouses_to': warehouses_to,
            'warehouses': warehouses_to,
            'groups': groups,
            'report': report,
            'error': error,
        },
    )


@login_required
@require_GET
@never_cache
def browse_wh_qty_compare(request):
    """مقارنة كميات الأصناف بين مستودعين (رصيد IAS_ITM_WCODE)."""
    selected_wh_a = str(
        request.GET.get('wh_a') or request.GET.get('warehouse_a') or ''
    ).strip()
    selected_wh_b = str(
        request.GET.get('wh_b') or request.GET.get('warehouse_b') or ''
    ).strip()
    selected_group = str(request.GET.get('group') or '').strip()
    item_q = str(request.GET.get('q') or request.GET.get('item') or '').strip()
    mode_raw = str(request.GET.get('mode') or 'all').strip().lower()
    if mode_raw not in ('all', 'zero_b', 'both', 'diff', 'gap', 'a_only', 'b_only'):
        mode_raw = 'all'
    qty_basis = str(
        request.GET.get('qty') or request.GET.get('qty_basis') or 'avail'
    ).strip().lower()
    if qty_basis in ('available', 'expected', 'after'):
        qty_basis = 'avail'
    if qty_basis in ('current', 'stock', 'balance'):
        qty_basis = 'avl'
    if qty_basis not in ('avail', 'avl'):
        qty_basis = 'avail'
    want_excel = str(request.GET.get('export') or '').strip().lower() in {
        'xls',
        'excel',
        'xlsx',
    }
    want_run = str(request.GET.get('run') or '').strip() in ('1', 'true', 'yes')
    want_run = want_run or want_excel

    report = None
    error = ''
    hint = ''
    branches: list[dict] = []
    groups: list[dict] = []
    all_warehouses: list[dict] = []
    wh_a_name = selected_wh_a
    wh_b_name = selected_wh_b

    try:
        from .oracle_stock import (
            fetch_sales_group_options,
            fetch_warehouse_options,
            oracle_enabled,
            oracle_session,
        )
        from .oracle_wh_qty_compare import (
            build_wh_qty_compare_excel,
            build_wh_qty_compare_report,
        )
        from .validators import sanitize_search_query

        if item_q:
            item_q = sanitize_search_query(item_q)

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن مقارنة كميات المستودعات.'
        else:
            with oracle_session():
                wh_rows = fetch_warehouse_options(active_only=True) or []
                all_warehouses = [
                    {
                        'code': str(w.get('code') or '').strip(),
                        'name': str(w.get('name') or '').strip()
                        or str(w.get('code') or '').strip(),
                        'branch_code': str(w.get('branch_code') or '').strip(),
                        'branch_name': str(w.get('branch_name') or '').strip(),
                    }
                    for w in wh_rows
                    if str(w.get('code') or '').strip()
                ]
                branch_map: dict[str, str] = {}
                wh_name_map: dict[str, str] = {}
                for w in all_warehouses:
                    wh_name_map[w['code']] = w['name']
                    brn = w['branch_code']
                    if brn:
                        branch_map[brn] = w['branch_name'] or brn
                branches = [
                    {'code': code, 'name': name}
                    for code, name in sorted(
                        branch_map.items(), key=lambda x: (x[1], x[0])
                    )
                ]
                groups = fetch_sales_group_options() or []
                group_codes = {str(g.get('code') or '').strip() for g in groups}
                if selected_group and selected_group not in group_codes:
                    selected_group = ''

                allowed = set(wh_name_map)
                if selected_wh_a and selected_wh_a not in allowed:
                    selected_wh_a = ''
                if selected_wh_b and selected_wh_b not in allowed:
                    selected_wh_b = ''
                wh_a_name = wh_name_map.get(selected_wh_a, selected_wh_a)
                wh_b_name = wh_name_map.get(selected_wh_b, selected_wh_b)

                if want_run:
                    if not selected_wh_a or not selected_wh_b:
                        raise ValidationError('حدد مستودع الأول والثاني ثم اضغط «بحث».')
                    if selected_wh_a == selected_wh_b:
                        raise ValidationError('اختر مستودعين مختلفين.')
                    report = build_wh_qty_compare_report(
                        warehouse_a=selected_wh_a,
                        warehouse_b=selected_wh_b,
                        group_code=selected_group,
                        item_q=item_q,
                        mode=mode_raw,
                        qty_basis=qty_basis,
                        wh_a_name=wh_a_name,
                        wh_b_name=wh_b_name,
                    )
                    if want_excel and report is not None:
                        return build_wh_qty_compare_excel(report)
                else:
                    hint = (
                        'اختر المستودع الأول ثم الثاني — '
                        'تُعرض أصناف الأول فقط مع كميتها في الثاني '
                        '(المتوفّر = الرصيد بعد خصم مبيعات POS غير المرحّلة).'
                    )
    except ValidationError as exc:
        error = str(exc)
        report = None
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_wh_qty_compare failed: %s', exc)
        error = f'تعذّر تحميل مقارنة الكميات: {exc}'
        report = None

    return render(
        request,
        'search/browse_wh_qty_compare.html',
        {
            'branches': branches,
            'groups': groups,
            'all_warehouses': all_warehouses,
            'selected_wh_a': selected_wh_a,
            'selected_wh_b': selected_wh_b,
            'wh_a_name': wh_a_name,
            'wh_b_name': wh_b_name,
            'selected_group': selected_group,
            'item_q': item_q,
            'mode': mode_raw,
            'qty_basis': qty_basis,
            'report': report,
            'error': error,
            'hint': hint,
        },
    )


@login_required
@require_GET
@never_cache
def browse_wh_no_transfer(request):
    """أصناف لها رصيد في المخزن ولم تخرج بتحويل صادر خلال الفترة."""
    from datetime import date as date_cls, timedelta

    today = date_cls.today()
    default_from = today - timedelta(days=365)
    date_from_raw = str(request.GET.get('date_from') or default_from.isoformat()).strip()
    date_to_raw = str(request.GET.get('date_to') or today.isoformat()).strip()
    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_warehouse = str(
        request.GET.get('warehouse') or request.GET.get('wh') or ''
    ).strip()
    selected_group = str(request.GET.get('group') or '').strip()
    qty_basis = str(
        request.GET.get('qty') or request.GET.get('qty_basis') or 'avail'
    ).strip().lower()
    if qty_basis in ('available', 'expected', 'after'):
        qty_basis = 'avail'
    if qty_basis in ('current', 'stock', 'balance'):
        qty_basis = 'avl'
    if qty_basis not in ('avail', 'avl'):
        qty_basis = 'avail'
    want_run = str(request.GET.get('run') or '').strip() in ('1', 'true', 'yes')
    want_run = want_run or bool(selected_warehouse)

    report = None
    error = ''
    hint = ''
    branches: list[dict] = []
    groups: list[dict] = []
    all_warehouses: list[dict] = []
    branch_warehouses: list[dict] = []
    wh_name = selected_warehouse

    try:
        date_from, date_to = _parse_sales_dates(date_from_raw, date_to_raw)
    except ValidationError as exc:
        return render(
            request,
            'search/browse_wh_no_transfer.html',
            {
                'date_from': (date_from_raw or '')[:10],
                'date_to': (date_to_raw or '')[:10],
                'default_from': default_from.isoformat(),
                'default_to': today.isoformat(),
                'branches': [],
                'groups': [],
                'all_warehouses': [],
                'branch_warehouses': [],
                'selected_branch': selected_branch,
                'selected_warehouse': selected_warehouse,
                'selected_group': selected_group,
                'qty_basis': qty_basis,
                'wh_name': wh_name,
                'report': None,
                'error': str(exc),
                'hint': '',
            },
        )

    try:
        from .oracle_stock import (
            fetch_sales_group_options,
            fetch_warehouse_options,
            oracle_enabled,
            oracle_session,
        )
        from .oracle_wh_no_transfer import build_wh_no_transfer_report

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن عرض الرصيد بلا تحويل.'
        else:
            with oracle_session():
                wh_rows = fetch_warehouse_options(active_only=True) or []
                all_warehouses = [
                    {
                        'code': str(w.get('code') or '').strip(),
                        'name': str(w.get('name') or '').strip()
                        or str(w.get('code') or '').strip(),
                        'branch_code': str(w.get('branch_code') or '').strip(),
                        'branch_name': str(w.get('branch_name') or '').strip(),
                    }
                    for w in wh_rows
                    if str(w.get('code') or '').strip()
                ]
                branch_map: dict[str, str] = {}
                wh_name_map: dict[str, str] = {}
                for w in all_warehouses:
                    wh_name_map[w['code']] = w['name']
                    brn = w['branch_code']
                    if brn:
                        branch_map[brn] = w['branch_name'] or brn
                branches = [
                    {'code': code, 'name': name}
                    for code, name in sorted(
                        branch_map.items(), key=lambda x: (x[1], x[0])
                    )
                ]
                groups = fetch_sales_group_options() or []
                group_codes = {str(g.get('code') or '').strip() for g in groups}
                if selected_group and selected_group not in group_codes:
                    selected_group = ''

                if selected_branch and selected_branch not in branch_map:
                    selected_branch = ''

                branch_warehouses = [
                    w
                    for w in all_warehouses
                    if not selected_branch or w['branch_code'] == selected_branch
                ]
                branch_warehouses.sort(key=lambda w: (w['name'], w['code']))
                allowed = {w['code'] for w in branch_warehouses}
                if selected_warehouse and selected_warehouse not in allowed:
                    selected_warehouse = ''
                wh_name = wh_name_map.get(selected_warehouse, selected_warehouse)

                if want_run:
                    if not selected_warehouse:
                        raise ValidationError('اختر المخزن ثم اضغط «عرض».')
                    report = build_wh_no_transfer_report(
                        warehouse=selected_warehouse,
                        date_from=date_from,
                        date_to=date_to,
                        group_code=selected_group,
                        qty_basis=qty_basis,
                        warehouse_name=wh_name,
                    )
                else:
                    hint = (
                        'اختر الفترة والمخزن — تُعرض الأصناف ذات الرصيد '
                        'التي لم تخرج بتحويل صادر من هذا المخزن خلال الفترة.'
                    )
    except ValidationError as exc:
        error = str(exc)
        report = None
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_wh_no_transfer failed: %s', exc)
        error = f'تعذّر تحميل رصيد بلا تحويل: {exc}'
        report = None

    return render(
        request,
        'search/browse_wh_no_transfer.html',
        {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'default_from': default_from.isoformat(),
            'default_to': today.isoformat(),
            'branches': branches,
            'groups': groups,
            'all_warehouses': all_warehouses,
            'branch_warehouses': branch_warehouses,
            'selected_branch': selected_branch,
            'selected_warehouse': selected_warehouse,
            'selected_group': selected_group,
            'qty_basis': qty_basis,
            'wh_name': wh_name,
            'report': report,
            'error': error,
            'hint': hint,
        },
    )


@login_required
@require_GET
@never_cache
def browse_sold_no_supply(request):
    """أصناف تُباع بلا مشتريات على الفرع ولا تحويل وارد إليه."""
    from datetime import date

    today = date.today()
    month_start = today.replace(day=1)
    selected_branch = str(request.GET.get('branch') or '').strip()
    selected_group = str(request.GET.get('group') or '').strip()
    q = str(request.GET.get('q') or '').strip()[:80]
    report = None
    error = ''
    branches: list[dict] = []
    groups: list[dict] = []

    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from') or month_start.isoformat(),
            request.GET.get('date_to') or today.isoformat(),
        )
    except ValidationError as exc:
        return render(
            request,
            'search/browse_sold_no_supply.html',
            {
                'date_from': (request.GET.get('date_from') or '')[:10],
                'date_to': (request.GET.get('date_to') or '')[:10],
                'default_from': month_start.isoformat(),
                'default_to': today.isoformat(),
                'selected_branch': selected_branch,
                'selected_group': selected_group,
                'q': q,
                'branches': [],
                'groups': [],
                'report': None,
                'error': str(exc),
            },
        )

    try:
        from .oracle_income import fetch_income_branches
        from .oracle_sold_no_supply import build_sold_no_supply_report
        from .oracle_stock import (
            fetch_sales_group_options,
            oracle_enabled,
            oracle_session,
        )

        if not oracle_enabled():
            error = 'أوراكل غير مفعّل — لا يمكن عرض التقرير.'
        else:
            with oracle_session():
                branches = fetch_income_branches()
                groups = fetch_sales_group_options()
                if selected_branch not in {row['code'] for row in branches}:
                    selected_branch = ''
                if selected_group not in {row['code'] for row in groups}:
                    selected_group = ''
                report = build_sold_no_supply_report(
                    date_from,
                    date_to,
                    branch_code=selected_branch,
                    group_code=selected_group,
                    q=q,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_sold_no_supply failed: %s', exc)
        error = f'تعذّر تحميل بيع بلا توريد: {exc}'
        report = None

    return render(
        request,
        'search/browse_sold_no_supply.html',
        {
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'default_from': month_start.isoformat(),
            'default_to': today.isoformat(),
            'selected_branch': selected_branch,
            'selected_group': selected_group,
            'q': q,
            'branches': branches,
            'groups': groups,
            'report': report,
            'error': error,
        },
    )


@login_required
@require_GET
def browse_sold_no_supply_api(request):
    """صفحة إضافية من أصناف البيع بلا توريد."""
    from datetime import date

    today = date.today()
    month_start = today.replace(day=1)
    try:
        date_from, date_to = _parse_sales_dates(
            request.GET.get('date_from') or month_start.isoformat(),
            request.GET.get('date_to') or today.isoformat(),
        )
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

    try:
        offset = max(0, int(request.GET.get('offset') or 0))
        limit = min(max(1, int(request.GET.get('limit') or 80)), 200)
    except (TypeError, ValueError):
        offset, limit = 0, 80

    try:
        from .oracle_sold_no_supply import fetch_sold_no_supply_items
        from .oracle_stock import oracle_enabled, oracle_session

        if not oracle_enabled():
            return JsonResponse(
                {'ok': False, 'error': 'أوراكل غير مفعّل.'}, status=400
            )
        with oracle_session():
            rows = fetch_sold_no_supply_items(
                date_from,
                date_to,
                branch_code=str(request.GET.get('branch') or '').strip(),
                group_code=str(request.GET.get('group') or '').strip(),
                q=str(request.GET.get('q') or '').strip()[:80],
                offset=offset,
                limit=limit,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning('browse_sold_no_supply_api failed: %s', exc)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)

    return JsonResponse({'ok': True, 'rows': rows, 'offset': offset, 'limit': limit})
