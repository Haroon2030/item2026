"""الأصول الثابتة المسجّلة على الفروع — FAS_ASSETS_MST."""

from __future__ import annotations

import io
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
_CACHE_VER = "v4"

# مصنع المنهل، ركن التغليف، البلاستيك
_EXCLUDED_BRN = (17, 16, 13)


def excluded_asset_branch_codes() -> set[str]:
    return {str(code) for code in _EXCLUDED_BRN}


def _money(value: Any) -> str:
    return f"{float(value or 0):,.2f}"


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
    branch_rows = []
    for bucket in sorted(
        by_brn.values(),
        key=lambda b: (-b["cost_total"], -b["bv_total"], b["branch_code"]),
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
                "depr_total": bucket["depr_total"],
                "depr_display": _money(bucket["depr_total"]),
                "bv_total": bucket["bv_total"],
                "book_display": _money(bucket["bv_total"]),
                "bar_pct": (
                    round(bucket["cost_total"] / max_cost * 100.0, 1)
                    if max_cost > 0
                    else 0.0
                ),
            }
        )

    line_count = len(rows)
    result = {
        "kpis": {
            "asset_count": line_count,
            "asset_count_display": _qty(line_count),
            "branch_count": len(by_brn),
            "cost_total": cost_total,
            "cost_display": _money(cost_total),
            "depr_total": depr_total,
            "depr_display": _money(depr_total),
            "book_total": bv_total,
            "book_display": _money(bv_total),
        },
        "branch_rows": branch_rows,
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
        buf.write(f"<td>{escape(str(row.get('asset_code') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('asset_name') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('group_code') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('group_name') or ''))}</td>")
        buf.write(f'<td class="int">{escape(str(row.get("branch_code") or ""))}</td>')
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
        buf.write(f'<td class="int">{escape(str(row.get("branch_code") or ""))}</td>')
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
