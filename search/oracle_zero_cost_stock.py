"""أصناف لها رصيد وتكلفتها صفر (قراءة فقط — SELECT).

نفس تعريف «تحليل المخزون» للتكلفة: NVL(I_CWTAVG, PRIMARY_COST) من IAS_ITM_WCODE؛ فرصيد هذه الصفوف
يُقيَّم بصفر ويُخفض قيمة المخزون. الكمية = الرصيد الدفتري AVL_QTY (قبل خصم مبيعات نقاط البيع غير المرحّلة).
يُستثنى الصنف الخدمي والمخزن المعطّل والكمية غير الموجبة.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.cache import cache

from .oracle_sqlutil import num_bind
from .oracle_stock import (
    OracleStockError,
    _bind_brn,
    _branch_names,
    _fetch_all,
    _norm_brn_code,
    _schema,
    fetch_sales_group_options,
    oracle_enabled,
)

logger = logging.getLogger(__name__)

ROW_LIMIT = 1000
CACHE_SECONDS = 300


def _qty(value: Any) -> str:
    v = float(value or 0)
    return f"{v:,.0f}" if abs(v - round(v)) < 0.005 else f"{v:,.2f}"


def _money(value: Any) -> str:
    return f"{float(value or 0):,.2f}"


def _where(branch: str, warehouse: str, group: str) -> tuple[str, dict]:
    params: dict[str, Any] = {}
    parts = [
        "NVL(w.AVL_QTY, 0) > 0",
        "NVL(NVL(w.I_CWTAVG, w.PRIMARY_COST), 0) = 0",
        "NVL(m.SERVICE_ITM, 0) = 0",
        "NVL(wh.INACTIVE, 0) = 0",
    ]
    if branch:
        params["brn"] = _bind_brn(branch)
        parts.append("wh.CONN_BRN_NO = :brn")
    if warehouse:
        params["wh"] = warehouse
        parts.append(f"w.W_CODE = {num_bind(':wh')}")
    if group:
        params["gcode"] = group
        parts.append("m.G_CODE = :gcode")
    return " AND ".join(parts), params


def _fetch(branch: str, warehouse: str, group: str, limit: int) -> tuple[list[dict], int]:
    schema = _schema()
    where, params = _where(branch, warehouse, group)
    base = f"""
        FROM {schema}.IAS_ITM_WCODE w
        JOIN {schema}.IAS_ITM_MST m ON m.I_CODE = w.I_CODE
        JOIN {schema}.WAREHOUSE_DETAILS wh ON wh.W_CODE = w.W_CODE
        WHERE {where}
    """
    total = _fetch_all(f"SELECT COUNT(*) AS N {base}", params)
    rows = _fetch_all(
        f"""
        SELECT * FROM (
          SELECT TO_CHAR(w.I_CODE) AS ITEM_CODE, m.I_NAME, m.G_CODE,
                 TO_CHAR(w.W_CODE) AS W_CODE, wh.W_NAME, TO_CHAR(wh.CONN_BRN_NO) AS BRANCH_CODE,
                 w.ITM_UNT AS UNIT, w.AVL_QTY AS QTY,
                 NVL(w.PRIMARY_COST, 0) AS PRIMARY_COST, NVL(m.I_CWTAVG, 0) AS CARD_COST
          {base}
          ORDER BY wh.CONN_BRN_NO, w.W_CODE, w.AVL_QTY DESC
        ) WHERE ROWNUM <= :lim
        """,
        {**params, "lim": limit},
    )
    return rows, int(total[0].get("N") or 0) if total else 0


def assemble_report(
    rows: list[dict],
    total_rows: int,
    *,
    branch_names: dict[str, str],
    group_names: dict[str, str],
    limit: int = ROW_LIMIT,
) -> dict[str, Any]:
    """يجهّز صفوف العرض والمؤشرات. دالة نقية قابلة للاختبار."""
    out = []
    items, warehouses, branches = set(), set(), set()
    for r in rows:
        code = str(r.get("ITEM_CODE") or "").strip()
        wh = str(r.get("W_CODE") or "").strip()
        brn = _norm_brn_code(r.get("BRANCH_CODE"))
        items.add(code)
        warehouses.add(wh)
        branches.add(brn)
        primary = float(r.get("PRIMARY_COST") or 0)
        card = float(r.get("CARD_COST") or 0)
        out.append(
            {
                "code": code,
                "name": str(r.get("I_NAME") or "").strip() or code,
                "group": group_names.get(str(r.get("G_CODE") or "").strip())
                or str(r.get("G_CODE") or "").strip() or "-",
                "branch": branch_names.get(brn) or brn or "-",
                "warehouse": str(r.get("W_NAME") or "").strip() or wh,
                "warehouse_code": wh,
                "unit": str(r.get("UNIT") or "").strip() or "-",
                "qty_display": _qty(r.get("QTY")),
                "primary_display": _money(primary) if primary else "—",
                "card_display": _money(card) if card else "—",
                # توجيه الإصلاح: تكلفة موجودة في مكان آخر يمكن نقلها
                "has_alt_cost": bool(primary or card),
            }
        )
    return {
        "rows": out,
        "total_rows": total_rows,
        "truncated": total_rows > len(out),
        "limit": limit,
        "kpis": {
            "items": f"{len(items):,}",
            "rows": f"{total_rows:,}",
            "warehouses": f"{len(warehouses):,}",
            "branches": f"{len(branches):,}",
            "fixable": f"{sum(1 for r in out if r['has_alt_cost']):,}",
        },
    }


def build_zero_cost_stock(
    *,
    branch_code: str = "",
    warehouse_code: str = "",
    group_code: str = "",
    limit: int = ROW_LIMIT,
) -> dict[str, Any]:
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    brn = _norm_brn_code(branch_code)
    wh = str(warehouse_code or "").strip()
    grp = str(group_code or "").strip()
    lim = max(10, min(int(limit or ROW_LIMIT), 3000))

    key = f"inv:zero_cost_stock:v1:{brn}:{wh}:{grp}:{lim}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    rows, total = _fetch(brn, wh, grp, lim)
    report = assemble_report(
        rows,
        total,
        branch_names=_branch_names(),
        group_names={g["code"]: g["name"] for g in fetch_sales_group_options()},
        limit=lim,
    )
    cache.set(key, report, CACHE_SECONDS)
    return report
