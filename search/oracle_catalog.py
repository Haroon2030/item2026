"""كتالوج الأصناف والأسعار والكميات من أوراكل أونكس — SELECT فقط.

يحل محل خدمة أونكس (GetAllItems / GetAllGroupDet / GetAllPrice / GetItemQtyCost):
- IAS_ITM_MST + IAS_ITM_DTL + IAS_ITM_UNT_BARCODE : الأصناف والوحدات والباركودات
- GROUP_DETAILS                                    : المجموعات
- IAS_ITEM_PRICE                                   : أسعار البيع حسب المخزن والوحدة
- IAS_ITM_WCODE                                    : الكمية ومتوسط التكلفة حسب المخزن
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

from .oracle_sqlutil import num_bind as _num_bind
from .oracle_stock import OracleStockError, _fetch_all, _schema, oracle_enabled

logger = logging.getLogger(__name__)

# مستوى التسعير الافتراضي (LEV_NO) — نفس ما تستخدمه بقية شاشات المقارنة
PRICE_LEVEL = 1


def _require_oracle() -> None:
    if not oracle_enabled():
        raise OracleStockError('أوراكل غير مفعّل — أضف بيانات الربط من شاشة «إعدادات الربط».')


def _text(value: Any) -> str:
    return '' if value is None else str(value).strip()


def _num_text(value: Any) -> str:
    """رقم أوراكل كنص بدون أصفار زائدة (3.0 → 3)."""
    if value in (None, ''):
        return ''
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _text(value)
    if number.is_integer():
        return str(int(number))
    return f'{number:.6f}'.rstrip('0').rstrip('.')


def resolve_item_code(query: str) -> str:
    """رقم الصنف من مدخل المستخدم: رقم صنف مباشرة أو باركود أي وحدة."""
    q = _text(query)
    if not q:
        return ''
    schema = _schema()
    rows = _fetch_all(
        f"SELECT I_CODE FROM {schema}.IAS_ITM_MST WHERE I_CODE = :q AND ROWNUM <= 1",
        {'q': q},
    )
    if rows:
        return _text(rows[0].get('I_CODE'))
    rows = _fetch_all(
        f"""
        SELECT I_CODE FROM {schema}.IAS_ITM_UNT_BARCODE WHERE BARCODE = :q AND ROWNUM <= 1
        UNION ALL
        SELECT I_CODE FROM {schema}.IAS_ITM_DTL WHERE BARCODE = :q AND ROWNUM <= 1
        """,
        {'q': q},
    )
    return _text(rows[0].get('I_CODE')) if rows else ''


def fetch_item_prices(query: str, warehouse: str | None = None) -> list[dict]:
    """أسعار صنف لكل وحدة في مخزن (بدل GetAllPrice). يقبل رقم صنف أو باركود."""
    _require_oracle()
    code = resolve_item_code(query)
    if not code:
        return []
    schema = _schema()
    wh = _text(warehouse) or _text((settings.ERP_CONFIG or {}).get('DEFAULT_WAREHOUSE'))
    rows = _fetch_all(
        f"""
        SELECT
            d.I_CODE AS CODE,
            m.I_NAME AS NAME,
            d.ITM_UNT AS UNIT,
            d.P_SIZE AS P_SIZE,
            NVL((SELECT MIN(b.BARCODE) KEEP (DENSE_RANK FIRST ORDER BY b.MAIN_BARCODE DESC NULLS LAST, b.BARCODE)
                   FROM {schema}.IAS_ITM_UNT_BARCODE b
                  WHERE b.I_CODE = d.I_CODE AND b.ITM_UNT = d.ITM_UNT), d.BARCODE) AS BARCODE,
            (SELECT MAX(p.I_PRICE) KEEP (DENSE_RANK LAST ORDER BY NVL(p.UP_DATE, p.AD_DATE))
               FROM {schema}.IAS_ITEM_PRICE p
              WHERE p.I_CODE = d.I_CODE AND p.ITM_UNT = d.ITM_UNT
                AND NVL(p.LEV_NO, {PRICE_LEVEL}) = {PRICE_LEVEL}
                AND p.W_CODE = {_num_bind(':wh')}) AS PRICE,
            m.I_CWTAVG AS AVG_COST
        FROM {schema}.IAS_ITM_DTL d
        JOIN {schema}.IAS_ITM_MST m ON m.I_CODE = d.I_CODE
        WHERE d.I_CODE = :code
        ORDER BY NVL(d.P_SIZE, 0), d.ITM_UNT
        """,
        {'code': code, 'wh': wh},
    )
    out = []
    for r in rows:
        price = r.get('PRICE')
        out.append(
            {
                'code': _text(r.get('CODE')),
                'name': _text(r.get('NAME')),
                'barcode': _text(r.get('BARCODE')),
                'price': '' if price in (None, '') else _num_text(price),
                'unit': _text(r.get('UNIT')),
                'quantity': '',
                'pack_size': _num_text(r.get('P_SIZE')),
                'avg_cost': '',
                'raw': {'P_SIZE': r.get('P_SIZE'), 'I_CODE': _text(r.get('CODE'))},
            }
        )
    return out


def fetch_item_qty(code: str, warehouse: str | None = None) -> list[dict]:
    """رصيد الصنف ومتوسط تكلفته في مخزن (بدل GetItemQtyCost)."""
    _require_oracle()
    code = _text(code)
    if not code:
        return []
    schema = _schema()
    wh = _text(warehouse) or _text((settings.ERP_CONFIG or {}).get('DEFAULT_WAREHOUSE'))
    rows = _fetch_all(
        f"""
        SELECT
            w.I_CODE AS CODE,
            m.I_NAME AS NAME,
            w.ITM_UNT AS UNIT,
            ROUND(NVL(w.AVL_QTY, 0), 4) AS QTY,
            ROUND(NVL(w.I_CWTAVG, w.PRIMARY_COST), 6) AS COST
        FROM {schema}.IAS_ITM_WCODE w
        JOIN {schema}.IAS_ITM_MST m ON m.I_CODE = w.I_CODE
        WHERE w.I_CODE = :code AND w.W_CODE = {_num_bind(':wh')}
        """,
        {'code': code, 'wh': wh},
    )
    out = []
    for r in rows:
        qty, cost = r.get('QTY'), r.get('COST')
        cost_text = '' if cost in (None, '') else _num_text(cost)
        out.append(
            {
                'code': _text(r.get('CODE')),
                'name': _text(r.get('NAME')),
                'unit': _text(r.get('UNIT')),
                'quantity': '' if qty in (None, '') else _num_text(qty),
                'avg_cost': cost_text,
                'cost': cost_text,
                'barcode': '',
            }
        )
    return out


def fetch_price_map(warehouse: str) -> dict[str, list[dict]]:
    """أسعار مخزن كاملة: رقم الصنف → [{unit, price}] (بدل GetAllPrice بلا i_code)."""
    _require_oracle()
    schema = _schema()
    rows = _fetch_all(
        f"""
        SELECT p.I_CODE AS CODE, p.ITM_UNT AS UNIT, MAX(p.I_PRICE) AS PRICE
          FROM {schema}.IAS_ITEM_PRICE p
         WHERE NVL(p.LEV_NO, {PRICE_LEVEL}) = {PRICE_LEVEL}
           AND p.W_CODE = {_num_bind(':wh')}
           AND NVL(p.I_PRICE, 0) > 0
         GROUP BY p.I_CODE, p.ITM_UNT
        """,
        {'wh': _text(warehouse)},
    )
    out: dict[str, list[dict]] = {}
    for r in rows:
        code = _text(r.get('CODE'))
        if not code:
            continue
        out.setdefault(code, []).append(
            {'unit': _text(r.get('UNIT')), 'price': _num_text(r.get('PRICE'))}
        )
    return out


def fetch_catalog_rows() -> list[dict]:
    """كل وحدات/باركودات الأصناف النشطة (بدل GetAllItems) لفهرس البحث المحلي."""
    _require_oracle()
    schema = _schema()
    rows = _fetch_all(
        f"""
        SELECT
            d.I_CODE AS CODE,
            m.I_NAME AS NAME,
            m.G_CODE AS G_CODE,
            d.ITM_UNT AS UNIT,
            d.P_SIZE AS P_SIZE,
            NVL(b.BARCODE, d.BARCODE) AS BARCODE
        FROM {schema}.IAS_ITM_DTL d
        JOIN {schema}.IAS_ITM_MST m ON m.I_CODE = d.I_CODE
        LEFT JOIN {schema}.IAS_ITM_UNT_BARCODE b
               ON b.I_CODE = d.I_CODE AND b.ITM_UNT = d.ITM_UNT
        WHERE NVL(m.INACTIVE, 0) = 0
        """
    )
    return [
        {
            'Item_code': _text(r.get('CODE')),
            'Item_ar_name': _text(r.get('NAME')),
            'Item_category': _text(r.get('G_CODE')),
            'itm_unt': _text(r.get('UNIT')),
            'p_size': _num_text(r.get('P_SIZE')),
            'Barcode': _text(r.get('BARCODE')),
        }
        for r in rows
    ]


def fetch_group_rows() -> list[dict]:
    """مجموعات الأصناف (بدل GetAllGroupDet)."""
    _require_oracle()
    schema = _schema()
    rows = _fetch_all(
        f"SELECT G_CODE, G_A_NAME, G_E_NAME FROM {schema}.GROUP_DETAILS ORDER BY G_CODE"
    )
    return [
        {
            'G_CODE': _text(r.get('G_CODE')),
            'G_A_NAME': _text(r.get('G_A_NAME')),
            'G_E_NAME': _text(r.get('G_E_NAME')),
        }
        for r in rows
    ]
