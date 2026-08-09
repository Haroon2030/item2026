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


def _empty_rank_card(title: str = "") -> dict[str, Any]:
    return {
        "title": title,
        "name": "—",
        "code": "",
        "value_display": "—",
        "hint": "لا بيانات",
        "pending": False,
    }


def _rank_highlights(
    pos_branches: list[dict], top_returns: dict[str, Any]
) -> dict[str, Any]:
    """أبرز الترتيبات لبطاقات الملخص (زيارة / مبيعات / إرجاع)."""
    visit = _empty_rank_card("أكثر فرع زيارة")
    sales = _empty_rank_card("أكثر فرع مبيعات")
    ret_br = _empty_rank_card("أكثر فرع إرجاع")
    ret_item = _empty_rank_card("أكثر صنف إرجاع")
    ret_item["pending"] = True
    ret_item["hint"] = "جاري التحميل…"
    ret_item["value_display"] = "…"

    if pos_branches:
        top_visit = max(
            pos_branches,
            key=lambda r: (
                int(r.get("invoice_count") or 0),
                float(r.get("sales_total") or 0),
            ),
        )
        if int(top_visit.get("invoice_count") or 0) > 0:
            visit = {
                "title": "أكثر فرع زيارة",
                "name": str(
                    top_visit.get("branch_name") or top_visit.get("branch_code") or "—"
                ),
                "code": str(top_visit.get("branch_code") or ""),
                "value_display": str(top_visit.get("invoice_count_display") or "0"),
                "hint": f"مبيعات {_money(top_visit.get('sales_total') or 0)}",
                "pending": False,
            }

        top_sales = max(
            pos_branches,
            key=lambda r: (
                float(r.get("sales_total") or 0),
                int(r.get("invoice_count") or 0),
            ),
        )
        if float(top_sales.get("sales_total") or 0) > 0:
            sales = {
                "title": "أكثر فرع مبيعات",
                "name": str(
                    top_sales.get("branch_name") or top_sales.get("branch_code") or "—"
                ),
                "code": str(top_sales.get("branch_code") or ""),
                "value_display": _money(top_sales.get("sales_total") or 0),
                "hint": f"{top_sales.get('invoice_count_display') or 0} فاتورة",
                "pending": False,
            }

    ret_rows = list((top_returns or {}).get("rows") or [])
    if ret_rows:
        best = ret_rows[0]
        ret_br = {
            "title": "أكثر فرع إرجاع",
            "name": str(best.get("name") or best.get("code") or "—"),
            "code": str(best.get("code") or ""),
            "value_display": str(best.get("amount_display") or "0.00"),
            "hint": f"{int(best.get('invoice_count') or 0):,} فاتورة مرتجع",
            "pending": False,
        }

    return {
        "top_visit_branch": visit,
        "top_sales_branch": sales,
        "top_return_branch": ret_br,
        "top_return_item": ret_item,
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
    ranks = _rank_highlights(pos_branches, top_returns)

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
        "ranks": ranks,
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


def peek_sales_groups(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
) -> dict[str, Any] | None:
    """مجموعات من الكاش فقط — لزرع الصفحة فوراً بلا انتظار أوراكل."""
    from .oracle_stock import peek_group_sales_totals, sales_long_range

    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    raw = peek_group_sales_totals(
        date_from,
        date_to,
        system="pos",
        branch_code=brn,
        group_code=gcode,
        by_branch=False,
    )
    if raw is None:
        return None
    rows, totals = _format_group_rows(raw)
    payload = {
        "rows": rows,
        "totals": totals,
        "period_label": f"{date_from.isoformat()} → {date_to.isoformat()}",
        "scope_label": _scope_label(brn, gcode),
        "from_cache": True,
    }
    if sales_long_range(date_from, date_to):
        payload["long_range"] = True
    return payload


def build_sales_groups(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
    reconcile: bool = True,
) -> dict[str, Any]:
    """مبيعات المجموعات من نقاط البيع فقط.

    التوزيع من بنود الأصناف؛ الإجمالي يُطابَق مع صافي مبيعات النقاط
    (جدول الفروع / بطاقة الملخص) — بدون أونكس أو آجل.
    """
    import logging

    from .oracle_stock import (
        fetch_branch_sales_totals,
        fetch_group_sales_totals,
        oracle_session,
        pop_groups_fetch_warning,
        pop_groups_incomplete,
        sales_long_range,
    )

    logger = logging.getLogger(__name__)
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    warning = ""

    # كاش أولاً / دمج شهور / كاش قديم فوري / أوراكل مرة واحدة
    groups_raw = fetch_group_sales_totals(
        date_from,
        date_to,
        system="pos",
        branch_code=brn,
        group_code=gcode,
        by_branch=False,
    )
    warning = pop_groups_fetch_warning() or ""
    incomplete = pop_groups_incomplete()

    pos_total = None
    matched = False
    # طابق إجمالي المجموعات مع جدول نقاط البيع عند اكتمال الفترة
    do_reconcile = bool(reconcile) and not gcode and bool(groups_raw) and not incomplete
    if do_reconcile:
        try:
            with oracle_session():
                pos_raw = _filter_branch_rows(
                    fetch_branch_sales_totals(date_from, date_to, system="pos"),
                    brn,
                )
            pos_total = round(
                sum(float(r.get("sales_total") or 0) for r in pos_raw),
                2,
            )
            before = round(
                sum(float(r.get("sales_total") or 0) for r in groups_raw), 2
            )
            groups_raw = _reconcile_group_sales_to_target(groups_raw, pos_total)
            after = round(
                sum(float(r.get("sales_total") or 0) for r in groups_raw), 2
            )
            matched = abs(after - pos_total) < 0.05
            if not matched and not warning:
                warning = (
                    f"إجمالي المجموعات {_money(before)} لا يطابق الفروع "
                    f"{_money(pos_total)} بعد المطابقة"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("groups reconcile skipped: %s", exc)
            if not warning:
                warning = "تم العرض بدون مطابقة ملخص الفروع"
    elif incomplete and not warning:
        warning = "البيانات جزئية — الإجمالي أقل من جدول الفروع حتى تكتمل الأشهر"

    rows, totals = _format_group_rows(groups_raw)
    # matched فقط بعد مطابقة ناجحة مع إجمالي الفروع — لا تُعلَن اكتمالاً وهمياً
    payload = {
        "rows": rows,
        "totals": totals,
        "period_label": f"{date_from.isoformat()} → {date_to.isoformat()}",
        "scope_label": _scope_label(brn, gcode),
        "incomplete": incomplete,
        "matched": bool(matched) and not incomplete,
    }
    if pos_total is not None:
        payload["pos_total"] = pos_total
        payload["pos_total_display"] = _money(pos_total)
    if warning:
        payload["warning"] = warning
    if sales_long_range(date_from, date_to):
        payload["long_range"] = True
    return payload


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


def _hour_label(hour: int) -> str:
    h = max(0, min(23, int(hour or 0)))
    return f"{h:02d}:00"


def _continuity_label(pct: float) -> str:
    if pct >= 70:
        return "مستمر جداً"
    if pct >= 50:
        return "مستمر"
    if pct >= 30:
        return "متوسط"
    return "متقطع"


def _format_branch_activity_rows(
    rows: list[dict],
) -> tuple[list[dict], dict[str, Any]]:
    total_inv = sum(int(r.get("invoice_count") or 0) for r in rows)
    total_sales = round(sum(float(r.get("sales_total") or 0) for r in rows), 2)
    out: list[dict] = []
    for idx, row in enumerate(rows):
        avg_h = float(row.get("avg_hours_per_day") or 0)
        cont = float(row.get("continuity_pct") or 0)
        first_h = int(row.get("first_hour") or 0)
        last_h = int(row.get("last_hour") or 0)
        out.append(
            {
                "rank": idx + 1,
                "branch_code": str(row.get("branch_code") or "").strip(),
                "branch_name": str(row.get("branch_name") or "").strip() or "—",
                "invoice_count": int(row.get("invoice_count") or 0),
                "invoice_count_display": f"{int(row.get('invoice_count') or 0):,}",
                "sales_total": round(float(row.get("sales_total") or 0), 2),
                "sales_total_display": _money(row.get("sales_total") or 0),
                "active_days": int(row.get("active_days") or 0),
                "active_days_display": f"{int(row.get('active_days') or 0):,}",
                "avg_hours_per_day": avg_h,
                "avg_hours_display": f"{avg_h:.1f}",
                "hours_span_display": f"{_hour_label(first_h)}–{_hour_label(last_h)}",
                "invoices_per_hour": float(row.get("invoices_per_hour") or 0),
                "invoices_per_hour_display": f"{float(row.get('invoices_per_hour') or 0):.1f}",
                "continuity_pct": cont,
                "continuity_display": f"{cont:.0f}%",
                "continuity_label": _continuity_label(cont),
            }
        )
    totals = {
        "branch_count": len(out),
        "branch_count_display": f"{len(out):,}",
        "invoice_count": total_inv,
        "invoice_count_display": f"{total_inv:,}",
        "sales_total": total_sales,
        "sales_total_display": _money(total_sales),
    }
    return out, totals


def build_sales_branch_activity(
    date_from,
    date_to,
    *,
    branch_code: str = "",
) -> dict[str, Any]:
    """ترتيب الفروع حسب استمرارية ساعات البيع (نقاط البيع)."""
    from .oracle_stock import fetch_branch_sales_activity, oracle_session

    brn = str(branch_code or "").strip()
    with oracle_session():
        raw = fetch_branch_sales_activity(date_from, date_to, branch_code=brn)
    rows, totals = _format_branch_activity_rows(raw)
    return {
        "rows": rows,
        "totals": totals,
        "period_label": f"{date_from.isoformat()} → {date_to.isoformat()}",
        "scope_label": _scope_label(brn, ""),
    }


def _format_top_user_rows(
    rows: list[dict],
) -> tuple[list[dict], dict[str, Any]]:
    total_sales = round(sum(float(r.get("sales_total") or 0) for r in rows), 2)
    total_inv = sum(int(r.get("invoice_count") or 0) for r in rows)
    out: list[dict] = []
    for idx, row in enumerate(rows):
        sales = round(float(row.get("sales_total") or 0), 2)
        inv = int(row.get("invoice_count") or 0)
        share_pct, share_display = _share(sales, total_sales)
        out.append(
            {
                "rank": idx + 1,
                "user_code": str(row.get("user_code") or "").strip(),
                "user_name": str(row.get("user_name") or row.get("user_code") or "—").strip(),
                "invoice_count": inv,
                "invoice_count_display": f"{inv:,}",
                "sales_total": sales,
                "sales_total_display": _money(sales),
                "avg_basket": round(sales / inv, 2) if inv else 0.0,
                "avg_basket_display": _money(sales / inv) if inv else "0.00",
                "share_pct": share_pct,
                "share_display": share_display,
            }
        )
    totals = {
        "user_count": len(out),
        "user_count_display": f"{len(out):,}",
        "invoice_count": total_inv,
        "invoice_count_display": f"{total_inv:,}",
        "sales_total": total_sales,
        "sales_total_display": _money(total_sales),
    }
    return out, totals


def build_sales_top_users(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    limit: int = 15,
) -> dict[str, Any]:
    """أكثر المستخدمين مبيعاً من نقاط البيع."""
    from .oracle_stock import fetch_top_sales_users, oracle_session

    brn = str(branch_code or "").strip()
    lim = max(1, min(int(limit or 15), 40))
    with oracle_session():
        raw = fetch_top_sales_users(
            date_from,
            date_to,
            system="pos",
            branch_code=brn,
            limit=lim,
        )
    rows, totals = _format_top_user_rows(raw)
    return {
        "rows": rows,
        "totals": totals,
        "period_label": f"{date_from.isoformat()} → {date_to.isoformat()}",
        "scope_label": _scope_label(brn, ""),
        "limit": lim,
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
