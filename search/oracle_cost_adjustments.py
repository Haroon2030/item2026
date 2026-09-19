"""تسوية تكاليف المخزون — STK_ADJUSTMENT / DET حيث ADJUST_TYPE = 2.

فهارس مؤكّدة (ALL_IND_COLUMNS على :schema):
  STKADJMST_PK(DOC_SER) · INDX_SER_STK_ADJUSTMENT_DET(DOC_SER) ·
  STKADJDTL_ICODE_FK(I_CODE, ITM_UNT) · STKADJMST_ACACY_FK(A_CODE, A_CY → ACCOUNT_CURR)
  — لا فهرس DOC_DATE؛ أبقِ الفترة قصيرة؛ LEADING(m) ثم NL إلى d على DOC_SER.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta
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
    _hung_ok,
    _schema,
    oracle_enabled,
)

# مسار سريع: تاريخ على رأس STK_ADJUSTMENT ثم JOIN التفاصيل على DOC_SER
# (فهرس INDX_SER_STK_ADJUSTMENT_DET). لا يوجد فهرس DOC_DATE — أبقِ الفترة قصيرة.

_FETCH_LIMIT = 8000
_EXCEL_LIMIT = 50000
_ADJUST_COST = 2
_DTL_TYPE_VENDOR = 4
_DTL_TYPE_LABELS = {
    1: "صندوق",
    2: "بنك",
    3: "عميل",
    4: "مورد",
    5: "مركز تكلفة",
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


def fetch_cost_adjustments(
    date_from: date | datetime | str | None = None,
    date_to: date | datetime | str | None = None,
    *,
    branch_code: str = "",
    warehouse_code: str = "",
    group_code: str = "",
    item_q: str = "",
    account_q: str = "",
    limit: int = _FETCH_LIMIT,
) -> dict[str, Any]:
    """بنود تسوية التكاليف (ADJUST_TYPE=2) مع الحساب المحاسبي التفصيلي."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")

    today = date.today()
    d_from = _as_date(date_from or (today - timedelta(days=6)))
    d_to = _as_date(date_to or today)
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    dates = _date_params(d_from, d_to)
    schema = _schema()
    lim = max(1, min(int(limit or _FETCH_LIMIT), _EXCEL_LIMIT))

    params: dict[str, Any] = {
        "d_from": dates["d_from"],
        "d_to_excl": dates["d_to_excl"],
        "lim": lim,
        "adj_type": _ADJUST_COST,
    }

    filters: list[str] = [
        "m.DOC_DATE >= :d_from",
        "m.DOC_DATE < :d_to_excl",
        "m.ADJUST_TYPE = :adj_type",
        _hung_ok("m"),
        "d.I_CODE IS NOT NULL",
    ]

    brn = str(branch_code or "").strip()
    if brn:
        params["brn"] = _bind_brn(brn)
        filters.append("m.BRN_NO = :brn")

    wh = _bind_wh(warehouse_code)
    if wh is not None and str(warehouse_code or "").strip():
        params["wh"] = wh
        filters.append("d.W_CODE = :wh")

    gcode = str(group_code or "").strip()
    if gcode:
        params["gcode"] = _bind_gcode(gcode)
        filters.append("i.G_CODE = :gcode")

    iq = str(item_q or "").strip()
    if iq:
        params["iq"] = f"%{iq}%"
        params["iq_exact"] = iq
        filters.append(
            "(d.I_CODE = :iq_exact OR UPPER(NVL(i.I_NAME, '')) LIKE UPPER(:iq))"
        )

    aq = str(account_q or "").strip()
    if aq:
        params["aq"] = f"%{aq}%"
        params["aq_exact"] = aq
        filters.append(
            "(m.A_CODE = :aq_exact OR m.AC_CODE_DTL = :aq_exact"
            " OR UPPER(NVL(a.A_NAME, '')) LIKE UPPER(:aq)"
            " OR UPPER(NVL(vd.V_A_NAME, '')) LIKE UPPER(:aq))"
        )

    where_sql = " AND ".join(filters)
    sql = f"""
        SELECT * FROM (
          SELECT /*+ LEADING(m d) USE_NL(d)
                     INDEX(d INDX_SER_STK_ADJUSTMENT_DET) */
            m.DOC_NO,
            m.DOC_SER,
            m.DOC_DATE,
            m.STK_DESC,
            m.BRN_NO,
            m.A_CODE,
            m.AC_CODE_DTL,
            m.AC_DTL_TYP,
            m.V_CODE AS M_V_CODE,
            m.AD_U_ID,
            d.I_CODE,
            d.W_CODE,
            d.ITM_UNT,
            d.P_SIZE,
            d.INC_COST,
            d.WTAVG,
            d.REAL_COST,
            d.V_CODE AS D_V_CODE,
            d.PLS_MINS,
            i.I_NAME,
            i.G_CODE,
            NVL(NULLIF(TRIM(a.A_NAME), ''), TO_CHAR(m.A_CODE)) AS A_NAME,
            CASE
              WHEN m.AC_DTL_TYP = {_DTL_TYPE_VENDOR} THEN NVL(NULLIF(TRIM(vd.V_A_NAME), ''), TO_CHAR(m.AC_CODE_DTL))
              ELSE TO_CHAR(m.AC_CODE_DTL)
            END AS DTL_NAME,
            NVL(u.U_A_NAME, NVL(u.U_E_NAME, TO_CHAR(m.AD_U_ID))) AS USER_NAME
          FROM {schema}.STK_ADJUSTMENT m
          JOIN {schema}.STK_ADJUSTMENT_DET d
            ON d.DOC_SER = m.DOC_SER
          LEFT JOIN {schema}.IAS_ITM_MST i
            ON i.I_CODE = d.I_CODE
          LEFT JOIN {schema}.ACCOUNT a
            ON a.A_CODE = m.A_CODE
          LEFT JOIN {schema}.V_DETAILS vd
            ON m.AC_DTL_TYP = {_DTL_TYPE_VENDOR}
           AND vd.V_CODE = m.AC_CODE_DTL
          LEFT JOIN {schema}.USER_R u
            ON u.U_ID = m.AD_U_ID
          WHERE {where_sql}
          ORDER BY m.DOC_DATE DESC, m.DOC_NO DESC, d.I_CODE
        )
        WHERE ROWNUM <= :lim
    """
    raw = _fetch_all(sql, params)

    rows: list[dict[str, Any]] = []
    doc_keys: set[str] = set()
    acct_keys: set[str] = set()
    sum_inc = 0.0

    for r in raw:
        inc = r.get("INC_COST")
        try:
            inc_cost = float(inc) if inc is not None else None
        except (TypeError, ValueError):
            inc_cost = None
        if inc_cost is not None:
            sum_inc += inc_cost

        doc_no = r.get("DOC_NO")
        doc_ser = r.get("DOC_SER")
        doc_keys.add(f"{doc_ser}")
        a_code = str(r.get("A_CODE") or "").strip()
        if a_code:
            acct_keys.add(a_code)

        doc_dt = r.get("DOC_DATE")
        if isinstance(doc_dt, datetime):
            date_display = doc_dt.strftime("%Y-%m-%d")
        elif isinstance(doc_dt, date):
            date_display = doc_dt.isoformat()
        elif doc_dt:
            date_display = str(doc_dt)[:10]
        else:
            date_display = "—"

        d_v = str(r.get("D_V_CODE") or "").strip()
        m_v = str(r.get("M_V_CODE") or "").strip()
        dtl_code = str(r.get("AC_CODE_DTL") or "").strip()
        dtl_typ = r.get("AC_DTL_TYP")
        try:
            dtl_typ_n = int(dtl_typ) if dtl_typ is not None else 0
        except (TypeError, ValueError):
            dtl_typ_n = 0

        vendor_code = d_v or m_v or (dtl_code if dtl_typ_n == _DTL_TYPE_VENDOR else "")
        vendor_name = (
            str(r.get("DTL_NAME") or "").strip()
            if (not d_v and not m_v and dtl_typ_n == _DTL_TYPE_VENDOR)
            else (str(r.get("DTL_NAME") or "").strip() if vendor_code == dtl_code and dtl_typ_n == _DTL_TYPE_VENDOR else "")
        )
        if not vendor_name and vendor_code and dtl_typ_n == _DTL_TYPE_VENDOR and vendor_code == dtl_code:
            vendor_name = str(r.get("DTL_NAME") or "").strip()
        if not vendor_name:
            vendor_name = vendor_code or "—"

        wtavg = _f(r.get("WTAVG"), 4)
        wtavg_display = _money(wtavg) if wtavg else "—"
        dtl_label = _DTL_TYPE_LABELS.get(dtl_typ_n, f"نوع {dtl_typ_n}" if dtl_typ_n else "—")
        rows.append(
            {
                "doc_no": doc_no,
                "doc_no_display": str(doc_no or "—"),
                "doc_ser": doc_ser,
                "doc_date": date_display,
                "date_display": date_display,
                "when": date_display,
                "item_code": str(r.get("I_CODE") or "").strip(),
                "item_name": str(r.get("I_NAME") or "").strip() or "—",
                "unit": str(r.get("ITM_UNT") or "").strip() or "—",
                "p_size": _f(r.get("P_SIZE"), 3),
                "wh_code": str(r.get("W_CODE") or "").strip(),
                "branch_code": str(r.get("BRN_NO") or "").strip(),
                "inc_cost": inc_cost,
                "inc_cost_display": _money(inc_cost) if inc_cost is not None else "—",
                "adj_amt_display": _money(inc_cost) if inc_cost is not None else "—",
                "wtavg": wtavg,
                "wtavg_display": wtavg_display,
                "avg_cost_display": wtavg_display,
                "account_code": a_code or "—",
                "acct_code": a_code or "—",
                "account_name": str(r.get("A_NAME") or "").strip() or a_code or "—",
                "acct_name": str(r.get("A_NAME") or "").strip() or a_code or "—",
                "detail_code": dtl_code or "—",
                "sub_code": dtl_code or "—",
                "detail_name": str(r.get("DTL_NAME") or "").strip() or dtl_code or "—",
                "sub_name": str(r.get("DTL_NAME") or "").strip() or dtl_code or "—",
                "dtl_type": dtl_typ_n,
                "dtl_type_label": dtl_label,
                "stk_desc": str(r.get("STK_DESC") or "").strip() or "—",
                "desc": str(r.get("STK_DESC") or "").strip() or "—",
                "vendor_code": vendor_code,
                "vendor_name": vendor_name,
                "user_name": str(r.get("USER_NAME") or "").strip() or "—",
                "g_code": str(r.get("G_CODE") or "").strip(),
            }
        )

    period_label = f"{d_from.isoformat()} — {d_to.isoformat()}"
    n_lines = len(rows)
    n_docs = len(doc_keys)
    n_accts = len(acct_keys)
    return {
        "filters": {
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat(),
            "branch_code": brn,
            "warehouse_code": str(warehouse_code or "").strip(),
            "group_code": gcode,
            "item_q": iq,
            "account_q": aq,
        },
        "period_label": period_label,
        "kpis": {
            "move_count": n_lines,
            "move_count_display": f"{n_lines:,}",
            "total_display": f"{n_lines:,}",
            "total_lines": n_lines,
            "total_lines_display": f"{n_lines:,}",
            "doc_count": n_docs,
            "doc_count_display": f"{n_docs:,}",
            "total_docs": n_docs,
            "total_docs_display": f"{n_docs:,}",
            "adj_total": sum_inc,
            "adj_total_display": _money(sum_inc),
            "account_count": n_accts,
            "account_count_display": f"{n_accts:,}",
            "distinct_accounts": n_accts,
            "distinct_accounts_display": f"{n_accts:,}",
            "capped": n_lines >= lim,
        },
        "rows": rows,
    }


def build_cost_adjustments_excel(report: dict[str, Any]) -> HttpResponse:
    """تصدير تسوية التكاليف إلى Excel (أكواد كنص)."""
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
        "<x:ExcelWorksheet><x:Name>تسوية التكاليف</x:Name>"
        "<x:WorksheetOptions><x:DisplayRightToLeft/></x:WorksheetOptions>"
        "</x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
        "<style>"
        "table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:12px;}"
        "th,td{border:1px solid #94a3b8;padding:4px 6px;}"
        "th{background:#1e293b;color:#fff;}"
        "td.num{mso-number-format:'\\#\\,\\#\\#0\\.0000';}"
        "td.txt{mso-number-format:'\\@';}"
        "</style></head><body dir=\"rtl\">"
        f"<p><b>تسوية التكاليف</b> — "
        f"{escape(str(filters.get('date_from') or ''))} → "
        f"{escape(str(filters.get('date_to') or ''))}"
        f" · صفوف {escape(str(kpis.get('move_count_display') or 0))}</p>"
        "<table><thead><tr>"
        "<th>#</th><th>التاريخ</th><th>المستند</th><th>الصنف</th><th>الاسم</th>"
        "<th>الوحدة</th><th>المخزن</th><th>الفرع</th>"
        "<th>مبلغ التسوية</th><th>متوسط مرجح</th>"
        "<th>الحساب</th><th>اسم الحساب</th>"
        "<th>التفصيلي</th><th>اسم التفصيلي</th>"
        "<th>البيان</th><th>المورد</th>"
        "</tr></thead><tbody>"
    )
    for i, row in enumerate(rows, 1):
        buf.write("<tr>")
        buf.write(f"<td>{i}</td>")
        buf.write(f'<td class="txt">{escape(str(row.get("date_display") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("doc_no_display") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("item_code") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("item_name") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("unit") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("wh_code") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("branch_code") or ""))}</td>')
        buf.write(f'<td class="num">{escape(str(row.get("adj_amt_display") or "—"))}</td>')
        buf.write(f'<td class="num">{escape(str(row.get("avg_cost_display") or "—"))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("account_code") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("account_name") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("detail_code") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("detail_name") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("stk_desc") or ""))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("vendor_name") or ""))}</td>')
        buf.write("</tr>")
    buf.write("</tbody></table></body></html>")
    day = str(filters.get("date_to") or date.today().isoformat())
    resp = HttpResponse(
        buf.getvalue().encode("utf-8"),
        content_type="application/vnd.ms-excel; charset=utf-8",
    )
    resp["Content-Disposition"] = f'attachment; filename="cost-adjustments-{day}.xls"'
    return resp
