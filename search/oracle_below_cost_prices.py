"""أصناف مسعّرة بأقل من متوسط التكلفة (خسارة تسعير) — كشاشة أسعار أونكس.

مثال أونكس (صنف 103397 · حبه · مخزن 701):
  السعر 0.24 · متوسط التكلفة 0.18 = I_CWTAVG(الوحدة الرئيسية) × P_SIZE
المقارنة: I_PRICE < متوسط تكلفة نفس وحدة السعر
المستوى: 1 (سعر بيع) · كل الوحدات المسعّرة
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
_CACHE_VER = "v9"
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


def _filter_key(*, wh: str, group_code: str, q: str) -> str:
    return f"purch:below_cost:{_CACHE_VER}:{wh}:{group_code}:{q}"


def _rows_from_oracle(rows: list[dict], *, wh_code: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        price = _f(r.get("I_PRICE"), 2)
        avg = _f(r.get("AVG_COST"), 2)
        unit_cost = _f(r.get("UNIT_COST"), 2)
        gap = _f(r.get("GAP"), 2)
        gap_pct = _f(r.get("GAP_PCT"), 2)
        qty = _f(r.get("AVL_QTY"), 3)
        out.append(
            {
                "item_code": str(r.get("I_CODE") or "").strip(),
                "item_name": str(r.get("I_NAME") or "").strip(),
                "unit": str(r.get("ITM_UNT") or "").strip(),
                "g_code": str(r.get("G_CODE") or "").strip(),
                "g_name": str(r.get("G_NAME") or "").strip() or "—",
                "wh_code": str(r.get("W_CODE") or wh_code).strip(),
                "price": price,
                "price_display": f"{price:.2f}",
                "avg_cost": avg,
                "avg_cost_display": f"{avg:.2f}",
                "unit_cost": unit_cost,
                "unit_cost_display": f"{unit_cost:.2f}",
                "gap": gap,
                "gap_display": f"{gap:.2f}",
                "gap_pct": gap_pct,
                "gap_pct_display": f"{gap_pct:.2f}",
                "qty": qty,
                "qty_display": f"{qty:g}",
            }
        )
    return out


def _base_sql(schema: str, *, group_sql: str, item_sql: str) -> str:
    """متوسط تكلفة وحدة السعر = I_CWTAVG للرئيسية × P_SIZE (كما في أونكس)."""
    avg_sql = """
        CASE
          WHEN NVL(w.I_CWTAVG, 0) > 0 THEN w.I_CWTAVG
          ELSE NVL(m.I_CWTAVG, 0)
        END
    """
    unit_cost_sql = f"({avg_sql}) * NVL(p.P_SIZE, 1)"
    return f"""
        SELECT /*+ LEADING(p) USE_NL(m d w g) INDEX(p INV_PRC_LEV_NO_INDX) */
          p.I_CODE,
          m.I_NAME,
          p.ITM_UNT,
          m.G_CODE,
          NVL(g.G_A_NAME, g.G_E_NAME) AS G_NAME,
          p.W_CODE,
          ROUND(p.I_PRICE, 4) AS I_PRICE,
          ROUND(({unit_cost_sql}), 4) AS AVG_COST,
          ROUND(({unit_cost_sql}), 4) AS UNIT_COST,
          ROUND(p.I_PRICE - ({unit_cost_sql}), 4) AS GAP,
          ROUND(
            (p.I_PRICE - ({unit_cost_sql}))
            / NULLIF(({unit_cost_sql}), 0) * 100
          , 2) AS GAP_PCT,
          ROUND(
            NVL(w.AVL_QTY, 0) * NVL(d.P_SIZE, 1) / NULLIF(NVL(p.P_SIZE, 1), 0)
          , 3) AS AVL_QTY
        FROM {schema}.IAS_ITEM_PRICE p
        JOIN {schema}.IAS_ITM_MST m
          ON m.I_CODE = p.I_CODE
        JOIN {schema}.IAS_ITM_DTL d
          ON d.I_CODE = p.I_CODE
         AND NVL(d.MAIN_UNIT, 0) = 1
        JOIN {schema}.IAS_ITM_WCODE w
          ON w.I_CODE = p.I_CODE
         AND w.W_CODE = p.W_CODE
         AND w.ITM_UNT = d.ITM_UNT
         AND w.AVL_QTY > 0
        LEFT JOIN {schema}.GROUP_DETAILS g
          ON g.G_CODE = m.G_CODE
        WHERE p.LEV_NO = 1
          AND p.W_CODE = :wh
          AND p.I_PRICE > 0
          AND NVL(p.P_SIZE, 0) > 0
          AND NVL(({avg_sql}), 0) > 0
          AND p.I_PRICE < ({unit_cost_sql})
          AND (m.INACTIVE IS NULL OR m.INACTIVE = 0)
          {group_sql}
          {item_sql}
    """


def fetch_below_cost_items(
    *,
    warehouse_code: str,
    group_code: str = "",
    item_q: str = "",
    limit: int = _PAGE_SIZE,
    offset: int = 0,
    with_total: bool = True,
) -> dict[str, Any]:
    """أصناف سعرها أقل من متوسط تكلفة نفس وحدة السعر (كأونكس)."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")

    wh = str(warehouse_code or "").strip()
    if not wh:
        raise OracleStockError("اختر مخزناً محدداً قبل العرض.")

    gcode = str(group_code or "").strip()
    q = str(item_q or "").strip()
    lim = max(1, min(int(limit or _PAGE_SIZE), _FETCH_LIMIT))
    off = max(0, int(offset or 0))

    ck = _filter_key(wh=wh, group_code=gcode, q=q)
    page_ck = f"{ck}:p:{off}:{lim}"
    cached = cache.get(page_ck)
    if cached is not None:
        return cached

    schema = _schema()
    params: dict[str, Any] = {"wh": _bind_wh(wh)}

    group_sql = ""
    if gcode:
        params["gcode"] = _bind_gcode(gcode)
        group_sql = "AND m.G_CODE = :gcode"

    item_sql = ""
    if q:
        params["iq"] = f"%{q}%"
        params["iq_exact"] = q
        item_sql = f"""
            AND (
              m.I_CODE = :iq_exact
              OR UPPER(m.I_NAME) LIKE UPPER(:iq)
              OR EXISTS (
                SELECT 1
                FROM {schema}.IAS_ITM_UNT_BARCODE b
                WHERE b.I_CODE = m.I_CODE
                  AND b.BARCODE = :iq_exact
              )
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
            count_rows = _fetch_all(f"SELECT COUNT(*) AS CNT FROM ({inner})", params)
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
            ORDER BY GAP ASC, I_NAME, I_CODE, ITM_UNT
          ) x
          WHERE ROWNUM <= :off + :lim
        )
        WHERE RN > :off
        """,
        page_params,
    )
    rows = _rows_from_oracle(rows_raw, wh_code=wh)
    shown = len(rows)
    has_more = (off + shown) < total_exact if with_total else shown >= lim

    report = {
        "kpis": {
            "total_matching": total_exact if with_total else shown,
            "shown": shown,
            "has_more": has_more,
            "wh_code": wh,
            "group_code": gcode,
        },
        "rows": rows,
        "meta": {"offset": off, "limit": lim},
    }
    cache.set(page_ck, report, _CACHE_TTL)
    return report


def build_below_cost_excel(
    *,
    warehouse_code: str,
    group_code: str = "",
    item_q: str = "",
    wh_name: str = "",
) -> HttpResponse:
    report = fetch_below_cost_items(
        warehouse_code=warehouse_code,
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
        "<x:ExcelWorksheet><x:Name>أقل من التكلفة</x:Name>"
        "<x:WorksheetOptions><x:DisplayRightToLeft/></x:WorksheetOptions>"
        "</x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
        "<style>"
        "table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:11px;}"
        "th,td{border:1px solid #94a3b8;padding:4px 7px;white-space:nowrap;}"
        "th{background:#d9e2f3;font-weight:700;}"
        "td.txt{mso-number-format:'\\@';}"
        "td.num{mso-number-format:'0\\.00';}"
        "td.pct{mso-number-format:'0\\.00';}"
        "td.qty{mso-number-format:'0\\.000';}"
        "td.int{mso-number-format:'0';text-align:center;}"
        "</style></head><body dir=\"rtl\">"
        f"<table><caption>تسعير أقل من التكلفة — مخزن {wh_label}"
        f" · مستوى 1 سعر بيع · كل الوحدات"
        f" · {int(kpis.get('total_matching') or 0)} صف"
        f"</caption><thead><tr>"
        "<th>#</th><th>الرقم</th><th>اسم الصنف</th><th>الوحدة</th>"
        "<th>المجموعة</th><th>المخزن</th><th>متوسط الوحدة</th>"
        "<th>سعر الوحدة</th><th>الفرق</th><th>نسبة الخسارة %</th><th>الكمية</th>"
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
        buf.write(f'<td class="num">{float(r.get("unit_cost") or 0):.2f}</td>')
        buf.write(f'<td class="num">{float(r.get("price") or 0):.2f}</td>')
        buf.write(f'<td class="num">{float(r.get("gap") or 0):.2f}</td>')
        buf.write(f'<td class="pct">{float(r.get("gap_pct") or 0):.2f}</td>')
        buf.write(f'<td class="qty">{float(r.get("qty") or 0):.3f}</td>')
        buf.write("</tr>")
    buf.write("</tbody></table></body></html>")
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.ms-excel; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="below-cost-prices.xls"'
    return resp
