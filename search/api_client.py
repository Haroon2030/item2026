"""
جلب الأسعار والكميات ومزامنة الأصناف/الباركود — كله من أوراكل.
"""

from __future__ import annotations

import logging
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)


class ApiClientError(Exception):
    """خطأ عام عند فشل الاتصال أو قراءة الاستجابة."""


def _normalize_text(value: Any) -> str:
    """يزيل علامات التشكيل/الاتجاه المخفية لتحسين مطابقة البحث."""
    text = unicodedata.normalize('NFKC', str(value or ''))
    cleaned = []
    for ch in text:
        if unicodedata.category(ch) in {'Mn', 'Me', 'Cf'}:
            continue
        if ch in '\u200e\u200f\u202a\u202b\u202c\u202d\u202e':
            continue
        cleaned.append(ch)
    return ''.join(cleaned).strip()


def _normalize_item(raw: dict, field_map: dict) -> dict:
    pack = raw.get(field_map.get('pack_size', 'P_SIZE'), '')
    if pack in (None, '', 0, '0'):
        pack = raw.get('p_size', '') or ''
    avg_cost = raw.get(field_map.get('avg_cost', 'I_CWTAVG'))
    if avg_cost in (None, ''):
        avg_cost = raw.get('I_CWTAVG')
    if avg_cost in (None, ''):
        avg_cost = raw.get('I_cost')
    return {
        'code': raw.get(field_map.get('code', 'I_CODE'), '') or '',
        'name': raw.get(field_map.get('name', 'I_NAME'), '') or '',
        'barcode': raw.get(field_map.get('barcode', 'BARCODE'), '') or '',
        'price': raw.get(field_map.get('price', 'I_PRICE'), '') or '',
        'unit': raw.get(field_map.get('unit', 'ITM_UNT'), '') or '',
        'quantity': raw.get(field_map.get('quantity', 'AVL_QTY'), '') or '',
        'pack_size': '' if pack in (None, '', 0, '0') else str(pack).strip(),
        'avg_cost': '' if avg_cost in (None, '') else str(avg_cost).strip(),
        'raw': raw,
    }


def _is_valid_item(raw: dict) -> bool:
    if not isinstance(raw, dict):
        return False
    if raw.get('errorId') not in (None, '', 0, '0'):
        return False
    if raw.get('errorDisc'):
        return False
    return bool(raw.get('I_CODE') or raw.get('I_NAME'))


def search_prices_by_code(item_code: str, price_w_code: str | None = None, **_kwargs) -> list[dict]:
    """أسعار صنف (رقم أو باركود) لكل وحدة في مخزن — من أوراكل."""
    from .oracle_catalog import fetch_item_prices
    from .oracle_stock import OracleStockError

    try:
        return fetch_item_prices(item_code, price_w_code)
    except OracleStockError as exc:
        raise ApiClientError(str(exc)) from exc


def fetch_qty_by_code(item_code: str, w_code: str | None = None, **_kwargs) -> list[dict]:
    """رصيد الصنف ومتوسط تكلفته في مخزن — من أوراكل."""
    from .oracle_catalog import fetch_item_qty
    from .oracle_stock import OracleStockError

    try:
        return fetch_item_qty(item_code, w_code)
    except OracleStockError as exc:
        raise ApiClientError(str(exc)) from exc


def get_unit_meta(item_code: str) -> dict[str, dict]:
    """وحدات الصنف من الفهرس المحلي: وحدة → عبوة/باركود/اسم."""
    from .models import ItemBarcode

    meta: dict[str, dict] = {}
    rows = ItemBarcode.objects.filter(item_code=item_code).order_by('unit', '-barcode')
    for row in rows:
        unit = (row.unit or '').strip()
        if not unit:
            continue
        pack_raw = str(row.pack_size or '').strip()
        try:
            pack = float(pack_raw) if pack_raw else None
        except ValueError:
            pack = None
        if pack is None or pack <= 0:
            continue

        # لا تستبدل عبوة معروفة بصف أضعف (بدون باركود) إن وُجدت
        existing = meta.get(unit)
        if existing and existing.get('barcode') and not (row.barcode or '').strip():
            continue

        meta[unit] = {
            'pack_size': pack,
            'pack_size_display': pack_raw or (
                str(int(pack)) if float(pack).is_integer() else str(pack)
            ),
            'barcode': (row.barcode or '').strip(),
            'name': row.name,
        }
    return meta


def _to_float(value: Any) -> float | None:
    if value is None or value == '':
        return None
    try:
        return float(str(value).replace(',', '').strip())
    except ValueError:
        return None


def _fmt_qty(value: float) -> str:
    text = f'{value:.4f}'.rstrip('0').rstrip('.')
    return text or '0'


def _fmt_cost(value: float) -> str:
    return f'{value:.2f}'


def _pick_avg_cost(*sources: Any) -> str:
    """استخراج متوسط التكلفة I_CWTAVG مع احتياطي I_cost."""
    keys = ('avg_cost', 'I_CWTAVG', 'i_cwtavg', 'I_cost', 'I_COST', 'cost')
    for src in sources:
        if not isinstance(src, dict):
            continue
        for key in keys:
            val = src.get(key)
            if val not in (None, ''):
                return str(val).strip()
    return ''


def _is_weight_unit(unit: str) -> bool:
    u = (unit or '').strip().lower()
    return any(k in u for k in ('كيلو', 'كغ', 'كجم', 'غم', 'جرام', 'غرام', 'kg', 'g'))


def _is_piece_unit(unit: str) -> bool:
    u = (unit or '').strip().lower()
    return any(
        k in u
        for k in (
            'حبة',
            'حبه',
            'كرتون',
            'علبة',
            'علبه',
            'باكت',
            'ربطة',
            'درزن',
            'صندوق',
            'كيس',
            'شكار',
            'سطل',
            'تنك',
            'استاند',
            'فلين',
        )
    )


def _convert_qty(
    qty: float,
    from_unit: str,
    to_unit: str,
    unit_meta: dict[str, dict],
) -> float | None:
    """
    تحويل الكمية بين وحدتين عبر حجم العبوة.
    qty_to = qty_from * pack_from / pack_to

    قواعد مهمة:
    - لا نفترض عبوة = 1 عند غياب البيانات.
    - لا نحوّل بين وحدة وزن (كيلو) ووحدة عدد (حبة/كرتون) إذا كانت العبوتان = 1
      لأن ذلك ينسخ كمية الحبة إلى الكيلو بالخطأ.
    """
    if from_unit == to_unit:
        return qty

    from_meta = unit_meta.get(from_unit)
    to_meta = unit_meta.get(to_unit)
    if not from_meta or not to_meta:
        return None

    from_pack = from_meta.get('pack_size')
    to_pack = to_meta.get('pack_size')
    if not from_pack or not to_pack or from_pack <= 0 or to_pack <= 0:
        return None

    # حبة(1) ↔ كيلو(1) ليستا نفس الوحدة رغم تطابق رقم العبوة
    if from_pack == 1 and to_pack == 1:
        from_w, to_w = _is_weight_unit(from_unit), _is_weight_unit(to_unit)
        from_p, to_p = _is_piece_unit(from_unit), _is_piece_unit(to_unit)
        if (from_w and to_p) or (from_p and to_w):
            return None
        if from_unit != to_unit and not (from_w and to_w) and not (from_p and to_p):
            # وحدتان مختلفتان بنفس العبوة 1 بدون علاقة واضحة
            return None

    return qty * from_pack / to_pack


def merge_prices_with_qty(
    prices: list[dict],
    qtys: list[dict],
    unit_meta: dict[str, dict] | None = None,
) -> list[dict]:
    """
    دمج الأسعار مع الكمية، مع توزيع الكمية على باقي الوحدات حسب حجم العبوة.
    الكمية القادمة من الـ API تُعتمد لوحدتها كما هي، وبقية الوحدات تُحوَّل فقط
    عند معرفة عبوة المصدر والهدف معًا.
    """
    unit_meta = unit_meta or {}
    qty_by_unit = {q['unit']: q for q in qtys if q.get('unit')}

    # مرجع التحويل: أول كمية صالحة من الـ API
    source_unit = ''
    source_qty = None
    for q in qtys:
        val = _to_float(q.get('quantity'))
        if val is None or not q.get('unit'):
            continue
        source_unit = q['unit']
        source_qty = val
        break

    units: list[str] = []
    for row in prices:
        u = row.get('unit') or ''
        if u and u not in units:
            units.append(u)
    for u in unit_meta:
        if u not in units:
            units.append(u)
    for u in qty_by_unit:
        if u not in units:
            units.append(u)

    # إن رجعت الكمية بوحدة غير موجودة في القائمة أضفها
    if source_unit and source_unit not in units:
        units.insert(0, source_unit)

    price_by_unit = {p.get('unit') or '': p for p in prices}
    sample = prices[0] if prices else (qtys[0] if qtys else {})

    merged = []
    for unit in units:
        price_row = price_by_unit.get(unit) or {}
        qty_row = qty_by_unit.get(unit) or {}
        meta = unit_meta.get(unit) or {}
        pack = meta.get('pack_size')
        pack_display = meta.get('pack_size_display') or ''
        if not pack_display and pack:
            pack_display = str(int(pack)) if float(pack).is_integer() else str(pack)

        # احتياطي من استجابة الأسعار إن الفهرس المحلي ناقص
        if not pack_display:
            raw = price_row.get('raw') or {}
            raw_pack = raw.get('P_SIZE')
            if raw_pack in (None, '', 0, '0'):
                raw_pack = raw.get('p_size')
            if raw_pack not in (None, '', 0, '0'):
                pack_display = str(raw_pack).strip()
                try:
                    pack = float(str(raw_pack).replace(',', '').strip())
                except ValueError:
                    pack = None
                if pack and pack > 0 and unit not in unit_meta:
                    unit_meta[unit] = {
                        'pack_size': pack,
                        'pack_size_display': pack_display,
                        'barcode': (price_row.get('barcode') or '').strip(),
                        'name': price_row.get('name') or '',
                    }

        # الوحدة المخزنية = الوحدة التي يرجع بها النظام الرصيد/التكلفة مباشرة
        is_stock_unit = unit in qty_by_unit and _to_float(qty_row.get('quantity')) is not None

        quantity = ''
        if is_stock_unit:
            # وحدة الـ API كما هي — بدون إعادة حساب
            quantity = _fmt_qty(_to_float(qty_row.get('quantity')))
        elif source_qty is not None and source_unit:
            converted = _convert_qty(source_qty, source_unit, unit, unit_meta)
            if converted is not None:
                quantity = _fmt_qty(converted)

        # متوسط التكلفة فقط لوحدتها من الـ API — لا نحوّله بين الوحدات (يتلف كيلو↔باكت)
        avg_cost = _pick_avg_cost(qty_row, price_row, price_row.get('raw') or {})
        if avg_cost:
            cost_num = _to_float(avg_cost)
            avg_cost = _fmt_cost(cost_num) if cost_num is not None else avg_cost

        merged.append(
            {
                'code': price_row.get('code') or qty_row.get('code') or sample.get('code') or '',
                'name': price_row.get('name') or qty_row.get('name') or meta.get('name') or '',
                'barcode': meta.get('barcode')
                or price_row.get('barcode')
                or qty_row.get('barcode')
                or '',
                'unit': unit,
                'pack_size': pack_display or (price_row.get('pack_size') or ''),
                'price': price_row.get('price', ''),
                'quantity': quantity,
                'avg_cost': avg_cost,
                'is_stock_unit': is_stock_unit,
                'raw': price_row.get('raw') or qty_row,
            }
        )
    return merged


def search_item_details(item_code: str, warehouse: str | None = None) -> list[dict]:
    """جلب الأسعار + الكمية بالتوازي، مع توزيع الكمية على الوحدات حسب العبوة."""
    prices: list[dict] = []
    qtys: list[dict] = []
    price_error: Exception | None = None
    qty_error: Exception | None = None
    queried = str(item_code or '').strip()

    with ThreadPoolExecutor(max_workers=2) as pool:
        price_future = pool.submit(search_prices_by_code, queried, warehouse)
        qty_future = pool.submit(fetch_qty_by_code, queried, warehouse)

        try:
            prices = price_future.result()
        except Exception as exc:  # noqa: BLE001
            price_error = exc

        try:
            qtys = qty_future.result()
        except Exception as exc:
            qty_error = exc
            logger.warning('Quantity fetch failed, showing prices only: %s', exc)
            qtys = []

    # إن كان البحث باركود: الأسعار تعيد رقم الصنف الحقيقي بينما الكمية تحتاج رقم الصنف
    resolved = ''
    if prices:
        resolved = str(prices[0].get('code') or '').strip()
    effective_code = resolved or queried

    if resolved and resolved != queried:
        try:
            qtys_resolved = fetch_qty_by_code(resolved, warehouse)
            if qtys_resolved:
                qtys = qtys_resolved
                qty_error = None
        except Exception as exc:  # noqa: BLE001
            qty_error = qty_error or exc
            logger.warning('Quantity refetch by resolved code failed: %s', exc)

    # إعادة محاولة الكمية تسلسلياً إن فشلت أو رجعت فارغة
    if not qtys:
        try:
            qtys = fetch_qty_by_code(effective_code, warehouse)
        except Exception as exc:  # noqa: BLE001
            qty_error = qty_error or exc
            logger.warning('Quantity retry failed: %s', exc)
            qtys = []

    # إن فشل السعر لكن نجحت الكمية نعرض الكمية
    if price_error and not prices:
        if qtys:
            logger.warning('Price fetch failed, showing qty only: %s', price_error)
        else:
            if isinstance(price_error, ApiClientError):
                raise price_error
            raise ApiClientError(str(price_error)) from price_error

    unit_meta = get_unit_meta(effective_code)
    merged = merge_prices_with_qty(prices, qtys, unit_meta=unit_meta)
    if qty_error and not any(str(r.get('quantity') or '').strip() for r in merged):
        for row in merged:
            row['_qty_warning'] = str(qty_error)
    return merged


def compare_item_across_warehouses(
    item_code: str,
    warehouses: list[str] | None = None,
    *,
    warehouse_names: dict[str, str] | None = None,
) -> list[dict]:
    """
    مقارنة سعر البيع والتكلفة وآخر توريد لنفس الصنف عبر عدة مخازن.
    المصدر: أوراكل فقط (أسرع وأكثر ثباتاً من REST).
    يعيد صفاً لكل مخزن: warehouse, name, unit, price, last_buy, avg_cost, quantity.
    """
    from django.core.cache import cache

    from .oracle_stock import fetch_item_compare_from_oracle, oracle_session

    cfg = settings.ERP_CONFIG
    codes = [
        str(c).strip()
        for c in (warehouses or cfg.get('COMPARE_WAREHOUSES') or [])
        if str(c).strip()
    ]
    names = {str(k).strip(): str(v).strip() for k, v in (warehouse_names or {}).items()}
    queried = str(item_code or '').strip()
    if not queried or not codes:
        return []

    cache_key = f"item:compare:v13:{queried}:{','.join(codes)}"
    cached = cache.get(cache_key)
    if isinstance(cached, list) and cached:
        return cached

    def _clean_name(wh: str, raw: str) -> str:
        label = (raw or '').strip() or f'مخزن {wh}'
        for suffix in (f' - {wh}', f'-{wh}', f'({wh})', f'（{wh}）'):
            if label.endswith(suffix):
                label = label[: -len(suffix)].strip()
        if label == wh or label == f'مخزن {wh}':
            return f'مخزن {wh}'
        return label

    try:
        with oracle_session():
            bundle = fetch_item_compare_from_oracle(queried, codes)
    except Exception as exc:  # noqa: BLE001
        logger.warning('Oracle warehouse compare failed: %s', exc)
        bundle = {'rows': {}}

    ora_rows = bundle.get('rows') or {}
    out: list[dict] = []
    for wh in codes:
        row = ora_rows.get(wh) or {}
        price_num = _to_float(row.get('price'))
        buy_num = _to_float(row.get('last_buy'))
        qty_num = _to_float(row.get('quantity'))
        pending_num = _to_float(row.get('pending_qty')) or 0.0
        expected_num = _to_float(row.get('expected_qty'))
        if expected_num is None and qty_num is not None:
            expected_num = round(qty_num - pending_num, 4)
        elif expected_num is None and pending_num:
            expected_num = round(0.0 - pending_num, 4)
            if qty_num is None:
                qty_num = 0.0
        price = _fmt_cost(price_num) if price_num is not None else (
            str(row.get('price') or '').strip()
        )
        last_buy = _fmt_cost(buy_num) if buy_num is not None else (
            str(row.get('last_buy') or '').strip()
        )
        avg_cost = str(row.get('avg_cost') or '').strip()
        quantity = ''
        if qty_num is not None:
            quantity = _fmt_qty(qty_num)
        elif str(row.get('quantity') or '').strip():
            quantity = str(row.get('quantity')).strip()
        expected = _fmt_qty(expected_num) if expected_num is not None else ''
        pending = _fmt_qty(pending_num) if pending_num else ''

        unit = str(row.get('unit') or '').strip()
        out.append(
            {
                'warehouse': wh,
                'name': _clean_name(wh, names.get(wh, '')),
                'code': queried,
                'unit': unit,
                'price': price,
                'last_buy': last_buy,
                'last_buy_date': str(row.get('last_buy_date') or ''),
                'avg_cost': avg_cost,
                'quantity': quantity,
                'pending_qty': pending,
                'expected_qty': expected,
                'expected_neg': False,
                'expected_low': bool(
                    expected_num is not None
                    and qty_num is not None
                    and expected_num < qty_num
                ),
                'ok': bool(
                    price or avg_cost or quantity or expected or last_buy or unit
                ),
            }
        )

    # المخازن ذات الرصيد/المتوقع أولاً حتى لا يظهر صف فارغ في الأعلى
    out.sort(
        key=lambda r: (
            0 if (r.get('quantity') or r.get('expected_qty')) else 1,
            -(_to_float(r.get('quantity')) or 0),
            str(r.get('name') or ''),
        )
    )

    try:
        cache.set(cache_key, out, int(cfg.get('COMPARE_CACHE_TTL', 1800) or 1800))
    except Exception:  # noqa: BLE001
        pass
    return out


def fetch_all_items() -> list[dict]:
    """كل وحدات/باركودات الأصناف النشطة من أوراكل."""
    from .oracle_catalog import fetch_catalog_rows
    from .oracle_stock import OracleStockError

    try:
        return fetch_catalog_rows()
    except OracleStockError as exc:
        raise ApiClientError(str(exc)) from exc


def fetch_all_groups() -> list[dict]:
    """مجموعات الأصناف من أوراكل."""
    from .oracle_catalog import fetch_group_rows
    from .oracle_stock import OracleStockError

    try:
        return fetch_group_rows()
    except OracleStockError as exc:
        raise ApiClientError(str(exc)) from exc


def sync_barcode_index() -> int:
    """
    مزامنة الباركود/العبوات/رمز المجموعة وأسماء المجموعات من أوراكل.

    الصفوف تُحفظ كما في المصدر (بما فيها التكرار واختلاف شكل الأحرف).
    """
    from .models import ItemBarcode, ItemGroup

    rows = fetch_all_items()
    mapped: list[ItemBarcode] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        # كما المصدر: قص الطول فقط لحدود الأعمدة.
        barcode = str(row.get('Barcode') or '').strip()[:128]
        item_code = str(row.get('Item_code') or '').strip()[:64]
        unit = str(row.get('itm_unt') or '').strip()[:64]
        if not item_code or not unit:
            continue

        g_raw = (
            row.get('Item_category')
            if row.get('Item_category') is not None
            else row.get('G_CODE')
            if row.get('G_CODE') is not None
            else row.get('g_code')
        )
        g_code = str('' if g_raw is None else g_raw).strip()[:64]

        pack_raw = (
            row.get('p_size')
            if row.get('p_size') is not None
            else row.get('P_SIZE')
            if row.get('P_SIZE') is not None
            else row.get('Pack_Size')
        )
        pack_size = '' if pack_raw is None else str(pack_raw).strip()

        mapped.append(
            ItemBarcode(
                barcode=barcode,
                item_code=item_code,
                name=str(row.get('Item_ar_name') or '').strip()[:255],
                unit=unit,
                pack_size=pack_size[:32],
                g_code=g_code,
            )
        )

    groups: list[ItemGroup] = []
    try:
        group_rows = fetch_all_groups()
        seen_g: set[str] = set()
        for row in group_rows:
            if not isinstance(row, dict):
                continue
            g_code = str(row.get('G_CODE') or row.get('g_code') or '').strip()
            if not g_code or g_code in seen_g:
                continue
            seen_g.add(g_code)
            groups.append(
                ItemGroup(
                    g_code=g_code,
                    g_name=str(row.get('G_A_NAME') or row.get('G_E_NAME') or '').strip(),
                )
            )
    except ApiClientError as exc:
        logger.warning('Group sync skipped: %s', exc)

    with transaction.atomic():
        ItemBarcode.objects.all().delete()
        ItemBarcode.objects.bulk_create(mapped, batch_size=2000)
        if groups:
            ItemGroup.objects.all().delete()
            ItemGroup.objects.bulk_create(groups, batch_size=500)

    logger.info('Synced %s barcode/unit rows and %s groups', len(mapped), len(groups))
    with_pack = sum(1 for m in mapped if m.pack_size)
    with_g = sum(1 for m in mapped if m.g_code)
    logger.info('Meta filled: pack=%s g_code=%s groups=%s', with_pack, with_g, len(groups))
    return len(mapped)


def index_meta_incomplete() -> bool:
    """هل الفهرس موجود لكن بدون عبوات/مجموعات؟ يحتاج إعادة مزامنة."""
    from .models import ItemBarcode

    total = ItemBarcode.objects.count()
    if total == 0:
        return False
    with_pack = ItemBarcode.objects.exclude(pack_size='').count()
    with_g = ItemBarcode.objects.exclude(g_code='').count()
    return with_pack < max(1, total // 20) or with_g < max(1, total // 20)


def get_item_group(item_code: str) -> dict:
    """جلب G_CODE واسم المجموعة لصنف من الفهرس المحلي."""
    from .models import ItemBarcode, ItemGroup

    row = (
        ItemBarcode.objects.filter(item_code=item_code)
        .exclude(g_code='')
        .order_by('-barcode')
        .first()
    )
    if not row:
        row = ItemBarcode.objects.filter(item_code=item_code).first()
    g_code = (row.g_code if row else '') or ''
    g_name = ''
    if g_code:
        group = ItemGroup.objects.filter(g_code=g_code).first()
        if group:
            g_name = group.g_name
    return {'g_code': g_code, 'g_name': g_name}


def list_groups() -> list[dict]:
    """كل مجموعات الأصناف من الفهرس المحلي مرتبة بالاسم."""
    from .models import ItemGroup

    return [
        {'g_code': g.g_code, 'g_name': g.g_name or g.g_code}
        for g in ItemGroup.objects.order_by('g_name', 'g_code')
    ]


def lookup_by_group(g_code: str) -> list[dict]:
    """
    أصناف المجموعة من الفهرس المحلي السريع (مزامنة من أوراكل).
    صف واحد لكل رقم صنف — بدون جلب حي ثقيل عند كل تصفح.
    """
    from .models import ItemBarcode

    code = str(g_code or '').strip()
    if not code:
        return []

    rows = (
        ItemBarcode.objects.filter(g_code=code)
        .exclude(item_code='')
        .order_by('name', 'item_code', '-barcode')
        .only('barcode', 'item_code', 'name', 'unit', 'pack_size', 'g_code')
    )
    best: dict[str, object] = {}
    ordered: list[str] = []
    for row in rows:
        item_code = (row.item_code or '').strip()
        if not item_code:
            continue
        if item_code not in best:
            best[item_code] = row
            ordered.append(item_code)
            continue
        prev = best[item_code]
        if not (prev.barcode or '').strip() and (row.barcode or '').strip():
            best[item_code] = row

    return _rows_to_item_dicts(best[c] for c in ordered)


def _pick_pricing_summary(prices: list[dict], qtys: list[dict]) -> dict:
    """
    ملخص للعرض والحساب:
    - الكمية والتكلفة دائماً من نفس صف رصيد المخزن (IAS_ITM_WCODE).
    - عند تعدّد الوحدات: اختر أعلى كمية موجبة (لا أول صف صفري).
    - السعر من IAS_ITEM_PRICE لنفس الوحدة إن وُجد (للعرض فقط).
    """
    stock: dict | None = None
    best_qty = float('-inf')
    for q in qtys or []:
        qty = _to_float(q.get('quantity'))
        if qty is None:
            continue
        # فضّل الكمية الموجبة الأعلى؛ عند التعادل خذ أولها
        if qty > best_qty:
            best_qty = qty
            stock = q
    if stock is None and qtys:
        for q in qtys:
            if str(q.get('avg_cost') or q.get('cost') or '').strip():
                stock = q
                break
        if stock is None:
            stock = qtys[0]

    stock_unit = str((stock or {}).get('unit') or '').strip()
    quantity = str((stock or {}).get('quantity') or '').strip() if stock else ''
    avg_cost = ''
    if stock:
        # أبقِ دقة التكلفة كما من الـ API — التقريب لرقمين قبل الضرب
        # يحرّف إجمالي المخزون عن تقارير أونكس.
        avg_cost = str(stock.get('avg_cost') or stock.get('cost') or '').strip()

    price = ''
    price_unit = stock_unit
    if stock_unit:
        for row in prices or []:
            if str(row.get('unit') or '').strip() == stock_unit:
                price = str(row.get('price') or '').strip()
                break
    elif prices:
        # لا وحدة مخزون بعد — خذ أول سعر مع وحدته (بدون خلط وحدات)
        row0 = prices[0]
        price = str(row0.get('price') or '').strip()
        price_unit = str(row0.get('unit') or '').strip()

    return {
        'unit': stock_unit or price_unit,
        'price': price,
        'avg_cost': avg_cost,
        'quantity': quantity,
    }


# كاش داخل العملية لخريطة أسعار المخزن كاملة (طلب bulk واحد بدل طلب لكل صنف)
_bulk_price_lock = threading.Lock()
_bulk_price_cache: dict[str, tuple[float, dict[str, list[dict]]]] = {}
_BULK_PRICE_TTL = 600  # ثوانٍ — بعدها نحاول التحديث لكن نبقي القديمة كاحتياطي


def _bulk_price_map(warehouse: str) -> dict[str, list[dict]]:
    """
    خريطة أسعار المخزن كاملة من أوراكل.
    ترجع: رقم الصنف → قائمة {unit, price}. تُخزَّن مؤقتاً، وعند الفشل تُرجع آخر نسخة ناجحة.
    """
    from .oracle_catalog import fetch_price_map

    now = time.time()
    with _bulk_price_lock:
        cached = _bulk_price_cache.get(warehouse)
        if cached and now - cached[0] < _BULK_PRICE_TTL:
            return cached[1]

    price_map: dict[str, list[dict]] = {}
    try:
        price_map = fetch_price_map(warehouse)
    except Exception as exc:  # noqa: BLE001
        logger.warning('Bulk price fetch failed: %s', exc)

    with _bulk_price_lock:
        if price_map:
            _bulk_price_cache[warehouse] = (time.time(), price_map)
            return price_map
        cached = _bulk_price_cache.get(warehouse)
        return cached[1] if cached else {}


def enrich_group_browse(
    items: list[dict],
    warehouse: str | None = None,
    *,
    max_workers: int = 20,
    group_code: str | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """
    تصفح المجموعة:
    - كمية/تكلفة من أوراكل (IAS_ITM_WCODE) قراءة فقط
      وتشمل غير النشط إن كان له رصيد
    - أسعار العرض من IAS_ITEM_PRICE عند توفرها
    - يعيد فقط الأصناف بكمية > 0 مع عدّادات الاكتمال
    """
    empty_counts = {
        'catalog_count': 0,
        'stocked_count': 0,
        'zero_count': 0,
        'fetch_failed': 0,
        'complete': True,
        'qty_source': 'oracle',
    }
    if not items and not group_code:
        return [], empty_counts

    wh = warehouse or (settings.ERP_CONFIG.get('DEFAULT_WAREHOUSE') or '')
    by_code = {
        str(it.get('code') or '').strip(): dict(it)
        for it in items
        if str(it.get('code') or '').strip()
    }
    unique_codes = list(by_code.keys())

    def _load_prices() -> dict[str, list[dict]]:
        try:
            return _bulk_price_map(wh) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning('Price map skipped during browse enrich: %s', exc)
            return {}

    # ——— مسار أوراكل (قراءة فقط) ———
    try:
        from .oracle_stock import (
            OracleStockError,
            count_oracle_group_catalog,
            fetch_oracle_group_stock,
            use_oracle_stock,
        )
    except Exception:  # noqa: BLE001
        use_oracle_stock = lambda: False  # noqa: E731
        OracleStockError = Exception  # type: ignore
        fetch_oracle_group_stock = None  # type: ignore
        count_oracle_group_catalog = None  # type: ignore

    if use_oracle_stock() and group_code and fetch_oracle_group_stock:
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_prices = pool.submit(_load_prices)
                fut_stock = pool.submit(fetch_oracle_group_stock, wh, group_code)
                price_map = fut_prices.result()
                oracle_rows = fut_stock.result()
            catalog_count = len(unique_codes)
            zero_count = 0
            if count_oracle_group_catalog:
                try:
                    catalog_count, zero_count = count_oracle_group_catalog(wh, group_code)
                except Exception as exc:  # noqa: BLE001
                    logger.warning('Oracle catalog count skipped: %s', exc)
                    zero_count = max(0, catalog_count - len(oracle_rows))

            stocked: list[dict] = []
            for stock in oracle_rows:
                code = str(stock.get('code') or '').strip()
                base = by_code.get(code) or {
                    'code': code,
                    'name': stock.get('name') or '',
                    'barcode': '',
                    'unit': stock.get('unit') or '',
                    'g_code': group_code,
                }
                # الكمية بوحدة أكبر عبوة؛ التكلفة I_CWTAVG كما أونكس (وحدة المخزون)
                stock_unit = str(stock.get('unit') or '').strip()
                price = ''
                for prow in price_map.get(code) or []:
                    if stock_unit and str(prow.get('unit') or '').strip() == stock_unit:
                        price = str(prow.get('price') or '').strip()
                        break
                if not price:
                    # بدون ضرب تقديري — فقط سعر مسجّل لنفس وحدة العرض أو أول سعر متاح
                    for prow in price_map.get(code) or []:
                        price = str(prow.get('price') or '').strip()
                        if price:
                            break
                row = dict(base)
                if stock.get('name'):
                    row['name'] = stock['name']
                row['unit'] = stock_unit or row.get('unit') or ''
                row['pricing_unit'] = row['unit']
                row['price'] = price
                row['avg_cost'] = str(stock.get('avg_cost') or stock.get('cost') or '').strip()
                row['quantity'] = str(stock.get('quantity') or '').strip()
                # كمية المخزون الأصلية لحساب تكلفة الصنف = AVL_QTY × I_CWTAVG
                row['stock_qty'] = str(
                    stock.get('stock_qty')
                    if stock.get('stock_qty') not in (None, '')
                    else stock.get('quantity')
                    or ''
                ).strip()
                if stock.get('inactive'):
                    row['inactive'] = True
                stocked.append(row)

            return stocked, {
                'catalog_count': catalog_count,
                'stocked_count': len(stocked),
                'zero_count': zero_count,
                'fetch_failed': 0,
                'complete': True,
                'qty_source': 'oracle',
            }
        except OracleStockError as exc:
            logger.warning('Oracle group stock failed: %s', exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning('Oracle group stock error: %s', exc)

    return [], {**empty_counts, 'fetch_failed': len(unique_codes), 'complete': False, 'qty_source': 'oracle'}


def compute_inventory_stock_cost(items: list[dict]) -> dict:
    """
    إجمالي تكلفة المخزون = Σ (كمية المخزون × I_CWTAVG) كما في أونكس.
    إن وُجدت stock_qty (بعد تحويل العرض لأكبر عبوة) تُستخدم بدل الكمية المعروضة.
    """
    total = 0.0
    used = 0
    for item in items:
        qty = _to_float(item.get('stock_qty'))
        if qty is None:
            qty = _to_float(item.get('quantity'))
        cost = _to_float(item.get('avg_cost') or item.get('cost'))
        if qty is None or cost is None or qty < 0 or cost < 0:
            item['line_cost'] = ''
            continue
        line = qty * cost
        item['line_cost'] = _fmt_cost(line)
        total += line
        used += 1
    return {
        'total': _fmt_cost(total),
        'total_value': round(total, 2),
        'used_count': used,
        'skipped_count': max(0, len(items) - used),
    }


def build_browse_groups_excel(
    *,
    items: list[dict],
    warehouse: str,
    warehouse_name: str = '',
    group_code: str = '',
    group_name: str = '',
    stock_cost_total: str = '',
    catalog_count: int = 0,
    qty_source: str = '',
) -> Any:
    """تصدير مخزون المجموعة إلى Excel ملون (HTML/XML يفتح في Excel)."""
    import io
    import re
    from html import escape

    from django.http import HttpResponse

    def _num(value: Any) -> float | None:
        return _to_float(value)

    def _cell_num(value: Any, css: str = 'num', digits: int = 2) -> str:
        n = _num(value)
        if n is None:
            return f'<td class="{css}">—</td>'
        return f'<td class="{css}">{n:.{digits}f}</td>'

    wh_label = escape(
        (warehouse_name or '').strip() or str(warehouse or '').strip() or '—'
    )
    wh_code = escape(str(warehouse or '').strip() or '—')
    g_label = escape((group_name or '').strip() or str(group_code or '').strip() or '—')
    g_code = escape(str(group_code or '').strip() or '—')
    src = 'أوراكل' if str(qty_source or '').lower() == 'oracle' else 'API'
    sheet = (str(group_name or group_code or 'مخزون')[:28]).strip() or 'مخزون'
    # اسم ورقة Excel بدون محارف ممنوعة
    sheet = re.sub(r'[\\/*?:\[\]]', '-', sheet)

    buf = io.StringIO()
    buf.write('\ufeff')
    buf.write(
        '<html xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:x="urn:schemas-microsoft-com:office:excel" '
        'xmlns="http://www.w3.org/TR/REC-html40">'
        '<head><meta charset="utf-8">'
        '<!--[if gte mso 9]><xml><x:ExcelWorkbook><x:ExcelWorksheets>'
        f'<x:ExcelWorksheet><x:Name>{escape(sheet)}</x:Name>'
        '<x:WorksheetOptions><x:DisplayRightToLeft/></x:WorksheetOptions>'
        '</x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->'
        '<style>'
        'table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:11px;}'
        'th,td{border:1px solid #94a3b8;padding:5px 8px;white-space:nowrap;vertical-align:middle;}'
        'th{background:#d9e2f3;color:#1a2b33;font-weight:700;text-align:center;}'
        'th.unit{background:#0c4a6e;}'
        'th.price{background:#92400e;}'
        'th.cost{background:#334155;}'
        'th.qty{background:#166534;}'
        'th.line{background:#1d4ed8;}'
        'td.num{mso-number-format:\'\\#\\,\\#\\#0\\.00\';text-align:left;}'
        'td.qty{mso-number-format:\'\\#\\,\\#\\#0\\.000\';background:#eafaf0;color:#166534;font-weight:700;}'
        'td.price{background:#fef7e8;color:#92400e;font-weight:700;}'
        'td.cost{background:#f1f5f9;color:#334155;}'
        'td.line{background:#dbeafe;color:#1e3a8a;font-weight:800;}'
        'td.unit{background:#e0f2fe;color:#0c4a6e;font-weight:700;text-align:center;}'
        'td.code{font-family:Consolas,monospace;color:#1e3a5f;font-weight:700;}'
        'td.txt{mso-number-format:\'\\@\';}'
        'td.idx{mso-number-format:\'\\#\\,\\#\\#0\';color:#64748b;text-align:center;}'
        'tr.even td{background:#f7f7f7;}'
        'tr.even td.qty{background:#eafaf0;}'
        'tr.even td.price{background:#fef7e8;}'
        'tr.even td.cost{background:#f1f5f9;}'
        'tr.even td.line{background:#dbeafe;}'
        'tr.even td.unit{background:#e0f2fe;}'
        'tr.foot td{background:#d9e2f3;color:#1a2b33;font-weight:800;}'
        'tr.foot td.line{background:#93c5fd;}'
        'tr.foot td.qty{background:#86efac;}'
        'caption{font-size:14px;font-weight:800;margin-bottom:8px;text-align:right;color:#0f172a;}'
        '.sub{font-size:10px;color:#475569;font-weight:400;}'
        '</style></head><body dir="rtl">'
    )
    buf.write(
        f'<caption>مخزون المجموعة — {g_label}'
        f'<br><span class="sub">'
        f'المخزن: {wh_label} ({wh_code}) · المجموعة: {g_code}'
        f' · أصناف بكمية: {len(items)}'
        f' · كتالوج: {int(catalog_count or 0)}'
        f' · المصدر: {src}'
        f' · الوحدة = أكبر عبوة عند توفرها'
        f'</span></caption>'
    )
    buf.write(
        '<table><thead><tr>'
        '<th>#</th><th>رقم الصنف</th><th>الاسم</th>'
        '<th class="unit">الوحدة</th>'
        '<th class="price">السعر</th>'
        '<th class="cost">التكلفة</th>'
        '<th class="qty">الكمية</th>'
        '<th class="line">تكلفة الصنف</th>'
        '</tr></thead><tbody>'
    )

    qty_sum = 0.0
    line_sum = 0.0
    qty_used = 0
    line_used = 0
    for i, item in enumerate(items or [], 1):
        even = ' even' if i % 2 == 0 else ''
        unit = str(item.get('pricing_unit') or item.get('unit') or '').strip() or '—'
        qn = _num(item.get('quantity'))
        ln = _num(item.get('line_cost'))
        if qn is not None:
            qty_sum += qn
            qty_used += 1
        if ln is not None:
            line_sum += ln
            line_used += 1
        buf.write(f'<tr class="{even.strip()}">')
        buf.write(f'<td class="idx">{i}</td>')
        buf.write(f'<td class="code txt">{escape(str(item.get("code") or ""))}</td>')
        buf.write(f'<td>{escape(str(item.get("name") or "—"))}</td>')
        buf.write(f'<td class="unit">{escape(unit)}</td>')
        buf.write(_cell_num(item.get('price'), 'num price', 2))
        buf.write(_cell_num(item.get('avg_cost') or item.get('cost'), 'num cost', 4))
        if qn is None:
            buf.write('<td class="qty">—</td>')
        else:
            buf.write(f'<td class="num qty">{qn:.3f}</td>')
        if ln is None:
            buf.write('<td class="line">—</td>')
        else:
            buf.write(f'<td class="num line">{ln:.2f}</td>')
        buf.write('</tr>')

    total_display = stock_cost_total or (f'{line_sum:.2f}' if line_used else '—')
    total_n = _num(total_display)
    buf.write('<tr class="foot">')
    buf.write(f'<td colspan="3">الإجمالي — {len(items)} صنف</td>')
    buf.write('<td class="unit">—</td><td class="price">—</td><td class="cost">—</td>')
    if qty_used:
        buf.write(f'<td class="num qty">{qty_sum:.3f}</td>')
    else:
        buf.write('<td class="qty">—</td>')
    if total_n is not None:
        buf.write(f'<td class="num line">{total_n:.2f}</td>')
    else:
        buf.write(f'<td class="line">{escape(str(total_display))}</td>')
    buf.write('</tr></tbody></table></body></html>')

    safe_g = re.sub(r'[^\w\-]+', '_', str(group_code or 'group'), flags=re.UNICODE)[:40]
    safe_w = re.sub(r'[^\w\-]+', '_', str(warehouse or 'wh'), flags=re.UNICODE)[:20]
    filename = f'browse_group_{safe_g}_wh{safe_w}.xls'
    resp = HttpResponse(
        buf.getvalue().encode('utf-8'),
        content_type='application/vnd.ms-excel; charset=utf-8',
    )
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


def _rows_to_item_dicts(rows) -> list[dict]:
    from .models import ItemGroup

    rows = list(rows)
    g_codes = {r.g_code for r in rows if r.g_code}
    names = {
        g.g_code: g.g_name
        for g in ItemGroup.objects.filter(g_code__in=g_codes)
    }
    return [
        {
            'barcode': row.barcode or '',
            'code': row.item_code,
            'name': row.name,
            'unit': row.unit,
            'pack_size': row.pack_size,
            'g_code': row.g_code,
            'g_name': names.get(row.g_code, ''),
            'price': '',
            'quantity': '',
        }
        for row in rows
    ]


def lookup_by_barcode(barcode: str) -> list[dict]:
    """البحث المحلي: باركود → رقم الصنف + المجموعة."""
    from .models import ItemBarcode

    raw = str(barcode or '').strip()
    cleaned = _normalize_text(raw)
    candidates = {v for v in (raw, cleaned) if v}
    rows = (
        ItemBarcode.objects.filter(barcode__in=candidates)
        .exclude(barcode='')
        .order_by('unit')
    )
    return _rows_to_item_dicts(rows)


def lookup_by_item_code(item_code: str) -> list[dict]:
    """
    كل وحدات الصنف من الفهرس المحلي مع الباركود والعبوة والمجموعة.
    يفضّل الصفوف التي فيها باركود عند تكرار نفس الوحدة.
    """
    from .models import ItemBarcode

    raw = str(item_code or '').strip()
    cleaned = _normalize_text(raw)
    candidates = {v for v in (raw, cleaned) if v}
    rows = ItemBarcode.objects.filter(item_code__in=candidates).order_by('unit', '-barcode')
    best: dict[str, object] = {}
    ordered_units: list[str] = []
    for row in rows:
        unit = (row.unit or '').strip() or '__'
        if unit not in best:
            best[unit] = row
            ordered_units.append(unit)
            continue
        # استبدل إذا الصف الحالي فيه باركود والسابق بدون
        prev = best[unit]
        if not (prev.barcode or '').strip() and (row.barcode or '').strip():
            best[unit] = row

    return _rows_to_item_dicts(best[u] for u in ordered_units)


def lookup_by_name(name_query: str, limit: int = 50) -> list[dict]:
    """
    بحث جزئي باسم الصنف من الفهرس المحلي.
    يرجع صنفاً واحداً لكل رقم صنف (مع تفضيل صف فيه باركود).
    """
    from .models import ItemBarcode

    q = _normalize_text(name_query)
    if len(q) < 2:
        return []

    rows = (
        ItemBarcode.objects.filter(name__icontains=q)
        .exclude(name='')
        .order_by('name', 'item_code', '-barcode')[: max(limit * 8, 200)]
    )
    best: dict[str, object] = {}
    ordered: list[str] = []
    for row in rows:
        code = (row.item_code or '').strip()
        if not code:
            continue
        if code not in best:
            best[code] = row
            ordered.append(code)
            if len(ordered) >= limit:
                break
            continue
        prev = best[code]
        if not (prev.barcode or '').strip() and (row.barcode or '').strip():
            best[code] = row

    return _rows_to_item_dicts(best[c] for c in ordered)


def lookup_by_name_in_codes(
    name_query: str,
    allowed_codes: set[str] | list[str],
    limit: int = 50,
) -> list[dict]:
    """بحث بالاسم ضمن مجموعة أصناف محددة (مثل أصناف مورد)."""
    from .models import ItemBarcode

    q = _normalize_text(name_query)
    codes = {str(c or "").strip() for c in (allowed_codes or []) if str(c or "").strip()}
    if len(q) < 2 or not codes:
        return []

    lim = max(1, min(int(limit or 50), 200))
    code_list = list(codes)
    best: dict[str, object] = {}
    ordered: list[str] = []
    chunk = 800
    for i in range(0, len(code_list), chunk):
        part = code_list[i : i + chunk]
        rows = (
            ItemBarcode.objects.filter(item_code__in=part, name__icontains=q)
            .exclude(name="")
            .order_by("name", "item_code", "-barcode")[: max(lim * 8, 200)]
        )
        for row in rows:
            code = (row.item_code or "").strip()
            if not code or code not in codes:
                continue
            if code not in best:
                best[code] = row
                ordered.append(code)
                continue
            prev = best[code]
            if not (prev.barcode or "").strip() and (row.barcode or "").strip():
                best[code] = row
        if len(ordered) >= lim:
            break

    ordered = ordered[:lim]
    return _rows_to_item_dicts(best[c] for c in ordered)
