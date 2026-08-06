"""مقارنة طلبات الشراء مع أرصدة المخازن — قراءة فقط من أوراكل."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .oracle_stock import (
    _branch_names,
    _fetch_all,
    _fmt_inv_qty,
    _schema,
    fetch_warehouse_options,
    oracle_enabled,
)


def _qty(value: Any) -> str:
    return _fmt_inv_qty(float(value or 0))


def _today_bounds(day: date | None = None) -> tuple[date, date]:
    d = day or date.today()
    return d, d + timedelta(days=1)


def fetch_warehouses_for_branch(branch_code: str = "") -> list[dict]:
    """مخازن الفرع (أو الكل إن لم يُحدد فرع)."""
    rows = fetch_warehouse_options(active_only=True)
    brn = str(branch_code or "").strip()
    if not brn:
        return rows
    return [row for row in rows if str(row.get("branch_code") or "").strip() == brn]


def fetch_today_purchase_requests(
    *,
    branch_code: str,
    day: date | None = None,
) -> list[dict]:
    """طلبات شراء اليوم لفرع محدد من P_REQUEST."""
    if not oracle_enabled():
        return []
    brn = str(branch_code or "").strip()
    if not brn:
        return []

    schema = _schema()
    d_from, d_to_excl = _today_bounds(day)
    names = _branch_names()
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(p.PR_TYPE) AS PR_TYPE,
               TO_CHAR(p.PR_NO) AS PR_NO,
               TO_CHAR(p.PR_SER) AS PR_SER,
               p.PR_DATE AS PR_DATE,
               TO_CHAR(p.BRN_NO) AS BRANCH_CODE,
               TO_CHAR(p.V_CODE) AS VENDOR_CODE,
               TO_CHAR(p.AD_U_ID) AS USER_CODE,
               MAX(NVL(u.U_A_NAME, NVL(u.U_E_NAME, TO_CHAR(p.AD_U_ID)))) AS USER_NAME,
               COUNT(DISTINCT TO_CHAR(d.I_CODE)) AS ITEM_COUNT,
               ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL
        FROM {schema}.P_REQUEST p
        JOIN {schema}.P_REQUEST_DETAIL d
          ON d.PR_TYPE = p.PR_TYPE
         AND d.PR_NO = p.PR_NO
         AND d.PR_SER = p.PR_SER
        LEFT JOIN {schema}.USER_R u ON u.U_ID = p.AD_U_ID
        WHERE p.PR_DATE >= :d_from
          AND p.PR_DATE < :d_to_excl
          AND NVL(p.INACTIVE, 0) = 0
          AND TO_CHAR(p.BRN_NO) = :branch
        GROUP BY TO_CHAR(p.PR_TYPE),
                 TO_CHAR(p.PR_NO),
                 TO_CHAR(p.PR_SER),
                 p.PR_DATE,
                 TO_CHAR(p.BRN_NO),
                 TO_CHAR(p.V_CODE),
                 TO_CHAR(p.AD_U_ID)
        ORDER BY p.PR_DATE DESC, TO_CHAR(p.PR_SER) DESC
        """,
        {"d_from": d_from, "d_to_excl": d_to_excl, "branch": brn},
    )

    out: list[dict] = []
    for row in rows:
        branch = str(row.get("BRANCH_CODE") or "").strip()
        pr_date = row.get("PR_DATE")
        if isinstance(pr_date, datetime):
            date_label = pr_date.strftime("%Y-%m-%d %H:%M")
        elif isinstance(pr_date, date):
            date_label = pr_date.isoformat()
        else:
            date_label = str(pr_date or "")
        out.append(
            {
                "pr_type": str(row.get("PR_TYPE") or "").strip(),
                "pr_no": str(row.get("PR_NO") or "").strip(),
                "pr_ser": str(row.get("PR_SER") or "").strip(),
                "date_label": date_label,
                "branch_code": branch,
                "branch_name": names.get(branch) or branch,
                "vendor_code": str(row.get("VENDOR_CODE") or "").strip(),
                "user_code": str(row.get("USER_CODE") or "").strip(),
                "user_name": str(row.get("USER_NAME") or "").strip(),
                "item_count": int(row.get("ITEM_COUNT") or 0),
                "qty_total": float(row.get("QTY_TOTAL") or 0),
                "qty_display": _qty(row.get("QTY_TOTAL") or 0),
            }
        )
    return out


def _fetch_request_header(pr_type: str, pr_no: str, pr_ser: str) -> dict | None:
    schema = _schema()
    names = _branch_names()
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(p.PR_TYPE) AS PR_TYPE,
               TO_CHAR(p.PR_NO) AS PR_NO,
               TO_CHAR(p.PR_SER) AS PR_SER,
               p.PR_DATE AS PR_DATE,
               TO_CHAR(p.BRN_NO) AS BRANCH_CODE,
               TO_CHAR(p.V_CODE) AS VENDOR_CODE,
               TO_CHAR(p.AD_U_ID) AS USER_CODE,
               NVL(u.U_A_NAME, NVL(u.U_E_NAME, TO_CHAR(p.AD_U_ID))) AS USER_NAME
        FROM {schema}.P_REQUEST p
        LEFT JOIN {schema}.USER_R u ON u.U_ID = p.AD_U_ID
        WHERE TO_CHAR(p.PR_TYPE) = :pr_type
          AND TO_CHAR(p.PR_NO) = :pr_no
          AND TO_CHAR(p.PR_SER) = :pr_ser
          AND NVL(p.INACTIVE, 0) = 0
        FETCH FIRST 1 ROWS ONLY
        """,
        {"pr_type": pr_type, "pr_no": pr_no, "pr_ser": pr_ser},
    )
    if not rows:
        return None
    row = rows[0]
    branch = str(row.get("BRANCH_CODE") or "").strip()
    pr_date = row.get("PR_DATE")
    if isinstance(pr_date, datetime):
        date_label = pr_date.strftime("%Y-%m-%d %H:%M")
    elif isinstance(pr_date, date):
        date_label = pr_date.isoformat()
    else:
        date_label = str(pr_date or "")
    return {
        "pr_type": str(row.get("PR_TYPE") or "").strip(),
        "pr_no": str(row.get("PR_NO") or "").strip(),
        "pr_ser": str(row.get("PR_SER") or "").strip(),
        "date_label": date_label,
        "branch_code": branch,
        "branch_name": names.get(branch) or branch,
        "vendor_code": str(row.get("VENDOR_CODE") or "").strip(),
        "user_code": str(row.get("USER_CODE") or "").strip(),
        "user_name": str(row.get("USER_NAME") or "").strip(),
    }


def _fetch_request_items(pr_type: str, pr_no: str, pr_ser: str) -> list[dict]:
    schema = _schema()
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS ITEM_CODE,
               MAX(NVL(NULLIF(TRIM(i.I_NAME), ''), TO_CHAR(d.I_CODE))) AS ITEM_NAME,
               ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS REQ_QTY
        FROM {schema}.P_REQUEST_DETAIL d
        LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE TO_CHAR(d.PR_TYPE) = :pr_type
          AND TO_CHAR(d.PR_NO) = :pr_no
          AND TO_CHAR(d.PR_SER) = :pr_ser
        GROUP BY TO_CHAR(d.I_CODE)
        HAVING ROUND(SUM(NVL(d.I_QTY, 0)), 2) <> 0
        ORDER BY MAX(NVL(NULLIF(TRIM(i.I_NAME), ''), TO_CHAR(d.I_CODE)))
        """,
        {"pr_type": pr_type, "pr_no": pr_no, "pr_ser": pr_ser},
    )
    return [
        {
            "code": str(row.get("ITEM_CODE") or "").strip(),
            "name": str(row.get("ITEM_NAME") or "").strip()
            or str(row.get("ITEM_CODE") or "").strip(),
            "req_qty": float(row.get("REQ_QTY") or 0),
            "req_display": _qty(row.get("REQ_QTY") or 0),
        }
        for row in rows
        if str(row.get("ITEM_CODE") or "").strip()
    ]


def _fetch_stock_for_items(
    item_codes: list[str],
    *,
    warehouse_codes: list[str] | None = None,
) -> dict[str, list[dict]]:
    """أرصدة موجبة فقط لكل صنف — يتجاهل المخازن بلا كمية."""
    codes = [str(c).strip() for c in item_codes if str(c).strip()]
    if not codes:
        return {}

    schema = _schema()
    params: dict[str, Any] = {}
    code_keys = []
    for i, code in enumerate(codes):
        key = f"c{i}"
        code_keys.append(f":{key}")
        params[key] = code

    wh_filter = ""
    wh_list = [str(w).strip() for w in (warehouse_codes or []) if str(w).strip()]
    if wh_list:
        wh_keys = []
        for i, wh in enumerate(wh_list):
            key = f"w{i}"
            wh_keys.append(f":{key}")
            params[key] = wh
        wh_filter = f"AND TO_CHAR(w.W_CODE) IN ({', '.join(wh_keys)})"

    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(w.I_CODE) AS ITEM_CODE,
               TO_CHAR(w.W_CODE) AS WAREHOUSE_CODE,
               MAX(NVL(wh.W_NAME, TO_CHAR(w.W_CODE))) AS WAREHOUSE_NAME,
               TO_CHAR(MAX(wh.CONN_BRN_NO)) AS BRANCH_CODE,
               ROUND(SUM(NVL(w.AVL_QTY, 0)), 2) AS QTY
        FROM {schema}.IAS_ITM_WCODE w
        LEFT JOIN {schema}.WAREHOUSE_DETAILS wh
          ON TO_CHAR(wh.W_CODE) = TO_CHAR(w.W_CODE)
        WHERE TO_CHAR(w.I_CODE) IN ({', '.join(code_keys)})
          AND NVL(w.AVL_QTY, 0) > 0
          {wh_filter}
        GROUP BY TO_CHAR(w.I_CODE), TO_CHAR(w.W_CODE)
        HAVING ROUND(SUM(NVL(w.AVL_QTY, 0)), 2) > 0
        ORDER BY TO_CHAR(w.I_CODE), SUM(NVL(w.AVL_QTY, 0)) DESC
        """,
        params,
    )

    names = _branch_names()
    by_item: dict[str, list[dict]] = {code: [] for code in codes}
    for row in rows:
        code = str(row.get("ITEM_CODE") or "").strip()
        wh = str(row.get("WAREHOUSE_CODE") or "").strip()
        if not code or not wh:
            continue
        qty = float(row.get("QTY") or 0)
        branch = str(row.get("BRANCH_CODE") or "").strip()
        by_item.setdefault(code, []).append(
            {
                "warehouse_code": wh,
                "warehouse_name": str(row.get("WAREHOUSE_NAME") or "").strip() or wh,
                "branch_code": branch,
                "branch_name": names.get(branch) or branch or "—",
                "qty": qty,
                "qty_display": _qty(qty),
            }
        )
    return by_item


def build_purchase_request_compare(
    *,
    pr_type: str,
    pr_no: str,
    pr_ser: str,
    warehouse_codes: list[str] | None = None,
) -> dict[str, Any] | None:
    """
    مقارنة أصناف طلب شراء مع المخازن ذات الرصيد فقط.
    لا يُرجع مخزناً بلا كمية.
    """
    if not oracle_enabled():
        return None
    pr_type = str(pr_type or "").strip()
    pr_no = str(pr_no or "").strip()
    pr_ser = str(pr_ser or "").strip()
    if not (pr_type and pr_no and pr_ser):
        return None

    header = _fetch_request_header(pr_type, pr_no, pr_ser)
    if not header:
        return None

    items = _fetch_request_items(pr_type, pr_no, pr_ser)
    stock_map = _fetch_stock_for_items(
        [row["code"] for row in items],
        warehouse_codes=warehouse_codes,
    )

    compare_items: list[dict] = []
    warehouse_map: dict[str, dict] = {}
    for item in items:
        stock_rows = stock_map.get(item["code"]) or []
        stock_by_wh: dict[str, dict] = {}
        for row in stock_rows:
            wh_code = row["warehouse_code"]
            stock_by_wh[wh_code] = {
                "qty": row["qty"],
                "qty_display": row["qty_display"],
            }
            if wh_code not in warehouse_map:
                warehouse_map[wh_code] = {
                    "code": wh_code,
                    "name": row["warehouse_name"],
                    "branch_name": row["branch_name"],
                }
        compare_items.append(
            {
                **item,
                "stock_count": len(stock_rows),
                "has_stock": bool(stock_rows),
                "stock_total": round(sum(float(r["qty"]) for r in stock_rows), 2),
                "stock_total_display": _qty(
                    sum(float(r["qty"]) for r in stock_rows)
                ),
                "stock_by_wh": stock_by_wh,
            }
        )

    warehouses = sorted(
        warehouse_map.values(),
        key=lambda row: (row["branch_name"], row["name"], row["code"]),
    )
    for item in compare_items:
        item["cells"] = [
            item["stock_by_wh"].get(wh["code"]) for wh in warehouses
        ]

    return {
        "header": header,
        "items": compare_items,
        "warehouses": warehouses,
        "item_count": len(compare_items),
        "warehouse_count": len(warehouses),
        "with_stock_count": sum(1 for row in compare_items if row["has_stock"]),
        "without_stock_count": sum(1 for row in compare_items if not row["has_stock"]),
    }
