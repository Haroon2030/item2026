"""الأصول الثابتة المسجّلة على الفروع — FAS_ASSETS_MST."""

from __future__ import annotations

import io
import math
from datetime import date, datetime
from html import escape
from typing import Any

from django.core.cache import cache
from django.http import HttpResponse

from .oracle_stock import (
    OracleStockError,
    _bind_brn,
    _branch_names,
    _fetch_all,
    _schema,
    oracle_enabled,
)

_CACHE_TTL = 1800
_CACHE_VER = "v5"

# مصنع المنهل، ركن التغليف، البلاستيك
_EXCLUDED_BRN = (17, 16, 13)


def excluded_asset_branch_codes() -> set[str]:
    return {str(code) for code in _EXCLUDED_BRN}


def _f(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _money(value: Any) -> str:
    return f"{_f(value):,.2f}"


def _fmt_compact(value: float) -> str:
    n = _f(value)
    sign = "-" if n < 0 else ""
    v = abs(n)
    if v >= 1_000_000:
        return f"{sign}{v / 1_000_000:,.1f} م"
    if v >= 1_000:
        return f"{sign}{v / 1_000:,.1f} ألف"
    return f"{sign}{v:,.0f}"


def _share_display(share: float) -> str:
    if share >= 10:
        return f"{round(share):.0f}%".replace(",", ".")
    if abs(share - round(share)) > 0.05:
        return f"{share:.1f}%".replace(",", ".")
    return f"{int(round(share))}%".replace(",", ".")


def _pct_css(value: float, peak: float) -> str:
    if not peak or abs(value) <= 0.005:
        return "0"
    return f"{min(100.0, abs(value) / peak * 100.0):.1f}"


def _qty(value: Any) -> str:
    num = float(value or 0)
    if abs(num - round(num)) < 1e-9:
        return f"{int(round(num)):,}"
    return f"{num:,.2f}"


def _brn_code(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        text = str(value).strip()
        return text[:-2] if text.endswith(".0") else text


def _date_label(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")[:10]


def _annular_slices_from_parts(parts: list[dict]) -> tuple[list[dict], float]:
    total = round(sum(float(p.get("amount") or 0) for p in parts), 2)
    slices: list[dict] = []
    for part in parts:
        amt = float(part.get("amount") or 0)
        if amt <= 0 and total > 0:
            continue
        share = (amt / total * 100.0) if total else 0.0
        slices.append(
            {
                **part,
                "amount": amt,
                "amount_display": part.get("amount_display") or _money(amt),
                "amount_compact": part.get("amount_compact") or _fmt_compact(amt),
                "share_pct": round(share, 1),
                "share_display": _share_display(share),
            }
        )

    r_out, r_in = 48.0, 26.8
    r_label = (r_out + r_in) / 2.0
    cx = cy = 50.0
    gap_deg = 1.8
    frac_acc = 0.0

    def _pt(angle_deg: float, radius: float) -> tuple[float, float]:
        a = math.radians(angle_deg)
        return (cx + radius * math.sin(a), cy - radius * math.cos(a))

    def _annular_path(start_deg: float, end_deg: float) -> str:
        sweep = end_deg - start_deg
        if sweep <= 0.05:
            return ""
        large = 1 if sweep > 180 else 0
        x0, y0 = _pt(start_deg, r_out)
        x1, y1 = _pt(end_deg, r_out)
        x2, y2 = _pt(end_deg, r_in)
        x3, y3 = _pt(start_deg, r_in)
        return (
            f"M {x0:.3f} {y0:.3f} "
            f"A {r_out:.3f} {r_out:.3f} 0 {large} 1 {x1:.3f} {y1:.3f} "
            f"L {x2:.3f} {y2:.3f} "
            f"A {r_in:.3f} {r_in:.3f} 0 {large} 0 {x3:.3f} {y3:.3f} Z"
        )

    for sl in slices:
        amt = float(sl["amount"])
        pct = (amt / total) if total else 0.0
        span = pct * 360.0
        pad = gap_deg if span > gap_deg * 2.5 else max(0.35, span * 0.07)
        start = frac_acc * 360.0 + pad / 2.0
        end = frac_acc * 360.0 + span - pad / 2.0
        if end < start:
            end = start
        mid = (start + end) / 2.0
        lx, ly = _pt(mid, r_label)
        sl["path_d"] = _annular_path(start, end)
        sl["label_x"] = f"{lx:.2f}"
        sl["label_y"] = f"{ly:.2f}"
        sl["show_label"] = pct >= 0.045
        frac_acc += pct

    return slices, total


def _build_asset_structure(cost_total: float, depr_total: float, bv_total: float) -> dict:
    """دونات: إهلاك متراكم مقابل قيمة دفترية متبقية — المركز = التكلفة."""
    depr = abs(_f(depr_total))
    book = abs(_f(bv_total))
    cost = abs(_f(cost_total)) or round(depr + book, 2)
    parts = [
        {
            "key": "depr",
            "name": "مجمع الإهلاك",
            "full_name": "مجمع الإهلاك",
            "amount": depr,
            "amount_display": _money(depr),
            "amount_compact": _fmt_compact(depr),
            "color": "#FBBF24",
            "tone": "depr",
        },
        {
            "key": "book",
            "name": "القيمة الدفترية",
            "full_name": "القيمة الدفترية المتبقية",
            "amount": book,
            "amount_display": _money(book),
            "amount_compact": _fmt_compact(book),
            "color": "#34D399",
            "tone": "book",
        },
    ]
    slices, total = _annular_slices_from_parts(parts)
    return {
        "slices": slices,
        "center": {
            "label": "التكلفة",
            "value_display": _fmt_compact(cost),
            "value_full": _money(cost),
        },
        "total": cost,
        "total_display": _money(cost),
        "has_data": bool(slices) and cost > 0,
    }


def _build_group_mix(rows: list[dict], *, head: int = 5) -> dict:
    by_grp: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("group_code") or "").strip() or "—"
        bucket = by_grp.setdefault(
            code,
            {
                "group_code": code,
                "group_name": str(row.get("group_name") or code),
                "cost_total": 0.0,
                "depr_total": 0.0,
                "bv_total": 0.0,
                "asset_count": 0,
            },
        )
        bucket["cost_total"] = round(bucket["cost_total"] + _f(row.get("cost")), 2)
        bucket["depr_total"] = round(bucket["depr_total"] + _f(row.get("depr")), 2)
        bucket["bv_total"] = round(bucket["bv_total"] + _f(row.get("book_value")), 2)
        bucket["asset_count"] += 1

    ranked = sorted(by_grp.values(), key=lambda g: (-g["cost_total"], g["group_code"]))
    colors = ("#22D3EE", "#A78BFA", "#FBBF24", "#FB7185", "#34D399", "#64748B")
    head_n = max(1, min(int(head or 5), 8))
    head_rows = ranked[:head_n]
    rest_cost = round(sum(g["cost_total"] for g in ranked[head_n:]), 2)

    parts: list[dict] = []
    for i, g in enumerate(head_rows):
        name = str(g["group_name"] or g["group_code"])
        parts.append(
            {
                "key": f"grp-{g['group_code'] or i}",
                "name": name,
                "full_name": name,
                "amount": g["cost_total"],
                "amount_display": _money(g["cost_total"]),
                "amount_compact": _fmt_compact(g["cost_total"]),
                "color": colors[i % len(colors)],
                "tone": "group",
            }
        )
    if rest_cost > 0:
        parts.append(
            {
                "key": "other",
                "name": "أخرى",
                "full_name": f"باقي المجموعات ({max(0, len(ranked) - head_n)})",
                "amount": rest_cost,
                "amount_display": _money(rest_cost),
                "amount_compact": _fmt_compact(rest_cost),
                "color": colors[-1],
                "tone": "other",
            }
        )

    slices, total = _annular_slices_from_parts(parts)
    named = [s for s in slices if s.get("key") != "other"]
    top1 = float(named[0]["share_pct"]) if named else 0.0
    top3_share = round(sum(float(s.get("share_pct") or 0) for s in named[:3]), 1)

    if not slices:
        decision = "لا مجموعات أصول ضمن الفلتر."
        decision_tone = "ok"
    elif top1 >= 40.0:
        decision = (
            f"مجموعة مهيمنة: «{named[0].get('full_name')}» = {_share_display(top1)} "
            "من تكلفة الأصول — راجع تركز الاستثمار الرأسمالي."
        )
        decision_tone = "high"
    elif top3_share >= 60.0:
        names = " · ".join(str(s.get("name") or "") for s in named[:3])
        decision = (
            f"أعلى 3 مجموعات = {_share_display(top3_share)} من التكلفة "
            f"({names}) — أولوية المتابعة المحاسبية."
        )
        decision_tone = "medium"
    else:
        decision = "توزيع المجموعات متوازن نسبيًا — راقب الانحراف عند الإضافات الجديدة."
        decision_tone = "ok"

    group_rows = []
    peak = max((g["cost_total"] for g in ranked), default=0.01)
    for g in ranked:
        group_rows.append(
            {
                "group_code": g["group_code"],
                "group_name": g["group_name"],
                "asset_count": g["asset_count"],
                "count_display": _qty(g["asset_count"]),
                "cost_total": g["cost_total"],
                "cost_display": _money(g["cost_total"]),
                "cost_compact": _fmt_compact(g["cost_total"]),
                "depr_display": _money(g["depr_total"]),
                "book_display": _money(g["bv_total"]),
                "bar_pct": _pct_css(g["cost_total"], peak),
            }
        )

    return {
        "slices": slices,
        "center": {
            "label": "التكلفة",
            "value_display": _fmt_compact(total),
            "value_full": _money(total),
        },
        "total": total,
        "total_display": _money(total),
        "top3_share": top3_share,
        "top3_share_display": _share_display(top3_share),
        "decision": decision,
        "decision_tone": decision_tone,
        "has_data": bool(slices),
        "group_rows": group_rows,
        "item_count": len(ranked),
    }


def _build_executive_alerts(
    *,
    branch_rows: list[dict],
    group_mix: dict,
    rows: list[dict],
    cost_total: float,
) -> dict:
    alerts: list[dict] = []
    cost_pool = abs(_f(cost_total))

    winners = [b for b in branch_rows if _f(b.get("cost_total")) > 0]
    if cost_pool > 0 and len(winners) >= 2:
        top2 = sorted(winners, key=lambda b: -_f(b.get("cost_total")))[:2]
        share = round(sum(_f(b.get("cost_total")) for b in top2) / cost_pool * 100.0, 1)
        if share >= 60.0:
            alerts.append(
                {
                    "key": "branch_concentration",
                    "severity": "medium",
                    "title": "تركّز التكلفة",
                    "metric": f"{share:.0f}%".replace(",", "."),
                    "detail": "أعلى فرعين من إجمالي تكلفة الأصول",
                    "hint": " · ".join(
                        f"{b.get('branch_name')} {_fmt_compact(_f(b.get('cost_total')))}"
                        for b in top2
                    ),
                }
            )

    worn = []
    worn_cost = 0.0
    for row in rows:
        cost = abs(_f(row.get("cost")))
        book = abs(_f(row.get("book_value")))
        if cost <= 0.005:
            continue
        if book / cost <= 0.05:
            worn.append(row)
            worn_cost = round(worn_cost + cost, 2)
    if worn:
        alerts.append(
            {
                "key": "fully_depreciated",
                "severity": "high",
                "title": "أصول مستهلكة تقريبًا",
                "metric": f"{len(worn)} أصل",
                "detail": f"قيمة دفترية ≤ 5% من التكلفة · تكلفة {_fmt_compact(worn_cost)}",
                "hint": "راجع قرار الاستبدال أو الاستبعاد الدفتري",
            }
        )

    named = [s for s in (group_mix.get("slices") or []) if s.get("key") != "other"]
    if named:
        top1 = float(named[0].get("share_pct") or 0)
        if top1 >= 40.0:
            alerts.append(
                {
                    "key": "group_dominance",
                    "severity": "high" if top1 >= 55.0 else "medium",
                    "title": "مجموعة مهيمنة",
                    "metric": _share_display(top1),
                    "detail": f"«{named[0].get('full_name') or named[0].get('name')}» من تكلفة الأصول",
                    "hint": "تركز رأس المال في مجموعة واحدة",
                }
            )

    severity_rank = {"high": 0, "medium": 1, "info": 2}
    alerts.sort(key=lambda a: (severity_rank.get(str(a.get("severity")), 9), a.get("key") or ""))
    count = len(alerts)
    return {
        "alerts": alerts,
        "count": count,
        "has_alerts": count > 0,
        "summary": f"{count} تنبيه" if count else "لا تنبيهات",
        "ok_message": "لا تنبيهات ضمن الفلتر — توزيع الأصول دون استثناءات حادة",
    }


def _build_top_assets(rows: list[dict], *, limit: int = 10) -> dict:
    top = rows[: max(0, min(int(limit or 10), 25))]
    peak = max((_f(r.get("cost")) for r in top), default=0.01)
    out = []
    for i, row in enumerate(top):
        cost = _f(row.get("cost"))
        out.append(
            {
                "rank": i + 1,
                "asset_code": row.get("asset_code"),
                "asset_name": row.get("asset_name"),
                "branch_name": row.get("branch_name"),
                "group_name": row.get("group_name"),
                "cost": cost,
                "cost_display": row.get("cost_display") or _money(cost),
                "cost_compact": _fmt_compact(cost),
                "book_display": row.get("book_display") or _money(row.get("book_value")),
                "book_compact": _fmt_compact(_f(row.get("book_value"))),
                "bar_pct": _pct_css(cost, peak),
            }
        )
    return {"rows": out, "count": len(out), "has_data": bool(out)}


def fetch_asset_groups() -> list[dict]:
    cache_key = f"assets:groups:{_CACHE_VER}"
    cached = cache.get(cache_key)
    if isinstance(cached, list):
        return cached
    rows = _fetch_all(
        f"""
        SELECT GRP_CODE, GRP_A_NAME
        FROM {_schema()}.FAS_GRP
        ORDER BY GRP_CODE
        """
    )
    out = []
    for row in rows:
        code = str(row.get("GRP_CODE") or "").strip()
        if not code:
            continue
        out.append(
            {
                "code": code,
                "name": str(row.get("GRP_A_NAME") or "").strip() or code,
            }
        )
    try:
        cache.set(cache_key, out, _CACHE_TTL)
    except Exception:
        pass
    return out


def build_assets_report(
    *,
    branch_code: str = "",
    group_code: str = "",
    q: str = "",
) -> dict[str, Any]:
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    branch = str(branch_code or "").strip()
    group = str(group_code or "").strip()
    query = str(q or "").strip()[:80]
    cache_key = f"assets:rep:{_CACHE_VER}:{branch}:{group}:{query.lower()}"
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached

    schema = _schema()
    params: dict[str, Any] = {}
    filters = [f"a.BRN_NO NOT IN ({', '.join(str(int(code)) for code in _EXCLUDED_BRN)})"]
    if branch:
        params["brn"] = _bind_brn(branch)
        filters.append("a.BRN_NO = :brn")
    if group:
        params["gcode"] = group
        filters.append("a.GRP_CODE = :gcode")
    if query:
        params["q_like"] = f"%{query}%"
        filters.append(
            "(UPPER(a.AS_CODE) LIKE UPPER(:q_like)"
            " OR UPPER(NVL(a.AS_A_NAME, ' ')) LIKE UPPER(:q_like))"
        )

    hint = "/*+ INDEX(a FAS_ASSETS_MST_BRN_FK) */" if branch else ""
    raw = _fetch_all(
        f"""
        SELECT {hint}
               a.BRN_NO,
               a.AS_CODE,
               NVL(NULLIF(TRIM(a.AS_A_NAME), ''), a.AS_CODE) AS AS_NAME,
               a.GRP_CODE,
               NVL(g.GRP_A_NAME, NVL(a.GRP_CODE, '—')) AS GRP_NAME,
               a.PRCH_DATE,
               ROUND(NVL(a.END_BLNC_CST, NVL(a.PRCH_CST, 0)), 2) AS COST,
               ROUND(NVL(a.END_BLNC_DEPR, 0), 2) AS DEPR,
               ROUND(
                 NVL(
                   a.END_BV,
                   NVL(a.END_BLNC_CST, 0) - NVL(a.END_BLNC_DEPR, 0)
                 ),
                 2
               ) AS BV
        FROM {schema}.FAS_ASSETS_MST a
        LEFT JOIN {schema}.FAS_GRP g ON g.GRP_CODE = a.GRP_CODE
        WHERE {" AND ".join(filters)}
        """,
        params,
    )

    names = _branch_names()
    rows: list[dict] = []
    by_brn: dict[str, dict[str, Any]] = {}
    cost_total = 0.0
    depr_total = 0.0
    bv_total = 0.0
    for item in raw:
        brn = _brn_code(item.get("BRN_NO"))
        cost = round(float(item.get("COST") or 0), 2)
        depr = round(float(item.get("DEPR") or 0), 2)
        bv = round(float(item.get("BV") or 0), 2)
        brn_name = names.get(brn) or brn or "—"
        rows.append(
            {
                "branch_code": brn,
                "branch_name": brn_name,
                "asset_code": str(item.get("AS_CODE") or "").strip(),
                "asset_name": str(item.get("AS_NAME") or "").strip() or "—",
                "group_code": str(item.get("GRP_CODE") or "").strip(),
                "group_name": str(item.get("GRP_NAME") or "").strip() or "—",
                "purchase_date": _date_label(item.get("PRCH_DATE")),
                "cost": cost,
                "cost_display": _money(cost),
                "depr": depr,
                "depr_display": _money(depr),
                "book_value": bv,
                "book_display": _money(bv),
            }
        )
        bucket = by_brn.setdefault(
            brn,
            {
                "branch_code": brn,
                "branch_name": brn_name,
                "asset_count": 0,
                "cost_total": 0.0,
                "depr_total": 0.0,
                "bv_total": 0.0,
            },
        )
        bucket["asset_count"] += 1
        bucket["cost_total"] = round(bucket["cost_total"] + cost, 2)
        bucket["depr_total"] = round(bucket["depr_total"] + depr, 2)
        bucket["bv_total"] = round(bucket["bv_total"] + bv, 2)
        cost_total += cost
        depr_total += depr
        bv_total += bv

    rows.sort(key=lambda r: (-r["cost"], -r["book_value"], r["asset_code"]))
    cost_total = round(cost_total, 2)
    depr_total = round(depr_total, 2)
    bv_total = round(bv_total, 2)
    max_cost = max((b["cost_total"] for b in by_brn.values()), default=0.0)
    max_bv = max((b["bv_total"] for b in by_brn.values()), default=0.0)
    branch_rows = []
    for bucket in sorted(
        by_brn.values(),
        key=lambda b: (-b["bv_total"], -b["cost_total"], b["branch_code"]),
    ):
        count = bucket["asset_count"]
        branch_rows.append(
            {
                "branch_code": bucket["branch_code"],
                "branch_name": bucket["branch_name"],
                "asset_count": count,
                "count_display": _qty(count),
                "cost_total": bucket["cost_total"],
                "cost_display": _money(bucket["cost_total"]),
                "cost_compact": _fmt_compact(bucket["cost_total"]),
                "depr_total": bucket["depr_total"],
                "depr_display": _money(bucket["depr_total"]),
                "bv_total": bucket["bv_total"],
                "book_display": _money(bucket["bv_total"]),
                "book_compact": _fmt_compact(bucket["bv_total"]),
                "bar_pct": _pct_css(bucket["cost_total"], max_cost),
                "bv_bar_pct": _pct_css(bucket["bv_total"], max_bv),
            }
        )

    depr_ratio = round(depr_total / cost_total * 100.0, 1) if cost_total > 0 else 0.0
    structure = _build_asset_structure(cost_total, depr_total, bv_total)
    group_mix = _build_group_mix(rows)
    executive_alerts = _build_executive_alerts(
        branch_rows=branch_rows,
        group_mix=group_mix,
        rows=rows,
        cost_total=cost_total,
    )
    top_assets = _build_top_assets(rows)

    line_count = len(rows)
    result = {
        "kpis": {
            "asset_count": line_count,
            "asset_count_display": _qty(line_count),
            "branch_count": len(by_brn),
            "cost_total": cost_total,
            "cost_display": _money(cost_total),
            "cost_compact": _fmt_compact(cost_total),
            "depr_total": depr_total,
            "depr_display": _money(depr_total),
            "depr_compact": _fmt_compact(depr_total),
            "book_total": bv_total,
            "book_display": _money(bv_total),
            "book_compact": _fmt_compact(bv_total),
            "depr_ratio": depr_ratio,
            "depr_ratio_display": _share_display(depr_ratio),
        },
        "branch_rows": branch_rows,
        "structure": structure,
        "group_mix": group_mix,
        "group_rows": group_mix.get("group_rows") or [],
        "executive_alerts": executive_alerts,
        "top_assets": top_assets,
        "rows": rows,
        "filters": {"branch": branch, "group": group, "q": query},
    }
    try:
        cache.set(cache_key, result, _CACHE_TTL)
    except Exception:
        pass
    return result


def _xls_num(value: Any) -> str:
    return f"{float(value or 0):.2f}"


def build_assets_excel(report: dict[str, Any]) -> HttpResponse:
    """تصدير سجل الأصول إلى Excel بخلايا رقمية مرتبة."""
    rows = report.get("rows") or []
    branches = report.get("branch_rows") or []
    kpis = report.get("kpis") or {}
    filters = report.get("filters") or {}
    buf = io.StringIO()
    buf.write("\ufeff")
    buf.write(
        "<html xmlns:o=\"urn:schemas-microsoft-com:office:office\" "
        "xmlns:x=\"urn:schemas-microsoft-com:office:excel\" "
        "xmlns=\"http://www.w3.org/TR/REC-html40\">"
        "<head><meta charset=\"utf-8\">"
        "<!--[if gte mso 9]><xml><x:ExcelWorkbook><x:ExcelWorksheets>"
        "<x:ExcelWorksheet><x:Name>الأصول</x:Name>"
        "<x:WorksheetOptions><x:DisplayRightToLeft/></x:WorksheetOptions>"
        "</x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
        "<style>"
        "table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:11px;}"
        "th,td{border:1px solid #94a3b8;padding:4px 7px;white-space:nowrap;vertical-align:middle;}"
        "th{background:#1e3a5f;color:#fff;font-weight:700;}"
        "th.cost{background:#166534;}"
        "th.depr{background:#9a3412;}"
        "th.book{background:#5b21b6;}"
        "td.txt{mso-number-format:'\\@';}"
        "td.num{mso-number-format:'\\#\\,\\#\\#0\\.00';text-align:left;}"
        "td.int{mso-number-format:'\\#\\,\\#\\#0';text-align:left;}"
        "td.cost{background:#ecfdf3;color:#166534;font-weight:700;}"
        "td.depr{background:#fff7ed;color:#9a3412;font-weight:700;}"
        "td.book{background:#f5f3ff;color:#5b21b6;font-weight:700;}"
        "tr.even td{background:#f8fafc;}"
        "tr.even td.cost{background:#dcfce7;}"
        "tr.even td.depr{background:#ffedd5;}"
        "tr.even td.book{background:#ede9fe;}"
        "tr.foot td{background:#dbeafe;font-weight:800;}"
        "h3,p,caption{font-family:Tahoma,Arial;text-align:right;}"
        "caption{font-size:13px;font-weight:700;margin:8px 0;}"
        ".sub{font-size:10px;color:#475569;font-weight:400;}"
        "</style></head><body dir=\"rtl\">"
    )
    bits = [f"عدد الأصول {escape(str(kpis.get('asset_count_display') or 0))}"]
    if filters.get("branch"):
        bits.append(f"فرع {escape(str(filters.get('branch')))}")
    if filters.get("group"):
        bits.append(f"مجموعة {escape(str(filters.get('group')))}")
    if filters.get("q"):
        bits.append(f"بحث {escape(str(filters.get('q')))}")
    buf.write(
        "<table><caption>سجل الأصول الثابتة — مرتّب حسب التكلفة من الأكبر إلى الأصغر"
        f'<br><span class="sub">{" · ".join(bits)}</span></caption><thead><tr>'
        "<th>#</th>"
        "<th>كود الأصل</th>"
        "<th>اسم الأصل</th>"
        "<th>كود المجموعة</th>"
        "<th>المجموعة</th>"
        "<th>رقم الفرع</th>"
        "<th>الفرع</th>"
        "<th>تاريخ الشراء</th>"
        "<th class=\"cost\">التكلفة</th>"
        "<th class=\"depr\">الإهلاك</th>"
        "<th class=\"book\">القيمة الدفترية</th>"
        "</tr></thead><tbody>"
    )
    for i, row in enumerate(rows, 1):
        even = " class=\"even\"" if i % 2 == 0 else ""
        buf.write(f"<tr{even}>")
        buf.write(f'<td class="int">{i}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("asset_code") or ""))}</td>')
        buf.write(f"<td>{escape(str(row.get('asset_name') or ''))}</td>")
        buf.write(f'<td class="txt">{escape(str(row.get("group_code") or ""))}</td>')
        buf.write(f"<td>{escape(str(row.get('group_name') or ''))}</td>")
        buf.write(f'<td class="txt">{escape(str(row.get("branch_code") or ""))}</td>')
        buf.write(f"<td>{escape(str(row.get('branch_name') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('purchase_date') or ''))}</td>")
        buf.write(f'<td class="num cost">{_xls_num(row.get("cost"))}</td>')
        buf.write(f'<td class="num depr">{_xls_num(row.get("depr"))}</td>')
        buf.write(f'<td class="num book">{_xls_num(row.get("book_value"))}</td>')
        buf.write("</tr>")
    buf.write(
        '<tr class="foot">'
        "<td></td><td></td><td></td><td></td><td></td><td></td><td></td>"
        "<td>الإجمالي</td>"
        f'<td class="num cost">{_xls_num(kpis.get("cost_total"))}</td>'
        f'<td class="num depr">{_xls_num(kpis.get("depr_total"))}</td>'
        f'<td class="num book">{_xls_num(kpis.get("book_total"))}</td>'
        "</tr>"
    )
    buf.write("</tbody></table>")

    buf.write(
        "<table><caption>إحصائيات الفروع — من الأكبر إلى الأصغر حسب التكلفة"
        f'<br><span class="sub">{escape(str(kpis.get("branch_count") or 0))} فرع</span></caption>'
        "<thead><tr>"
        "<th>#</th><th>رقم الفرع</th><th>الفرع</th>"
        "<th>عدد الأصول</th>"
        "<th class=\"cost\">التكلفة</th>"
        "<th class=\"depr\">الإهلاك</th>"
        "<th class=\"book\">القيمة الدفترية</th>"
        "</tr></thead><tbody>"
    )
    for i, row in enumerate(branches, 1):
        even = " class=\"even\"" if i % 2 == 0 else ""
        buf.write(f"<tr{even}>")
        buf.write(f'<td class="int">{i}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("branch_code") or ""))}</td>')
        buf.write(f"<td>{escape(str(row.get('branch_name') or ''))}</td>")
        buf.write(f'<td class="int">{int(row.get("asset_count") or 0)}</td>')
        buf.write(f'<td class="num cost">{_xls_num(row.get("cost_total"))}</td>')
        buf.write(f'<td class="num depr">{_xls_num(row.get("depr_total"))}</td>')
        buf.write(f'<td class="num book">{_xls_num(row.get("bv_total"))}</td>')
        buf.write("</tr>")
    buf.write(
        '<tr class="foot">'
        "<td></td><td></td><td>الإجمالي</td>"
        f'<td class="int">{int(kpis.get("asset_count") or 0)}</td>'
        f'<td class="num cost">{_xls_num(kpis.get("cost_total"))}</td>'
        f'<td class="num depr">{_xls_num(kpis.get("depr_total"))}</td>'
        f'<td class="num book">{_xls_num(kpis.get("book_total"))}</td>'
        "</tr>"
    )
    buf.write("</tbody></table></body></html>")

    filename = "assets.xls"
    resp = HttpResponse(
        buf.getvalue().encode("utf-8"),
        content_type="application/vnd.ms-excel; charset=utf-8",
    )
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp
