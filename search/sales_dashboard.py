"""تحليل المبيعات — ملخص نقاط البيع + جدول الجملة + مبيعات المجموعات."""

from __future__ import annotations

from typing import Any


def _money(v: float) -> str:
    return f"{float(v or 0):,.2f}"


def _qty(v: float) -> str:
    num = float(v or 0)
    if abs(num - round(num)) < 1e-9:
        return f"{int(round(num)):,}"
    return f"{num:,.2f}"


def _share(part: float, total: float) -> tuple[float, str]:
    if total <= 0:
        return 0.0, "0%"
    pct = round(part / total * 100.0, 1)
    return pct, f"{pct:.1f}%"


def _scope_label(branch_code: str, group_code: str) -> str:
    parts: list[str] = []
    if branch_code:
        parts.append(f"فرع {branch_code}")
    if group_code:
        parts.append(f"مجموعة {group_code}")
    return " · ".join(parts) if parts else "كل الفروع والمجموعات"


def _format_branch_rows(rows: list[dict]) -> tuple[list[dict], dict[str, Any]]:
    total_sales = round(sum(float(r.get("sales_total") or 0) for r in rows), 2)
    total_invoices = sum(int(r.get("invoice_count") or 0) for r in rows)
    total_returns = round(sum(float(r.get("return_total") or 0) for r in rows), 2)
    total_return_bills = sum(int(r.get("return_count") or 0) for r in rows)
    out: list[dict] = []
    for row in rows:
        sales = round(float(row.get("sales_total") or 0), 2)
        invoices = int(row.get("invoice_count") or 0)
        returns = round(float(row.get("return_total") or 0), 2)
        share_pct, share_display = _share(sales, total_sales)
        out.append(
            {
                "branch_code": str(row.get("branch_code") or "").strip(),
                "branch_name": str(
                    row.get("branch_name") or row.get("branch_code") or "—"
                ).strip(),
                "invoice_count": invoices,
                "invoice_count_display": f"{invoices:,}",
                "return_count": int(row.get("return_count") or 0),
                "return_count_display": f"{int(row.get('return_count') or 0):,}",
                "return_total": returns,
                "return_total_display": _money(returns),
                "sales_total": sales,
                "sales_total_display": _money(sales),
                "avg_basket": round(float(row.get("avg_basket") or 0), 2),
                "avg_basket_display": _money(row.get("avg_basket") or 0),
                "share_pct": share_pct,
                "share_display": share_display,
            }
        )
    avg_basket = round(total_sales / total_invoices, 2) if total_invoices else 0.0
    totals = {
        "sales_total": total_sales,
        "sales_total_display": _money(total_sales),
        "invoice_count": total_invoices,
        "invoice_count_display": f"{total_invoices:,}",
        "return_total": total_returns,
        "return_total_display": _money(total_returns),
        "return_count": total_return_bills,
        "return_count_display": f"{total_return_bills:,}",
        "avg_basket": avg_basket,
        "avg_basket_display": _money(avg_basket),
        "branch_count": len(out),
        "branch_count_display": f"{len(out):,}",
    }
    return out, totals


def _reconcile_group_sales_to_target(
    rows: list[dict], target_sales: float
) -> list[dict]:
    """يطابق مجموع المجموعات مع صافي مبيعات نقاط البيع (نفس إجمالي جدول الفروع)."""
    if not rows:
        return rows
    target = round(float(target_sales or 0), 2)
    current = round(sum(float(r.get("sales_total") or 0) for r in rows), 2)
    if current <= 0 or abs(current - target) < 0.005:
        return rows

    factor = target / current
    adjusted: list[dict] = []
    running = 0.0
    for row in rows:
        item = dict(row)
        sales = round(float(row.get("sales_total") or 0) * factor, 2)
        item["sales_total"] = sales
        if "net_total" in item or "vat_total" in item:
            net = round(float(row.get("net_total") or 0) * factor, 2)
            item["net_total"] = net
            item["vat_total"] = round(sales - net, 2)
        if "gross_total" in item:
            item["gross_total"] = round(float(row.get("gross_total") or 0) * factor, 2)
        inv = int(row.get("invoice_count") or 0)
        item["avg_basket"] = round(sales / inv, 2) if inv else 0.0
        running = round(running + sales, 2)
        adjusted.append(item)

    drift = round(target - running, 2)
    if abs(drift) >= 0.01 and adjusted:
        best = max(adjusted, key=lambda r: abs(float(r.get("sales_total") or 0)))
        best["sales_total"] = round(float(best["sales_total"]) + drift, 2)
        inv = int(best.get("invoice_count") or 0)
        best["avg_basket"] = (
            round(float(best["sales_total"]) / inv, 2) if inv else 0.0
        )
    return adjusted


def _format_group_rows(rows: list[dict]) -> tuple[list[dict], dict[str, Any]]:
    total_sales = round(sum(float(r.get("sales_total") or 0) for r in rows), 2)
    total_qty = round(sum(float(r.get("qty_total") or 0) for r in rows), 2)
    total_invoices = sum(int(r.get("invoice_count") or 0) for r in rows)
    out: list[dict] = []
    for row in rows:
        sales = round(float(row.get("sales_total") or 0), 2)
        qty = round(float(row.get("qty_total") or 0), 2)
        invoices = int(row.get("invoice_count") or 0)
        share_pct, share_display = _share(sales, total_sales)
        out.append(
            {
                "group_code": str(row.get("group_code") or "").strip(),
                "group_name": str(
                    row.get("group_name") or row.get("group_code") or "—"
                ).strip(),
                "invoice_count": invoices,
                "invoice_count_display": f"{invoices:,}",
                "qty_total": qty,
                "qty_display": _qty(qty),
                "sales_total": sales,
                "sales_total_display": _money(sales),
                "share_pct": share_pct,
                "share_display": share_display,
            }
        )
    totals = {
        "sales_total": total_sales,
        "sales_total_display": _money(total_sales),
        "qty_total": total_qty,
        "qty_display": _qty(total_qty),
        "invoice_count": total_invoices,
        "invoice_count_display": f"{total_invoices:,}",
        "group_count": len(out),
        "group_count_display": f"{len(out):,}",
    }
    return out, totals


def _empty_groups() -> dict[str, Any]:
    return {
        "rows": [],
        "totals": {
            "sales_total": 0.0,
            "sales_total_display": "0.00",
            "qty_total": 0.0,
            "qty_display": "0",
            "invoice_count": 0,
            "invoice_count_display": "0",
            "group_count": 0,
            "group_count_display": "0",
        },
    }


def _filter_branch_rows(rows: list[dict], branch_code: str) -> list[dict]:
    brn = str(branch_code or "").strip()
    if not brn:
        return list(rows or [])
    return [
        r
        for r in (rows or [])
        if str(r.get("branch_code") or "").strip() == brn
    ]


def _top_return_branches(pos_branches: list[dict], *, limit: int = 12) -> dict[str, Any]:
    """أعلى فروع مرتجعاً من بيانات نقاط البيع الجاهزة (بدون استعلام إضافي)."""
    ranked = sorted(
        (r for r in pos_branches if float(r.get("return_total") or 0) > 0),
        key=lambda r: (
            -float(r.get("return_total") or 0),
            str(r.get("branch_name") or ""),
        ),
    )[: max(1, min(int(limit or 12), 20))]
    total = round(sum(float(r.get("return_total") or 0) for r in ranked), 2)
    chart_rows: list[dict] = []
    for row in ranked:
        amount = round(float(row.get("return_total") or 0), 2)
        share_pct, share_display = _share(amount, total)
        chart_rows.append(
            {
                "code": row.get("branch_code") or "",
                "name": row.get("branch_name") or row.get("branch_code") or "—",
                "amount": amount,
                "amount_display": _money(amount),
                "invoice_count": int(row.get("return_count") or 0),
                "share_pct": share_pct,
                "share_display": share_display,
            }
        )
    return {
        "rows": chart_rows,
        "total": total,
        "total_display": _money(total),
        "count": len(chart_rows),
    }


def build_sales_branches(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
) -> dict[str, Any]:
    """فروع نقاط البيع + الجملة (سريع — من رأس الفاتورة).

    فلتر الفرع يُطبَّق على جداول الفروع.
    فلتر المجموعة يُمرَّر لجدول المجموعات فقط (رأس الفاتورة بلا مجموعة).
    """
    from .oracle_stock import fetch_branch_sales_totals, oracle_session

    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()

    with oracle_session():
        pos_raw = fetch_branch_sales_totals(date_from, date_to, system="pos")
        wholesale_raw = fetch_branch_sales_totals(
            date_from, date_to, system="wholesale"
        )

    pos_raw = _filter_branch_rows(pos_raw, brn)
    wholesale_raw = _filter_branch_rows(wholesale_raw, brn)

    pos_branches, pos_totals = _format_branch_rows(pos_raw)
    wholesale_branches, wholesale_totals = _format_branch_rows(wholesale_raw)
    groups = _empty_groups()
    top_returns = _top_return_branches(pos_branches)

    combined_sales = round(
        float(pos_totals["sales_total"]) + float(wholesale_totals["sales_total"]),
        2,
    )
    combined_invoices = int(pos_totals["invoice_count"]) + int(
        wholesale_totals["invoice_count"]
    )

    return {
        "period_label": f"{date_from.isoformat()} → {date_to.isoformat()}",
        "scope_label": _scope_label(brn, gcode),
        "branch_code": brn,
        "group_code": gcode,
        "groups_pending": True,
        "pos": {"branches": pos_branches, "totals": pos_totals},
        "wholesale": {"branches": wholesale_branches, "totals": wholesale_totals},
        "groups": groups,
        "top_returns": top_returns,
        "kpis": {
            "pos_sales": pos_totals["sales_total_display"],
            "pos_invoices": pos_totals["invoice_count_display"],
            "pos_returns": pos_totals["return_total_display"],
            "pos_return_bills": pos_totals["return_count_display"],
            "pos_avg": pos_totals["avg_basket_display"],
            "pos_branches": pos_totals["branch_count_display"],
            "wholesale_sales": wholesale_totals["sales_total_display"],
            "wholesale_invoices": wholesale_totals["invoice_count_display"],
            "wholesale_branches": wholesale_totals["branch_count_display"],
            "group_sales": groups["totals"]["sales_total_display"],
            "group_count": groups["totals"]["group_count_display"],
            "combined_sales": _money(combined_sales),
            "combined_invoices": f"{combined_invoices:,}",
        },
    }


def build_sales_groups(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
) -> dict[str, Any]:
    """مبيعات المجموعات من نقاط البيع فقط.

    التوزيع من بنود الأصناف؛ الإجمالي يُطابَق مع صافي مبيعات النقاط
    (جدول الفروع / بطاقة الملخص) — بدون أونكس أو آجل.
    """
    from .oracle_stock import (
        fetch_branch_sales_totals,
        fetch_group_sales_totals,
        oracle_session,
    )

    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()

    with oracle_session():
        groups_raw = fetch_group_sales_totals(
            date_from,
            date_to,
            system="pos",
            branch_code=brn,
            group_code=gcode,
            by_branch=False,
        )
        if not gcode and groups_raw:
            pos_raw = _filter_branch_rows(
                fetch_branch_sales_totals(date_from, date_to, system="pos"),
                brn,
            )
            target = round(
                sum(float(r.get("sales_total") or 0) for r in pos_raw),
                2,
            )
            groups_raw = _reconcile_group_sales_to_target(groups_raw, target)

    rows, totals = _format_group_rows(groups_raw)
    return {
        "rows": rows,
        "totals": totals,
        "period_label": f"{date_from.isoformat()} → {date_to.isoformat()}",
        "scope_label": _scope_label(brn, gcode),
    }


def _format_top_return_item_rows(
    rows: list[dict],
) -> tuple[list[dict], dict[str, Any]]:
    total_amt = round(
        sum(float(r.get("return_total") or r.get("sales_total") or 0) for r in rows),
        2,
    )
    total_qty = round(sum(float(r.get("qty_total") or 0) for r in rows), 2)
    total_bills = sum(
        int(r.get("return_count") or r.get("invoice_count") or 0) for r in rows
    )
    out: list[dict] = []
    for idx, row in enumerate(rows):
        amount = round(float(row.get("return_total") or row.get("sales_total") or 0), 2)
        qty = round(float(row.get("qty_total") or 0), 2)
        bills = int(row.get("return_count") or row.get("invoice_count") or 0)
        share_pct, share_display = _share(amount, total_amt)
        name = str(row.get("item_name") or row.get("item_code") or "—").strip()
        code = str(row.get("item_code") or "").strip()
        out.append(
            {
                "rank": idx + 1,
                "item_code": code,
                "item_name": name,
                "return_count": bills,
                "return_count_display": f"{bills:,}",
                "qty_total": qty,
                "qty_display": _qty(qty),
                "return_total": amount,
                "return_total_display": _money(amount),
                # توافق مع محمّل الجدول الحالي
                "invoice_count_display": f"{bills:,}",
                "sales_total_display": _money(amount),
                "share_pct": share_pct,
                "share_display": share_display,
            }
        )
    totals = {
        "return_total": total_amt,
        "return_total_display": _money(total_amt),
        "sales_total_display": _money(total_amt),
        "qty_total": total_qty,
        "qty_display": _qty(total_qty),
        "return_count": total_bills,
        "return_count_display": f"{total_bills:,}",
        "invoice_count_display": f"{total_bills:,}",
        "item_count": len(out),
        "item_count_display": f"{len(out):,}",
    }
    return out, totals


def build_sales_top_items(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """أعلى أصناف الإرجاع من نقاط البيع (افتراضياً أفضل 20)."""
    from .oracle_stock import fetch_top_returned_items, oracle_session

    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    lim = max(1, min(int(limit or 20), 40))

    with oracle_session():
        raw = fetch_top_returned_items(
            date_from,
            date_to,
            system="pos",
            branch_code=brn,
            group_code=gcode,
            limit=lim,
        )
    rows, totals = _format_top_return_item_rows(raw)
    return {
        "rows": rows,
        "totals": totals,
        "period_label": f"{date_from.isoformat()} → {date_to.isoformat()}",
        "scope_label": _scope_label(brn, gcode),
        "limit": lim,
        "kind": "returns",
    }


def build_sales_dashboard(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
) -> dict[str, Any]:
    """لوحة كاملة (فروع + مجموعات) — للاستخدام المباشر/الاختبار."""
    dash = build_sales_branches(
        date_from, date_to, branch_code=branch_code, group_code=group_code
    )
    groups = build_sales_groups(
        date_from, date_to, branch_code=branch_code, group_code=group_code
    )
    dash["groups"] = {"rows": groups["rows"], "totals": groups["totals"]}
    dash["groups_pending"] = False
    dash["kpis"]["group_sales"] = groups["totals"]["sales_total_display"]
    dash["kpis"]["group_count"] = groups["totals"]["group_count_display"]
    return dash
