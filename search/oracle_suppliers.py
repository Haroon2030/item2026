"""تقرير الموردين — فواتير الشراء، رصيد المخزون، والسداد من أوراكل (قراءة فقط)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.core.cache import cache

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _fetch_all,
    _schema,
    oracle_enabled,
)

_CACHE_TTL = 900


def _money(value: Any) -> str:
    return f"{float(value or 0):,.2f}"


def _qty(value: Any) -> str:
    number = float(value or 0)
    if abs(number - round(number)) < 1e-9:
        return f"{int(round(number)):,}"
    return f"{number:,.2f}"


def _date_label(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value.isoformat()
    try:
        return value.date().isoformat()  # type: ignore[union-attr]
    except Exception:
        text = str(value or "").strip()
        return text[:10] if text else "—"


def build_suppliers_report(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    q: str = "",
    scope: str = "all",
    limit: int = 300,
) -> dict[str, Any]:
    """جدول موردين: عدد الفواتير، رصيد المخزون، إجمالي السداد، آخر سداد.

    scope: all | both (توريد وسداد معاً) | inv_only | pay_only
    """
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")

    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    brn = str(branch_code or "").strip()
    query = str(q or "").strip()
    scope_key = str(scope or "all").strip().lower()
    if scope_key not in {"all", "both", "inv_only", "pay_only"}:
        scope_key = "all"
    lim = max(1, min(int(limit or 300), 5000))
    cache_key = (
        f"suppliers:report:v4:{d_from.isoformat()}:{d_to.isoformat()}:"
        f"{brn}:{query.lower()}:{scope_key}:{lim}"
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    schema = _schema()
    params: dict[str, Any] = {
        "d_from": d_from,
        "d_to_excl": d_to + timedelta(days=1),
        "lim": lim,
    }

    inv_filters = [
        "m.BILL_DATE >= :d_from",
        "m.BILL_DATE < :d_to_excl",
        "NVL(m.HUNG, 0) = 0",
        "m.V_CODE IS NOT NULL",
    ]
    pay_filters = [
        "v.VOUCHER_TYPE = 2",
        "d.AC_DTL_TYP = 4",
        "d.AC_CODE_DTL IS NOT NULL",
        "v.VOUCHER_DATE >= :d_from",
        "v.VOUCHER_DATE < :d_to_excl",
    ]
    stock_filters = ["1=1"]

    if brn:
        params["brn"] = brn
        inv_filters.append("TO_CHAR(m.BRN_NO) = :brn")
        pay_filters.append("TO_CHAR(v.BRN_NO) = :brn")
        stock_filters.append("TO_CHAR(wh.CONN_BRN_NO) = :brn")

    search_sql = ""
    if query:
        params["q_exact"] = query
        params["q_like"] = f"%{query}%"
        search_sql = """
          AND (
            TO_CHAR(vend.V_CODE) = :q_exact
            OR UPPER(NVL(vend.V_NAME, '')) LIKE UPPER(:q_like)
          )
        """

    scope_sql = ""
    if scope_key == "both":
        scope_sql = "AND vend.INVOICE_COUNT > 0 AND vend.PAY_COUNT > 0"
    elif scope_key == "inv_only":
        scope_sql = "AND vend.INVOICE_COUNT > 0 AND vend.PAY_COUNT = 0"
    elif scope_key == "pay_only":
        scope_sql = "AND vend.INVOICE_COUNT = 0 AND vend.PAY_COUNT > 0"

    sql = f"""
    WITH inv AS (
        SELECT
            TO_CHAR(m.V_CODE) AS V_CODE,
            MAX(
                NVL(
                    NULLIF(TRIM(m.V_NAME), ''),
                    NVL(NULLIF(TRIM(vd.V_A_NAME), ''), TO_CHAR(m.V_CODE))
                )
            ) AS V_NAME,
            COUNT(DISTINCT m.BILL_SER) AS INVOICE_COUNT,
            ROUND(SUM(NVL(m.BILL_AMT, 0) + NVL(m.VAT_AMT, 0)), 2) AS PURCHASE_TOTAL,
            MAX(m.BILL_DATE) AS LAST_BILL_DATE
        FROM {schema}.IAS_PI_BILL_MST m
        LEFT JOIN {schema}.V_DETAILS vd
          ON TO_CHAR(vd.V_CODE) = TO_CHAR(m.V_CODE)
        WHERE {' AND '.join(inv_filters)}
        GROUP BY TO_CHAR(m.V_CODE)
    ),
    pay AS (
        SELECT
            TO_CHAR(d.AC_CODE_DTL) AS V_CODE,
            COUNT(DISTINCT TO_CHAR(v.VOUCHER_NO) || ':' || TO_CHAR(NVL(v.V_SER, 0))) AS PAY_COUNT,
            ROUND(SUM(NVL(d.AC_AMT, 0)), 2) AS PAY_TOTAL,
            MAX(v.VOUCHER_DATE) AS LAST_PAY_DATE
        FROM {schema}.VOUCHER_DETAIL d
        JOIN {schema}.VOUCHERS v
          ON v.VOUCHER_TYPE = d.VOUCHER_TYPE
         AND v.VOUCHER_PAY_TYPE = d.VOUCHER_PAY_TYPE
         AND v.VOUCHER_NO = d.VOUCHER_NO
         AND NVL(v.V_SER, 0) = NVL(d.V_SER, 0)
        WHERE {' AND '.join(pay_filters)}
        GROUP BY TO_CHAR(d.AC_CODE_DTL)
    ),
    itm_v AS (
        -- مورد الصنف مثل تقرير "أرصدة المخزن حسب المورد": مورد بطاقة الصنف،
        -- وإن خلت البطاقة يُؤخذ المورد الرئيسي من ربط الموردين
        SELECT
            TO_CHAR(im.I_CODE) AS I_CODE,
            COALESCE(TO_CHAR(im.V_CODE), mv.V_CODE) AS V_CODE
        FROM {schema}.IAS_ITM_MST im
        LEFT JOIN (
            SELECT I_CODE, V_CODE
            FROM (
                SELECT
                    TO_CHAR(vi0.I_CODE) AS I_CODE,
                    TO_CHAR(vi0.V_CODE) AS V_CODE,
                    ROW_NUMBER() OVER (
                        PARTITION BY TO_CHAR(vi0.I_CODE)
                        ORDER BY NVL(vi0.MAIN_VNDR, 0) DESC, TO_CHAR(vi0.V_CODE)
                    ) AS RN
                FROM {schema}.IAS_VNDR_ITM vi0
            )
            WHERE RN = 1
        ) mv ON mv.I_CODE = TO_CHAR(im.I_CODE)
    ),
    stk AS (
        -- الرصيد المتاح بعد الترحيل (AVL_QTY) × متوسط التكلفة لكل مخزن
        SELECT
            iv.V_CODE AS V_CODE,
            ROUND(SUM(NVL(w.AVL_QTY, 0)), 2) AS STOCK_QTY,
            ROUND(
                SUM(NVL(w.AVL_QTY, 0) * NVL(w.I_CWTAVG, NVL(w.PRIMARY_COST, 0))),
                2
            ) AS STOCK_VALUE
        FROM itm_v iv
        JOIN {schema}.IAS_ITM_WCODE w
          ON TO_CHAR(w.I_CODE) = iv.I_CODE
        LEFT JOIN {schema}.WAREHOUSE_DETAILS wh
          ON wh.W_CODE = w.W_CODE
        WHERE iv.V_CODE IS NOT NULL
          AND {' AND '.join(stock_filters)}
          AND EXISTS (
              SELECT 1 FROM inv i WHERE i.V_CODE = iv.V_CODE
              UNION ALL
              SELECT 1 FROM pay p WHERE p.V_CODE = iv.V_CODE
          )
        GROUP BY iv.V_CODE
    ),
    vend AS (
        SELECT
            NVL(i.V_CODE, p.V_CODE) AS V_CODE,
            NVL(i.V_NAME, NVL(vd.V_A_NAME, NVL(i.V_CODE, p.V_CODE))) AS V_NAME,
            NVL(i.INVOICE_COUNT, 0) AS INVOICE_COUNT,
            NVL(i.PURCHASE_TOTAL, 0) AS PURCHASE_TOTAL,
            i.LAST_BILL_DATE,
            NVL(p.PAY_COUNT, 0) AS PAY_COUNT,
            NVL(p.PAY_TOTAL, 0) AS PAY_TOTAL,
            p.LAST_PAY_DATE
        FROM inv i
        FULL OUTER JOIN pay p ON p.V_CODE = i.V_CODE
        LEFT JOIN {schema}.V_DETAILS vd
          ON TO_CHAR(vd.V_CODE) = NVL(i.V_CODE, p.V_CODE)
    )
    SELECT * FROM (
        SELECT
            vend.V_CODE,
            vend.V_NAME,
            vend.INVOICE_COUNT,
            vend.PURCHASE_TOTAL,
            vend.LAST_BILL_DATE,
            vend.PAY_COUNT,
            vend.PAY_TOTAL,
            vend.LAST_PAY_DATE,
            NVL(s.STOCK_QTY, 0) AS STOCK_QTY,
            NVL(s.STOCK_VALUE, 0) AS STOCK_VALUE
        FROM vend
        LEFT JOIN stk s ON s.V_CODE = vend.V_CODE
        WHERE vend.V_CODE IS NOT NULL
          {search_sql}
          {scope_sql}
        ORDER BY
            vend.PAY_TOTAL DESC NULLS LAST,
            vend.INVOICE_COUNT DESC,
            vend.V_NAME
    ) WHERE ROWNUM <= :lim
    """

    raw = _fetch_all(sql, params)
    rows: list[dict] = []
    total_inv = 0
    total_purchase = 0.0
    total_pay = 0.0
    total_stock_val = 0.0
    for idx, row in enumerate(raw):
        code = str(row.get("V_CODE") or "").strip()
        if not code:
            continue
        inv_count = int(row.get("INVOICE_COUNT") or 0)
        purchase_total = round(float(row.get("PURCHASE_TOTAL") or 0), 2)
        pay_total = round(float(row.get("PAY_TOTAL") or 0), 2)
        stock_qty = round(float(row.get("STOCK_QTY") or 0), 2)
        stock_val = round(float(row.get("STOCK_VALUE") or 0), 2)
        total_inv += inv_count
        total_purchase += purchase_total
        total_pay += pay_total
        total_stock_val += stock_val
        rows.append(
            {
                "rank": idx + 1,
                "code": code,
                "name": str(row.get("V_NAME") or code).strip() or code,
                "invoice_count": inv_count,
                "invoice_count_display": f"{inv_count:,}",
                "purchase_total": round(float(row.get("PURCHASE_TOTAL") or 0), 2),
                "purchase_total_display": _money(row.get("PURCHASE_TOTAL") or 0),
                "stock_qty": stock_qty,
                "stock_qty_display": _qty(stock_qty),
                "stock_value": stock_val,
                "stock_value_display": _money(stock_val),
                "pay_count": int(row.get("PAY_COUNT") or 0),
                "pay_count_display": f"{int(row.get('PAY_COUNT') or 0):,}",
                "pay_total": pay_total,
                "pay_total_display": _money(pay_total),
                "last_pay_date": _date_label(row.get("LAST_PAY_DATE")),
                "last_bill_date": _date_label(row.get("LAST_BILL_DATE")),
            }
        )

    payload = {
        "rows": rows,
        "totals": {
            "supplier_count": len(rows),
            "supplier_count_display": f"{len(rows):,}",
            "invoice_count": total_inv,
            "invoice_count_display": f"{total_inv:,}",
            "purchase_total": round(total_purchase, 2),
            "purchase_total_display": _money(total_purchase),
            "pay_total": round(total_pay, 2),
            "pay_total_display": _money(total_pay),
            "stock_value": round(total_stock_val, 2),
            "stock_value_display": _money(total_stock_val),
        },
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "scope": scope_key,
        "limit": lim,
    }
    cache.set(cache_key, payload, _CACHE_TTL)
    return payload
