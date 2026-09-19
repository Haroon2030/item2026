"""أصناف عُدّل سعر بيعها (المستوى 1) خلال يوم محدد — من سجل IAS_ITEM_PRICE_HISTORY."""

from __future__ import annotations

import io
from datetime import date, datetime
from typing import Any

from django.http import HttpResponse
from django.utils.html import escape

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _bind_brn,
    _bind_gcode,
    _date_params,
    _fetch_all,
    _schema,
    oracle_enabled,
)

_LEV = 1
_FETCH_LIMIT = 8000
_EXCEL_LIMIT = 50000

_AUD_TYPE_LABELS = {
    1: "إضافة",
    2: "تعديل",
    3: "حذف",
}


def _f(value: Any, nd: int = 4) -> float:
    try:
        return round(float(value or 0), nd)
    except (TypeError, ValueError):
        return 0.0


def _money(value: Any, nd: int = 4) -> str:
    return f"{_f(value, nd):,.{nd}f}".rstrip("0").rstrip(".") or "0"


def _bind_wh(raw: str):
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return text


def _aud_label(code: Any) -> str:
    try:
        n = int(code)
    except (TypeError, ValueError):
        return "—"
    return _AUD_TYPE_LABELS.get(n, f"نوع {n}")


def _fetch_last_buy_info(codes: list[str]) -> dict[str, dict[str, Any]]:
    """آخر توريد لكل صنف: تكلفة الوحدة الأساسية + كود/اسم المورد من آخر فاتورة شراء."""
    out: dict[str, dict[str, Any]] = {}
    clean = [str(c).strip() for c in codes if str(c).strip()]
    if not clean or not oracle_enabled():
        return out

    schema = _schema()
    batch_size = 400
    for i in range(0, len(clean), batch_size):
        chunk = clean[i : i + batch_size]
        params: dict[str, Any] = {}
        keys: list[str] = []
        for j, code in enumerate(chunk):
            key = f"c{j}"
            keys.append(f":{key}")
            params[key] = code
        sql = f"""
            SELECT I_CODE, UNIT_COST, V_CODE, V_NAME
            FROM (
              SELECT
                d.I_CODE,
                CASE
                  WHEN NVL(d.P_SIZE, 0) > 0 THEN ROUND(d.I_PRICE / d.P_SIZE, 6)
                  ELSE ROUND(d.I_PRICE, 6)
                END AS UNIT_COST,
                m.V_CODE AS V_CODE,
                NVL(
                  NULLIF(TRIM(m.V_NAME), ''),
                  NULLIF(TRIM(vd.V_A_NAME), '')
                ) AS V_NAME,
                ROW_NUMBER() OVER (
                  PARTITION BY d.I_CODE
                  ORDER BY m.BILL_DATE DESC NULLS LAST, m.BILL_NO DESC NULLS LAST
                ) AS RN
              FROM {schema}.IAS_PI_BILL_DTL d
              JOIN {schema}.IAS_PI_BILL_MST m
                ON m.BILL_NO = d.BILL_NO
               AND m.BILL_SER = d.BILL_SER
               AND m.BILL_DOC_TYPE = d.BILL_DOC_TYPE
              LEFT JOIN {schema}.V_DETAILS vd
                ON vd.V_CODE = m.V_CODE
              WHERE d.I_CODE IN ({", ".join(keys)})
                AND (m.HUNG IS NULL OR m.HUNG = 0)
                AND NVL(d.I_PRICE, 0) > 0
            )
            WHERE RN = 1
        """
        try:
            for row in _fetch_all(sql, params):
                ic = str(row.get("I_CODE") or "").strip()
                cost = _f(row.get("UNIT_COST"), 6)
                if not ic or cost <= 0:
                    continue
                v_code = str(row.get("V_CODE") or "").strip()
                v_name = str(row.get("V_NAME") or "").strip() or v_code or "—"
                out[ic] = {
                    "unit_cost": cost,
                    "vendor_code": v_code,
                    "vendor_name": v_name,
                }
        except Exception:  # noqa: BLE001
            continue
    return out


def _attach_last_buy(rows: list[dict[str, Any]]) -> None:
    """يلصق آخر سعر توريد (تكلفة الوحدة × P_SIZE) واسم آخر مورد نزل."""
    if not rows:
        return
    codes = sorted(
        {str(r.get("item_code") or "").strip() for r in rows if r.get("item_code")}
    )
    buys = _fetch_last_buy_info(codes)
    for r in rows:
        ic = str(r.get("item_code") or "").strip()
        info = buys.get(ic) or {}
        unit_cost = float(info.get("unit_cost") or 0)
        if unit_cost <= 0:
            r["last_buy"] = 0.0
            r["last_buy_display"] = "—"
            r["last_vendor_code"] = ""
            r["last_vendor_name"] = "—"
            continue
        p_size = float(r.get("p_size") or 1) or 1.0
        last_buy = round(unit_cost * p_size, 4)
        r["last_buy"] = last_buy
        r["last_buy_display"] = _money(last_buy)
        r["last_vendor_code"] = str(info.get("vendor_code") or "").strip()
        r["last_vendor_name"] = str(info.get("vendor_name") or "").strip() or "—"


def fetch_price_changes(
    *,
    day: date | datetime | str | None = None,
    warehouse_code: str = "",
    branch_code: str = "",
    group_code: str = "",
    item_q: str = "",
    changed_only: bool = True,
    limit: int = _FETCH_LIMIT,
) -> dict[str, Any]:
    """حركات تعديل سعر البيع المستوى 1 ليوم واحد من IAS_ITEM_PRICE_HISTORY."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")

    d = _as_date(day or date.today())
    dates = _date_params(d, d)
    schema = _schema()
    lim = max(1, min(int(limit or _FETCH_LIMIT), _EXCEL_LIMIT))

    params: dict[str, Any] = {
        "lev": _LEV,
        "d_from": dates["d_from"],
        "d_to_excl": dates["d_to_excl"],
        "lim": lim,
    }

    filters: list[str] = [
        "h.LEV_NO = :lev",
        "h.AUD_DATE >= :d_from",
        "h.AUD_DATE < :d_to_excl",
    ]

    wh = _bind_wh(warehouse_code)
    if wh is not None and warehouse_code.strip():
        params["wh"] = wh
        filters.append("h.W_CODE = :wh")

    brn = str(branch_code or "").strip()
    if brn:
        params["brn"] = _bind_brn(brn)
        filters.append("h.BRN_NO = :brn")

    gcode = str(group_code or "").strip()
    if gcode:
        params["gcode"] = _bind_gcode(gcode)
        filters.append("m.G_CODE = :gcode")

    iq = str(item_q or "").strip()
    if iq:
        params["iq"] = f"%{iq}%"
        filters.append(
            "(h.I_CODE LIKE :iq OR UPPER(NVL(m.I_NAME, '')) LIKE UPPER(:iq))"
        )

    if changed_only:
        filters.append("NVL(h.I_PRICE, 0) <> NVL(h.PREV_I_PRICE, 0)")

    where_sql = " AND ".join(filters)
    # متوسط تكلفة وحدة السعر = (I_CWTAVG للمخزن على الوحدة الرئيسية أو الصنف) × P_SIZE
    avg_base_sql = """
        CASE
          WHEN NVL(w.I_CWTAVG, 0) > 0 THEN w.I_CWTAVG
          ELSE NVL(m.I_CWTAVG, 0)
        END
    """
    unit_cost_sql = f"({avg_base_sql}) * NVL(h.P_SIZE, 1)"
    sql = f"""
        SELECT * FROM (
          SELECT
            h.AUD_NO,
            h.AUD_TYPE,
            h.AUD_DATE,
            h.I_CODE,
            h.ITM_UNT,
            h.P_SIZE,
            h.W_CODE,
            h.BRN_NO,
            h.LEV_NO,
            h.I_PRICE,
            h.PREV_I_PRICE,
            h.AUD_U_ID,
            h.DOC_NO,
            h.DOC_DATE,
            m.I_NAME,
            m.G_CODE,
            g.G_A_NAME AS G_NAME,
            NVL(u.U_A_NAME, NVL(u.U_E_NAME, TO_CHAR(h.AUD_U_ID))) AS USER_NAME,
            ROUND(({unit_cost_sql}), 4) AS AVG_COST
          FROM {schema}.IAS_ITEM_PRICE_HISTORY h
          LEFT JOIN {schema}.IAS_ITM_MST m
            ON m.I_CODE = h.I_CODE
          LEFT JOIN {schema}.GROUP_DETAILS g
            ON g.G_CODE = m.G_CODE
          LEFT JOIN {schema}.USER_R u
            ON u.U_ID = h.AUD_U_ID
          LEFT JOIN {schema}.IAS_ITM_DTL d
            ON d.I_CODE = h.I_CODE
           AND NVL(d.MAIN_UNIT, 0) = 1
          LEFT JOIN {schema}.IAS_ITM_WCODE w
            ON w.I_CODE = h.I_CODE
           AND w.W_CODE = h.W_CODE
           AND w.ITM_UNT = d.ITM_UNT
          WHERE {where_sql}
          ORDER BY h.AUD_DATE DESC, h.AUD_NO DESC
        )
        WHERE ROWNUM <= :lim
    """
    raw = _fetch_all(sql, params)

    rows: list[dict[str, Any]] = []
    up_n = down_n = same_n = 0
    for r in raw:
        price = _f(r.get("I_PRICE"), 4)
        prev = _f(r.get("PREV_I_PRICE"), 4)
        avg_cost = _f(r.get("AVG_COST"), 4)
        diff = round(price - prev, 4)
        if diff > 0:
            up_n += 1
            direction = "up"
            direction_label = "رفع"
        elif diff < 0:
            down_n += 1
            direction = "down"
            direction_label = "تخفيض"
        else:
            same_n += 1
            direction = "same"
            direction_label = "بدون فرق"

        aud_dt = r.get("AUD_DATE")
        if isinstance(aud_dt, datetime):
            when_label = aud_dt.strftime("%Y-%m-%d %H:%M")
        elif aud_dt:
            when_label = str(aud_dt)
        else:
            when_label = "—"

        rows.append(
            {
                "aud_no": int(r.get("AUD_NO") or 0),
                "aud_type": int(r.get("AUD_TYPE") or 0),
                "aud_type_label": _aud_label(r.get("AUD_TYPE")),
                "when": when_label,
                "item_code": str(r.get("I_CODE") or "").strip(),
                "item_name": str(r.get("I_NAME") or "").strip() or "—",
                "unit": str(r.get("ITM_UNT") or "").strip(),
                "p_size": _f(r.get("P_SIZE"), 3),
                "wh_code": str(r.get("W_CODE") or "").strip(),
                "branch_code": str(r.get("BRN_NO") or "").strip(),
                "lev_no": int(r.get("LEV_NO") or _LEV),
                "price": price,
                "price_display": _money(price),
                "prev_price": prev,
                "prev_price_display": _money(prev),
                "avg_cost": avg_cost,
                "avg_cost_display": _money(avg_cost) if avg_cost > 0 else "—",
                "last_buy": 0.0,
                "last_buy_display": "—",
                "last_vendor_code": "",
                "last_vendor_name": "—",
                "diff": diff,
                "diff_display": _money(diff),
                "direction": direction,
                "direction_label": direction_label,
                "user_id": str(r.get("AUD_U_ID") or "").strip(),
                "user_name": str(r.get("USER_NAME") or "").strip() or "—",
                "g_code": str(r.get("G_CODE") or "").strip(),
                "g_name": str(r.get("G_NAME") or "").strip() or "—",
                "doc_no": str(r.get("DOC_NO") or "").strip() or "—",
            }
        )

    _attach_last_buy(rows)

    return {
        "filters": {
            "day": d.isoformat(),
            "warehouse_code": str(warehouse_code or "").strip(),
            "branch_code": brn,
            "group_code": gcode,
            "item_q": iq,
            "changed_only": bool(changed_only),
            "lev_no": _LEV,
        },
        "kpis": {
            "total": len(rows),
            "total_display": f"{len(rows):,}",
            "up": up_n,
            "up_display": f"{up_n:,}",
            "down": down_n,
            "down_display": f"{down_n:,}",
            "same": same_n,
            "same_display": f"{same_n:,}",
            "capped": len(rows) >= lim,
        },
        "rows": rows,
    }


def build_price_changes_excel(report: dict[str, Any]) -> HttpResponse:
    """تصدير تعديلات سعر البيع إلى Excel."""
    rows = report.get("rows") or []
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
        "<x:ExcelWorksheet><x:Name>تعديل أسعار</x:Name>"
        "<x:WorksheetOptions><x:DisplayRightToLeft/></x:WorksheetOptions>"
        "</x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
        "<style>"
        "table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:12px;}"
        "th,td{border:1px solid #94a3b8;padding:4px 6px;}"
        "th{background:#1e293b;color:#fff;}"
        "td.num{mso-number-format:'\\#\\,\\#\\#0\\.0000';}"
        "td.txt{mso-number-format:'\\@';}"
        "tr.up td{background:#ecfdf5;}"
        "tr.down td{background:#fef2f2;}"
        "</style></head><body dir=\"rtl\">"
        f"<p><b>تعديلات سعر البيع · المستوى 1</b> — يوم "
        f"{escape(str(filters.get('day') or ''))}"
        f" · صفوف {escape(str(kpis.get('total_display') or 0))}</p>"
        "<table><thead><tr>"
        "<th>#</th><th>الوقت</th><th>النوع</th><th>الصنف</th><th>الاسم</th>"
        "<th>الوحدة</th><th>المخزن</th><th>الفرع</th>"
        "<th>السابق</th><th>الجديد</th><th>متوسط التكلفة</th>"
        "<th>آخر سعر توريد</th><th>آخر مورد</th>"
        "<th>الفرق</th><th>الاتجاه</th><th>المستخدم</th><th>المستند</th>"
        "</tr></thead><tbody>"
    )
    for i, row in enumerate(rows, 1):
        cls = row.get("direction") or ""
        buf.write(f'<tr class="{escape(str(cls))}">')
        buf.write(f"<td>{i}</td>")
        buf.write(f'<td class="txt">{escape(str(row.get("when") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("aud_type_label") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("item_code") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("item_name") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("unit") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("wh_code") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("branch_code") or ""))}</td>')
        buf.write(f'<td class="num">{escape(str(row.get("prev_price_display") or ""))}</td>')
        buf.write(f'<td class="num">{escape(str(row.get("price_display") or ""))}</td>')
        buf.write(f'<td class="num">{escape(str(row.get("avg_cost_display") or "—"))}</td>')
        buf.write(f'<td class="num">{escape(str(row.get("last_buy_display") or "—"))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("last_vendor_name") or "—"))}</td>')
        buf.write(f'<td class="num">{escape(str(row.get("diff_display") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("direction_label") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("user_name") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("doc_no") or ""))}</td>')
        buf.write("</tr>")
    buf.write("</tbody></table></body></html>")
    day = str(filters.get("day") or date.today().isoformat())
    resp = HttpResponse(
        buf.getvalue().encode("utf-8"),
        content_type="application/vnd.ms-excel; charset=utf-8",
    )
    resp["Content-Disposition"] = f'attachment; filename="price-changes-{day}.xls"'
    return resp
