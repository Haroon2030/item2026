"""محرك تحليل المخزون — إجماليات حسب المخزن والمجموعة والفرع."""

from __future__ import annotations

from typing import Any


def _scope_label(warehouse: str, group_code: str, branch_code: str) -> str:
    parts: list[str] = []
    if branch_code:
        parts.append(f"فرع {branch_code}")
    if warehouse:
        parts.append(f"مخزن {warehouse}")
    if group_code:
        parts.append(f"مجموعة {group_code}")
    return " · ".join(parts) if parts else "كل المخزون المتاح"


def _money(v: float) -> str:
    return f"{float(v or 0):,.2f}"


def _qty(v: float) -> str:
    num = float(v or 0)
    if abs(num - round(num)) < 1e-9:
        return f"{int(round(num)):,}"
    return f"{num:,.2f}"


def _table_totals(rows: list[dict]) -> dict[str, Any]:
    """إجماليات صف التذييل من صفوف البعد (قيمة وكمية بدون تكرار محاسبي)."""
    value = round(sum(float(r.get("stock_value") or 0) for r in rows), 2)
    qty = round(sum(float(r.get("qty_total") or 0) for r in rows), 2)
    items = sum(int(r.get("item_count") or 0) for r in rows)
    warehouses = sum(int(r.get("warehouse_count") or 0) for r in rows)
    return {
        "stock_value": value,
        "qty_total": qty,
        "item_count": items,
        "warehouse_count": warehouses,
        "stock_value_display": _money(value),
        "qty_display": _qty(qty),
        "item_count_display": f"{items:,}",
        "warehouse_count_display": f"{warehouses:,}",
        "share_display": "100%" if rows else "0%",
        "row_count": len(rows),
    }


def _build_group_sales_activity(
    by_group: list[dict],
    *,
    warehouse: str = "",
    group_code: str = "",
    branch_code: str = "",
) -> tuple[list[dict], str, dict[str, float]]:
    """ترتيب المجموعات حسب دوران المخزون في المبيعات (آخر 7 أيام / نقاط البيع)."""
    from datetime import date, timedelta

    from .oracle_stock import fetch_group_sales_totals

    date_to = date.today()
    date_from = date_to - timedelta(days=6)
    period_label = f"{date_from.isoformat()} → {date_to.isoformat()}"

    brn = str(branch_code or "").strip()
    wh = str(warehouse or "").strip()
    gcode = str(group_code or "").strip()
    if wh and not brn:
        from .oracle_stock import fetch_warehouse_options

        for w in fetch_warehouse_options(active_only=True):
            if str(w.get("code") or "") == wh:
                brn = str(w.get("branch_code") or "").strip()
                break

    sales_rows = fetch_group_sales_totals(
        date_from,
        date_to,
        system="pos",
        branch_code=brn,
        group_code=gcode,
        by_branch=False,
    )
    stock_map = {
        str(r.get("code") or "").strip(): r
        for r in by_group
        if str(r.get("code") or "").strip()
    }

    sales_by_code: dict[str, float] = {}
    for row in sales_rows:
        code = str(row.get("group_code") or "").strip() or "(بلا)"
        sales_by_code[code] = round(
            sales_by_code.get(code, 0.0) + float(row.get("sales_total") or 0),
            2,
        )

    total_sales = round(sum(sales_by_code.values()), 2)
    ranked: list[dict] = []
    for row in sales_rows:
        code = str(row.get("group_code") or "").strip() or "(بلا)"
        sales = round(float(row.get("sales_total") or 0), 2)
        if sales <= 0:
            continue
        stock = stock_map.get(code) or {}
        stock_val = float(stock.get("stock_value") or 0)
        turnover = (sales / stock_val) if stock_val > 0 else None
        sales_share = (sales / total_sales * 100.0) if total_sales else 0.0
        ranked.append(
            {
                "code": code,
                "name": str(row.get("group_name") or stock.get("name") or code),
                "sales_total": sales,
                "sales_display": _money(sales),
                "stock_value": stock_val,
                "stock_value_display": _money(stock_val) if stock_val else "—",
                "turnover": round(turnover, 2) if turnover is not None else None,
                "turnover_display": (
                    f"دوران {turnover:.2f}×" if turnover is not None else "بدون رصيد"
                ),
                "share_pct": round(sales_share, 1),
                "share_display": f"{sales_share:.1f}%",
                "bar_pct": 0.0,
            }
        )

    ranked.sort(key=lambda r: (-r["sales_total"], r["name"], r["code"]))
    peak = float(ranked[0]["sales_total"]) if ranked else 0.0
    for row in ranked:
        bar = (row["sales_total"] / peak * 100.0) if peak else 0.0
        row["bar_pct"] = round(bar, 1)
    return ranked[:12], period_label, sales_by_code


def _build_stagnant_groups(
    by_group: list[dict],
    sales_by_code: dict[str, float],
    *,
    total_stock_value: float,
) -> dict[str, Any]:
    """مجموعات لها رصيد ولا حركة مبيعات في الفترة — للرسم الدائري."""
    stagnant: list[dict] = []
    for row in by_group:
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        stock_val = float(row.get("stock_value") or 0)
        if stock_val <= 0:
            continue
        sales = float(sales_by_code.get(code) or 0)
        if sales > 0:
            continue
        stagnant.append(
            {
                "code": code,
                "name": str(row.get("name") or code),
                "stock_value": stock_val,
                "stock_value_display": _money(stock_val),
                "qty_display": str(row.get("qty_display") or "0"),
                "item_count_display": str(row.get("item_count_display") or "0"),
            }
        )
    stagnant.sort(key=lambda r: (-r["stock_value"], r["name"], r["code"]))
    stagnant_total = round(sum(r["stock_value"] for r in stagnant), 2)
    for row in stagnant:
        share = (row["stock_value"] / stagnant_total * 100.0) if stagnant_total else 0.0
        row["share_pct"] = round(share, 1)
        row["share_display"] = f"{share:.1f}%"
    of_all = (
        (stagnant_total / total_stock_value * 100.0) if total_stock_value > 0 else 0.0
    )
    return {
        "rows": stagnant[:15],
        "count": len(stagnant),
        "stock_value": stagnant_total,
        "stock_value_display": _money(stagnant_total),
        "of_total_pct": round(of_all, 1),
        "of_total_display": f"{of_all:.1f}%",
    }


def build_inventory_insights(
    *,
    warehouse: str = "",
    group_code: str = "",
    branch_code: str = "",
) -> dict[str, Any]:
    """يبني لوحة تحليل مخزون من أوراكل (قيمة بالتكلفة × الكمية المتاحة)."""
    from .oracle_stock import (
        fetch_inventory_by_branch,
        fetch_inventory_by_group,
        fetch_inventory_by_warehouse,
    )

    wh = str(warehouse or "").strip()
    gcode = str(group_code or "").strip()
    brn = str(branch_code or "").strip()

    by_warehouse = fetch_inventory_by_warehouse(
        warehouse=wh, group_code=gcode, branch_code=brn
    )
    by_group = fetch_inventory_by_group(
        warehouse=wh, group_code=gcode, branch_code=brn
    )
    by_branch = fetch_inventory_by_branch(
        warehouse=wh, group_code=gcode, branch_code=brn
    )

    total_value = round(sum(float(r.get("stock_value") or 0) for r in by_warehouse), 2)
    total_qty = round(sum(float(r.get("qty_total") or 0) for r in by_warehouse), 2)
    total_rows = sum(int(r.get("row_count") or 0) for r in by_warehouse)
    # أصناف مميزة عبر المجموعات أدق عند فلتر مخزن واحد؛ عند الكل قد يتكرر الصنف
    # لذلك نعرض مجموع صفوف المخزون وعدد المخازن والمجموعات كمؤشرات منفصلة
    warehouse_count = len(by_warehouse)
    group_count = len(by_group)
    branch_count = len(by_branch)
    distinct_items_est = sum(int(r.get("item_count") or 0) for r in by_warehouse)

    warehouse_totals = _table_totals(by_warehouse)
    group_totals = _table_totals(by_group)
    branch_totals = _table_totals(by_branch)
    # عدد المخازن في تذييل المجموعات = المخازن الفريدة ضمن الفلتر (لا مجموع الحصص)
    group_totals["warehouse_count"] = warehouse_count
    group_totals["warehouse_count_display"] = f"{warehouse_count:,}"
    branch_totals["warehouse_count"] = warehouse_count
    branch_totals["warehouse_count_display"] = f"{warehouse_count:,}"

    group_sales_rank, sales_period_label, sales_by_code = _build_group_sales_activity(
        by_group,
        warehouse=wh,
        group_code=gcode,
        branch_code=brn,
    )
    stagnant = _build_stagnant_groups(
        by_group,
        sales_by_code,
        total_stock_value=total_value,
    )

    top_wh = by_warehouse[0] if by_warehouse else None
    top_group = by_group[0] if by_group else None
    top_branch = by_branch[0] if by_branch else None
    top_sales_group = group_sales_rank[0] if group_sales_rank else None

    alerts: list[dict] = []
    actions: list[dict] = []
    if top_wh and float(top_wh.get("share_pct") or 0) >= 40:
        alerts.append(
            {
                "severity": "warn",
                "title": "تركّز عالٍ في مخزن واحد",
                "detail": (
                    f"{top_wh['name']} يستحوذ على {top_wh['share_display']} "
                    f"من قيمة المخزون ضمن الفلتر."
                ),
            }
        )
        actions.append(
            {
                "severity": "warn",
                "text": "راجع توزيع المخزون بين المخازن لتقليل المخاطر التشغيلية.",
                "from_alert": "تركّز مخزن",
            }
        )
    if top_group and float(top_group.get("share_pct") or 0) >= 35:
        alerts.append(
            {
                "severity": "info",
                "title": "مجموعة مهيمنة بالقيمة",
                "detail": (
                    f"{top_group['name']} تمثّل {top_group['share_display']} "
                    f"من قيمة المخزون."
                ),
            }
        )
    if top_sales_group and float(top_sales_group.get("share_pct") or 0) >= 30:
        alerts.append(
            {
                "severity": "info",
                "title": "مجموعة تقود دوران المبيعات",
                "detail": (
                    f"{top_sales_group['name']} تمثّل {top_sales_group['share_display']} "
                    f"من مبيعات آخر 7 أيام ({top_sales_group['turnover_display']})."
                ),
            }
        )
    if warehouse_count == 0:
        alerts.append(
            {
                "severity": "bad",
                "title": "لا رصيد ضمن الفلتر",
                "detail": "لا توجد كميات متاحة > 0 بالمعايير المحددة.",
            }
        )
    if stagnant["count"] > 0 and float(stagnant.get("of_total_pct") or 0) >= 10:
        alerts.append(
            {
                "severity": "warn",
                "title": "مخزون راكد بلا حركة مبيعات",
                "detail": (
                    f"{stagnant['count']} مجموعة بقيمة {stagnant['stock_value_display']} "
                    f"({stagnant['of_total_display']} من المخزون) بلا مبيعات خلال آخر 7 أيام."
                ),
            }
        )
        actions.append(
            {
                "severity": "warn",
                "text": "راجع المجموعات الراكدة: عروض، نقل مخزون، أو إعادة تسعير لتقليل الركود.",
                "from_alert": "مخزون راكد",
            }
        )

    return {
        "scope_label": _scope_label(wh, gcode, brn),
        "warehouse": wh,
        "group_code": gcode,
        "branch_code": brn,
        "kpis": {
            "stock_value": _money(total_value),
            "stock_value_num": total_value,
            "qty_total": _qty(total_qty),
            "qty_num": total_qty,
            "row_count": f"{total_rows:,}",
            "item_rows": f"{distinct_items_est:,}",
            "warehouse_count": warehouse_count,
            "group_count": group_count,
            "branch_count": branch_count,
            "top_warehouse_share": float((top_wh or {}).get("share_pct") or 0),
            "top_warehouse_share_display": (top_wh or {}).get("share_display") or "0%",
            "top_group_share": float((top_group or {}).get("share_pct") or 0),
            "top_group_share_display": (top_group or {}).get("share_display") or "0%",
        },
        "highlights": {
            "top_warehouse": {
                "name": (top_wh or {}).get("name") or "—",
                "value": (top_wh or {}).get("stock_value_display") or "0.00",
                "share": (top_wh or {}).get("share_display") or "0%",
            },
            "top_group": {
                "name": (top_group or {}).get("name") or "—",
                "value": (top_group or {}).get("stock_value_display") or "0.00",
                "share": (top_group or {}).get("share_display") or "0%",
            },
            "top_branch": {
                "name": (top_branch or {}).get("name") or "—",
                "value": (top_branch or {}).get("stock_value_display") or "0.00",
                "share": (top_branch or {}).get("share_display") or "0%",
            },
        },
        "by_warehouse": by_warehouse[:80],
        "by_group": by_group[:80],
        "by_branch": by_branch[:40],
        "group_sales_rank": group_sales_rank,
        "sales_period_label": sales_period_label,
        "stagnant": stagnant,
        "warehouse_totals": warehouse_totals,
        "group_totals": group_totals,
        "branch_totals": branch_totals,
        "alerts": alerts,
        "actions": actions,
        "note": (
            "القيمة محسوبة من الكمية المتاحة × متوسط التكلفة. "
            "الكمية مجموع وحدات التخزين وقد تختلف وحداتها بين الأصناف."
        ),
    }
