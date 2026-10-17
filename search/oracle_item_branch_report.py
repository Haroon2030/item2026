"""تقرير الأصناف حسب الفرع/المخزن: الرصيد الحالي + إجمالي المبيعات + المرتجعات + الصافي.

- الرصيد: حالي (لا يعتمد على التاريخ) من IAS_ITM_WCODE للمخازن المختارة.
- المبيعات: نقاط البيع غير المعلّقة + فواتير أونكس (أنواع 1 و5)، مجمّعة حسب المخزن.
- المرتجعات: مرتجعات نقاط البيع + مرتجعات الفواتير (أنواع 1 و5)، بنفس الأساس.
- الأساس: شامل الضريبة للمصدرين (I_PRICE_VAT × الكمية − الخصم)، كما في «صافي المبيعات» بتقرير أونكس.
SELECT فقط.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.cache import cache
from django.http import HttpResponse

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
_WHOLESALE_DOC_TYPES = (4, 8)  # الآجل (الجملة) كما في تحليل المبيعات
_CACHE_VER = "v7"


def _bill_net_sql(alias: str = "d") -> str:
    """صافي بند فاتورة أونكس شامل الضريبة: I_PRICE_VAT × الكمية − الخصم.

    يطابق عمود «صافي المبيعات» في تقرير أونكس. إن كان I_PRICE_VAT فارغاً/صفراً (صفوف قليلة)
    يُستعاض عنه بـ I_PRICE × الكمية + VAT_AMT."""
    a = alias
    return (
        f"(CASE WHEN NVL({a}.I_PRICE_VAT, 0) = 0 "
        f"THEN NVL({a}.I_PRICE, 0) * NVL({a}.I_QTY, 0) + NVL({a}.VAT_AMT, 0) "
        f"ELSE {a}.I_PRICE_VAT * NVL({a}.I_QTY, 0) END - NVL({a}.DIS_AMT, 0))"
    )


def _pct_display(sold: float, stock: float) -> str:
    """نسبة كمية المبيعات من الرصيد الحالي (%)؛ «—» إن لم يوجد رصيد."""
    if stock <= 1e-9:
        return "—"
    return f"{sold / stock * 100:,.1f}%"


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


def _fetch_bill_sales(
    date_from, date_to, group_code: str, subgroup_code: str, doc_types: tuple = _ONIX_ITEM_SALES_DOC_TYPES
) -> list[dict]:
    schema = _schema()
    params: dict[str, Any] = _date_params(date_from, date_to)
    conf = {"doc_types": doc_types}
    doc_filter = _doc_type_filter(conf, "b", "BILL_DOC_TYPE", params)
    group_sql = _group_filters(params, group_code, subgroup_code, "i")
    return _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS I_CODE,
               TO_CHAR(NVL(d.W_CODE, b.W_CODE)) AS W_CODE,
               ROUND(SUM(NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(NULLIF(d.P_SIZE, 0), 1))), 4) AS QTY_TOTAL,
               ROUND(SUM({_bill_net_sql("d")}), 2) AS NET_TOTAL
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


def _fetch_bill_returns(
    date_from, date_to, group_code: str, subgroup_code: str, doc_types: tuple = _ONIX_ITEM_SALES_DOC_TYPES
) -> list[dict]:
    """مرتجعات الفواتير بنفس أساس مبيعات الفواتير (شامل الضريبة)."""
    schema = _schema()
    params: dict[str, Any] = _date_params(date_from, date_to)
    conf = {"doc_types": doc_types}
    ret_doc = _doc_type_filter(conf, "r", "RT_BILL_DOC_TYPE", params)
    group_sql = _group_filters(params, group_code, subgroup_code, "i")
    return _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS I_CODE,
               TO_CHAR(NVL(d.W_CODE, r.W_CODE)) AS W_CODE,
               ROUND(SUM({_bill_net_sql("d")}), 2) AS RET_NET
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
    """{(صنف, مخزن): {...}} — نقاط البيع، نقدي 1/5، آجل 4/8، ومرتجعاتها؛ مخزّن مؤقتاً.

    مستقل عن اختيار الفرع/المخزن. نقدي 1/5 غالباً انعكاس لنقاط البيع، فلا يُعرض كاملاً."""
    key = (
        f"itmwh:{_CACHE_VER}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:{group_code}:{subgroup_code}"
    )
    cached = cache.get(key)
    if cached is not None:
        return cached
    out: dict[tuple[str, str], dict[str, float]] = {}

    def _add(rows: list[dict], field: str, value_key: str, qty_key: str = "") -> None:
        for row in rows or []:
            code = _norm_code(row.get("I_CODE"))
            wh = _norm_code(row.get("W_CODE"))
            if not code or not wh:
                continue
            cur = out.setdefault(
                (code, wh),
                {
                    "pos_qty": 0.0,
                    "pos": 0.0,
                    "cash_qty": 0.0,
                    "cash": 0.0,
                    "credit_qty": 0.0,
                    "credit": 0.0,
                    "ret_pos": 0.0,
                    "ret_cash": 0.0,
                    "ret_credit": 0.0,
                },
            )
            cur[field] += _f(row.get(value_key))
            if qty_key:
                cur[qty_key] += _f(row.get("QTY_TOTAL"))

    _add(_fetch_pos_sales(date_from, date_to, group_code, subgroup_code), "pos", "NET_TOTAL", "pos_qty")
    _add(_fetch_bill_sales(date_from, date_to, group_code, subgroup_code), "cash", "NET_TOTAL", "cash_qty")
    _add(
        _fetch_bill_sales(date_from, date_to, group_code, subgroup_code, _WHOLESALE_DOC_TYPES),
        "credit", "NET_TOTAL", "credit_qty",
    )
    _add(_fetch_pos_returns(date_from, date_to, group_code, subgroup_code), "ret_pos", "RET_NET")
    _add(_fetch_bill_returns(date_from, date_to, group_code, subgroup_code), "ret_cash", "RET_NET")
    _add(
        _fetch_bill_returns(date_from, date_to, group_code, subgroup_code, _WHOLESALE_DOC_TYPES),
        "ret_credit", "RET_NET",
    )
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
        wh_to_branch = {w["code"]: w["branch_code"] for w in all_wh}

        stock = _fetch_stock(sorted(sel_codes), gcode, mcode)
        sales = _sales_by_warehouse(date_from, date_to, gcode, mcode)

        per_item: dict[str, dict[str, float]] = {}

        def _acc(code: str) -> dict[str, float]:
            return per_item.setdefault(
                code,
                {
                    "stock": 0.0,
                    "pos_qty": 0.0,
                    "bill_qty": 0.0,
                    "pos": 0.0,
                    "bill": 0.0,
                    "returns": 0.0,
                },
            )

        for (code, wh), qty in stock.items():
            if wh in sel_codes:
                _acc(code)["stock"] += qty
        # الجملة كما في تحليل المبيعات لكل فرع: الآجل 4/8 + الزائد من نقدي 1/5 عن نقاط البيع
        # (مستند 1/5 غالباً انعكاس لنقاط البيع). نسبة الزائد تُحسب على كامل مخازن الفرع
        # ثم تُطبَّق على المخازن المختارة، فيطابق مجموع الفرع الشاشة الأم.
        br_pos: dict[str, float] = {}
        br_cash: dict[str, float] = {}
        for (code, wh), v in sales.items():
            b = wh_to_branch.get(wh, "")
            br_pos[b] = br_pos.get(b, 0.0) + v["pos"]
            br_cash[b] = br_cash.get(b, 0.0) + v["cash"]
        cash_ratio: dict[str, float] = {}
        for b, cash_total in br_cash.items():
            extra = max(0.0, cash_total - br_pos.get(b, 0.0))
            cash_ratio[b] = (extra / cash_total) if cash_total > 0 else 0.0

        for (code, wh), v in sales.items():
            if wh not in sel_codes:
                continue
            b = wh_to_branch.get(wh, "")
            ratio = cash_ratio.get(b, 0.0)
            has_pos = br_pos.get(b, 0.0) > 0
            a = _acc(code)
            a["pos"] += v["pos"]
            a["pos_qty"] += v["pos_qty"]
            a["bill"] += v["credit"] + v["cash"] * ratio
            a["bill_qty"] += v["credit_qty"] + v["cash_qty"] * ratio
            a["returns"] += (
                v["ret_pos"]
                + v["ret_credit"]
                + (0.0 if has_pos else v["ret_cash"])
            )

        meta = _hydrate_items(sorted(per_item)) if per_item else {}
        pack_map = fetch_item_max_pack_map(sorted(per_item)) if per_item else {}

    rows: list[dict[str, Any]] = []
    tot = {
        "stock": 0.0,
        "pos_qty": 0.0,
        "bill_qty": 0.0,
        "pos": 0.0,
        "bill": 0.0,
        "returns": 0.0,
    }
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
                "pos_qty_display": _fmt_qty(_to_pack_qty(a["pos_qty"], pack)),
                "bill_qty_display": _fmt_qty(_to_pack_qty(a["bill_qty"], pack)),
                "pos_display": _fmt_qty(a["pos"]),
                "bill_display": _fmt_qty(a["bill"]),
                "gross_display": _fmt_qty(gross),
                "returns_display": _fmt_qty(a["returns"]),
                "net_display": _fmt_qty(net),
                "turnover_display": _pct_display(a["pos_qty"] + a["bill_qty"], a["stock"]),
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
        "turnover_total_display": _pct_display(tot["pos_qty"] + tot["bill_qty"], tot["stock"]),
        "summary": [
            {
                "label": "الرصيد الحالي",
                "qty_display": _fmt_qty(tot["stock"]),
                "amount_display": "",
                "pct_display": "100.0%" if tot["stock"] > 1e-9 else "—",
            },
            {
                "label": "نقاط البيع",
                "qty_display": _fmt_qty(tot["pos_qty"]),
                "amount_display": _fmt_qty(tot["pos"]),
                "pct_display": _pct_display(tot["pos_qty"], tot["stock"]),
            },
            {
                "label": "الجملة",
                "qty_display": _fmt_qty(tot["bill_qty"]),
                "amount_display": _fmt_qty(tot["bill"]),
                "pct_display": _pct_display(tot["bill_qty"], tot["stock"]),
            },
            {
                "label": "إجمالي المبيعات",
                "qty_display": _fmt_qty(tot["pos_qty"] + tot["bill_qty"]),
                "amount_display": _fmt_qty(tot["pos"] + tot["bill"]),
                "pct_display": _pct_display(tot["pos_qty"] + tot["bill_qty"], tot["stock"]),
            },
        ],
        "stock_total_display": _fmt_qty(tot["stock"]),
        "pos_qty_total_display": _fmt_qty(tot["pos_qty"]),
        "bill_qty_total_display": _fmt_qty(tot["bill_qty"]),
        "pos_row_total_display": _fmt_qty(tot["pos"]),
        "bill_row_total_display": _fmt_qty(tot["bill"]),
        "gross_total_display": _fmt_qty(gross_total),
        "returns_total_display": _fmt_qty(tot["returns"]),
        "net_total_display": _fmt_qty(gross_total - tot["returns"]),
        "pos_total_display": _fmt_qty(tot["pos"]),
        "bill_total_display": _fmt_qty(tot["bill"]),
        "from": _as_date(date_from).isoformat() if date_from else "",
        "to": _as_date(date_to).isoformat() if date_to else "",
    }


def build_item_branch_excel(report: dict[str, Any]) -> HttpResponse:
    """ملف Excel (HTML بامتداد xls) لكل صفوف التقرير بلا قطع."""
    from html import escape

    cols = [
        ("#", None),
        ("رقم الصنف", "item_code"),
        ("اسم الصنف", "item_name"),
        ("المجموعة", "g_name"),
        ("الوحدة", "unit"),
        ("الرصيد الحالي", "stock_display"),
        ("كمية نقاط البيع", "pos_qty_display"),
        ("كمية الجملة", "bill_qty_display"),
        ("مبيعات نقاط البيع", "pos_display"),
        ("مبيعات الجملة", "bill_display"),
        ("إجمالي المبيعات", "gross_display"),
        ("المرتجعات", "returns_display"),
        ("صافي المبيعات", "net_display"),
        ("نسبة المبيعات من المخزون", "turnover_display"),
    ]
    head = "".join(f"<th>{escape(h)}</th>" for h, _ in cols)
    body = []
    for i, row in enumerate(report.get("rows") or [], 1):
        cells = [f"<td>{i}</td>"]
        for h, key in cols[1:]:
            val = row.get(key) or ""
            numeric = key and key.endswith("display") and key != "turnover_display"
            cls = ' class="num"' if numeric else ' class="txt"'
            cells.append(f"<td{cls}>{escape(str(val))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    foot = (
        '<tr class="foot"><td></td><td>الإجمالي</td><td></td><td></td><td></td>'
        f'<td class="num">{escape(str(report.get("stock_total_display") or ""))}</td>'
        f'<td class="num">{escape(str(report.get("pos_qty_total_display") or ""))}</td>'
        f'<td class="num">{escape(str(report.get("bill_qty_total_display") or ""))}</td>'
        f'<td class="num">{escape(str(report.get("pos_row_total_display") or ""))}</td>'
        f'<td class="num">{escape(str(report.get("bill_row_total_display") or ""))}</td>'
        f'<td class="num">{escape(str(report.get("gross_total_display") or ""))}</td>'
        f'<td class="num">{escape(str(report.get("returns_total_display") or ""))}</td>'
        f'<td class="num">{escape(str(report.get("net_total_display") or ""))}</td>'
        f'<td class="txt">{escape(str(report.get("turnover_total_display") or ""))}</td></tr>'
    )
    summary_rows = "".join(
        "<tr>"
        f'<td>{escape(str(r.get("label") or ""))}</td>'
        f'<td class="num">{escape(str(r.get("qty_display") or ""))}</td>'
        f'<td class="num">{escape(str(r.get("amount_display") or ""))}</td>'
        f'<td class="txt">{escape(str(r.get("pct_display") or ""))}</td>'
        "</tr>"
        for r in (report.get("summary") or [])
    )
    summary_html = (
        "<br><table><caption>ملخص المبيعات ونسبتها من المخزون الحالي "
        "(الكمية بالوحدة الأساسية)</caption>"
        "<thead><tr><th>البيان</th><th>الكمية</th><th>المبلغ</th>"
        "<th>النسبة من المخزون الحالي</th></tr></thead>"
        f"<tbody>{summary_rows}</tbody></table>"
    )
    html = (
        '<html xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:x="urn:schemas-microsoft-com:office:excel" '
        'xmlns="http://www.w3.org/TR/REC-html40">'
        '<head><meta charset="utf-8">'
        "<!--[if gte mso 9]><xml><x:ExcelWorkbook><x:ExcelWorksheets>"
        "<x:ExcelWorksheet><x:Name>الأصناف</x:Name>"
        "<x:WorksheetOptions><x:DisplayRightToLeft/></x:WorksheetOptions>"
        "</x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
        "<style>table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:11px;}"
        "th,td{border:1px solid #94a3b8;padding:4px 6px;white-space:nowrap;}"
        "th{background:#d9e2f3;font-weight:700;}"
        r"td.txt{mso-number-format:'\@';}"
        r"td.num{mso-number-format:'\#\,\#\#0\.000';text-align:left;}"
        "tr.foot td{background:#d9e2f3;font-weight:800;}</style></head><body dir=\"rtl\">"
        f"<table><caption>رصيد ومبيعات الأصناف — {escape(str(report.get('scope_label') or ''))}"
        f" · {escape(str(report.get('from') or ''))} ← {escape(str(report.get('to') or ''))}</caption>"
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody><tfoot>{foot}</tfoot></table>"
        f"{summary_html}"
        "</body></html>"
    )
    filename = f"item-branch-{report.get('from') or ''}-{report.get('to') or ''}.xls"
    resp = HttpResponse(("﻿" + html).encode("utf-8"), content_type="application/vnd.ms-excel; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp
