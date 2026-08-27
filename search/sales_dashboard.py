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


# فروع نقاط البيع المعتمدة — تظهر دائماً حتى بلا حركة
_POS_STORE_TOKENS = (
    "الدمام",
    "حائل",
    "سكاي",
    "الربو",
    "خميس",
    "بريد",
    "منصور",
    "الواح",
)
_POS_STORE_FALLBACK_CODES = ("1", "6", "7", "8", "12", "18", "19", "20")


def _empty_pos_branch_row(code: str, name: str) -> dict[str, Any]:
    label = str(name or code or "").strip() or str(code or "—")
    return {
        "branch_code": str(code or "").strip(),
        "branch_name": label,
        "invoice_count": 0,
        "return_count": 0,
        "return_total": 0.0,
        "sales_total": 0.0,
        "gross_total": 0.0,
        "avg_basket": 0.0,
    }


def _fold_ar_name(name: str) -> str:
    text = str(name or "")
    for src, dst in (("ة", "ه"), ("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ى", "ي")):
        text = text.replace(src, dst)
    while "اا" in text:
        text = text.replace("اا", "ا")
    return text


def _is_pos_store_name(name: str) -> bool:
    text = _fold_ar_name(name)
    return any(token in text for token in _POS_STORE_TOKENS)


def _pos_store_branches() -> dict[str, str]:
    """الدمام، حائل، سكاي، الربوة، الخميس، بريدة، المنصورة، الواحة."""
    from .oracle_stock import _norm_brn_code

    names: dict[str, str] = {}
    try:
        from .oracle_stock import _branch_names

        names = dict(_branch_names() or {})
    except Exception:
        names = {}
    out: dict[str, str] = {}
    for code, name in names.items():
        key = _norm_brn_code(code)
        if key and _is_pos_store_name(str(name or "")):
            out[key] = str(name).strip() or key
    for code in _POS_STORE_FALLBACK_CODES:
        key = _norm_brn_code(code)
        if key and key not in out:
            out[key] = names.get(key) or key
    return out


def _pad_pos_store_branches(
    pos_raw: list[dict],
    stores: dict[str, str],
    selected: str = "",
) -> list[dict]:
    """أضف فروع نقاط البيع بلا حركة كصفوف صفرية — دون مخازن/إدارة."""
    from .oracle_stock import _norm_brn_code

    names = {
        _norm_brn_code(code): str(name or code).strip() or _norm_brn_code(code)
        for code, name in (stores or {}).items()
        if _norm_brn_code(code)
    }
    rows = list(pos_raw or [])
    # وحّد أكواد الصفوف القادمة من أوراكل واربط الاسم المعتمد
    for row in rows:
        code = _norm_brn_code(row.get("branch_code"))
        row["branch_code"] = code
        if code and code in names:
            row["branch_name"] = names[code]
    have = {_norm_brn_code(r.get("branch_code")) for r in rows}
    have.discard("")
    brn = _norm_brn_code(selected)
    if brn:
        if brn in names and brn not in have:
            rows.append(_empty_pos_branch_row(brn, names.get(brn) or brn))
        return rows
    for code, name in names.items():
        if code and code not in have:
            rows.append(_empty_pos_branch_row(code, name))
    return rows


def _format_branch_rows(rows: list[dict]) -> tuple[list[dict], dict[str, Any]]:
    from .oracle_stock import _norm_brn_code

    total_sales = round(sum(float(r.get("sales_total") or 0) for r in rows), 2)
    total_invoices = sum(int(r.get("invoice_count") or 0) for r in rows)
    total_returns = round(sum(float(r.get("return_total") or 0) for r in rows), 2)
    total_return_bills = sum(int(r.get("return_count") or 0) for r in rows)
    out: list[dict] = []
    for row in rows:
        code = _norm_brn_code(row.get("branch_code"))
        sales = round(float(row.get("sales_total") or 0), 2)
        invoices = int(row.get("invoice_count") or 0)
        returns = round(float(row.get("return_total") or 0), 2)
        return_count = int(row.get("return_count") or 0)
        share_pct, share_display = _share(sales, total_sales)
        gross = round(float(row.get("gross_total") or 0), 2)
        if gross <= 0:
            gross = round(max(sales, 0.0) + returns, 2)
        no_sales = (
            invoices == 0
            and return_count == 0
            and abs(sales) < 0.005
            and abs(returns) < 0.005
        )
        label = str(row.get("branch_name") or code or "—").strip() or code or "—"
        out.append(
            {
                "branch_code": code,
                "branch_name": label,
                "invoice_count": invoices,
                "invoice_count_display": f"{invoices:,}",
                "return_count": return_count,
                "return_count_display": f"{return_count:,}",
                "return_total": returns,
                "return_total_display": _money(returns),
                "sales_total": sales,
                "sales_total_display": _money(sales),
                "gross_total": gross,
                "gross_total_display": _money(gross),
                "avg_basket": round(float(row.get("avg_basket") or 0), 2),
                "avg_basket_display": _money(row.get("avg_basket") or 0),
                "share_pct": share_pct,
                "share_display": share_display,
                "no_sales": no_sales,
            }
        )
    out.sort(
        key=lambda r: (
            1 if r.get("no_sales") else 0,
            -float(r.get("sales_total") or 0),
            -int(r.get("invoice_count") or 0),
            str(r.get("branch_name") or ""),
            str(r.get("branch_code") or ""),
        )
    )
    avg_basket = round(total_sales / total_invoices, 2) if total_invoices else 0.0
    active_count = sum(1 for r in out if not r.get("no_sales"))
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
        "branch_count": active_count,
        "branch_count_display": f"{active_count:,}",
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


def _format_group_rows(
    rows: list[dict], *, by_branch: bool = False
) -> tuple[list[dict], dict[str, Any]]:
    total_sales = round(sum(float(r.get("sales_total") or 0) for r in rows), 2)
    total_qty = round(sum(float(r.get("qty_total") or 0) for r in rows), 2)
    total_invoices = sum(int(r.get("invoice_count") or 0) for r in rows)
    out: list[dict] = []
    for row in rows:
        sales = round(float(row.get("sales_total") or 0), 2)
        qty = round(float(row.get("qty_total") or 0), 2)
        invoices = int(row.get("invoice_count") or 0)
        avg = row.get("avg_basket")
        if avg is None:
            avg = round(sales / invoices, 2) if invoices else 0.0
        else:
            avg = round(float(avg or 0), 2)
        share_pct, share_display = _share(sales, total_sales)
        branch_code = str(row.get("branch_code") or "").strip()
        branch_name = str(
            row.get("branch_name") or branch_code or "—"
        ).strip()
        group_code = str(row.get("group_code") or "").strip()
        group_name = str(
            row.get("group_name") or group_code or "—"
        ).strip()
        # عند التفصيل بالفرع: عمود الاسم = الفرع
        name = branch_name if by_branch else group_name
        code = branch_code if by_branch else group_code
        out.append(
            {
                "group_code": group_code,
                "group_name": group_name,
                "branch_code": branch_code,
                "branch_name": branch_name,
                "name": name,
                "code": code,
                "invoice_count": invoices,
                "invoice_count_display": f"{invoices:,}",
                "qty_total": qty,
                "qty_display": _qty(qty),
                "sales_total": sales,
                "sales_total_display": _money(sales),
                "avg_basket": avg,
                "avg_basket_display": _money(avg),
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
        "avg_basket": (
            round(total_sales / total_invoices, 2) if total_invoices else 0.0
        ),
        "avg_basket_display": _money(
            round(total_sales / total_invoices, 2) if total_invoices else 0.0
        ),
        "group_count": len(out),
        "group_count_display": f"{len(out):,}",
        "branch_count": len(out) if by_branch else 0,
        "branch_count_display": f"{len(out):,}" if by_branch else "0",
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
    from .oracle_stock import _norm_brn_code

    brn = _norm_brn_code(branch_code)
    if not brn:
        return list(rows or [])
    return [
        r
        for r in (rows or [])
        if _norm_brn_code(r.get("branch_code")) == brn
    ]


def _combined_channel_totals(
    pos_raw: list[dict],
    credit_raw: list[dict],
    cash_raw: list[dict],
) -> tuple[float, int]:
    """إجمالي بلا تكرار: لكل فرع max(POS، نقدي الفواتير) + الآجل."""
    pos_map = {
        str(r.get("branch_code") or "").strip(): r for r in (pos_raw or [])
    }
    credit_map = {
        str(r.get("branch_code") or "").strip(): r for r in (credit_raw or [])
    }
    cash_map = {
        str(r.get("branch_code") or "").strip(): r for r in (cash_raw or [])
    }
    codes = {c for c in (set(pos_map) | set(credit_map) | set(cash_map)) if c}
    sales = 0.0
    invoices = 0
    for code in codes:
        p = pos_map.get(code) or {}
        w = credit_map.get(code) or {}
        c = cash_map.get(code) or {}
        pos_sales = float(p.get("sales_total") or 0)
        cash_sales = float(c.get("sales_total") or 0)
        credit_sales = float(w.get("sales_total") or 0)
        sales += max(pos_sales, cash_sales) + credit_sales
        pos_inv = int(p.get("invoice_count") or 0)
        cash_inv = int(c.get("invoice_count") or 0)
        credit_inv = int(w.get("invoice_count") or 0)
        if pos_inv > 0:
            invoices += pos_inv + credit_inv
        else:
            invoices += cash_inv + credit_inv
    return round(sales, 2), invoices


def _sales_system_excluding_pos(
    pos_raw: list[dict],
    cash_raw: list[dict],
    credit_raw: list[dict],
) -> list[dict]:
    """نظام المبيعات بلا تكرار POS: آجل (4/8) + نقدي فواتير يزيد عن نقاط البيع فقط.

    مستند 1/5 في IAS_BILL غالباً انعكاس نقاط البيع — لا يُعرض كاملاً مع POS.
    """
    pos_map = {
        str(r.get("branch_code") or "").strip(): r for r in (pos_raw or [])
    }
    cash_map = {
        str(r.get("branch_code") or "").strip(): r for r in (cash_raw or [])
    }
    credit_map = {
        str(r.get("branch_code") or "").strip(): r for r in (credit_raw or [])
    }
    codes = {c for c in (set(cash_map) | set(credit_map)) if c}
    out: list[dict] = []
    for code in codes:
        p = pos_map.get(code) or {}
        c = cash_map.get(code) or {}
        w = credit_map.get(code) or {}
        pos_sales = float(p.get("sales_total") or 0)
        cash_sales = float(c.get("sales_total") or 0)
        credit_sales = float(w.get("sales_total") or 0)
        extra_cash = max(0.0, round(cash_sales - pos_sales, 2))
        sales = round(credit_sales + extra_cash, 2)
        credit_inv = int(w.get("invoice_count") or 0)
        cash_inv = int(c.get("invoice_count") or 0)
        if pos_sales > 0:
            # فواتير الآجل فقط + لا نضاعف فواتير POS المنعكسة
            invoices = credit_inv
            returns = round(float(w.get("return_total") or 0), 2)
            return_count = int(w.get("return_count") or 0)
        else:
            invoices = credit_inv + cash_inv
            returns = round(
                float(w.get("return_total") or 0)
                + float(c.get("return_total") or 0),
                2,
            )
            return_count = int(w.get("return_count") or 0) + int(
                c.get("return_count") or 0
            )
        if sales <= 0 and invoices <= 0 and returns <= 0:
            continue
        name = (
            str(
                w.get("branch_name")
                or c.get("branch_name")
                or p.get("branch_name")
                or code
            ).strip()
            or code
        )
        out.append(
            {
                "branch_code": code,
                "branch_name": name,
                "invoice_count": invoices,
                "return_count": return_count,
                "return_total": returns,
                "net_invoice_count": max(0, invoices - return_count),
                "gross_total": round(sales + returns, 2),
                "net_total": sales,
                "vat_total": 0.0,
                "sales_total": sales,
                "avg_basket": round(sales / invoices, 2) if invoices else 0.0,
            }
        )
    out.sort(key=lambda r: float(r.get("sales_total") or 0), reverse=True)
    return out


def _top_return_branches(pos_branches: list[dict], *, limit: int = 12) -> dict[str, Any]:
    """أعلى فروع مرتجعاً — النسبة = مرتجع الفرع ÷ (مبيعاته + مرتجعه)."""
    chart_rows: list[dict] = []
    for row in pos_branches or []:
        amount = round(float(row.get("return_total") or 0), 2)
        if amount <= 0:
            continue
        sales_net = round(float(row.get("sales_total") or 0), 2)
        gross = round(float(row.get("gross_total") or 0), 2)
        if gross <= 0:
            # صافي المبيعات + المرتجع = إجمالي حركة الفرع قبل خصم المرتجع
            gross = round(max(sales_net, 0.0) + amount, 2)
        rate_pct, rate_display = _share(amount, gross)
        chart_rows.append(
            {
                "code": row.get("branch_code") or "",
                "name": row.get("branch_name") or row.get("branch_code") or "—",
                "amount": amount,
                "amount_display": _money(amount),
                "sales_total": sales_net,
                "sales_total_display": _money(sales_net),
                "gross_total": gross,
                "gross_total_display": _money(gross),
                "invoice_count": int(row.get("return_count") or 0),
                "share_pct": rate_pct,
                "share_display": rate_display,
            }
        )
    chart_rows.sort(
        key=lambda r: (
            -float(r.get("share_pct") or 0),
            -float(r.get("amount") or 0),
            str(r.get("name") or ""),
        )
    )
    lim = max(1, min(int(limit or 12), 20))
    chart_rows = chart_rows[:lim]
    total = round(sum(float(r.get("amount") or 0) for r in chart_rows), 2)
    return {
        "rows": chart_rows,
        "total": total,
        "total_display": _money(total),
        "count": len(chart_rows),
        "rate_basis": "branch_gross",
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
            "hint": (
                f"{int(best.get('invoice_count') or 0):,} مرتجع"
                f" · {best.get('share_display') or '—'}"
                f" من مبيعات الفرع"
            ),
            "pending": False,
        }

    return {
        "top_visit_branch": visit,
        "top_sales_branch": sales,
        "top_return_branch": ret_br,
        "top_return_item": ret_item,
    }


def _cached_branch_totals(date_from, date_to, system: str) -> list[dict] | None:
    from .oracle_stock import (
        _as_date,
        _sales_cache_get,
        _sales_cache_get_stale,
        _skip_mst_returns,
    )

    key = (
        f"sales:branches:v8:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:"
        f"r{int(not _skip_mst_returns(date_from, date_to))}"
    )
    hit = _sales_cache_get(key)
    if hit is not None:
        return hit
    return _sales_cache_get_stale(key)


def _assemble_sales_branches_dashboard(
    pos_raw: list[dict],
    credit_raw: list[dict],
    cash_raw: list[dict],
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
    from_cache: bool = False,
) -> dict[str, Any]:
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()

    pos_raw = _filter_branch_rows(pos_raw, brn)
    credit_raw = _filter_branch_rows(credit_raw, brn)
    cash_raw = _filter_branch_rows(cash_raw, brn)
    onix_raw = list(cash_raw)

    wholesale_raw = _sales_system_excluding_pos(pos_raw, cash_raw, credit_raw)

    if not from_cache:
        # #region agent log
        try:
            from .oracle_stock import _agent_dbg

            b6p = next(
                (r for r in pos_raw if str(r.get("branch_code")) == "6"), {}
            )
            b6c = next(
                (r for r in cash_raw if str(r.get("branch_code")) == "6"), {}
            )
            b6w = next(
                (r for r in credit_raw if str(r.get("branch_code")) == "6"), {}
            )
            b6o = next(
                (r for r in wholesale_raw if str(r.get("branch_code")) == "6"),
                {},
            )
            _agent_dbg(
                "A",
                "sales_dashboard.py:build_sales_branches:dedupe",
                "branch6 sales system after POS dedupe",
                {
                    "pos": float(b6p.get("sales_total") or 0),
                    "cash_1_5": float(b6c.get("sales_total") or 0),
                    "credit_4_8": float(b6w.get("sales_total") or 0),
                    "panel": float(b6o.get("sales_total") or 0),
                },
            )
        except Exception:
            pass
        # #endregion

    pos_branches, pos_totals = _format_branch_rows(
        _pad_pos_store_branches(pos_raw, _pos_store_branches(), brn)
    )
    wholesale_branches, wholesale_totals = _format_branch_rows(wholesale_raw)
    _onix_branches, onix_totals = _format_branch_rows(onix_raw)
    groups = _empty_groups()
    top_returns = _top_return_branches(pos_branches)
    ranks = _rank_highlights(pos_branches, top_returns)

    combined_sales, combined_invoices = _combined_channel_totals(
        pos_raw, credit_raw, cash_raw
    )

    payload = {
        "period_label": f"{date_from.isoformat()} → {date_to.isoformat()}",
        "scope_label": _scope_label(brn, gcode),
        "branch_code": brn,
        "group_code": gcode,
        "groups_pending": True,
        "pos": {"branches": pos_branches, "totals": pos_totals},
        "wholesale": {"branches": wholesale_branches, "totals": wholesale_totals},
        "onix": {"totals": onix_totals},
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
            "onix_sales": onix_totals["sales_total_display"],
            "onix_invoices": onix_totals["invoice_count_display"],
            "onix_returns": onix_totals["return_total_display"],
            "group_sales": groups["totals"]["sales_total_display"],
            "group_count": groups["totals"]["group_count_display"],
            "combined_sales": _money(combined_sales),
            "combined_invoices": f"{combined_invoices:,}",
        },
    }
    if from_cache:
        payload["from_cache"] = True
    return payload


def build_sales_branches_from_cache(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
) -> dict[str, Any] | None:
    """عند انقطاع أوراكل: أرقام الفروع من الكاش المحلي إن وُجدت."""
    pos_raw = _cached_branch_totals(date_from, date_to, "pos")
    credit_raw = _cached_branch_totals(date_from, date_to, "wholesale")
    cash_raw = _cached_branch_totals(date_from, date_to, "onix")
    if pos_raw is None and credit_raw is None and cash_raw is None:
        return None
    # #region agent log
    try:
        from .oracle_stock import _agent_dbg

        _agent_dbg(
            "G",
            "sales_dashboard.py:build_sales_branches_from_cache",
            "oracle down — serving branch cache",
            {
                "pos_rows": len(pos_raw or []),
                "credit_rows": len(credit_raw or []),
                "cash_rows": len(cash_raw or []),
            },
        )
    except Exception:
        pass
    # #endregion
    return _assemble_sales_branches_dashboard(
        list(pos_raw or []),
        list(credit_raw or []),
        list(cash_raw or []),
        date_from,
        date_to,
        branch_code=branch_code,
        group_code=group_code,
        from_cache=True,
    )


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
        # آجل نظام المبيعات (4/8) + نقدي فواتير (1/5) منفصلين ثم نفك تكرار POS
        credit_raw = fetch_branch_sales_totals(
            date_from, date_to, system="wholesale"
        )
        cash_raw = fetch_branch_sales_totals(date_from, date_to, system="onix")

    return _assemble_sales_branches_dashboard(
        pos_raw,
        credit_raw,
        cash_raw,
        date_from,
        date_to,
        branch_code=brn,
        group_code=gcode,
    )


def peek_sales_groups(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
) -> dict[str, Any] | None:
    """مجموعات من الكاش فقط — لزرع الصفحة فوراً بلا انتظار أوراكل."""
    from datetime import date as _date

    from .oracle_stock import (
        _as_date,
        _merge_available_monthly_group_cache,
        _month_spans,
        _sales_cache_get,
        _sales_cache_get_stale,
        _skip_mst_returns,
        peek_group_sales_totals,
        sales_long_range,
    )

    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    by_branch = bool(gcode)
    # مجموعة + إلى اليوم: لا تزرع رقمًا من الكاش (يتخلف عن أوراكل)
    if gcode and date_to >= _date.today():
        return None
    raw = peek_group_sales_totals(
        date_from,
        date_to,
        system="pos",
        branch_code=brn,
        group_code=gcode,
        by_branch=by_branch,
    )
    if raw is None:
        return None

    # هل شهور الفترة مكتملة في كاش المجموعات؟
    months = _month_spans(date_from, date_to)
    _merged, missing = _merge_available_monthly_group_cache(
        system="pos",
        date_from=date_from,
        date_to=date_to,
        brn=brn,
        gcode=gcode,
        split_by_branch=by_branch,
        mode="gross",
    )
    incomplete = bool(missing) and len(months) >= 2

    # طابق إجمالي الزرع مع كاش الفروع إن وُجد (حتى لا يظهر فرق مع جدول الفروع)
    matched = False
    pos_total = None
    raw_total = round(sum(float(r.get("sales_total") or 0) for r in raw), 2)
    if not by_branch and raw:
        br_key = (
            f"sales:branches:v7:pos:{_as_date(date_from).isoformat()}:"
            f"{_as_date(date_to).isoformat()}:"
            f"r{int(not _skip_mst_returns(date_from, date_to))}"
        )
        br_rows = _sales_cache_get(br_key)
        if br_rows is None:
            br_rows = _sales_cache_get_stale(br_key)
        if brn and br_rows is not None:
            br_rows = [
                r
                for r in br_rows
                if str(r.get("branch_code") or "").strip() == brn
            ]
        if br_rows is not None:
            pos_total = round(
                sum(float(r.get("sales_total") or 0) for r in br_rows), 2
            )
            raw = _reconcile_group_sales_to_target(raw, pos_total)
            matched = abs(
                round(sum(float(r.get("sales_total") or 0) for r in raw), 2)
                - pos_total
            ) < 0.05
            # #region agent log
            try:
                from .oracle_stock import _agent_dbg

                _agent_dbg(
                    "F",
                    "sales_dashboard.py:peek_sales_groups:reconcile",
                    "peek reconciled to branch cache",
                    {
                        "raw_total": raw_total,
                        "pos_total": pos_total,
                        "matched": matched,
                        "incomplete": incomplete,
                        "missing_months": len(missing or []),
                    },
                )
            except Exception:
                pass
            # #endregion

    rows, totals = _format_group_rows(raw, by_branch=by_branch)
    if matched and pos_total is not None:
        totals["sales_total"] = pos_total
        totals["sales_total_display"] = _money(pos_total)
    payload = {
        "rows": rows,
        "totals": totals,
        "period_label": f"{date_from.isoformat()} → {date_to.isoformat()}",
        "scope_label": _scope_label(brn, gcode),
        "from_cache": True,
        "by_branch": by_branch,
        "matched": matched and not incomplete,
        "incomplete": incomplete and not by_branch,
        "cache": {
            "source": "json",
            "months_ready": max(0, len(months) - len(missing or [])),
            "months_total": len(months),
        },
        "raw_total": raw_total,
        "raw_total_display": _money(raw_total),
    }
    if pos_total is not None:
        payload["pos_total"] = pos_total
        payload["pos_total_display"] = _money(pos_total)
    if incomplete and not by_branch:
        payload["warning"] = (
            f"جزئي JSON {payload['cache']['months_ready']}/{len(months)} — "
            "الإجمالي مطابق للفروع؛ التوزيع يُكمَّل مع الشهور"
        )
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
    """مبيعات المجموعات من نقاط البيع.

    بدون مجموعة: توزيع المجموعات ثم مطابقة الإجمالي مع صافي جدول الفروع.
    مع مجموعة مختارة: صف لكل فرع بعدد فواتير صحيح ومتوسط سلة (بلا مطابقة على كل POS).
    """
    import logging

    from .oracle_stock import (
        fetch_branch_sales_totals,
        fetch_group_sales_totals,
        oracle_session,
        pop_groups_fetch_warning,
        pop_groups_incomplete,
        pop_groups_months_progress,
        pop_groups_source,
        sales_long_range,
    )

    logger = logging.getLogger(__name__)
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    by_branch = bool(gcode)
    warning = ""

    # Exact من POS ثم مطابقة الإجمالي مع صافي الفروع عند اكتمال البيانات
    groups_raw = fetch_group_sales_totals(
        date_from,
        date_to,
        system="pos",
        branch_code=brn,
        group_code=gcode,
        by_branch=by_branch,
        force_fast=True,
    )
    warning = pop_groups_fetch_warning() or ""
    incomplete = pop_groups_incomplete()
    months_ready, months_total = pop_groups_months_progress()
    groups_source = pop_groups_source() or "json"

    # اكتمال شهور JSON ⇒ صالح للمطابقة
    months_complete = bool(months_total) and months_ready >= months_total
    if months_complete:
        incomplete = False

    pos_total = None
    matched = False
    raw_groups_total = round(
        sum(float(r.get("sales_total") or 0) for r in (groups_raw or [])), 2
    )

    # مجموع المجموعات = صافي جدول نقاط البيع (حتى لو الشهور ناقصة — التوزيع يُنسَب)
    do_reconcile = (
        bool(reconcile) and not gcode and not by_branch and bool(groups_raw)
    )
    if do_reconcile:
        try:
            # #region agent log
            import time as _time

            _rt0 = _time.monotonic()
            from .oracle_stock import _agent_dbg

            _agent_dbg(
                "E",
                "sales_dashboard.py:build_sales_groups:reconcile:start",
                "reconcile start",
                {
                    "raw_groups_total": raw_groups_total,
                    "incomplete": incomplete,
                    "months_ready": months_ready,
                    "months_total": months_total,
                },
            )
            # #endregion
            with oracle_session():
                pos_raw = _filter_branch_rows(
                    fetch_branch_sales_totals(date_from, date_to, system="pos"),
                    brn,
                )
            pos_total = round(
                sum(float(r.get("sales_total") or 0) for r in pos_raw),
                2,
            )
            before = raw_groups_total
            groups_raw = _reconcile_group_sales_to_target(groups_raw, pos_total)
            after = round(
                sum(float(r.get("sales_total") or 0) for r in groups_raw), 2
            )
            matched = abs(after - pos_total) < 0.05
            # لا تُلغِ incomplete هنا — التوزيع قد يبقى تقريبياً حتى تكتمل الشهور
            # #region agent log
            _agent_dbg(
                "E",
                "sales_dashboard.py:build_sales_groups:reconcile:ok",
                "reconcile ok",
                {
                    "elapsed_ms": int((_time.monotonic() - _rt0) * 1000),
                    "pos_total": pos_total,
                    "matched": matched,
                    "incomplete": incomplete,
                },
            )
            # #endregion
            if not matched and not warning:
                warning = (
                    f"إجمالي المجموعات {_money(before)} لا يطابق الفروع "
                    f"{_money(pos_total)} بعد المطابقة"
                )
            elif matched and abs(before - after) >= 1 and not warning:
                logger.info(
                    "groups reconciled %s → %s (POS net)",
                    before,
                    after,
                )
            if incomplete and matched and not warning:
                warning = (
                    f"جزئي JSON {months_ready}/{months_total or '?'} — "
                    "الإجمالي مطابق للفروع؛ التوزيع يُكمَّل مع الشهور"
                )
        except Exception as exc:  # noqa: BLE001
            # #region agent log
            try:
                from .oracle_stock import _agent_dbg

                _agent_dbg(
                    "E",
                    "sales_dashboard.py:build_sales_groups:reconcile:err",
                    "reconcile failed",
                    {"error": str(exc)[:300]},
                )
            except Exception:
                pass
            # #endregion
            logger.warning("groups reconcile skipped: %s", exc)
            if not warning:
                warning = "تم العرض بدون مطابقة ملخص الفروع"
    elif incomplete and not warning:
        # #region agent log
        try:
            from .oracle_stock import _agent_dbg

            _agent_dbg(
                "F",
                "sales_dashboard.py:build_sales_groups:reconcile:skipped",
                "reconcile skipped",
                {
                    "reason": "no_rows_or_by_branch",
                    "incomplete": incomplete,
                    "raw_groups_total": raw_groups_total,
                    "gcode": gcode,
                    "by_branch": by_branch,
                },
            )
        except Exception:
            pass
        # #endregion
        warning = (
            f"جزئي JSON {months_ready}/{months_total or '?'} — "
            "الإجمالي غير مطابق للفروع حتى تكتمل الشهور"
        )

    rows, totals = _format_group_rows(groups_raw, by_branch=by_branch)
    # بعد المطابقة: ثبّت إجمالي العرض = صافي الفروع بالهللة
    if matched and pos_total is not None:
        totals["sales_total"] = pos_total
        totals["sales_total_display"] = _money(pos_total)
    payload = {
        "rows": rows,
        "totals": totals,
        "period_label": f"{date_from.isoformat()} → {date_to.isoformat()}",
        "scope_label": _scope_label(brn, gcode),
        "incomplete": incomplete and not by_branch,
        "matched": bool(matched),
        "exact": (not incomplete) and groups_source != "sample" and bool(matched),
        "by_branch": by_branch,
        "cache": {
            "source": groups_source,
            "months_ready": months_ready,
            "months_total": months_total,
        },
        "raw_total": raw_groups_total,
        "raw_total_display": _money(raw_groups_total),
    }
    if pos_total is not None:
        payload["pos_total"] = pos_total
        payload["pos_total_display"] = _money(pos_total)
    if warning:
        payload["warning"] = warning
    if sales_long_range(date_from, date_to):
        payload["long_range"] = True
    # شهور ناقصة — فقط لوضع كل المجموعات (ليس تفصيل فرع لمجموعة)
    if (
        not by_branch
        and (incomplete or (months_total and months_ready < months_total))
    ):
        try:
            from .oracle_stock import _load_groups_month_json, _month_spans

            missing_jobs: list[dict[str, str]] = []
            for a, b in _month_spans(date_from, date_to):
                hit = _load_groups_month_json(
                    system="pos",
                    date_from=a,
                    date_to=b,
                    brn=brn,
                    gcode=gcode,
                    split_by_branch=False,
                    mode="gross",
                )
                if hit is None:
                    missing_jobs.append(
                        {
                            "date_from": a.isoformat(),
                            "date_to": b.isoformat(),
                        }
                    )
            if missing_jobs:
                payload["sql_months"] = missing_jobs
        except Exception:
            pass
    return payload


def build_sales_groups_month(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
) -> dict[str, Any]:
    """جلب شهر واحد بـ SQL → JSON كاش → صفوف منسّقة للواجهة."""
    from .oracle_stock import _fetch_one_month_group_totals

    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    raw = _fetch_one_month_group_totals(
        system="pos",
        date_from=date_from,
        date_to=date_to,
        brn=brn,
        gcode=gcode,
        split_by_branch=False,
        fast=True,
    )
    rows, totals = _format_group_rows(raw or [])
    return {
        "rows": rows,
        "totals": totals,
        "period_label": f"{date_from.isoformat()} → {date_to.isoformat()}",
        "scope_label": _scope_label(brn, gcode),
        "source": "sql",
        "incomplete": False,
        "matched": False,
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
