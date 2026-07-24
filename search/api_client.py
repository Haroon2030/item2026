"""
عميل API نظام أونكس: الأسعار + مزامنة الأصناف/الباركود.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)


class ApiClientError(Exception):
    """خطأ عام عند فشل الاتصال أو قراءة الاستجابة."""


def _base_url() -> str:
    """يرجع رابط الأساس بعد التحقق من المضيف المسموح (حماية من SSRF)."""
    from urllib.parse import urlparse

    cfg = settings.EXTERNAL_API
    base_url = (cfg.get('BASE_URL') or '').rstrip('/')
    if not base_url:
        raise ApiClientError('لم يتم ضبط رابط الـ API في EXTERNAL_API.')

    parsed = urlparse(base_url)
    if parsed.scheme not in {'http', 'https'}:
        raise ApiClientError('بروتوكول رابط الـ API غير مسموح.')
    host = (parsed.hostname or '').lower()
    allowed = {h.lower() for h in (cfg.get('ALLOWED_HOSTS') or []) if h}
    if allowed and host not in allowed:
        raise ApiClientError('مضيف الـ API غير مدرج في القائمة المسموحة.')
    return base_url


def _safe_url(path: str) -> str:
    """يبني رابطًا داخل نفس المضيف المسموح فقط."""
    base = _base_url()
    if not path.startswith('/'):
        path = '/' + path
    # منع الخروج عن المسار الأساسي عبر ../ أو روابط مطلقة
    if '://' in path or '..' in path:
        raise ApiClientError('مسار API غير صالح.')
    return f'{base}{path}'


def _headers() -> dict[str, str]:
    cfg = settings.EXTERNAL_API
    headers = {'Accept': 'application/json'}
    api_key = cfg.get('API_KEY') or ''
    if api_key:
        header_name = cfg.get('API_KEY_HEADER') or 'Authorization'
        prefix = (cfg.get('API_KEY_PREFIX') or '').strip()
        headers[header_name] = f'{prefix} {api_key}'.strip() if prefix else api_key
    return headers


def _request_get(url: str, params: dict, timeout: int | None = None) -> requests.Response:
    """GET مع إعادة محاولة عند البطء أو انقطاع الشبكة."""
    cfg = settings.EXTERNAL_API
    timeout = timeout if timeout is not None else cfg.get('TIMEOUT', 90)
    retries = int(cfg.get('RETRIES', 2))
    last_exc: Exception | None = None

    for attempt in range(retries + 1):
        try:
            response = requests.get(url, params=params, headers=_headers(), timeout=timeout)
            response.raise_for_status()
            return response
        except requests.Timeout as exc:
            last_exc = exc
            logger.warning('API timeout attempt %s/%s url=%s', attempt + 1, retries + 1, url)
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning('API error attempt %s/%s url=%s err=%s', attempt + 1, retries + 1, url, exc)

        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))

    if isinstance(last_exc, requests.Timeout):
        raise ApiClientError(
            'انتهت مهلة الاتصال بالنظام. الشبكة بطيئة أو الخدمة مشغولة — أعد المحاولة.'
        ) from last_exc
    raise ApiClientError(f'فشل الاتصال بالنظام: {last_exc}') from last_exc


def _dig(data: Any, path: str) -> Any:
    if not path:
        return data
    current = data
    for key in path.split('.'):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _normalize_item(raw: dict, field_map: dict) -> dict:
    pack = raw.get(field_map.get('pack_size', 'P_SIZE'), '')
    if pack in (None, '', 0, '0'):
        pack = raw.get('p_size', '') or ''
    return {
        'code': raw.get(field_map.get('code', 'I_CODE'), '') or '',
        'name': raw.get(field_map.get('name', 'I_NAME'), '') or '',
        'barcode': raw.get(field_map.get('barcode', 'BARCODE'), '') or '',
        'price': raw.get(field_map.get('price', 'I_PRICE'), '') or '',
        'unit': raw.get(field_map.get('unit', 'ITM_UNT'), '') or '',
        'quantity': raw.get(field_map.get('quantity', 'AVL_QTY'), '') or '',
        'pack_size': '' if pack in (None, '', 0, '0') else str(pack).strip(),
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


def search_prices_by_code(item_code: str, price_w_code: str | None = None) -> list[dict]:
    """جلب أسعار صنف عبر GetAllPrice باستخدام رقم الصنف والمخزن."""
    cfg = settings.EXTERNAL_API
    url = _safe_url(cfg.get('SEARCH_PATH', '/GetAllPrice'))
    query_param = cfg.get('QUERY_PARAM') or 'i_code'

    params = dict(cfg.get('EXTRA_PARAMS') or {})
    params[query_param] = item_code
    if price_w_code:
        params['price_w_code'] = price_w_code

    response = _request_get(url, params)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ApiClientError('الاستجابة ليست JSON صالحًا.') from exc

    results = _dig(payload, cfg.get('RESULTS_PATH') or '')
    if results is None:
        raise ApiClientError('تعذّر قراءة النتائج من الاستجابة.')
    if isinstance(results, dict):
        results = [results]
    if not isinstance(results, list):
        raise ApiClientError('شكل نتائج الـ API غير متوقع (ليست قائمة).')

    if len(results) == 1 and isinstance(results[0], dict) and results[0].get('errorDisc'):
        raise ApiClientError(f"خطأ من النظام: {results[0].get('errorDisc')}")

    field_map = cfg.get('FIELD_MAP') or {}
    return [
        _normalize_item(item, field_map)
        for item in results
        if _is_valid_item(item)
    ]


def fetch_qty_by_code(item_code: str, w_code: str | None = None) -> list[dict]:
    """جلب الكمية المتاحة عبر GetItemQtyCost."""
    cfg = settings.EXTERNAL_API
    url = _safe_url('/GetItemQtyCost')
    params = {
        'year': (cfg.get('EXTRA_PARAMS') or {}).get('year', 2026),
        'active': (cfg.get('EXTRA_PARAMS') or {}).get('active', 1),
        'i_code': item_code,
        'w_code': w_code or cfg.get('DEFAULT_WAREHOUSE') or '60',
    }

    response = _request_get(url, params, timeout=cfg.get('QTY_TIMEOUT', 45))

    try:
        payload = response.json()
    except ValueError as exc:
        raise ApiClientError('استجابة الكمية ليست JSON صالحًا.') from exc

    if not isinstance(payload, list):
        return []

    rows = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        if raw.get('errorId') not in (None, '', 0, '0'):
            continue
        if raw.get('errorDisc'):
            continue
        code = str(
            raw.get('Item_code')
            or raw.get('I_CODE')
            or raw.get('item_code')
            or ''
        ).strip()
        if not code:
            continue
        qty_val = raw.get('Avl_Qty')
        if qty_val is None:
            qty_val = raw.get('AVL_QTY')
        if qty_val is None:
            qty_val = raw.get('avl_qty')
        unit = str(
            raw.get('itm_unt')
            or raw.get('ITM_UNT')
            or raw.get('Itm_Unt')
            or ''
        ).strip()
        rows.append(
            {
                'code': code,
                'name': str(raw.get('Item_ar_name') or raw.get('I_NAME') or '').strip(),
                'unit': unit,
                'quantity': str(qty_val).strip() if qty_val is not None else '',
                'cost': str(raw.get('I_cost') if raw.get('I_cost') is not None else '').strip(),
                'barcode': str(raw.get('Barcode') or raw.get('BARCODE') or '').strip(),
            }
        )
    return rows


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

        quantity = ''
        if unit in qty_by_unit and _to_float(qty_row.get('quantity')) is not None:
            # وحدة الـ API كما هي — بدون إعادة حساب
            quantity = _fmt_qty(_to_float(qty_row.get('quantity')))
        elif source_qty is not None and source_unit:
            converted = _convert_qty(source_qty, source_unit, unit, unit_meta)
            if converted is not None:
                quantity = _fmt_qty(converted)

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

    with ThreadPoolExecutor(max_workers=2) as pool:
        price_future = pool.submit(search_prices_by_code, item_code, warehouse)
        qty_future = pool.submit(fetch_qty_by_code, item_code, warehouse)

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

    # إعادة محاولة الكمية تسلسلياً إن فشلت أو رجعت فارغة
    if not qtys:
        try:
            qtys = fetch_qty_by_code(item_code, warehouse)
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

    unit_meta = get_unit_meta(item_code)
    merged = merge_prices_with_qty(prices, qtys, unit_meta=unit_meta)
    # #region agent log
    try:
        from .debug_agent import agent_log

        agent_log(
            'C',
            'api_client.py:search_item_details',
            'merge_summary',
            {
                'item_code': item_code,
                'warehouse': warehouse,
                'prices_n': len(prices),
                'qtys_n': len(qtys),
                'qty_units': [q.get('unit') for q in qtys],
                'qty_vals': [q.get('quantity') for q in qtys],
                'unit_meta_n': len(unit_meta),
                'unit_meta_units': list(unit_meta.keys()),
                'merged_pack': [m.get('pack_size') for m in merged],
                'merged_qty': [m.get('quantity') for m in merged],
                'price_error': str(price_error)[:160] if price_error else '',
                'qty_error': str(qty_error)[:160] if qty_error else '',
            },
        )
    except Exception:
        pass
    # #endregion
    if qty_error and not any(str(r.get('quantity') or '').strip() for r in merged):
        for row in merged:
            row['_qty_warning'] = str(qty_error)
    return merged


def fetch_all_items() -> list[dict]:
    """جلب كل الأصناف من GetAllItems (للمزامنة المحلية)."""
    cfg = settings.EXTERNAL_API
    url = _safe_url('/GetAllItems')
    timeout = cfg.get('ITEMS_TIMEOUT', 180)
    params = dict(cfg.get('ITEMS_PARAMS') or {'year': 2026, 'active': 1})

    response = _request_get(url, params, timeout=timeout)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ApiClientError('استجابة GetAllItems ليست JSON صالحًا.') from exc

    if not isinstance(payload, list):
        raise ApiClientError('شكل GetAllItems غير متوقع.')
    return payload


def fetch_all_groups() -> list[dict]:
    """جلب مجموعات الأصناف من GetAllGroupDet."""
    cfg = settings.EXTERNAL_API
    url = _safe_url('/GetAllGroupDet')
    timeout = cfg.get('TIMEOUT', 60)
    params = {
        'year': (cfg.get('EXTRA_PARAMS') or {}).get('year', 2026),
        'active': (cfg.get('EXTRA_PARAMS') or {}).get('active', 1),
    }
    response = _request_get(url, params, timeout=timeout)
    try:
        payload = response.json()
    except ValueError as exc:
        raise ApiClientError('استجابة GetAllGroupDet ليست JSON صالحًا.') from exc
    if not isinstance(payload, list):
        raise ApiClientError('شكل GetAllGroupDet غير متوقع.')
    return payload


def sync_barcode_index() -> int:
    """
    مزامنة الباركود/العبوات/رمز المجموعة من GetAllItems
    وأسماء المجموعات من GetAllGroupDet.
    """
    from .models import ItemBarcode, ItemGroup

    rows = fetch_all_items()
    mapped: list[ItemBarcode] = []
    seen: set[tuple[str, str, str]] = set()

    for row in rows:
        if not isinstance(row, dict):
            continue
        barcode = str(row.get('Barcode') or '').strip()
        item_code = str(row.get('Item_code') or '').strip()
        unit = str(row.get('itm_unt') or '').strip()
        if not item_code or not unit:
            continue
        key = (barcode, item_code, unit)
        if key in seen:
            continue
        seen.add(key)
        # Item_category في GetAllItems = G_CODE
        g_raw = (
            row.get('Item_category')
            if row.get('Item_category') is not None
            else row.get('G_CODE')
            if row.get('G_CODE') is not None
            else row.get('g_code')
        )
        g_code = '' if g_raw is None else str(g_raw).strip()

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
                name=str(row.get('Item_ar_name') or '').strip(),
                unit=unit,
                pack_size=pack_size,
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

    rows = (
        ItemBarcode.objects.filter(barcode=barcode)
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

    rows = ItemBarcode.objects.filter(item_code=item_code).order_by('unit', '-barcode')
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


# توافق مع الاستدعاءات القديمة
def search_items(query: str) -> list[dict]:
    return search_prices_by_code(query)
