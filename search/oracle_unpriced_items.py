"""أصناف بلا سعر بيع على الوحدة الرئيسية لمخزن/مستوى معيّن.

غير مسعّر = لا يوجد صف في IAS_ITEM_PRICE للوحدة الرئيسية
            بنفس المخزن والمستوى بسعر > 0.
"""

from __future__ import annotations

import io
from typing import Any

from django.core.cache import cache
from django.http import HttpResponse
from django.utils.html import escape

from .oracle_stock import (
    OracleStockError,
    _bind_gcode,
    _fetch_all,
    _schema,
    oracle_enabled,
)

_CACHE_TTL = 600
_CACHE_VER = "v2"
_PAGE_SIZE = 500
_EXCEL_LIMIT = 100000
_FETCH_LIMIT = 100000


def _f(value: Any, nd: int = 2) -> float:
    try:
        return round(float(value or 0), nd)
    except (TypeError, ValueError):
        return 0.0


def _bind_wh(raw: str):
    text = str(raw or "").strip()
    if not text:
        return text
    try:
        return int(text)
    except ValueError:
        return text


def _filter_key(
    *,
    wh: str,
    lev: int,
    group_code: str,
    q: str,
) -> str:
    return f"purch:unpriced:{_CACHE_VER}:{wh}:{lev}:{group_code}:{q}"


def _rows_from_oracle(rows: list[dict], *, lev: int, wh_code: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        qty = _f(r.get("AVL_QTY"), 3)
        avg = _f(r.get("AVG_COST"), 4)
        out.append(
            {
                "item_code": str(r.get("I_CODE") or "").strip(),
                "item_name": str(r.get("I_NAME") or "").strip(),
                "unit": str(r.get("ITM_UNT") or "").strip(),
                "g_code": str(r.get("G_CODE") or "").strip(),
                "g_name": str(r.get("G_NAME") or "").strip() or "—",
                "wh_code": str(r.get("W_CODE") or wh_code).strip(),
                "lev_no": int(r.get("LEV_NO") or lev),
                "qty": qty,
                "qty_display": f"{qty:g}",
                "avg_cost": avg,
                "avg_cost_display": f"{avg:g}",
            }
        )
    return out


def _base_sql(schema: str, *, group_sql: str, item_sql: str) -> str:
    """قيادة بالصنف + الوحدة الرئيسية ثم استبعاد المسعّر عبر PK السعر."""
    return f"""
        SELECT /*+ LEADING(m d) USE_NL(g w) */
          m.I_CODE,
          m.I_NAME,
          d.ITM_UNT,
          m.G_CODE,
          NVL(g.G_A_NAME, g.G_E_NAME) AS G_NAME,
          :wh AS W_CODE,
          :lev AS LEV_NO,
          ROUND(NVL(w.AVL_QTY, 0), 3) AS AVL_QTY,
          ROUND(
            CASE
              WHEN NVL(w.I_CWTAVG, 0) > 0 THEN w.I_CWTAVG
              ELSE NVL(m.I_CWTAVG, 0)
            END
          , 4) AS AVG_COST
        FROM {schema}.IAS_ITM_MST m
        JOIN {schema}.IAS_ITM_DTL d
          ON d.I_CODE = m.I_CODE
         AND NVL(d.MAIN_UNIT, 0) = 1
        LEFT JOIN {schema}.GROUP_DETAILS g
          ON g.G_CODE = m.G_CODE
        LEFT JOIN {schema}.IAS_ITM_WCODE w
          ON w.I_CODE = m.I_CODE
         AND w.W_CODE = :wh
         AND w.ITM_UNT = d.ITM_UNT
        WHERE (m.INACTIVE IS NULL OR m.INACTIVE = 0)
          {group_sql}
          {item_sql}
          AND NOT EXISTS (
            SELECT /*+ INDEX(p IASITMPR_PK) */ 1
            FROM {schema}.IAS_ITEM_PRICE p
            WHERE p.LEV_NO = :lev
              AND p.I_CODE = m.I_CODE
              AND p.ITM_UNT = d.ITM_UNT
              AND p.W_CODE = :wh
              AND p.I_PRICE > 0
          )
    """


def fetch_unpriced_items(
    *,
    warehouse_code: str,
    lev_no: int = 1,
    group_code: str = "",
    item_q: str = "",
    limit: int = _PAGE_SIZE,
    offset: int = 0,
    with_total: bool = True,
) -> dict[str, Any]:
    """أصناف بلا سعر على الوحدة الرئيسية لمخزن ومستوى."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")

    wh = str(warehouse_code or "").strip()
    if not wh:
        raise OracleStockError("اختر مخزناً محدداً قبل العرض.")

    lev = int(lev_no or 1)
    gcode = str(group_code or "").strip()
    q = str(item_q or "").strip()
    lim = max(1, min(int(limit or _PAGE_SIZE), _FETCH_LIMIT))
    off = max(0, int(offset or 0))

    ck = _filter_key(wh=wh, lev=lev, group_code=gcode, q=q)
    page_ck = f"{ck}:p:{off}:{lim}"
    cached = cache.get(page_ck)
    if cached is not None:
        return cached

    schema = _schema()
    params: dict[str, Any] = {
        "wh": _bind_wh(wh),
        "lev": lev,
    }

    group_sql = ""
    if gcode:
        params["gcode"] = _bind_gcode(gcode)
        group_sql = "AND m.G_CODE = :gcode"

    item_sql = ""
    if q:
        params["iq"] = f"%{q}%"
        params["iq_exact"] = q
        item_sql = """
            AND (
              m.I_CODE = :iq_exact
              OR UPPER(m.I_NAME) LIKE UPPER(:iq)
            )
        """

    inner = _base_sql(schema, group_sql=group_sql, item_sql=item_sql)

    total_exact = 0
    if with_total:
        count_ck = f"{ck}:count"
        hit = cache.get(count_ck)
        if hit is not None:
            total_exact = int(hit)
        else:
            count_rows = _fetch_all(
                f"SELECT COUNT(*) AS CNT FROM ({inner})",
                params,
            )
            total_exact = int((count_rows[0] or {}).get("CNT") or 0) if count_rows else 0
            cache.set(count_ck, total_exact, _CACHE_TTL)

    page_params = dict(params)
    page_params["lim"] = lim
    page_params["off"] = off
    rows_raw = _fetch_all(
        f"""
        SELECT * FROM (
          SELECT x.*, ROWNUM AS RN FROM (
            SELECT * FROM ({inner})
            ORDER BY I_NAME, I_CODE
          ) x
          WHERE ROWNUM <= :off + :lim
        )
        WHERE RN > :off
        """,
        page_params,
    )
    rows = _rows_from_oracle(rows_raw, lev=lev, wh_code=wh)
    shown = len(rows)
    has_more = (off + shown) < total_exact if with_total else shown >= lim

    report = {
        "kpis": {
            "total_matching": total_exact if with_total else shown,
            "shown": shown,
            "has_more": has_more,
            "lev_no": lev,
            "wh_code": wh,
            "group_code": gcode,
        },
        "rows": rows,
        "meta": {"offset": off, "limit": lim},
    }
    cache.set(page_ck, report, _CACHE_TTL)
    return report


def build_unpriced_excel(
    *,
    warehouse_code: str,
    lev_no: int = 1,
    group_code: str = "",
    item_q: str = "",
    wh_name: str = "",
) -> HttpResponse:
    """تصدير الأصناف غير المسعّرة إلى Excel."""
    report = fetch_unpriced_items(
        warehouse_code=warehouse_code,
        lev_no=lev_no,
        group_code=group_code,
        item_q=item_q,
        limit=_EXCEL_LIMIT,
        offset=0,
        with_total=True,
    )
    kpis = report.get("kpis") or {}
    wh = str(warehouse_code or "").strip()
    wh_label = escape(str(wh_name or "").strip() or wh)
    buf = io.StringIO()
    buf.write("\ufeff")
    buf.write(
        '<html xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:x="urn:schemas-microsoft-com:office:excel" '
        'xmlns="http://www.w3.org/TR/REC-html40">'
        "<head><meta charset=\"utf-8\">"
        "<!--[if gte mso 9]><xml><x:ExcelWorkbook><x:ExcelWorksheets>"
        "<x:ExcelWorksheet><x:Name>غير مسعّر</x:Name>"
        "<x:WorksheetOptions><x:DisplayRightToLeft/></x:WorksheetOptions>"
        "</x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
        "<style>"
        "table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:11px;}"
        "th,td{border:1px solid #94a3b8;padding:4px 7px;white-space:nowrap;}"
        "th{background:#d9e2f3;font-weight:700;}"
        "td.txt{mso-number-format:'\\@';}"
        "td.num{mso-number-format:'0\\.0000';}"
        "td.qty{mso-number-format:'0\\.000';}"
        "td.int{mso-number-format:'0';text-align:center;}"
        "</style></head><body dir=\"rtl\">"
        f"<table><caption>أصناف غير مسعّرة — مخزن {wh_label}"
        f" · مستوى السعر 1"
        f" · {int(kpis.get('total_matching') or 0)} صنف"
        f"</caption><thead><tr>"
        "<th>#</th><th>الرقم</th><th>اسم الصنف</th><th>الوحدة</th>"
        "<th>المجموعة</th><th>المخزن</th><th>الكمية</th>"
        "<th>متوسط التكلفة</th>"
        "</tr></thead><tbody>"
    )
    for i, r in enumerate(report.get("rows") or [], start=1):
        buf.write("<tr>")
        buf.write(f'<td class="int">{i}</td>')
        buf.write(f'<td class="txt">{escape(str(r.get("item_code") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(r.get("item_name") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(r.get("unit") or ""))}</td>')
        g_label = str(r.get("g_name") or "").strip()
        if r.get("g_code"):
            g_label = f'{r.get("g_code")} — {g_label}'
        buf.write(f'<td class="txt">{escape(g_label)}</td>')
        buf.write(f'<td class="txt">{escape(str(r.get("wh_code") or ""))}</td>')
        buf.write(f'<td class="qty">{float(r.get("qty") or 0):.3f}</td>')
        buf.write(f'<td class="num">{float(r.get("avg_cost") or 0):.4f}</td>')
        buf.write("</tr>")
    buf.write("</tbody></table></body></html>")
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.ms-excel; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="unpriced-items.xls"'
    return resp
