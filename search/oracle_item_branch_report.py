"""تقرير الأصناف حسب الفرع/المخزن: الرصيد الحالي + إجمالي المبيعات + المرتجعات + الصافي.

- الرصيد: حالي (لا يعتمد على التاريخ) من IAS_ITM_WCODE للمخازن المختارة.
- المبيعات: نقاط البيع غير المعلّقة + فواتير أونكس (أنواع 1 و5)، مجمّعة حسب المخزن.
- المرتجعات: مرتجعات نقاط البيع + مرتجعات الفواتير (أنواع 1 و5)، بنفس الأساس.
- الأساس: نقاط البيع بـ I_PRICE_VAT، والفواتير بـ I_PRICE (كما في شاشات التطبيق الحالية).
SELECT فقط.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.cache import cache

from .oracle_stock import (
    _ONIX_ITEM_SALES_DOC_TYPES,
    OracleStockError,
    _as_date,
    _bill_mst_ok,
    _bind_gcode,
    _date_params,
    _doc_type_filter,
    _fetch_all,
    _hung_ok,
    _norm_brn_code,
    _pos_dtl_net_sql,
    _pos_owner,
    _rt_bill_mst_ok,
    _schema,
    _sales_cache_set,
    fetch_item_max_pack_map,
    fetch_warehouse_options,
    oracle_enabled,
    oracle_session,
)
from .oracle_wh_qty_compare import _fmt_qty, _hydrate_items, _norm_code, _to_pack_qty

logger = logging.getLogger(__name__)

_SALES_TTL = 60 * 60 * 2  # ساعتان للمبيعات (الرصيد حي دائماً)
_CACHE_VER = "v4"


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _group_filters(params: dict, group_code: str, subgroup_code: str, alias: str) -> str:
    """شرط المجموعة/الفرعية على IAS_ITM_MST المستعار بـ i."""
    parts = []
    gcode = _bind_gcode(group_code) if group_code else None
    if gcode not in ("", None):
        params["gcode"] = gcode
        parts.append(f"AND {alias}.G_CODE = :gcode")
    if subgroup_code:
        params["mng"] = subgroup_code
        parts.append(f"AND TO_CHAR({alias}.MNG_CODE) = :mng")
    return " ".join(parts)


def _warehouses() -> tuple[list[dict[str, str]], dict[str, str]]:
    """المخازن النشطة [{code, name, branch_code}] وأسماء الفروع."""
    out: list[dict[str, str]] = []
    branch_names: dict[str, str] = {}
    for w in fetch_warehouse_options(active_only=True) or []:
        code = _norm_code(w.get("code"))
        if not code:
            continue
        brn = _norm_brn_code(w.get("branch_code"))
        if brn:
            branch_names[brn] = str(w.get("branch_name") or brn).strip() or brn
        out.append(
            {
                "code": code,
                "name": str(w.get("name") or code).strip() or code,
                "branch_code": brn,
            }
        )
    return out, branch_names


def _fetch_stock(
    wh_codes: list[str], group_code: str, subgroup_code: str
) -> dict[tuple[str, str], float]:
    """{(صنف, مخزن): رصيد} من IAS_ITM_WCODE."""
    if not wh_codes:
        return {}
    schema = _schema()
    params: dict[str, Any] = {}
    keys = []
    for i, wh in enumerate(wh_codes):
        params[f"w{i}"] = wh
        keys.append(f":w{i}")
    group_sql = _group_filters(params, group_code, subgroup_code, "i")
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(w.I_CODE) AS I_CODE,
               TO_CHAR(w.W_CODE) AS W_CODE,
               ROUND(SUM(NVL(w.AVL_QTY, 0)), 4) AS QTY
        FROM {schema}.IAS_ITM_WCODE w
        JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = w.I_CODE
        WHERE TO_CHAR(w.W_CODE) IN ({", ".join(keys)})
          AND w.I_CODE IS NOT NULL
          {group_sql}
        GROUP BY w.I_CODE, w.W_CODE
        """,
        params,
    )
    out: dict[tuple[str, str], float] = {}
    for row in rows or []:
        code = _norm_code(row.get("I_CODE"))
        wh = _norm_code(row.get("W_CODE"))
        if code and wh:
            out[(code, wh)] = out.get((code, wh), 0.0) + _f(row.get("QTY"))
    return out


def _fetch_pos_sales(date_from, date_to, group_code: str, subgroup_code: str) -> list[dict]:
    pos = _pos_owner()
    schema = _schema()
    params: dict[str, Any] = _date_params(date_from, date_to)
    group_sql = _group_filters(params, group_code, subgroup_code, "i")
    hung_m = _hung_ok("m")
    return _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS I_CODE,
               TO_CHAR(NVL(d.W_CODE, m.W_CODE)) AS W_CODE,
               ROUND(SUM(NVL(d.I_QTY, 0)), 4) AS QTY_TOTAL,
               ROUND(SUM({_pos_dtl_net_sql("d")}), 2) AS NET_TOTAL
        FROM {pos}.IAS_POS_BILL_DTL d
        JOIN {pos}.IAS_POS_BILL_MST m
          ON m.BILL_NO = d.BILL_NO
         AND m.BRN_NO = d.BRN_NO
         AND NVL(m.BILL_SRL, 0) = NVL(d.BILL_SRL, 0)
        JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
          AND {hung_m}
          AND d.I_CODE IS NOT NULL
          {group_sql}
        GROUP BY d.I_CODE, NVL(d.W_CODE, m.W_CODE)
        """,
        params,
    )


def _fetch_bill_sales(date_from, date_to, group_code: str, subgroup_code: str) -> list[dict]:
    schema = _schema()
    params: dict[str, Any] = _date_params(date_from, date_to)
    conf = {"doc_types": _ONIX_ITEM_SALES_DOC_TYPES}
    doc_filter = _doc_type_filter(conf, "b", "BILL_DOC_TYPE", params)
    group_sql = _group_filters(params, group_code, subgroup_code, "i")
    return _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS I_CODE,
               TO_CHAR(NVL(d.W_CODE, b.W_CODE)) AS W_CODE,
               ROUND(SUM(NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(NULLIF(d.P_SIZE, 0), 1))), 4) AS QTY_TOTAL,
               ROUND(SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)), 2) AS NET_TOTAL
        FROM {schema}.IAS_BILL_DTL d
        JOIN {schema}.IAS_BILL_MST b
          ON b.BILL_NO = d.BILL_NO
         AND b.BILL_SER = d.BILL_SER
         AND b.BILL_DOC_TYPE = d.BILL_DOC_TYPE
        JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
          AND {_bill_mst_ok("b")}
          {doc_filter}
          AND d.I_CODE IS NOT NULL
          {group_sql}
        GROUP BY d.I_CODE, NVL(d.W_CODE, b.W_CODE)
        """,
        params,
    )


def _fetch_pos_returns(date_from, date_to, group_code: str, subgroup_code: str) -> list[dict]:
    """مرتجعات نقاط البيع بـ I_PRICE_VAT (نفس أساس مبيعات نقاط البيع)."""
    pos = _pos_owner()
    schema = _schema()
    params: dict[str, Any] = _date_params(date_from, date_to)
    group_sql = _group_filters(params, group_code, subgroup_code, "i")
    return _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS I_CODE,
               TO_CHAR(NVL(d.W_CODE, m.W_CODE)) AS W_CODE,
               ROUND(SUM({_pos_dtl_net_sql("d")}), 2) AS RET_NET
        FROM {pos}.IAS_POS_RT_BILL_DTL d
        JOIN {pos}.IAS_POS_RT_BILL_MST m
          ON m.RT_BILL_NO = d.RT_BILL_NO
         AND m.BRN_NO = d.BRN_NO
        JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE m.RT_BILL_DATE >= :d_from AND m.RT_BILL_DATE < :d_to_excl
          AND NVL(m.HUNG, 0) = 0
          AND d.I_CODE IS NOT NULL
          {group_sql}
        GROUP BY d.I_CODE, NVL(d.W_CODE, m.W_CODE)
        """,
        params,
    )


def _fetch_bill_returns(date_from, date_to, group_code: str, subgroup_code: str) -> list[dict]:
    """مرتجعات الفواتير بـ I_PRICE (نفس أساس مبيعات الفواتير)."""
    schema = _schema()
    params: dict[str, Any] = _date_params(date_from, date_to)
    conf = {"doc_types": _ONIX_ITEM_SALES_DOC_TYPES}
    ret_doc = _doc_type_filter(conf, "r", "RT_BILL_DOC_TYPE", params)
    group_sql = _group_filters(params, group_code, subgroup_code, "i")
    return _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS I_CODE,
               TO_CHAR(NVL(d.W_CODE, r.W_CODE)) AS W_CODE,
               ROUND(SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)), 2) AS RET_NET
        FROM {schema}.IAS_RT_BILL_DTL d
        JOIN {schema}.IAS_RT_BILL_MST r
          ON r.RT_BILL_SER = d.RT_BILL_SER
         AND r.BRN_NO = d.BRN_NO
        JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
          AND {_rt_bill_mst_ok("r")}
          {ret_doc}
          AND d.I_CODE IS NOT NULL
          {group_sql}
        GROUP BY d.I_CODE, NVL(d.W_CODE, r.W_CODE)
        """,
        params,
    )


def _sales_by_warehouse(
    date_from, date_to, group_code: str, subgroup_code: str
) -> dict[tuple[str, str], dict[str, float]]:
    """{(صنف, مخزن): {qty, pos, bill, returns}} — مخزّن مؤقتاً، مستقل عن اختيار الفرع/المخزن."""
    key = (
        f"itmwh:{_CACHE_VER}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:{group_code}:{subgroup_code}"
    )
    cached = cache.get(key)
    if cached is not None:
        return cached
    out: dict[tuple[str, str], dict[str, float]] = {}

    def _add(rows: list[dict], field: str, value_key: str, qty: bool = False) -> None:
        for row in rows or []:
            code = _norm_code(row.get("I_CODE"))
            wh = _norm_code(row.get("W_CODE"))
            if not code or not wh:
                continue
            cur = out.setdefault(
                (code, wh), {"qty": 0.0, "pos": 0.0, "bill": 0.0, "returns": 0.0}
            )
            cur[field] += _f(row.get(value_key))
            if qty:
                cur["qty"] += _f(row.get("QTY_TOTAL"))

    _add(_fetch_pos_sales(date_from, date_to, group_code, subgroup_code), "pos", "NET_TOTAL", qty=True)
    _add(_fetch_bill_sales(date_from, date_to, group_code, subgroup_code), "bill", "NET_TOTAL", qty=True)
    _add(_fetch_pos_returns(date_from, date_to, group_code, subgroup_code), "returns", "RET_NET")
    _add(_fetch_bill_returns(date_from, date_to, group_code, subgroup_code), "returns", "RET_NET")
    _sales_cache_set(key, out, _SALES_TTL, keep_stale=False)
    return out


def build_item_branch_report(
    *,
    group_code: str = "",
    subgroup_code: str = "",
    date_from=None,
    date_to=None,
    branch_code: str = "",
    warehouse_code: str = "",
    item_q: str = "",
) -> dict[str, Any]:
    """صفوف الأصناف للمخازن المختارة: رصيد، إجمالي مبيعات، مرتجعات، صافي.

    مخزن محدد ← مخزنه فقط؛ فرع ← كل مخازن الفرع؛ بلا اختيار ← كل المخازن.
    """
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    gcode = _norm_code(group_code)
    mcode = _norm_code(subgroup_code) if gcode else ""
    brn_sel = _norm_brn_code(branch_code) if branch_code else ""
    wh_sel = _norm_code(warehouse_code) if warehouse_code else ""
    needle = str(item_q or "").strip().lower()

    with oracle_session():
        all_wh, branch_names = _warehouses()
        if wh_sel:
            selected = [w for w in all_wh if w["code"] == wh_sel]
        elif brn_sel:
            selected = [w for w in all_wh if w["branch_code"] == brn_sel]
        else:
            selected = list(all_wh)
        sel_codes = {w["code"] for w in selected}
        wh_by_code = {w["code"]: w for w in selected}

        stock = _fetch_stock(sorted(sel_codes), gcode, mcode)
        sales = _sales_by_warehouse(date_from, date_to, gcode, mcode)

        per_item: dict[str, dict[str, float]] = {}

        def _acc(code: str) -> dict[str, float]:
            return per_item.setdefault(
                code, {"stock": 0.0, "qty": 0.0, "pos": 0.0, "bill": 0.0, "returns": 0.0}
            )

        for (code, wh), qty in stock.items():
            if wh in sel_codes:
                _acc(code)["stock"] += qty
        for (code, wh), v in sales.items():
            if wh not in sel_codes:
                continue
            a = _acc(code)
            for k in ("qty", "pos", "bill", "returns"):
                a[k] += v[k]

        meta = _hydrate_items(sorted(per_item)) if per_item else {}
        pack_map = fetch_item_max_pack_map(sorted(per_item)) if per_item else {}

    rows: list[dict[str, Any]] = []
    tot = {"stock": 0.0, "qty": 0.0, "pos": 0.0, "bill": 0.0, "returns": 0.0}
    for code, a in per_item.items():
        info = meta.get(code) or {}
        name = info.get("item_name") or code
        if needle and needle not in code.lower() and needle not in name.lower():
            continue
        if all(abs(v) <= 1e-9 for v in a.values()):
            continue
        pack = _f((pack_map.get(code) or {}).get("pack")) or 1.0
        unit = str((pack_map.get(code) or {}).get("unit") or "").strip() or "—"
        gross = a["pos"] + a["bill"]
        net = gross - a["returns"]
        for k in tot:
            tot[k] += a[k]
        rows.append(
            {
                "item_code": code,
                "item_name": name,
                "g_name": info.get("g_name") or "",
                "unit": unit,
                "stock_display": _fmt_qty(_to_pack_qty(a["stock"], pack)),
                "gross_display": _fmt_qty(gross),
                "returns_display": _fmt_qty(a["returns"]),
                "net_display": _fmt_qty(net),
                "net": net,
            }
        )

    rows.sort(key=lambda r: (-r["net"], r["item_name"], r["item_code"]))
    gross_total = tot["pos"] + tot["bill"]
    if wh_sel and wh_sel in wh_by_code:
        scope_label = wh_by_code[wh_sel]["name"]
    elif brn_sel:
        scope_label = branch_names.get(brn_sel, brn_sel)
    else:
        scope_label = "كل الفروع وكل المخازن"
    return {
        "scope_label": scope_label,
        "rows": rows,
        "item_count": len(rows),
        "stock_total_display": _fmt_qty(tot["stock"]),
        "gross_total_display": _fmt_qty(gross_total),
        "returns_total_display": _fmt_qty(tot["returns"]),
        "net_total_display": _fmt_qty(gross_total - tot["returns"]),
        "pos_total_display": _fmt_qty(tot["pos"]),
        "bill_total_display": _fmt_qty(tot["bill"]),
        "from": _as_date(date_from).isoformat() if date_from else "",
        "to": _as_date(date_to).isoformat() if date_to else "",
    }
