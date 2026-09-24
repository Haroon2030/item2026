"""مقارنة أسعار شراء المورد بين الفروع/المخازن — كشف تلاعب التسعير."""

from __future__ import annotations

import io
from datetime import date
from html import escape
from typing import Any

from django.core.cache import cache
from django.http import HttpResponse

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _bind_brn,
    _branch_names,
    _date_params,
    _fetch_all,
    _hung_ok,
    _schema,
    fetch_warehouse_options,
    oracle_enabled,
)

_CACHE_TTL = 600
_CACHE_VER = "v11"
_MAX_DAYS = 366


def _f(value: Any, digits: int = 4) -> float:
    try:
        return round(float(value or 0), digits)
    except (TypeError, ValueError):
        return 0.0


def _fmt_money(value: Any, digits: int = 4) -> str:
    return f"{_f(value, digits):,.{digits}f}"


def _fmt_pct(value: Any) -> str:
    return f"{_f(value, 2):,.2f}%"


def _norm_code(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].replace("-", "").isdigit():
        text = text[:-2]
    return text


def _bind_vendor(vendor_code: str) -> str:
    return str(vendor_code or "").strip()


def _validate(date_from, date_to, vendor: str):
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")
    if (d_to - d_from).days > _MAX_DAYS:
        raise OracleStockError(f"الفترة القصوى {_MAX_DAYS} يوم.")
    if not vendor:
        raise OracleStockError("اختر مورداً لمقارنة أسعار الشراء.")
    return d_from, d_to


def _build_branch_pivot(summary: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """صف لكل صنف×وحدة وأعمدة متجاورة لكل فرع (مقارنة سعر الشراء لنفس الوحدة)."""
    branch_map: dict[str, str] = {}
    for row in summary:
        for loc in row.get("locations") or []:
            bc = _norm_code(loc.get("branch_code"))
            if not bc:
                continue
            branch_map[bc] = str(loc.get("branch_name") or "").strip() or f"فرع {bc}"

    pivot_branches = [
        {"code": code, "name": name}
        for code, name in sorted(branch_map.items(), key=lambda x: (x[1], x[0]))
    ]

    pivot_rows: list[dict[str, Any]] = []
    for row in summary:
        by_brn: dict[str, dict[str, Any]] = {}
        for loc in row.get("locations") or []:
            bc = _norm_code(loc.get("branch_code"))
            if not bc:
                continue
            prev = by_brn.get(bc)
            # عدة مخازن بنفس الفرع ونفس الوحدة: أقل سعر شراء
            if prev is None or _f(loc.get("pack_price"), 6) < _f(
                prev.get("pack_price"), 6
            ):
                by_brn[bc] = loc

        prices = [_f(loc.get("pack_price"), 6) for loc in by_brn.values()]
        min_p = min(prices) if prices else 0.0
        max_p = max(prices) if prices else 0.0
        cells: list[dict[str, Any]] = []
        for br in pivot_branches:
            loc = by_brn.get(br["code"])
            if not loc:
                cells.append(
                    {
                        "branch_code": br["code"],
                        "has_price": False,
                        "price": None,
                        "price_display": "—",
                        "bill_date": "",
                        "bill_date_display": "—",
                        "bill_no": "",
                        "bill_no_display": "—",
                        "is_min": False,
                        "is_max": False,
                        "title": "",
                    }
                )
                continue
            price = _f(loc.get("pack_price"), 6)
            bill_date = str(loc.get("bill_date") or "").strip()[:10]
            bill_no = _norm_code(loc.get("bill_no"))
            title = (
                f"{loc.get('wh_code') or ''} {loc.get('wh_name') or ''}"
                f" · فاتورة {bill_no or '—'}"
                f" · {bill_date or '—'}"
            ).strip()
            cells.append(
                {
                    "branch_code": br["code"],
                    "has_price": True,
                    "price": price,
                    "price_display": loc.get("pack_price_display")
                    or _fmt_money(price),
                    "is_min": len(prices) > 1 and abs(price - min_p) < 1e-9,
                    "is_max": len(prices) > 1
                    and max_p > min_p
                    and abs(price - max_p) < 1e-9,
                    "title": title,
                    "bill_no": bill_no,
                    "bill_no_display": bill_no or "—",
                    "bill_date": bill_date,
                    "bill_date_display": bill_date or "—",
                    "wh_code": loc.get("wh_code") or "",
                }
            )

        pivot_rows.append(
            {
                "item_code": row.get("item_code"),
                "item_name": row.get("item_name"),
                "unit": row.get("unit") or "—",
                "is_flag": bool(row.get("is_flag")),
                "spread_pct": row.get("spread_pct"),
                "spread_pct_display": row.get("spread_pct_display"),
                "branch_cells": cells,
            }
        )
    return pivot_branches, pivot_rows


def _fetch_last_prices(
    date_from: date,
    date_to: date,
    *,
    vendor: str,
    branch: str = "",
    warehouse: str = "",
) -> list[dict]:
    """آخر سعر شراء لكل صنف×مخزن×وحدة من فواتير المورد في الفترة."""
    schema = _schema()
    dates = _date_params(date_from, date_to)
    params: dict[str, Any] = {
        "d_from": dates["d_from"],
        "d_to_excl": dates["d_to_excl"],
        "vendor": _bind_vendor(vendor),
    }
    filters = [
        "m.BILL_DATE >= :d_from",
        "m.BILL_DATE < :d_to_excl",
        _hung_ok("m"),
        "m.V_CODE = :vendor",
        "d.I_CODE IS NOT NULL",
        "NVL(d.W_CODE, m.W_CODE) IS NOT NULL",
        "NVL(d.I_PRICE, 0) <> 0",
    ]
    if branch:
        params["brn"] = _bind_brn(branch)
        filters.append("m.BRN_NO = :brn")
    if warehouse:
        params["wh"] = _norm_code(warehouse)
        filters.append("TO_CHAR(NVL(d.W_CODE, m.W_CODE)) = :wh")

    where = " AND ".join(filters)
    return _fetch_all(
        f"""
        SELECT TO_CHAR(lp.I_CODE) AS I_CODE,
               TO_CHAR(lp.W_CODE) AS W_CODE,
               TO_CHAR(lp.BRN_NO) AS BRN_NO,
               lp.I_PRICE,
               lp.UNIT_PRICE,
               lp.P_SIZE,
               lp.ITM_UNT,
               lp.BILL_DATE,
               TO_CHAR(lp.BILL_NO) AS BILL_NO,
               lp.V_NAME,
               NVL(NULLIF(TRIM(i.I_NAME), ''), TO_CHAR(lp.I_CODE)) AS I_NAME
        FROM (
          SELECT /*+ LEADING(m d) USE_NL(d) INDEX(d INDX_SER_PI_BILL_DTL) */
                 d.I_CODE AS I_CODE,
                 NVL(d.W_CODE, m.W_CODE) AS W_CODE,
                 m.BRN_NO AS BRN_NO,
                 ROUND(NVL(d.I_PRICE, 0), 6) AS I_PRICE,
                 ROUND(
                   CASE
                     WHEN NVL(d.P_SIZE, 0) > 0 THEN NVL(d.I_PRICE, 0) / d.P_SIZE
                     ELSE NVL(d.I_PRICE, 0)
                   END,
                   6
                 ) AS UNIT_PRICE,
                 ROUND(NVL(d.P_SIZE, 1), 4) AS P_SIZE,
                 NVL(NULLIF(TRIM(TO_CHAR(d.ITM_UNT)), ''), '—') AS ITM_UNT,
                 TO_CHAR(m.BILL_DATE, 'YYYY-MM-DD') AS BILL_DATE,
                 m.BILL_NO AS BILL_NO,
                 NVL(NULLIF(TRIM(m.V_NAME), ''), TO_CHAR(m.V_CODE)) AS V_NAME,
                 ROW_NUMBER() OVER (
                   PARTITION BY d.I_CODE,
                                NVL(d.W_CODE, m.W_CODE),
                                NVL(NULLIF(TRIM(TO_CHAR(d.ITM_UNT)), ''), '—')
                   ORDER BY m.BILL_DATE DESC NULLS LAST,
                            m.BILL_NO DESC NULLS LAST
                 ) AS RN
          FROM {schema}.IAS_PI_BILL_MST m
          JOIN {schema}.IAS_PI_BILL_DTL d
            ON d.BILL_SER = m.BILL_SER
          WHERE {where}
        ) lp
        LEFT JOIN {schema}.IAS_ITM_MST i
          ON i.I_CODE = lp.I_CODE
        WHERE lp.RN = 1
        """,
        params,
    )


def _wh_name_map() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for w in fetch_warehouse_options(active_only=False) or []:
            code = _norm_code(w.get("code"))
            if code:
                out[code] = str(w.get("name") or "").strip() or code
    except Exception:
        pass
    return out


def build_vendor_price_compare(
    date_from,
    date_to,
    *,
    vendor_code: str,
    branch_code: str = "",
    warehouse_code: str = "",
    min_spread_pct: float = 0.0,
    diffs_only: bool = False,
) -> dict[str, Any]:
    vendor = _bind_vendor(vendor_code)
    brn = _norm_code(branch_code)
    wh = _norm_code(warehouse_code)
    d_from, d_to = _validate(date_from, date_to, vendor)
    min_spread = max(0.0, _f(min_spread_pct, 2))

    cache_key = (
        f"vpc:{_CACHE_VER}:{d_from}:{d_to}:{vendor}:"
        f"{brn or 'ALL'}:{wh or 'ALL'}:{min_spread:.2f}:{int(diffs_only)}"
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached

    raw = _fetch_last_prices(
        d_from, d_to, vendor=vendor, branch=brn, warehouse=wh
    )
    branch_names = _branch_names()
    wh_names = _wh_name_map()

    by_item: dict[str, dict[str, Any]] = {}
    detail_rows: list[dict[str, Any]] = []
    vendor_name = vendor

    for row in raw or []:
        ic = _norm_code(row.get("I_CODE"))
        wc = _norm_code(row.get("W_CODE"))
        if not ic or not wc:
            continue
        unit_price = _f(row.get("UNIT_PRICE"), 6)
        pack_price = _f(row.get("I_PRICE"), 6)
        if unit_price == 0 and pack_price == 0:
            continue
        brn_no = _norm_code(row.get("BRN_NO"))
        item_name = str(row.get("I_NAME") or "").strip() or ic
        vendor_name = str(row.get("V_NAME") or "").strip() or vendor_name or vendor
        unit = str(row.get("ITM_UNT") or "").strip() or "—"
        loc = {
            "item_code": ic,
            "item_name": item_name,
            "wh_code": wc,
            "wh_name": wh_names.get(wc) or wc,
            "branch_code": brn_no,
            "branch_name": branch_names.get(brn_no) or (f"فرع {brn_no}" if brn_no else "—"),
            "unit": unit,
            "p_size": _f(row.get("P_SIZE"), 4) or 1.0,
            "pack_price": pack_price,
            "pack_price_display": _fmt_money(pack_price),
            "unit_price": unit_price,
            "unit_price_display": _fmt_money(unit_price),
            "bill_date": str(row.get("BILL_DATE") or "")[:10],
            "bill_no": _norm_code(row.get("BILL_NO")),
        }
        detail_rows.append(loc)
        # مقارنة فقط ضمن نفس الصنف×الوحدة
        bucket_key = f"{ic}\0{unit}"
        bucket = by_item.setdefault(
            bucket_key,
            {
                "item_code": ic,
                "item_name": item_name,
                "unit": unit,
                "locations": [],
                "prices": [],
            },
        )
        bucket["locations"].append(loc)
        bucket["prices"].append(pack_price)
        if item_name and item_name != ic:
            bucket["item_name"] = item_name

    summary: list[dict[str, Any]] = []
    flagged = 0
    for bucket in by_item.values():
        prices = bucket["prices"]
        if not prices:
            continue
        min_p = min(prices)
        max_p = max(prices)
        spread_pct = 0.0
        if min_p > 0:
            spread_pct = round(((max_p - min_p) / min_p) * 100.0, 2)
        elif max_p > 0:
            spread_pct = 100.0
        is_flag = (
            spread_pct >= min_spread
            and len({round(p, 4) for p in prices}) > 1
        )
        if diffs_only and not is_flag:
            continue
        if is_flag:
            flagged += 1

        locs = sorted(
            bucket["locations"],
            key=lambda r: (r["pack_price"], r["wh_code"]),
        )
        cheapest = locs[0] if locs else {}
        dearest = locs[-1] if locs else {}
        unit = bucket.get("unit") or "—"
        summary.append(
            {
                "item_code": bucket["item_code"],
                "item_name": bucket["item_name"],
                "unit": unit,
                "loc_count": len(locs),
                "branch_count": len({r["branch_code"] for r in locs if r["branch_code"]}),
                "min_price": min_p,
                "min_price_display": _fmt_money(min_p),
                "min_unit": unit,
                "min_bill_date": cheapest.get("bill_date") or "—",
                "min_bill_no": cheapest.get("bill_no") or "—",
                "max_price": max_p,
                "max_price_display": _fmt_money(max_p),
                "max_unit": unit,
                "max_bill_date": dearest.get("bill_date") or "—",
                "max_bill_no": dearest.get("bill_no") or "—",
                "spread": round(max_p - min_p, 6),
                "spread_display": _fmt_money(max_p - min_p),
                "spread_pct": spread_pct,
                "spread_pct_display": _fmt_pct(spread_pct),
                "min_wh": cheapest.get("wh_code") or "—",
                "min_wh_name": cheapest.get("wh_name") or "—",
                "min_branch": cheapest.get("branch_name") or "—",
                "max_wh": dearest.get("wh_code") or "—",
                "max_wh_name": dearest.get("wh_name") or "—",
                "max_branch": dearest.get("branch_name") or "—",
                "is_flag": is_flag,
                "locations": locs,
            }
        )

    summary.sort(
        key=lambda r: (-r["spread_pct"], -r["spread"], r["item_code"], r["unit"])
    )
    detail_rows.sort(
        key=lambda r: (r["item_code"], r["unit"], r["pack_price"], r["wh_code"])
    )

    # عند «فروقات فقط» صفّي التفاصيل لأصناف الملخص (صنف×وحدة)
    if diffs_only:
        keep = {(r["item_code"], r["unit"]) for r in summary}
        detail_rows = [
            r for r in detail_rows if (r["item_code"], r["unit"]) in keep
        ]

    pivot_branches, pivot_rows = _build_branch_pivot(summary)

    result = {
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "vendor_code": vendor,
        "vendor_name": vendor_name,
        "filters": {
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat(),
            "vendor": vendor,
            "branch": brn,
            "warehouse": wh,
            "min_spread_pct": min_spread,
            "diffs_only": diffs_only,
        },
        "kpis": {
            "item_count": len(summary),
            "location_count": len(detail_rows),
            "branch_col_count": len(pivot_branches),
            "flagged_count": flagged,
            "max_spread_pct": summary[0]["spread_pct"] if summary else 0.0,
            "max_spread_pct_display": (
                summary[0]["spread_pct_display"] if summary else "0.00%"
            ),
        },
        "rows": summary,
        "detail_rows": detail_rows,
        "pivot_branches": pivot_branches,
        "pivot_rows": pivot_rows,
    }
    try:
        cache.set(cache_key, result, _CACHE_TTL)
    except Exception:
        pass
    return result


def _xls_num(value: Any, digits: int = 4) -> str:
    try:
        return f"{float(value or 0):.{digits}f}"
    except (TypeError, ValueError):
        return "0.0000"


def build_vendor_price_compare_excel(report: dict[str, Any]) -> HttpResponse:
    rows = report.get("rows") or []
    pivot_branches = report.get("pivot_branches") or []
    pivot_rows = report.get("pivot_rows") or []
    filters = report.get("filters") or {}
    kpis = report.get("kpis") or {}
    buf = io.StringIO()
    buf.write("\ufeff")
    buf.write(
        "<html xmlns:o=\"urn:schemas-microsoft-com:office:office\" "
        "xmlns:x=\"urn:schemas-microsoft-com:office:excel\" "
        "xmlns=\"http://www.w3.org/TR/REC-html40\">"
        "<head><meta charset=\"utf-8\">"
        "<!--[if gte mso 9]><xml><x:ExcelWorkbook><x:ExcelWorksheets>"
        "<x:ExcelWorksheet><x:Name>مقارنة أسعار</x:Name>"
        "<x:WorksheetOptions><x:DisplayRightToLeft/></x:WorksheetOptions>"
        "</x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
        "<style>"
        "table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:11px;margin-bottom:18px;}"
        "th,td{border:1px solid #94a3b8;padding:4px 7px;white-space:nowrap;}"
        "th{background:#d9e2f3;color:#1a2b33;font-weight:700;}"
        "th.spread{background:#9a3412;}"
        "th.price{background:#166534;}"
        "td.txt{mso-number-format:'\\@';}"
        "td.num{mso-number-format:'\\#\\,\\#\\#0\\.0000';}"
        "td.pct{mso-number-format:'\\#\\,\\#\\#0\\.00';}"
        "td.flag{background:#ffedd5;color:#9a3412;font-weight:700;}"
        "td.min{background:#dcfce7;color:#166534;font-weight:700;}"
        "td.max{background:#ffedd5;color:#9a3412;font-weight:700;}"
        "tr.even td{background:#f7f7f7;}"
        "caption{font-family:Tahoma,Arial;font-size:13px;font-weight:700;text-align:right;}"
        ".sub{font-size:10px;color:#475569;font-weight:400;}"
        "</style></head><body dir=\"rtl\">"
    )
    title = (
        f"مقارنة أسعار شراء المورد {escape(str(report.get('vendor_name') or ''))}"
        f" ({escape(str(report.get('vendor_code') or ''))})"
    )
    buf.write(
        f"<table><caption>{title}"
        f'<br><span class="sub">{escape(str(report.get("period_label") or ""))}'
        f" · أصناف {escape(str(kpis.get('item_count') or 0))}"
        f" · فروقات {escape(str(kpis.get('flagged_count') or 0))}</span></caption>"
        "<thead><tr>"
        "<th>#</th><th>رقم الصنف</th><th>الاسم</th><th>الوحدة</th>"
        "<th>مخازن</th><th>فروع</th>"
        "<th class=\"price\">أقل سعر شراء</th>"
        "<th>تاريخ الأقل</th><th>فاتورة الأقل</th>"
        "<th class=\"price\">أعلى سعر شراء</th>"
        "<th>تاريخ الأعلى</th><th>فاتورة الأعلى</th>"
        "<th class=\"spread\">الفرق %</th>"
        "<th>أرخص فرع/مخزن</th><th>أغلى فرع/مخزن</th>"
        "</tr></thead><tbody>"
    )
    for i, row in enumerate(rows, 1):
        even = ' class="even"' if i % 2 == 0 else ""
        flag = ' class="flag"' if row.get("is_flag") else ""
        buf.write(f"<tr{even}>")
        buf.write(f"<td>{i}</td>")
        buf.write(f'<td class="txt">{escape(str(row.get("item_code") or ""))}</td>')
        buf.write(f"<td>{escape(str(row.get('item_name') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('unit') or row.get('min_unit') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('loc_count') or 0))}</td>")
        buf.write(f"<td>{escape(str(row.get('branch_count') or 0))}</td>")
        buf.write(f'<td class="num">{_xls_num(row.get("min_price"))}</td>')
        buf.write(f"<td>{escape(str(row.get('min_bill_date') or ''))}</td>")
        buf.write(f'<td class="txt">{escape(str(row.get("min_bill_no") or ""))}</td>')
        buf.write(f'<td class="num">{_xls_num(row.get("max_price"))}</td>')
        buf.write(f"<td>{escape(str(row.get('max_bill_date') or ''))}</td>")
        buf.write(f'<td class="txt">{escape(str(row.get("max_bill_no") or ""))}</td>')
        buf.write(f'<td class="pct{flag}">{_xls_num(row.get("spread_pct"), 2)}</td>')
        buf.write(
            f"<td>{escape(str(row.get('min_branch') or ''))} / "
            f"{escape(str(row.get('min_wh') or ''))}</td>"
        )
        buf.write(
            f"<td>{escape(str(row.get('max_branch') or ''))} / "
            f"{escape(str(row.get('max_wh') or ''))}</td>"
        )
        buf.write("</tr>")
    buf.write("</tbody></table>")

    buf.write(
        "<table><caption>سعر الشراء حسب الفرع — نفس الوحدة</caption>"
        "<thead><tr>"
        "<th>#</th><th>رقم الصنف</th><th>الاسم</th><th>الوحدة</th>"
    )
    for br in pivot_branches:
        buf.write(
            f'<th class="price">{escape(str(br.get("name") or br.get("code") or ""))}</th>'
        )
        buf.write("<th>تاريخ</th><th>رقم الفاتورة</th>")
    buf.write('<th class="spread">الفرق %</th></tr></thead><tbody>')
    for i, row in enumerate(pivot_rows, 1):
        even = ' class="even"' if i % 2 == 0 else ""
        buf.write(f"<tr{even}>")
        buf.write(f"<td>{i}</td>")
        buf.write(f'<td class="txt">{escape(str(row.get("item_code") or ""))}</td>')
        buf.write(f"<td>{escape(str(row.get('item_name') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('unit') or ''))}</td>")
        for cell in row.get("branch_cells") or []:
            if cell.get("has_price"):
                cls = "num"
                if cell.get("is_min"):
                    cls += " min"
                elif cell.get("is_max"):
                    cls += " max"
                buf.write(f'<td class="{cls}">{_xls_num(cell.get("price"))}</td>')
                buf.write(
                    f"<td>{escape(str(cell.get('bill_date_display') or cell.get('bill_date') or ''))}</td>"
                )
                buf.write(
                    f'<td class="txt">{escape(str(cell.get("bill_no_display") or cell.get("bill_no") or ""))}</td>'
                )
            else:
                buf.write("<td>—</td><td>—</td><td>—</td>")
        flag = ' class="flag"' if row.get("is_flag") else ""
        buf.write(f'<td class="pct{flag}">{_xls_num(row.get("spread_pct"), 2)}</td>')
        buf.write("</tr>")
    buf.write("</tbody></table></body></html>")

    payload = buf.getvalue().encode("utf-8")
    filename = f"vendor-price-compare-{filters.get('vendor') or 'v'}-{filters.get('date_from') or ''}.xls"
    resp = HttpResponse(payload, content_type="application/vnd.ms-excel; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp
