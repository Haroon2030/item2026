"""
عميل API نظام أونكس: الأسعار + مزامنة الأصناف/الباركود.
"""

from __future__ import annotations

import logging
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)
_thread_local = threading.local()


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


def _request_get(url: str, params: dict, timeout: int | None = None, retries: int | None = None) -> requests.Response:
    """GET مع إعادة محاولة عند البطء أو انقطاع الشبكة."""
    cfg = settings.EXTERNAL_API
    timeout = timeout if timeout is not None else cfg.get('TIMEOUT', 90)
    retries = int(cfg.get('RETRIES', 2) if retries is None else retries)
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


def search_prices_by_code(
    item_code: str,
    price_w_code: str | None = None,
    *,
    timeout: int | None = None,
    retries: int | None = None,
    fast: bool = False,
) -> list[dict]:
    """جلب أسعار صنف عبر GetAllPrice باستخدام رقم الصنف والمخزن."""
    cfg = settings.EXTERNAL_API
    url = _safe_url(cfg.get('SEARCH_PATH', '/GetAllPrice'))
    query_param = cfg.get('QUERY_PARAM') or 'i_code'

    params = dict(cfg.get('EXTRA_PARAMS') or {})
    params[query_param] = item_code
    if price_w_code:
        params['price_w_code'] = price_w_code

    req_timeout = timeout if timeout is not None else cfg.get('TIMEOUT', 60)
    if fast:
        session = _stock_session()
        response = session.get(url, params=params, headers=_headers(), timeout=req_timeout)
        response.raise_for_status()
    else:
        response = _request_get(url, params, timeout=req_timeout, retries=retries)

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


def fetch_qty_by_code(
    item_code: str,
    w_code: str | None = None,
    timeout: int | None = None,
    *,
    fast: bool = False,
) -> list[dict]:
    """جلب الكمية المتاحة عبر GetItemQtyCost."""
    cfg = settings.EXTERNAL_API
    url = _safe_url('/GetItemQtyCost')
    params = {
        'year': (cfg.get('EXTRA_PARAMS') or {}).get('year', 2026),
        'active': (cfg.get('EXTRA_PARAMS') or {}).get('active', 1),
        'i_code': item_code,
        'w_code': w_code or cfg.get('DEFAULT_WAREHOUSE') or '60',
    }

    req_timeout = timeout if timeout is not None else cfg.get('QTY_TIMEOUT', 45)
    if fast:
        session = _stock_session()
        response = session.get(url, params=params, headers=_headers(), timeout=req_timeout)
        response.raise_for_status()
    else:
        response = _request_get(url, params, timeout=req_timeout)

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
        avg_cost = raw.get('I_CWTAVG')
        if avg_cost in (None, ''):
            avg_cost = raw.get('i_cwtavg')
        if avg_cost in (None, ''):
            avg_cost = raw.get('I_cost')
        if avg_cost in (None, ''):
            avg_cost = raw.get('I_COST')
        rows.append(
            {
                'code': code,
                'name': str(raw.get('Item_ar_name') or raw.get('I_NAME') or '').strip(),
                'unit': unit,
                'quantity': str(qty_val).strip() if qty_val is not None else '',
                'avg_cost': str(avg_cost).strip() if avg_cost not in (None, '') else '',
                'cost': str(avg_cost).strip() if avg_cost not in (None, '') else '',
                'barcode': str(raw.get('Barcode') or raw.get('BARCODE') or '').strip(),
            }
        )
    return rows


def _stock_session() -> requests.Session:
    session = getattr(_thread_local, 'stock_session', None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=48,
            pool_maxsize=48,
            max_retries=0,
        )
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        _thread_local.stock_session = session
    return session


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

    # إن كان البحث باركود: GetAllPrice يعيد I_CODE الحقيقي بينما GetItemQtyCost يحتاج رقم الصنف
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

    cfg = settings.EXTERNAL_API
    codes = [
        str(c).strip()
        for c in (warehouses or cfg.get('COMPARE_WAREHOUSES') or [])
        if str(c).strip()
    ]
    if not codes:
        codes = ['1201', '1', '30', '1901', '2001', '1801', '60', '701']
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


def fetch_all_items(*, g_code: str | None = None, subg_code: str | None = None) -> list[dict]:
    """جلب الأصناف من GetAllItems، مع تصفية اختيارية بالمجموعة/الفرعية."""
    cfg = settings.EXTERNAL_API
    url = _safe_url('/GetAllItems')
    timeout = cfg.get('ITEMS_TIMEOUT', 180)
    params = dict(cfg.get('ITEMS_PARAMS') or {'year': 2026, 'active': 1})
    if g_code:
        params['g_code'] = str(g_code).strip()
    if subg_code:
        params['subg_code'] = str(subg_code).strip()

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
    أصناف المجموعة من الفهرس المحلي السريع (مزامنة GetAllItems).
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
    - الكمية والتكلفة دائماً من نفس صف GetItemQtyCost.
    - عند تعدّد الوحدات: اختر أعلى كمية موجبة (لا أول صف صفري).
    - السعر من GetAllPrice لنفس الوحدة إن وُجد (للعرض فقط).
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
    خريطة أسعار المخزن كاملة بطلب GetAllPrice واحد (بدون i_code).
    ترجع: رقم الصنف → قائمة {unit, price}.

    النظام الخارجي متقلب (يرجع أحياناً صفر سجلات أو يرفض الاتصال)،
    لذا لا نخزّن نتيجة فارغة أبداً، ونرجع آخر نسخة ناجحة عند الفشل.
    """
    now = time.time()
    with _bulk_price_lock:
        cached = _bulk_price_cache.get(warehouse)
        if cached and now - cached[0] < _BULK_PRICE_TTL:
            return cached[1]

    cfg = settings.EXTERNAL_API
    url = _safe_url(cfg.get('SEARCH_PATH', '/GetAllPrice'))
    params = dict(cfg.get('EXTRA_PARAMS') or {})
    params['price_w_code'] = warehouse

    price_map: dict[str, list[dict]] = {}
    try:
        response = _request_get(url, params, timeout=120, retries=1)
        payload = response.json()
        if isinstance(payload, list):
            for raw in payload:
                if not isinstance(raw, dict):
                    continue
                code = str(raw.get('I_CODE') or '').strip()
                if not code:
                    continue
                price_map.setdefault(code, []).append(
                    {
                        'unit': str(raw.get('ITM_UNT') or '').strip(),
                        'price': str(raw.get('I_PRICE') or '').strip(),
                    }
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning('Bulk price fetch failed: %s', exc)

    with _bulk_price_lock:
        if price_map:
            _bulk_price_cache[warehouse] = (time.time(), price_map)
            return price_map
        # فشل أو استجابة فارغة: أرجع آخر نسخة ناجحة إن وُجدت (حتى لو قديمة)
        cached = _bulk_price_cache.get(warehouse)
        return cached[1] if cached else {}


_warehouse_stock_lock = threading.Lock()
# warehouse -> (monotonic_ts, stock_map, source)
_warehouse_stock_cache: dict[str, tuple[float, dict[str, dict], str]] = {}
_WAREHOUSE_STOCK_TTL = 900.0
# بعد فشل الجلب الجماعي: لا نعيد المحاولة الثقيلة فوراً
_bulk_stock_fail_until: dict[str, float] = {}
_BULK_STOCK_FAIL_TTL = 1800.0
# قاطع دائرة: عند انقطاع DNS/الاتصال لا نعيد آلاف الطلبات الفاشلة
_stock_circuit_lock = threading.Lock()
_stock_circuit_open_until = 0.0
_stock_circuit_fails = 0
_STOCK_CIRCUIT_THRESHOLD = 8
_STOCK_CIRCUIT_COOLDOWN = 90.0


def _stock_circuit_is_open() -> bool:
    return time.monotonic() < _stock_circuit_open_until


def _stock_circuit_reset() -> None:
    global _stock_circuit_fails, _stock_circuit_open_until
    with _stock_circuit_lock:
        _stock_circuit_fails = 0
        _stock_circuit_open_until = 0.0


def _stock_circuit_note_success() -> None:
    global _stock_circuit_fails
    with _stock_circuit_lock:
        _stock_circuit_fails = 0


def _stock_circuit_note_failure(exc: BaseException | None = None) -> None:
    """يفتح القاطع عند أعطال شبكة قاسية متكررة."""
    global _stock_circuit_fails, _stock_circuit_open_until
    msg = str(exc or '').lower()
    hard = any(
        token in msg
        for token in (
            'nameresolutionerror',
            'getaddrinfo failed',
            'failed to resolve',
            'connection refused',
            'connection aborted',
            'connection reset',
            'max retries exceeded',
            'timed out',
            'timeout',
        )
    )
    if not hard and exc is not None:
        return
    with _stock_circuit_lock:
        _stock_circuit_fails += 1
        if _stock_circuit_fails >= _STOCK_CIRCUIT_THRESHOLD:
            _stock_circuit_open_until = time.monotonic() + _STOCK_CIRCUIT_COOLDOWN
            _stock_circuit_fails = 0
            logger.warning(
                'Stock circuit OPEN for %.0fs — pausing qty fetches',
                _STOCK_CIRCUIT_COOLDOWN,
            )


def _stock_row_from_item_payload(raw: dict) -> dict | None:
    """صف رصيد موحّد من استجابة Item (GetItemQtyCost / GetItemQtyPrice)."""
    if not isinstance(raw, dict):
        return None
    if raw.get('errorId') not in (None, '', 0, '0') and raw.get('errorDisc'):
        return None
    if raw.get('errorDisc') and not (
        raw.get('Item_code') or raw.get('I_CODE') or raw.get('item_code')
    ):
        return None
    code = str(
        raw.get('Item_code') or raw.get('I_CODE') or raw.get('item_code') or raw.get('i_code') or ''
    ).strip()
    if not code:
        return None
    qty_val = raw.get('Avl_Qty')
    if qty_val is None:
        qty_val = raw.get('AVL_QTY')
    if qty_val is None:
        qty_val = raw.get('avl_qty')
    if qty_val is None:
        qty_val = raw.get('qty')
    unit = str(
        raw.get('itm_unt') or raw.get('ITM_UNT') or raw.get('Itm_Unt') or ''
    ).strip()
    avg_cost = raw.get('I_CWTAVG')
    if avg_cost in (None, ''):
        avg_cost = raw.get('i_cwtavg')
    if avg_cost in (None, ''):
        avg_cost = raw.get('I_cost')
    if avg_cost in (None, ''):
        avg_cost = raw.get('I_COST')
    return {
        'code': code,
        'name': str(raw.get('Item_ar_name') or raw.get('I_NAME') or raw.get('i_a_name') or '').strip(),
        'unit': unit,
        'quantity': str(qty_val).strip() if qty_val is not None else '',
        'avg_cost': str(avg_cost).strip() if avg_cost not in (None, '') else '',
        'cost': str(avg_cost).strip() if avg_cost not in (None, '') else '',
        'barcode': str(raw.get('Barcode') or raw.get('BARCODE') or '').strip(),
    }


def _try_bulk_warehouse_stock(warehouse: str) -> tuple[dict[str, dict], str]:
    """
    محاولة الجلب الجماعي الرسمي لمخزن واحد.
    على نشر أونكس الحالي: GetItemQtyPrice / GetAllQty / getallqtybywarehouse
    غالباً ترجع فارغة أو 400 — نحتفظ بالمحاولة لتفعيلها عند إصلاح الخدمة.
    """
    cfg = settings.EXTERNAL_API
    year = (cfg.get('EXTRA_PARAMS') or {}).get('year', 2026)
    active = (cfg.get('EXTRA_PARAMS') or {}).get('active', 1)
    lev = (cfg.get('EXTRA_PARAMS') or {}).get('lev_no', 1)
    wh = str(warehouse or '').strip()
    out: dict[str, dict] = {}

    # 1) GetItemQtyPrice — عقد جماعي (warehouse + price_level) يعيد ArrayOfItem مع Avl_Qty/I_cost
    try:
        url = _safe_url('/GetItemQtyPrice')
        params = {
            'year': year,
            'active': active,
            'warehouse': int(wh) if wh.isdigit() else wh,
            'price_level': int(lev) if str(lev).isdigit() else lev,
        }
        response = _request_get(url, params, timeout=120, retries=0)
        payload = response.json()
        if isinstance(payload, list) and payload:
            # تجاهل صف خطأ أوراكل الوحيد
            if len(payload) == 1 and isinstance(payload[0], dict) and payload[0].get('errorDisc'):
                logger.info('GetItemQtyPrice unavailable: %s', payload[0].get('errorDisc'))
            else:
                for raw in payload:
                    row = _stock_row_from_item_payload(raw)
                    if not row:
                        continue
                    # فضّل أول صف فيه كمية رقمية
                    prev = out.get(row['code'])
                    if prev is None or (
                        _to_float(prev.get('quantity')) is None
                        and _to_float(row.get('quantity')) is not None
                    ):
                        out[row['code']] = row
                if out:
                    return out, 'GetItemQtyPrice'
    except Exception as exc:  # noqa: BLE001
        logger.info('GetItemQtyPrice bulk skipped: %s', exc)

    # 2) GetAllQty / getallqtybywarehouse — كمية فقط (بدون تكلفة) عبر rep_code
    # غير كافية وحدها لإجمالي التكلفة؛ نتخطاها إن لم تُرجع بيانات.
    for path in ('/GetAllQty', '/getallqtybywarehouse'):
        try:
            url = _safe_url(path)
            params = {'year': year, 'active': active, 'rep_code': wh}
            response = requests.get(url, params=params, headers=_headers(), timeout=60)
            if response.status_code != 200:
                continue
            payload = response.json()
            if not isinstance(payload, list) or not payload:
                continue
            qty_only = 0
            for raw in payload:
                if not isinstance(raw, dict):
                    continue
                code = str(raw.get('i_code') or raw.get('I_CODE') or '').strip()
                if not code:
                    continue
                if raw.get('w_code') not in (None, '', wh, int(wh) if wh.isdigit() else wh):
                    # إن وُجد w_code صفّي، اقتصر على المخزن المطلوب
                    if str(raw.get('w_code')) != wh:
                        continue
                out[code] = {
                    'code': code,
                    'name': '',
                    'unit': str(raw.get('itm_unt') or '').strip(),
                    'quantity': str(raw.get('qty') or '').strip(),
                    'avg_cost': '',
                    'cost': '',
                    'barcode': '',
                }
                qty_only += 1
            if qty_only:
                logger.info('%s returned %s qty rows (no cost) — not used alone for valuation', path, qty_only)
                out.clear()
        except Exception as exc:  # noqa: BLE001
            logger.info('%s bulk skipped: %s', path, exc)

    return {}, ''


def fetch_warehouse_stock(
    warehouse: str | None = None,
    item_codes: list[str] | None = None,
    *,
    max_workers: int = 20,
) -> dict[str, dict]:
    """
    خريطة رصيد المخزن: رقم صنف → {quantity, avg_cost, unit, name, ...}.

    - استجابة ناجحة فارغة من GetItemQtyCost = كمية 0.
    - فشل الشبكة يُعاد مرة واحدة فقط؛ ما يبقى يُعلَّم _fetch_failed.
    - يعتمد كاش Django بقوة لتسريع التصفح المتكرر.
    """
    from django.core.cache import cache as django_cache

    wh = str(warehouse or (settings.EXTERNAL_API.get('DEFAULT_WAREHOUSE') or '60')).strip()
    codes = [str(c or '').strip() for c in (item_codes or []) if str(c or '').strip()]
    codes = list(dict.fromkeys(codes))
    partial_reuse: dict[str, dict] = {}

    def _zero_row(code: str) -> dict:
        return {
            'code': code,
            'name': '',
            'unit': '',
            'quantity': '0',
            'avg_cost': '',
            'cost': '',
            'barcode': '',
            '_confirmed_empty': True,
        }

    with _warehouse_stock_lock:
        cached = _warehouse_stock_cache.get(wh)
        if cached and (time.monotonic() - cached[0]) < _WAREHOUSE_STOCK_TTL:
            stock_map = cached[1]
            src = cached[2]
            if src.startswith('bulk:'):
                if not codes:
                    return dict(stock_map)
                # الخريطة الجماعية كاملة للمخزن: الناقص = كمية 0
                return {c: stock_map[c] if c in stock_map else _zero_row(c) for c in codes}
            if src.startswith('partial:') and codes:
                hit = {c: stock_map[c] for c in codes if c in stock_map}
                if len(hit) == len(codes):
                    return hit
                codes = [c for c in codes if c not in hit]
                partial_reuse = hit

    # لا تُعِد تجربة الجماعي إن فشل مؤخراً (يوفر ثوانٍ في كل تصفح)
    now_m = time.monotonic()
    skip_bulk = _bulk_stock_fail_until.get(wh, 0) > now_m
    if not skip_bulk:
        bulk_map, bulk_src = _try_bulk_warehouse_stock(wh)
        if bulk_map:
            with _warehouse_stock_lock:
                _warehouse_stock_cache[wh] = (time.monotonic(), bulk_map, f'bulk:{bulk_src}')
            if not codes and not partial_reuse:
                return dict(bulk_map)
            if codes:
                return {
                    **partial_reuse,
                    **{c: bulk_map[c] if c in bulk_map else _zero_row(c) for c in codes},
                }
        else:
            _bulk_stock_fail_until[wh] = now_m + _BULK_STOCK_FAIL_TTL

    partial = dict(partial_reuse)
    if not codes:
        return partial

    CACHE_EMPTY = '__EMPTY__'
    stock_map: dict[str, dict] = dict(partial)
    failed: set[str] = set()

    def _from_qtys(code: str, qtys: list[dict]) -> dict:
        summary = _pick_pricing_summary([], qtys)
        return {
            'code': code,
            'name': str((qtys[0] or {}).get('name') or ''),
            'unit': summary.get('unit') or '',
            'quantity': summary.get('quantity') or '0',
            'avg_cost': summary.get('avg_cost') or '',
            'cost': summary.get('avg_cost') or '',
            'barcode': str((qtys[0] or {}).get('barcode') or ''),
        }

    def _fetch_one(code: str, *, timeout: int, fast: bool) -> tuple[str, dict | None, str]:
        if _stock_circuit_is_open():
            return code, None, 'fail'
        cache_key = f'qtycost:v5:{wh}:{code}'
        cached_q = django_cache.get(cache_key)
        if cached_q == CACHE_EMPTY:
            return code, _zero_row(code), 'empty'
        if isinstance(cached_q, list) and cached_q:
            return code, _from_qtys(code, cached_q), 'ok'
        try:
            qtys = fetch_qty_by_code(code, wh, timeout=timeout, fast=fast)
        except Exception as exc:  # noqa: BLE001
            _stock_circuit_note_failure(exc)
            logger.warning('Warehouse stock fetch failed for %s: %s', code, exc)
            return code, None, 'fail'
        _stock_circuit_note_success()
        if not qtys:
            # فارغ مؤكد من استجابة ناجحة — كاش متوسط (لا 30 دقيقة حتى لا تتجمد أخطاء عابرة)
            django_cache.set(cache_key, CACHE_EMPTY, 1800)
            return code, _zero_row(code), 'empty'
        django_cache.set(cache_key, qtys, 1800)
        return code, _from_qtys(code, qtys), 'ok'

    pending = list(codes)
    rounds = [
        (max(20, max_workers), 8, True),
        (10, 20, False),
    ]
    for round_i, (workers_n, timeout, fast) in enumerate(rounds):
        if not pending:
            break
        if round_i > 0 and _stock_circuit_is_open():
            # لا نعيد محاولة آلاف الأصناف والشبكة مقطوعة
            failed = set(pending)
            break
        batch = list(pending)
        pending = []
        workers = max(1, min(workers_n, len(batch)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_fetch_one, c, timeout=timeout, fast=fast) for c in batch]
            for fut in futs:
                code, row, status = fut.result()
                if status == 'fail':
                    pending.append(code)
                    continue
                if row:
                    stock_map[code] = row
        failed = set(pending)
        # إن فشل أكثر من نصف الدفعة سريعاً → أوقف الجولة التالية
        if batch and len(failed) >= max(20, int(0.8 * len(batch))) and _stock_circuit_is_open():
            break

    for code in failed:
        stock_map[code] = {
            'code': code,
            'name': '',
            'unit': '',
            'quantity': '',
            'avg_cost': '',
            'cost': '',
            'barcode': '',
            '_fetch_failed': True,
        }

    with _warehouse_stock_lock:
        prev = _warehouse_stock_cache.get(wh)
        merged = dict(prev[1]) if prev and prev[2].startswith('partial:') else {}
        merged.update(stock_map)
        _warehouse_stock_cache[wh] = (time.monotonic(), merged, 'partial:qtycost')

    return stock_map


def enrich_group_browse(
    items: list[dict],
    warehouse: str | None = None,
    *,
    max_workers: int = 20,
    group_code: str | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """
    تصفح المجموعة:
    - إن STOCK_QTY_SOURCE=oracle: كمية/تكلفة من أوراكل (IAS_ITM_WCODE) قراءة فقط
      وتشمل غير النشط إن كان له رصيد
    - وإلا: GetItemQtyCost (Avl_Qty بعد التخصيم)
    - أسعار العرض من GetAllPrice عند توفرها
    - يعيد فقط الأصناف بكمية > 0 مع عدّادات الاكتمال
    """
    empty_counts = {
        'catalog_count': 0,
        'stocked_count': 0,
        'zero_count': 0,
        'fetch_failed': 0,
        'complete': True,
        'qty_source': 'api',
    }
    if not items and not group_code:
        return [], empty_counts

    wh = warehouse or (settings.EXTERNAL_API.get('DEFAULT_WAREHOUSE') or '60')
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
                # الكمية والتكلفة من أوراكل فقط — السعر المعروض من API إن وُجد
                stock_unit = str(stock.get('unit') or '').strip()
                price = ''
                for prow in price_map.get(code) or []:
                    if stock_unit and str(prow.get('unit') or '').strip() == stock_unit:
                        price = str(prow.get('price') or '').strip()
                        break
                if not price:
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
            logger.warning('Oracle group stock failed, fallback to API: %s', exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning('Oracle group stock error, fallback to API: %s', exc)

    if not unique_codes:
        return [], empty_counts

    _stock_circuit_reset()
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_prices = pool.submit(_load_prices)
        fut_stock = pool.submit(fetch_warehouse_stock, wh, unique_codes, max_workers=max_workers)
        price_map = fut_prices.result()
        stock_map = fut_stock.result()

    failed_codes = [
        c
        for c in unique_codes
        if (not stock_map.get(c))
        or stock_map[c].get('_fetch_failed')
        or _to_float(stock_map[c].get('quantity')) is None
    ]
    if failed_codes:
        _stock_circuit_reset()
        stock_map.update(
            fetch_warehouse_stock(wh, failed_codes, max_workers=min(8, max_workers))
        )

    stocked = []
    zero_count = 0
    fetch_failed = 0

    for code, base in by_code.items():
        stock = stock_map.get(code)
        if not stock or stock.get('_fetch_failed'):
            fetch_failed += 1
            continue
        qty = _to_float(stock.get('quantity'))
        if qty is None:
            fetch_failed += 1
            continue
        if qty <= 0:
            zero_count += 1
            continue

        prices = price_map.get(code) or []
        summary = _pick_pricing_summary(prices, [stock])
        row = dict(base)
        if summary.get('unit') and not row.get('unit'):
            row['unit'] = summary['unit']
        row['price'] = summary.get('price', '') or ''
        row['avg_cost'] = summary.get('avg_cost', '') or ''
        row['quantity'] = summary.get('quantity', '') or stock.get('quantity') or ''
        if summary.get('unit'):
            row['pricing_unit'] = summary['unit']
        if stock.get('name') and not row.get('name'):
            row['name'] = stock['name']
        stocked.append(row)

    return stocked, {
        'catalog_count': len(unique_codes),
        'stocked_count': len(stocked),
        'zero_count': zero_count,
        'fetch_failed': fetch_failed,
        'complete': fetch_failed == 0,
        'qty_source': 'api',
    }


def compute_inventory_stock_cost(items: list[dict]) -> dict:
    """
    إجمالي تكلفة المخزون = Σ (الكمية × التكلفة) لنفس الوحدة المخزنية.
    المصدر: أوراكل (IAS_ITM_WCODE) أو API (Avl_Qty بعد التخصيم).
    يتجاهل الأصناف بلا كمية رقمية أو بلا تكلفة، والكمية السالبة.
    """
    total = 0.0
    used = 0
    for item in items:
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
