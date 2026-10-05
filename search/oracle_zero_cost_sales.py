"""أصناف تُباع بتكلفة صفرية (قراءة فقط — SELECT).

التكلفة الفعلية للصنف في النظام = متوسط تكلفة المخزن (I_CWTAVG) إن كان > 0، وإلا متوسط بطاقة الصنف.
فالصنف «بتكلفة صفرية» عند البيع إذا كان الاثنان صفراً.

- نقاط البيع (IAS_POS_BILL_*): سطر الفاتورة لا يحمل تكلفة، فتُقارَن مبيعاتها بالتكلفة الحالية.
  للسرعة نبدأ من الأصناف النشطة التي متوسط بطاقتها صفر (آلاف قليلة) ثم نحسب مبيعاتها وحدها،
  ثم نتحقق من متوسط المخزن الذي بيع منه.
- الفواتير (IAS_BILL_*، أنواع 1/4/5/8): السطر يحمل التكلفة وقت البيع (STK_COST) فنأخذ ما تكلفته صفر.
يُستثنى: الأصناف الخدمية والأسطر بلا سعر (هدايا).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.core.cache import cache

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _bill_mst_ok,
    _branch_names,
    _fetch_all,
    _hung_ok,
    _norm_brn_code,
    _pos_owner,
    _schema,
    fetch_sales_group_options,
    oracle_enabled,
)

logger = logging.getLogger(__name__)

MAX_DAYS = 31
CACHE_SECONDS = 300
ROW_LIMIT = 500

_POS_QTY = "NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(d.P_SIZE, 1))"
_LINE_AMT = "NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)"


def _money(value: Any) -> str:
    return f"{float(value or 0):,.2f}"


def _qty(value: Any) -> str:
    v = float(value or 0)
    return f"{v:,.0f}" if abs(v - round(v)) < 0.005 else f"{v:,.2f}"


def _fetch_pos(d_from, d_to_excl, branch: str, group: str) -> list[dict]:
    pos, schema = _pos_owner(), _schema()
    params: dict[str, Any] = {"d_from": d_from, "d_to_excl": d_to_excl}
    cand_group = ""
    if group:
        params["gcode"] = group
        cand_group = "AND i.G_CODE = :gcode"
    brn_sql = ""
    if branch:
        params["brn"] = int(branch) if branch.isdigit() else branch
        brn_sql = "AND m.BRN_NO = :brn"
    return _fetch_all(
        f"""
        SELECT TO_CHAR(x.I_CODE) AS ITEM_CODE, x.I_NAME, x.G_CODE, TO_CHAR(x.BRN_NO) AS BRANCH_CODE,
               TO_CHAR(x.W_CODE) AS W_CODE, x.QTY, x.AMT, x.BILLS
        FROM (
          SELECT /*+ LEADING(c d m) USE_NL(d m) */
                 d.I_CODE, MAX(c.I_NAME) AS I_NAME, MAX(c.G_CODE) AS G_CODE, m.BRN_NO,
                 NVL(d.W_CODE, m.W_CODE) AS W_CODE,
                 ROUND(SUM({_POS_QTY}), 2) AS QTY,
                 ROUND(SUM({_LINE_AMT}), 2) AS AMT,
                 COUNT(DISTINCT m.BILL_NO) AS BILLS
          FROM (SELECT i.I_CODE, i.I_NAME, i.G_CODE
                FROM {schema}.IAS_ITM_MST i
                WHERE NVL(i.I_CWTAVG, 0) = 0 AND NVL(i.SERVICE_ITM, 0) = 0
                  AND NVL(i.INACTIVE, 0) = 0 {cand_group}) c
          JOIN {pos}.IAS_POS_BILL_DTL d ON d.I_CODE = c.I_CODE
          JOIN {pos}.IAS_POS_BILL_MST m
            ON m.BILL_NO = d.BILL_NO AND m.BRN_NO = d.BRN_NO AND NVL(m.BILL_SRL, 0) = NVL(d.BILL_SRL, 0)
          WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
            AND {_hung_ok("m")} AND NVL(d.I_PRICE, 0) > 0 {brn_sql}
          GROUP BY d.I_CODE, m.BRN_NO, NVL(d.W_CODE, m.W_CODE)
        ) x
        WHERE NVL((SELECT MAX(NVL(w.I_CWTAVG, 0)) FROM {schema}.IAS_ITM_WCODE w
                   WHERE w.I_CODE = x.I_CODE AND w.W_CODE = x.W_CODE), 0) = 0
        """,
        params,
    )


def _fetch_bills(d_from, d_to_excl, branch: str, group: str) -> list[dict]:
    schema = _schema()
    params: dict[str, Any] = {"d_from": d_from, "d_to_excl": d_to_excl}
    extra = ""
    if group:
        params["gcode"] = group
        extra += " AND i.G_CODE = :gcode"
    if branch:
        params["brn"] = int(branch) if branch.isdigit() else branch
        extra += " AND m.BRN_NO = :brn"
    return _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS ITEM_CODE, MAX(i.I_NAME) AS I_NAME, MAX(i.G_CODE) AS G_CODE,
               TO_CHAR(m.BRN_NO) AS BRANCH_CODE, TO_CHAR(NVL(d.W_CODE, m.W_CODE)) AS W_CODE,
               ROUND(SUM(NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(d.P_SIZE, 1))), 2) AS QTY,
               ROUND(SUM({_LINE_AMT}), 2) AS AMT,
               COUNT(DISTINCT m.BILL_SER) AS BILLS
        FROM {schema}.IAS_BILL_MST m
        JOIN {schema}.IAS_BILL_DTL d
          ON d.BILL_DOC_TYPE = m.BILL_DOC_TYPE AND d.BILL_NO = m.BILL_NO AND d.BILL_SER = m.BILL_SER
        JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
          AND m.BILL_DOC_TYPE IN (1, 4, 5, 8) AND {_bill_mst_ok("m")}
          AND NVL(d.STK_COST, 0) = 0 AND NVL(d.I_PRICE, 0) > 0 AND NVL(d.I_QTY, 0) > 0
          AND NVL(i.SERVICE_ITM, 0) = 0 {extra}
        GROUP BY d.I_CODE, m.BRN_NO, NVL(d.W_CODE, m.W_CODE)
        """,
        params,
    )


def assemble_report(
    pos_rows: list[dict],
    bill_rows: list[dict],
    *,
    branch_names: dict[str, str],
    group_names: dict[str, str],
    limit: int = ROW_LIMIT,
) -> dict[str, Any]:
    """يدمج مبيعات نقاط البيع والفواتير لكل صنف. دالة نقية قابلة للاختبار."""
    items: dict[str, dict] = {}

    def slot(r: dict) -> dict:
        code = str(r.get("ITEM_CODE") or "").strip()
        return items.setdefault(
            code,
            {
                "code": code, "name": "", "group_code": "",
                "pos_qty": 0.0, "pos_amt": 0.0, "pos_bills": 0,
                "bill_qty": 0.0, "bill_amt": 0.0, "bill_docs": 0,
                "branches": {}, "warehouses": set(),
            },
        )

    for kind, rows in (("pos", pos_rows), ("bill", bill_rows)):
        for r in rows:
            it = slot(r)
            it["name"] = it["name"] or str(r.get("I_NAME") or "").strip()
            it["group_code"] = it["group_code"] or str(r.get("G_CODE") or "").strip()
            qty, amt, n = float(r.get("QTY") or 0), float(r.get("AMT") or 0), int(r.get("BILLS") or 0)
            it[f"{kind}_qty"] += qty
            it[f"{kind}_amt"] += amt
            it["pos_bills" if kind == "pos" else "bill_docs"] += n
            brn = _norm_brn_code(r.get("BRANCH_CODE"))
            it["branches"][brn] = it["branches"].get(brn, 0.0) + amt
            wh = str(r.get("W_CODE") or "").strip()
            if wh:
                it["warehouses"].add(wh)

    rows_out = []
    for it in items.values():
        total_amt = it["pos_amt"] + it["bill_amt"]
        ranked = sorted(it["branches"].items(), key=lambda kv: -kv[1])
        top = ranked[0][0] if ranked else ""
        top_name = branch_names.get(top) or top or "-"
        extra = len(ranked) - 1
        rows_out.append(
            {
                "code": it["code"],
                "name": it["name"] or it["code"],
                "group": group_names.get(it["group_code"]) or it["group_code"] or "-",
                "branch": f"{top_name} +{extra}" if extra > 0 else top_name,
                "branch_count": len(ranked),
                "warehouses": "، ".join(sorted(it["warehouses"])),
                "pos_amt": it["pos_amt"], "bill_amt": it["bill_amt"], "total_amt": total_amt,
                "pos_amt_display": _money(it["pos_amt"]), "bill_amt_display": _money(it["bill_amt"]),
                "total_display": _money(total_amt),
                "qty_display": _qty(it["pos_qty"] + it["bill_qty"]),
                "docs": it["pos_bills"] + it["bill_docs"],
            }
        )
    rows_out.sort(key=lambda r: -r["total_amt"])
    all_branches = {b for it in items.values() for b in it["branches"]}
    total = sum(r["total_amt"] for r in rows_out)
    return {
        "rows": rows_out[:limit],
        "truncated": len(rows_out) > limit,
        "limit": limit,
        "kpis": {
            "items": f"{len(rows_out):,}",
            "total": _money(total),
            "pos": _money(sum(r["pos_amt"] for r in rows_out)),
            "bills": _money(sum(r["bill_amt"] for r in rows_out)),
            "branches": f"{len(all_branches):,}",
        },
    }


def build_zero_cost_sales(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
) -> dict[str, Any]:
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    d_from, d_to = _as_date(date_from), _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")
    if (d_to - d_from).days >= MAX_DAYS:
        raise OracleStockError(f"الفترة القصوى {MAX_DAYS} يوماً.")
    brn = _norm_brn_code(branch_code)
    grp = str(group_code or "").strip()

    key = f"sales:zero_cost:v1:{d_from}:{d_to}:{brn}:{grp}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    d_excl = d_to + timedelta(days=1)
    pos_rows = _fetch_pos(d_from, d_excl, brn, grp)
    bill_rows = _fetch_bills(d_from, d_excl, brn, grp)
    report = assemble_report(
        pos_rows,
        bill_rows,
        branch_names=_branch_names(),
        group_names={g["code"]: g["name"] for g in fetch_sales_group_options()},
    )
    report["period_label"] = f"{d_from.isoformat()} → {d_to.isoformat()}"
    cache.set(key, report, CACHE_SECONDS)
    return report
