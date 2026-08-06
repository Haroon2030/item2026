"""لوحة تحليل المشتريات من فواتير الشراء ومرتجعاتها في أوراكل — قراءة فقط."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.cache import cache

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _branch_names,
    _fetch_all,
    _schema,
    fetch_sales_group_options,
    oracle_enabled,
)

_CACHE_TTL = 180


def _money(value: Any) -> str:
    return f"{float(value or 0):,.2f}"


def _qty(value: Any) -> str:
    number = float(value or 0)
    if abs(number - round(number)) < 1e-9:
        return f"{int(round(number)):,}"
    return f"{number:,.2f}"


def _request_user_chart_rows(rows: list[dict]) -> list[dict]:
    counts = [int(r.get("REQUEST_COUNT") or 0) for r in rows]
    max_requests = max(counts, default=0) or 1
    total_requests = sum(counts) or 1
    output: list[dict] = []
    for row in rows:
        count = int(row.get("REQUEST_COUNT") or 0)
        share = count / total_requests * 100.0
        output.append(
            {
                "code": str(row.get("USER_CODE") or "").strip(),
                "name": str(
                    row.get("USER_NAME") or row.get("USER_CODE") or ""
                ).strip(),
                "request_count": count,
                "item_count": int(row.get("ITEM_COUNT") or 0),
                "branch_count": int(row.get("BRANCH_COUNT") or 0),
                "qty_display": _qty(row.get("QTY_TOTAL") or 0),
                "bar_pct": round(count / max_requests * 100.0, 1),
                "share_pct": round(share, 1),
                "share_display": f"{share:.1f}%",
            }
        )
    return output


def _base_params(date_from, date_to) -> dict[str, Any]:
    return {
        "d_from": _as_date(date_from),
        "d_to_excl": _as_date(date_to) + timedelta(days=1),
    }


def _scope_filters(
    *,
    alias: str,
    branch_code: str = "",
    vendor_code: str = "",
    group_code: str = "",
    date_col: str,
    hung_col: str = "HUNG",
) -> tuple[list[str], dict[str, Any]]:
    """فلاتر مشتركة للفواتير/المرتجعات مع استبعاد المسودات (HUNG)."""
    schema = _schema()
    filters = [
        f"{alias}.{date_col} >= :d_from",
        f"{alias}.{date_col} < :d_to_excl",
        f"NVL({alias}.{hung_col}, 0) = 0",
    ]
    params: dict[str, Any] = {}
    branch = str(branch_code or "").strip()
    vendor = str(vendor_code or "").strip()
    group = str(group_code or "").strip()
    if branch:
        filters.append(f"TO_CHAR({alias}.BRN_NO) = :branch")
        params["branch"] = branch
    if vendor:
        filters.append(f"TO_CHAR({alias}.V_CODE) = :vendor")
        params["vendor"] = vendor
    if group:
        # المجموعات عبر تفاصيل الفاتورة + بطاقة الصنف
        if alias == "m":
            filters.append(
                f"""EXISTS (
                    SELECT 1
                    FROM {schema}.IAS_PI_BILL_DTL gx
                    JOIN {schema}.IAS_ITM_MST gi ON gi.I_CODE = gx.I_CODE
                    WHERE gx.BILL_NO = m.BILL_NO
                      AND gx.BILL_SER = m.BILL_SER
                      AND gx.BILL_DOC_TYPE = m.BILL_DOC_TYPE
                      AND TO_CHAR(gi.G_CODE) = :group_code
                )"""
            )
        else:
            filters.append(
                f"""EXISTS (
                    SELECT 1
                    FROM {schema}.IAS_PR_BILL_DTL gx
                    JOIN {schema}.IAS_ITM_MST gi ON gi.I_CODE = gx.I_CODE
                    WHERE gx.RT_BILL_NO = r.RT_BILL_NO
                      AND gx.RT_BILL_SER = r.RT_BILL_SER
                      AND gx.RT_BILL_DOC_TYPE = r.RT_BILL_DOC_TYPE
                      AND TO_CHAR(gi.G_CODE) = :group_code
                )"""
            )
        params["group_code"] = group
    return filters, params


def _detail_filters(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
    vendor_code: str = "",
    purchase: bool = True,
) -> tuple[str, dict[str, Any]]:
    if purchase:
        filters = [
            "m.BILL_DATE >= :d_from",
            "m.BILL_DATE < :d_to_excl",
            "NVL(m.HUNG, 0) = 0",
        ]
        prefix = "m"
    else:
        filters = [
            "r.RT_BILL_DATE >= :d_from",
            "r.RT_BILL_DATE < :d_to_excl",
            "NVL(r.HUNG, 0) = 0",
        ]
        prefix = "r"
    params = _base_params(date_from, date_to)
    branch = str(branch_code or "").strip()
    group = str(group_code or "").strip()
    vendor = str(vendor_code or "").strip()
    if branch:
        filters.append(f"TO_CHAR({prefix}.BRN_NO) = :branch")
        params["branch"] = branch
    if group:
        filters.append("TO_CHAR(i.G_CODE) = :group_code")
        params["group_code"] = group
    if vendor:
        filters.append(f"TO_CHAR({prefix}.V_CODE) = :vendor")
        params["vendor"] = vendor
    return " AND ".join(filters), params


def fetch_purchase_vendor_options(date_from, date_to) -> list[dict]:
    """الموردون الذين لديهم فواتير خلال الفترة."""
    schema = _schema()
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(m.V_CODE) AS V_CODE,
               MAX(NVL(m.V_NAME, TO_CHAR(m.V_CODE))) AS V_NAME
        FROM {schema}.IAS_PI_BILL_MST m
        WHERE m.BILL_DATE >= :d_from
          AND m.BILL_DATE < :d_to_excl
          AND NVL(m.HUNG, 0) = 0
          AND m.V_CODE IS NOT NULL
        GROUP BY TO_CHAR(m.V_CODE)
        ORDER BY V_NAME, V_CODE
        """,
        _base_params(date_from, date_to),
    )
    return [
        {
            "code": str(row.get("V_CODE") or "").strip(),
            "name": str(row.get("V_NAME") or row.get("V_CODE") or "").strip(),
        }
        for row in rows
        if str(row.get("V_CODE") or "").strip()
    ]


def _mst_summary(
    schema: str,
    *,
    table: str,
    alias: str,
    date_col: str,
    ser_col: str,
    filters: list[str],
    params: dict[str, Any],
) -> dict[str, Any]:
    rows = _fetch_all(
        f"""
        SELECT COUNT(DISTINCT {alias}.{ser_col}) AS DOC_COUNT,
               COUNT(DISTINCT TO_CHAR({alias}.V_CODE)) AS VENDOR_COUNT,
               ROUND(SUM(NVL({alias}.BILL_AMT, 0)), 2) AS NET_AMOUNT,
               ROUND(SUM(NVL({alias}.VAT_AMT, 0)), 2) AS VAT_AMOUNT
        FROM {schema}.{table} {alias}
        WHERE {" AND ".join(filters)}
        """,
        params,
    )
    return rows[0] if rows else {}


def build_purchase_dashboard(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
    vendor_code: str = "",
) -> dict[str, Any]:
    """مؤشرات المشتريات بعد خصم المرتجعات، مع تفصيل الفرع/المورد/المجموعة."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")

    branch = str(branch_code or "").strip()
    group = str(group_code or "").strip()
    vendor = str(vendor_code or "").strip()
    cache_key = (
        f"purchases:dashboard:v15:{d_from}:{d_to}:{branch}:{group}:{vendor}"
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached

    schema = _schema()
    base = _base_params(d_from, d_to)

    pi_filters, pi_extra = _scope_filters(
        alias="m",
        branch_code=branch,
        vendor_code=vendor,
        group_code=group,
        date_col="BILL_DATE",
    )
    pr_filters, pr_extra = _scope_filters(
        alias="r",
        branch_code=branch,
        vendor_code=vendor,
        group_code=group,
        date_col="RT_BILL_DATE",
    )
    pi_params = {**base, **pi_extra}
    pr_params = {**base, **pr_extra}

    purchase = _mst_summary(
        schema,
        table="IAS_PI_BILL_MST",
        alias="m",
        date_col="BILL_DATE",
        ser_col="BILL_SER",
        filters=pi_filters,
        params=pi_params,
    )
    returns = _mst_summary(
        schema,
        table="IAS_PR_BILL_MST",
        alias="r",
        date_col="RT_BILL_DATE",
        ser_col="RT_BILL_SER",
        filters=pr_filters,
        params=pr_params,
    )

    purchase_net = round(float(purchase.get("NET_AMOUNT") or 0), 2)
    purchase_vat = round(float(purchase.get("VAT_AMOUNT") or 0), 2)
    return_net = round(float(returns.get("NET_AMOUNT") or 0), 2)
    return_vat = round(float(returns.get("VAT_AMOUNT") or 0), 2)
    invoice_count = int(purchase.get("DOC_COUNT") or 0)
    return_count = int(returns.get("DOC_COUNT") or 0)
    net_amount = round(purchase_net - return_net, 2)
    vat_amount = round(purchase_vat - return_vat, 2)
    total_amount = round(net_amount + vat_amount, 2)
    average = round(total_amount / invoice_count, 2) if invoice_count else 0.0

    detail_where, detail_params = _detail_filters(
        d_from,
        d_to,
        branch_code=branch,
        group_code=group,
        vendor_code=vendor,
        purchase=True,
    )
    ret_detail_where, ret_detail_params = _detail_filters(
        d_from,
        d_to,
        branch_code=branch,
        group_code=group,
        vendor_code=vendor,
        purchase=False,
    )

    qty_rows = _fetch_all(
        f"""
        SELECT COUNT(DISTINCT TO_CHAR(d.I_CODE)) AS ITEM_COUNT,
               ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL
        FROM {schema}.IAS_PI_BILL_MST m
        JOIN {schema}.IAS_PI_BILL_DTL d
          ON d.BILL_NO = m.BILL_NO
         AND d.BILL_SER = m.BILL_SER
         AND d.BILL_DOC_TYPE = m.BILL_DOC_TYPE
        JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE {detail_where}
        """,
        detail_params,
    )
    ret_qty_rows = _fetch_all(
        f"""
        SELECT ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL
        FROM {schema}.IAS_PR_BILL_MST r
        JOIN {schema}.IAS_PR_BILL_DTL d
          ON d.RT_BILL_NO = r.RT_BILL_NO
         AND d.RT_BILL_SER = r.RT_BILL_SER
         AND d.RT_BILL_DOC_TYPE = r.RT_BILL_DOC_TYPE
        JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE {ret_detail_where}
        """,
        ret_detail_params,
    )
    qty_total = round(
        float((qty_rows[0] if qty_rows else {}).get("QTY_TOTAL") or 0)
        - float((ret_qty_rows[0] if ret_qty_rows else {}).get("QTY_TOTAL") or 0),
        2,
    )
    item_count = int((qty_rows[0] if qty_rows else {}).get("ITEM_COUNT") or 0)

    branch_rows = _fetch_all(
        f"""
        SELECT CODE,
               SUM(INVOICE_COUNT) AS INVOICE_COUNT,
               SUM(VENDOR_COUNT) AS VENDOR_COUNT,
               ROUND(SUM(AMOUNT), 2) AS AMOUNT
        FROM (
            SELECT NVL(TO_CHAR(m.BRN_NO), '(بلا)') AS CODE,
                   COUNT(DISTINCT m.BILL_SER) AS INVOICE_COUNT,
                   COUNT(DISTINCT TO_CHAR(m.V_CODE)) AS VENDOR_COUNT,
                   ROUND(SUM(NVL(m.BILL_AMT, 0) + NVL(m.VAT_AMT, 0)), 2) AS AMOUNT
            FROM {schema}.IAS_PI_BILL_MST m
            WHERE {" AND ".join(pi_filters)}
            GROUP BY NVL(TO_CHAR(m.BRN_NO), '(بلا)')
            UNION ALL
            SELECT NVL(TO_CHAR(r.BRN_NO), '(بلا)') AS CODE,
                   0 AS INVOICE_COUNT,
                   0 AS VENDOR_COUNT,
                   ROUND(-SUM(NVL(r.BILL_AMT, 0) + NVL(r.VAT_AMT, 0)), 2) AS AMOUNT
            FROM {schema}.IAS_PR_BILL_MST r
            WHERE {" AND ".join(pr_filters)}
            GROUP BY NVL(TO_CHAR(r.BRN_NO), '(بلا)')
        )
        GROUP BY CODE
        HAVING ROUND(SUM(AMOUNT), 2) <> 0
        ORDER BY AMOUNT DESC
        """,
        {**pi_params, **pr_params},
    )

    vendor_rows = _fetch_all(
        f"""
        SELECT CODE,
               MAX(NAME) AS NAME,
               SUM(INVOICE_COUNT) AS INVOICE_COUNT,
               ROUND(SUM(AMOUNT), 2) AS AMOUNT
        FROM (
            SELECT NVL(TO_CHAR(m.V_CODE), '(بلا)') AS CODE,
                   MAX(NVL(m.V_NAME, TO_CHAR(m.V_CODE))) AS NAME,
                   COUNT(DISTINCT m.BILL_SER) AS INVOICE_COUNT,
                   ROUND(SUM(NVL(m.BILL_AMT, 0) + NVL(m.VAT_AMT, 0)), 2) AS AMOUNT
            FROM {schema}.IAS_PI_BILL_MST m
            WHERE {" AND ".join(pi_filters)}
            GROUP BY NVL(TO_CHAR(m.V_CODE), '(بلا)')
            UNION ALL
            SELECT NVL(TO_CHAR(r.V_CODE), '(بلا)') AS CODE,
                   MAX(NVL(r.V_NAME, TO_CHAR(r.V_CODE))) AS NAME,
                   0 AS INVOICE_COUNT,
                   ROUND(-SUM(NVL(r.BILL_AMT, 0) + NVL(r.VAT_AMT, 0)), 2) AS AMOUNT
            FROM {schema}.IAS_PR_BILL_MST r
            WHERE {" AND ".join(pr_filters)}
            GROUP BY NVL(TO_CHAR(r.V_CODE), '(بلا)')
        )
        GROUP BY CODE
        HAVING ROUND(SUM(AMOUNT), 2) <> 0
        ORDER BY AMOUNT DESC
        FETCH FIRST 20 ROWS ONLY
        """,
        {**pi_params, **pr_params},
    )

    group_rows = _fetch_all(
        f"""
        SELECT CODE,
               SUM(INVOICE_COUNT) AS INVOICE_COUNT,
               SUM(ITEM_COUNT) AS ITEM_COUNT,
               ROUND(SUM(QTY_TOTAL), 2) AS QTY_TOTAL,
               ROUND(SUM(AMOUNT), 2) AS AMOUNT
        FROM (
            SELECT NVL(TO_CHAR(i.G_CODE), '(بلا)') AS CODE,
                   COUNT(DISTINCT m.BILL_SER) AS INVOICE_COUNT,
                   COUNT(DISTINCT TO_CHAR(d.I_CODE)) AS ITEM_COUNT,
                   ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL,
                   ROUND(
                     SUM(NVL(d.I_QTY, 0) * NVL(d.I_PRICE, 0) - NVL(d.DIS_AMT, 0)),
                     2
                   ) AS AMOUNT
            FROM {schema}.IAS_PI_BILL_MST m
            JOIN {schema}.IAS_PI_BILL_DTL d
              ON d.BILL_NO = m.BILL_NO
             AND d.BILL_SER = m.BILL_SER
             AND d.BILL_DOC_TYPE = m.BILL_DOC_TYPE
            JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
            WHERE {detail_where}
            GROUP BY NVL(TO_CHAR(i.G_CODE), '(بلا)')
            UNION ALL
            SELECT NVL(TO_CHAR(i.G_CODE), '(بلا)') AS CODE,
                   0 AS INVOICE_COUNT,
                   0 AS ITEM_COUNT,
                   ROUND(-SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL,
                   ROUND(
                     -SUM(NVL(d.I_QTY, 0) * NVL(d.I_PRICE, 0) - NVL(d.DIS_AMT, 0)),
                     2
                   ) AS AMOUNT
            FROM {schema}.IAS_PR_BILL_MST r
            JOIN {schema}.IAS_PR_BILL_DTL d
              ON d.RT_BILL_NO = r.RT_BILL_NO
             AND d.RT_BILL_SER = r.RT_BILL_SER
             AND d.RT_BILL_DOC_TYPE = r.RT_BILL_DOC_TYPE
            JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
            WHERE {ret_detail_where}
            GROUP BY NVL(TO_CHAR(i.G_CODE), '(بلا)')
        )
        GROUP BY CODE
        HAVING ROUND(SUM(AMOUNT), 2) <> 0
        ORDER BY AMOUNT DESC
        """,
        {**detail_params, **ret_detail_params},
    )

    item_rows = _fetch_all(
        f"""
        SELECT CODE,
               MAX(NAME) AS NAME,
               SUM(INVOICE_COUNT) AS INVOICE_COUNT,
               ROUND(SUM(QTY_TOTAL), 2) AS QTY_TOTAL,
               ROUND(SUM(AMOUNT), 2) AS AMOUNT
        FROM (
            SELECT TO_CHAR(d.I_CODE) AS CODE,
                   MAX(
                     NVL(
                       NULLIF(TRIM(i.I_NAME), ''),
                       NVL(NULLIF(TRIM(d.I_NM), ''), TO_CHAR(d.I_CODE))
                     )
                   ) AS NAME,
                   COUNT(DISTINCT m.BILL_SER) AS INVOICE_COUNT,
                   ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL,
                   ROUND(
                     SUM(NVL(d.I_QTY, 0) * NVL(d.I_PRICE, 0) - NVL(d.DIS_AMT, 0)),
                     2
                   ) AS AMOUNT
            FROM {schema}.IAS_PI_BILL_MST m
            JOIN {schema}.IAS_PI_BILL_DTL d
              ON d.BILL_NO = m.BILL_NO
             AND d.BILL_SER = m.BILL_SER
             AND d.BILL_DOC_TYPE = m.BILL_DOC_TYPE
            LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
            WHERE {detail_where}
            GROUP BY TO_CHAR(d.I_CODE)
            UNION ALL
            SELECT TO_CHAR(d.I_CODE) AS CODE,
                   MAX(NVL(NULLIF(TRIM(i.I_NAME), ''), TO_CHAR(d.I_CODE))) AS NAME,
                   0 AS INVOICE_COUNT,
                   ROUND(-SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL,
                   ROUND(
                     -SUM(NVL(d.I_QTY, 0) * NVL(d.I_PRICE, 0) - NVL(d.DIS_AMT, 0)),
                     2
                   ) AS AMOUNT
            FROM {schema}.IAS_PR_BILL_MST r
            JOIN {schema}.IAS_PR_BILL_DTL d
              ON d.RT_BILL_NO = r.RT_BILL_NO
             AND d.RT_BILL_SER = r.RT_BILL_SER
             AND d.RT_BILL_DOC_TYPE = r.RT_BILL_DOC_TYPE
            LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
            WHERE {ret_detail_where}
            GROUP BY TO_CHAR(d.I_CODE)
        )
        GROUP BY CODE
        HAVING ROUND(SUM(AMOUNT), 2) <> 0
        ORDER BY AMOUNT DESC
        FETCH FIRST 20 ROWS ONLY
        """,
        {**detail_params, **ret_detail_params},
    )

    request_filters = [
        "p.PR_DATE >= :d_from",
        "p.PR_DATE < :d_to_excl",
        "p.AD_U_ID IS NOT NULL",
        "NVL(p.INACTIVE, 0) = 0",
    ]
    request_params = _base_params(d_from, d_to)
    if branch:
        request_filters.append("TO_CHAR(p.BRN_NO) = :branch")
        request_params["branch"] = branch
    if vendor:
        request_filters.append("TO_CHAR(p.V_CODE) = :vendor")
        request_params["vendor"] = vendor
    if group:
        request_filters.append("TO_CHAR(i.G_CODE) = :group_code")
        request_params["group_code"] = group

    request_user_rows = _fetch_all(
        f"""
        SELECT TO_CHAR(p.AD_U_ID) AS USER_CODE,
               MAX(NVL(u.U_A_NAME, NVL(u.U_E_NAME, TO_CHAR(p.AD_U_ID)))) AS USER_NAME,
               COUNT(
                 DISTINCT TO_CHAR(p.PR_TYPE) || ':' || TO_CHAR(p.PR_SER)
               ) AS REQUEST_COUNT,
               COUNT(DISTINCT TO_CHAR(d.I_CODE)) AS ITEM_COUNT,
               COUNT(DISTINCT TO_CHAR(p.BRN_NO)) AS BRANCH_COUNT,
               ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL
        FROM {schema}.P_REQUEST p
        JOIN {schema}.P_REQUEST_DETAIL d
          ON d.PR_TYPE = p.PR_TYPE
         AND d.PR_NO = p.PR_NO
         AND d.PR_SER = p.PR_SER
        JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        LEFT JOIN {schema}.USER_R u ON u.U_ID = p.AD_U_ID
        WHERE {" AND ".join(request_filters)}
        GROUP BY TO_CHAR(p.AD_U_ID)
        ORDER BY REQUEST_COUNT DESC, QTY_TOTAL DESC
        FETCH FIRST 20 ROWS ONLY
        """,
        request_params,
    )

    request_group_rows = _fetch_all(
        f"""
        SELECT NVL(TO_CHAR(i.G_CODE), '(بلا)') AS GROUP_CODE,
               COUNT(
                 DISTINCT TO_CHAR(p.PR_TYPE) || ':' || TO_CHAR(p.PR_SER)
               ) AS REQUEST_COUNT,
               COUNT(DISTINCT TO_CHAR(d.I_CODE)) AS ITEM_COUNT,
               COUNT(DISTINCT TO_CHAR(p.BRN_NO)) AS BRANCH_COUNT,
               ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL
        FROM {schema}.P_REQUEST p
        JOIN {schema}.P_REQUEST_DETAIL d
          ON d.PR_TYPE = p.PR_TYPE
         AND d.PR_NO = p.PR_NO
         AND d.PR_SER = p.PR_SER
        JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE {" AND ".join(request_filters)}
        GROUP BY NVL(TO_CHAR(i.G_CODE), '(بلا)')
        ORDER BY REQUEST_COUNT DESC, QTY_TOTAL DESC
        FETCH FIRST 20 ROWS ONLY
        """,
        request_params,
    )

    branch_names = _branch_names()
    group_names = {
        str(row.get("code") or "").strip(): str(row.get("name") or "").strip()
        for row in fetch_sales_group_options()
    }
    share_base = abs(total_amount) if total_amount else 0.0

    def amount_rows(
        rows: list[dict],
        *,
        names: dict[str, str] | None = None,
    ) -> list[dict]:
        output: list[dict] = []
        for row in rows:
            code = str(row.get("CODE") or "").strip() or "(بلا)"
            amount = round(float(row.get("AMOUNT") or 0), 2)
            share = (amount / share_base * 100.0) if share_base else 0.0
            output.append(
                {
                    "code": code,
                    "name": str(row.get("NAME") or "").strip()
                    or (names or {}).get(code)
                    or code,
                    "amount": amount,
                    "amount_display": _money(amount),
                    "invoice_count": int(row.get("INVOICE_COUNT") or 0),
                    "vendor_count": int(row.get("VENDOR_COUNT") or 0),
                    "item_count": int(row.get("ITEM_COUNT") or 0),
                    "qty_total": round(float(row.get("QTY_TOTAL") or 0), 2),
                    "qty_display": _qty(row.get("QTY_TOTAL") or 0),
                    "share_pct": round(share, 1),
                    "share_display": f"{share:.1f}%",
                }
            )
        return output

    result = {
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "kpis": {
            "invoice_count": invoice_count,
            "return_count": return_count,
            "vendor_count": int(purchase.get("VENDOR_COUNT") or 0),
            "item_count": item_count,
            "qty_total": qty_total,
            "qty_display": _qty(qty_total),
            "purchase_net": purchase_net,
            "purchase_net_display": _money(purchase_net),
            "return_net": return_net,
            "return_net_display": _money(return_net),
            "net_amount": net_amount,
            "net_display": _money(net_amount),
            "vat_amount": vat_amount,
            "vat_display": _money(vat_amount),
            "total_amount": total_amount,
            "total_display": _money(total_amount),
            "average_amount": average,
            "average_display": _money(average),
        },
        "by_branch": amount_rows(branch_rows, names=branch_names),
        "by_vendor": amount_rows(vendor_rows),
        "by_group": amount_rows(group_rows, names=group_names),
        "top_items": amount_rows(item_rows),
        "top_request_users": _request_user_chart_rows(request_user_rows),
        "top_request_groups": [
            {
                "code": code,
                "name": group_names.get(code) or code,
                "request_count": int(row.get("REQUEST_COUNT") or 0),
                "item_count": int(row.get("ITEM_COUNT") or 0),
                "branch_count": int(row.get("BRANCH_COUNT") or 0),
                "qty_display": _qty(row.get("QTY_TOTAL") or 0),
            }
            for row in request_group_rows
            for code in [str(row.get("GROUP_CODE") or "").strip() or "(بلا)"]
        ],
    }
    cache.set(cache_key, result, _CACHE_TTL)
    return result
