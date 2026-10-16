"""تقرير الأصناف حسب الفرع: الرصيد الحالي + مبيعات فترة (نقاط البيع + فواتير).

- الرصيد: حالي (لا يعتمد على التاريخ) من IAS_ITM_WCODE، مجموع مخازن كل فرع.
- المبيعات: صافي الفترة لكل فرع، نقاط البيع غير المعلّقة + فواتير أونكس (أنواع 1 و5).
  المرتجعات غير مخصومة في هذا الإصدار.
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
    _rt_bill_mst_ok,
    _bind_gcode,
    _date_params,
    _doc_type_filter,
    _fetch_all,
    _hung_ok,
    _norm_brn_code,
    _pos_dtl_net_sql,
    _pos_owner,
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
_CACHE_VER = "v3"


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


def _warehouses_by_branch() -> tuple[dict[str, list[str]], dict[str, str], dict[str, str]]:
    """{فرع: [مخازن]}، أسماء الفروع، أسماء المخازن."""
    by_branch: dict[str, list[str]] = {}
    branch_names: dict[str, str] = {}
    wh_names: dict[str, str] = {}
    for w in fetch_warehouse_options(active_only=True) or []:
        code = str(w.get("code") or "").strip()
        brn = _norm_brn_code(w.get("branch_code"))
        if not code or not brn:
            continue
        by_branch.setdefault(brn, []).append(code)
        wh_names[code] = str(w.get("name") or code).strip()
        branch_names[brn] = str(w.get("branch_name") or brn).strip() or brn
    return by_branch, branch_names, wh_names


def _stock_by_branch(
    by_branch: dict[str, list[str]], group_code: str, subgroup_code: str
) -> dict[tuple[str, str], float]:
    wh_to_branch = {wh: brn for brn, whs in by_branch.items() for wh in whs}
    wh_codes = sorted(wh_to_branch)
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
        brn = wh_to_branch.get(_norm_code(row.get("W_CODE")))
        if not code or not brn:
            continue
        key = (code, brn)
        out[key] = out.get(key, 0.0) + _f(row.get("QTY"))
    return out


def _fetch_pos_sales(
    date_from, date_to, group_code: str, subgroup_code: str
) -> list[dict]:
    pos = _pos_owner()
    schema = _schema()
    params: dict[str, Any] = _date_params(date_from, date_to)
    group_sql = _group_filters(params, group_code, subgroup_code, "i")
    hung_m = _hung_ok("m")
    return _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS I_CODE,
               TO_CHAR(m.BRN_NO) AS BRANCH_CODE,
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
        GROUP BY d.I_CODE, m.BRN_NO
        """,
        params,
    )


def _fetch_bill_sales(
    date_from, date_to, group_code: str, subgroup_code: str
) -> list[dict]:
    schema = _schema()
    params: dict[str, Any] = _date_params(date_from, date_to)
    conf = {"doc_types": _ONIX_ITEM_SALES_DOC_TYPES}
    doc_filter = _doc_type_filter(conf, "b", "BILL_DOC_TYPE", params)
    group_sql = _group_filters(params, group_code, subgroup_code, "i")
    return _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS I_CODE,
               TO_CHAR(b.BRN_NO) AS BRANCH_CODE,
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
        GROUP BY d.I_CODE, b.BRN_NO
        """,
        params,
    )


def _fetch_pos_returns(
    date_from, date_to, group_code: str, subgroup_code: str
) -> list[dict]:
    """مرتجعات نقاط البيع بـ I_PRICE_VAT (نفس أساس مبيعات نقاط البيع)."""
    pos = _pos_owner()
    schema = _schema()
    params: dict[str, Any] = _date_params(date_from, date_to)
    group_sql = _group_filters(params, group_code, subgroup_code, "i")
    return _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS I_CODE,
               TO_CHAR(m.BRN_NO) AS BRANCH_CODE,
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
        GROUP BY d.I_CODE, m.BRN_NO
        """,
        params,
    )


def _fetch_bill_returns(
    date_from, date_to, group_code: str, subgroup_code: str
) -> list[dict]:
    """مرتجعات الفواتير بـ I_PRICE (نفس أساس مبيعات الفواتير)."""
    schema = _schema()
    params: dict[str, Any] = _date_params(date_from, date_to)
    conf = {"doc_types": _ONIX_ITEM_SALES_DOC_TYPES}
    ret_doc = _doc_type_filter(conf, "r", "RT_BILL_DOC_TYPE", params)
    group_sql = _group_filters(params, group_code, subgroup_code, "i")
    return _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS I_CODE,
               TO_CHAR(r.BRN_NO) AS BRANCH_CODE,
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
        GROUP BY d.I_CODE, r.BRN_NO
        """,
        params,
    )


def _sales_by_branch(
    date_from, date_to, group_code: str, subgroup_code: str
) -> dict[tuple[str, str], dict[str, float]]:
    """{(صنف, فرع): {qty, gross, returns}} — المبيعات والمرتجعات، مخزّن مؤقتاً.

    gross: إجمالي المبيعات (نقاط البيع + فواتير) قبل المرتجع.
    returns: المرتجعات (نقاط البيع + فواتير) بنفس الأساس."""
    key = (
        f"itmbr:{_CACHE_VER}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:{group_code}:{subgroup_code}"
    )
    cached = cache.get(key)
    if cached is not None:
        return cached
    out: dict[tuple[str, str], dict[str, float]] = {}

    def _add(rows: list[dict], field: str, qty_field: str | None = None, value_key: str = "NET_TOTAL") -> None:
        for row in rows or []:
            code = _norm_code(row.get("I_CODE"))
            brn = _norm_brn_code(row.get("BRANCH_CODE"))
            if not code or not brn:
                continue
            cur = out.setdefault(
                (code, brn),
                {"qty": 0.0, "pos_gross": 0.0, "bill_gross": 0.0, "returns": 0.0},
            )
            cur[field] += _f(row.get(value_key))
            if qty_field:
                cur["qty"] += _f(row.get(qty_field))

    _add(_fetch_pos_sales(date_from, date_to, group_code, subgroup_code), "pos_gross", "QTY_TOTAL")
    _add(_fetch_bill_sales(date_from, date_to, group_code, subgroup_code), "bill_gross", "QTY_TOTAL")
    _add(_fetch_pos_returns(date_from, date_to, group_code, subgroup_code), "returns", value_key="RET_NET")
    _add(_fetch_bill_returns(date_from, date_to, group_code, subgroup_code), "returns", value_key="RET_NET")
    _sales_cache_set(key, out, _SALES_TTL, keep_stale=False)
    return out


def build_item_branch_report(
    *,
    group_code: str = "",
    subgroup_code: str = "",
    date_from=None,
    date_to=None,
    branch_code: str = "",
    item_q: str = "",
) -> dict[str, Any]:
    """يرجع: branches (أعمدة)، rows (صنف + لكل فرع رصيد/كمية مبيعات/صافي)، totals."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    gcode = _norm_code(group_code)
    mcode = _norm_code(subgroup_code) if gcode else ""
    brn_sel = _norm_brn_code(branch_code) if branch_code else ""
    needle = str(item_q or "").strip().lower()

    with oracle_session():
        by_branch, branch_names, wh_names = _warehouses_by_branch()
        if brn_sel:
            by_branch = {k: v for k, v in by_branch.items() if k == brn_sel}
        branches = [
            {"code": b, "name": branch_names.get(b, b)}
            for b in sorted(by_branch, key=lambda x: (branch_names.get(x, x), x))
        ]
        stock = _stock_by_branch(by_branch, gcode, mcode)
        sales = _sales_by_branch(date_from, date_to, gcode, mcode)

        codes = {c for c, _ in stock} | {c for c, _ in sales}
        meta = _hydrate_items(sorted(codes)) if codes else {}
        pack_map = fetch_item_max_pack_map(sorted(codes)) if codes else {}

    rows: list[dict[str, Any]] = []
    empty_sales = {"qty": 0.0, "pos_gross": 0.0, "bill_gross": 0.0, "returns": 0.0}
    totals = {
        b["code"]: {
            "stock": 0.0,
            "qty": 0.0,
            "pos": 0.0,
            "bill": 0.0,
            "gross": 0.0,
            "returns": 0.0,
            "net": 0.0,
        }
        for b in branches
    }
    for code in codes:
        info = meta.get(code) or {}
        name = info.get("item_name") or code
        if needle and needle not in code.lower() and needle not in name.lower():
            continue
        pack = _f((pack_map.get(code) or {}).get("pack")) or 1.0
        unit = str((pack_map.get(code) or {}).get("unit") or "").strip() or "—"
        cells = []
        has_value = False
        for b in branches:
            brn = b["code"]
            st = stock.get((code, brn), 0.0)
            sl = sales.get((code, brn), empty_sales)
            pos_gross = sl["pos_gross"]
            bill_gross = sl["bill_gross"]
            gross = pos_gross + bill_gross
            returns = sl["returns"]
            net = gross - returns
            if any(abs(v) > 1e-9 for v in (st, sl["qty"], gross, returns)):
                has_value = True
            t = totals[brn]
            t["stock"] += st
            t["qty"] += sl["qty"]
            t["pos"] += pos_gross
            t["bill"] += bill_gross
            t["gross"] += gross
            t["returns"] += returns
            t["net"] += net
            cells.append(
                {
                    "stock_display": _fmt_qty(_to_pack_qty(st, pack)),
                    "qty_display": _fmt_qty(_to_pack_qty(sl["qty"], pack)),
                    "pos_display": _fmt_qty(pos_gross),
                    "bill_display": _fmt_qty(bill_gross),
                    "gross_display": _fmt_qty(gross),
                    "returns_display": _fmt_qty(returns),
                    "net_display": _fmt_qty(net),
                    "net": net,
                }
            )
        if not has_value:
            continue
        rows.append(
            {
                "item_code": code,
                "item_name": name,
                "g_code": info.get("g_code") or "",
                "g_name": info.get("g_name") or "",
                "unit": unit,
                "cells": cells,
                "net_total": sum(c["net"] for c in cells),
            }
        )

    rows.sort(key=lambda r: (-r["net_total"], r["item_name"], r["item_code"]))
    for b in branches:
        t = totals[b["code"]]
        b["stock_display"] = _fmt_qty(t["stock"])
        b["qty_display"] = _fmt_qty(t["qty"])
        b["pos_display"] = _fmt_qty(t["pos"])
        b["bill_display"] = _fmt_qty(t["bill"])
        b["gross_display"] = _fmt_qty(t["gross"])
        b["returns_display"] = _fmt_qty(t["returns"])
        b["net_display"] = _fmt_qty(t["net"])
    return {
        "branches": branches,
        "rows": rows,
        "item_count": len(rows),
        "net_total_display": _fmt_qty(sum(r["net_total"] for r in rows)),
        "pos_total_display": _fmt_qty(sum(t["pos"] for t in totals.values())),
        "bill_total_display": _fmt_qty(sum(t["bill"] for t in totals.values())),
        "gross_total_display": _fmt_qty(sum(t["gross"] for t in totals.values())),
        "from": _as_date(date_from).isoformat() if date_from else "",
        "to": _as_date(date_to).isoformat() if date_to else "",
        "wh_names": wh_names,
    }
