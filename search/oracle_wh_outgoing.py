"""تحويلات صادرة من مستودعات مركزية — تتبع الاستلام والمجموعة."""

from __future__ import annotations

import io
from datetime import date, datetime
from html import escape
from typing import Any

from django.core.cache import cache
from django.http import HttpResponse

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _bind_brn,
    _bind_gcode,
    _branch_names,
    _date_params,
    _fetch_all,
    _hung_ok,
    _schema,
    oracle_enabled,
)

_CACHE_TTL = 300
_CACHE_VER = "v8"
_DEFAULT_SRC = ("401", "3", "90", "902")
_LATE_DAYS = 2


def _f(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _fmt_qty(value: Any) -> str:
    qty = float(value or 0)
    if abs(qty - round(qty)) < 1e-9:
        return f"{int(round(qty)):,}"
    return f"{qty:,.2f}"


def _norm_code(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].replace("-", "").isdigit():
        text = text[:-2]
    return text


def _bind_wh(value: Any):
    text = _norm_code(value)
    if text.isdigit():
        try:
            return int(text)
        except ValueError:
            return text
    return text


def _wh_name_sql(alias: str) -> str:
    return f"NVL(NULLIF(TRIM({alias}.W_NAME), ''), TO_CHAR({alias}.W_CODE))"


def default_source_warehouses() -> list[str]:
    return list(_DEFAULT_SRC)


def _parse_wh_codes(raw: str | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    text = str(raw or "").replace("،", ",")
    for part in text.split(","):
        code = _norm_code(part)
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out or list(_DEFAULT_SRC)


def _validate(date_from, date_to):
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")
    if (d_to - d_from).days > 366:
        raise OracleStockError("الفترة القصوى سنة واحدة.")
    return d_from, d_to


def _as_day(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return _as_date(value)
    except Exception:  # noqa: BLE001
        return None


def _elapsed_days(create_on: date | None, stop_on: date | None) -> int:
    if not create_on or not stop_on:
        return 0
    return max(0, (stop_on - create_on).days)


def _days_since(tr_date: Any, today: date) -> int:
    return _elapsed_days(_as_day(tr_date), today)


def _status(received: bool, days: int) -> dict:
    delay = max(0, days - _LATE_DAYS)
    if received:
        return {
            "received": True,
            "label": "مكتمل",
            "kind": "ok",
            "delay_days": delay,
        }
    if days > _LATE_DAYS:
        return {
            "received": False,
            "label": "متأخر",
            "kind": "late",
            "delay_days": delay,
        }
    return {
        "received": False,
        "label": "معلق",
        "kind": "pending",
        "delay_days": 0,
    }


def _fetch_outgoing_rows(
    date_from: date,
    date_to: date,
    *,
    wh_codes: list[str],
    branch_code: str = "",
    warehouse_code: str = "",
) -> list[dict]:
    schema = _schema()
    dates = _date_params(date_from, date_to)
    params: dict[str, Any] = {
        "d_from": dates["d_from"],
        "d_to_excl": dates["d_to_excl"],
    }
    wh_keys: list[str] = []
    for i, code in enumerate(wh_codes):
        key = f"w{i}"
        params[key] = _bind_wh(code)
        wh_keys.append(f":{key}")

    branch_sql = ""
    brn = _bind_brn(branch_code) if branch_code else None
    if brn not in ("", None):
        params["dst_brn"] = brn
        branch_sql = "AND tw.CONN_BRN_NO = :dst_brn"

    wh_sql = ""
    dst_wh = _norm_code(warehouse_code)
    if dst_wh:
        params["dst_wh"] = _bind_wh(dst_wh)
        wh_sql = "AND m.T_W_CODE = :dst_wh"

    return _fetch_all(
        f"""
        SELECT /*+ LEADING(m d) USE_NL(d) INDEX(d INDX_SER_WHTRNS_DTL) */
               m.TR_NO,
               m.TR_SER,
               m.TR_DATE,
               m.AD_DATE,
               TO_CHAR(m.TR_DATE, 'YYYY-MM-DD') AS TR_DATE_LABEL,
               TO_CHAR(NVL(m.AD_DATE, m.TR_DATE), 'YYYY-MM-DD HH24:MI') AS CREATE_LABEL,
               NVL(NULLIF(TRIM(m.TR_DESC), ''), 'تحويل #' || TO_CHAR(m.TR_NO)) AS TR_NAME,
               m.F_W_CODE AS SRC_WH,
               {_wh_name_sql("fw")} AS SRC_WH_NAME,
               m.T_W_CODE AS DST_WH,
               {_wh_name_sql("tw")} AS DST_WH_NAME,
               tw.CONN_BRN_NO AS DST_BRN,
               NVL(m.PROCESSED, 0) AS PROCESSED,
               ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL,
               COUNT(DISTINCT d.I_CODE) AS ITEM_COUNT,
               ROUND(SUM(NVL(d.TR_QTY_NOT_RECE, 0)), 2) AS QTY_NOT_RECV
        FROM {schema}.IAS_WHTRNS_MST m
        JOIN {schema}.IAS_WHTRNS_DTL d
          ON d.TR_SER = m.TR_SER
        LEFT JOIN {schema}.WAREHOUSE_DETAILS fw
          ON fw.W_CODE = m.F_W_CODE
        LEFT JOIN {schema}.WAREHOUSE_DETAILS tw
          ON tw.W_CODE = m.T_W_CODE
        WHERE m.TR_DATE >= :d_from
          AND m.TR_DATE < :d_to_excl
          AND m.TR_INOUT_TYPE = 1
          AND {_hung_ok("m")}
          AND m.F_W_CODE IN ({", ".join(wh_keys)})
          {branch_sql}
          {wh_sql}
        GROUP BY m.TR_NO, m.TR_SER, m.TR_DATE, m.AD_DATE, m.TR_DESC,
                 m.F_W_CODE, fw.W_NAME, fw.W_CODE,
                 m.T_W_CODE, tw.W_NAME, tw.W_CODE, tw.CONN_BRN_NO,
                 m.PROCESSED
        ORDER BY m.TR_DATE DESC, m.TR_NO DESC
        """,
        params,
    )


def _fetch_recv_dates_by_ser(tr_sers: list[Any]) -> dict[str, date]:
    """تاريخ استلام التحويل الصادر = أقدم AD_DATE/TR_DATE للوارد المرتبط بـ F_TR_SER."""
    if not tr_sers:
        return {}
    schema = _schema()
    out: dict[str, date] = {}
    chunk = 400
    for start in range(0, len(tr_sers), chunk):
        part = tr_sers[start : start + chunk]
        params: dict[str, Any] = {}
        keys: list[str] = []
        for i, ser in enumerate(part):
            key = f"s{i}"
            params[key] = ser
            keys.append(f":{key}")
        rows = _fetch_all(
            f"""
            SELECT m.F_TR_SER AS OUT_SER,
                   MIN(NVL(m.AD_DATE, m.TR_DATE)) AS RECV_WHEN
            FROM {schema}.IAS_WHTRNS_MST m
            WHERE m.TR_INOUT_TYPE = 2
              AND m.F_TR_SER IN ({", ".join(keys)})
            GROUP BY m.F_TR_SER
            """,
            params,
        )
        for row in rows or []:
            ser = _norm_code(row.get("OUT_SER"))
            day = _as_day(row.get("RECV_WHEN"))
            if ser and day:
                out[ser] = day
    return out


def _fetch_groups_by_ser(tr_sers: list[Any]) -> dict[str, list[dict]]:
    if not tr_sers:
        return {}
    schema = _schema()
    out: dict[str, list[dict]] = {}
    chunk = 400
    for start in range(0, len(tr_sers), chunk):
        part = tr_sers[start : start + chunk]
        params: dict[str, Any] = {}
        keys: list[str] = []
        for i, ser in enumerate(part):
            key = f"s{i}"
            params[key] = ser
            keys.append(f":{key}")
        rows = _fetch_all(
            f"""
            SELECT d.TR_SER,
                   NVL(TO_CHAR(i.G_CODE), '(بلا)') AS G_CODE,
                   NVL(NULLIF(TRIM(g.G_A_NAME), ''), NVL(TO_CHAR(i.G_CODE), '(بلا)')) AS G_NAME,
                   COUNT(DISTINCT d.I_CODE) AS ITEM_COUNT
            FROM {schema}.IAS_WHTRNS_DTL d
            JOIN {schema}.IAS_ITM_MST i
              ON i.I_CODE = d.I_CODE
            LEFT JOIN {schema}.GROUP_DETAILS g
              ON g.G_CODE = i.G_CODE
            WHERE d.TR_SER IN ({", ".join(keys)})
            GROUP BY d.TR_SER, i.G_CODE, g.G_A_NAME
            ORDER BY d.TR_SER, COUNT(DISTINCT d.I_CODE) DESC, g.G_A_NAME
            """,
            params,
        )
        for row in rows or []:
            ser = _norm_code(row.get("TR_SER"))
            if not ser:
                continue
            out.setdefault(ser, []).append(
                {
                    "code": _norm_code(row.get("G_CODE")),
                    "name": str(row.get("G_NAME") or "").strip() or "—",
                    "item_count": int(row.get("ITEM_COUNT") or 0),
                }
            )
    return out


def _wh_display(code: Any, name: Any = "") -> str:
    code = _norm_code(code)
    label = str(name or "").strip()
    if code and label and label != code and not label.startswith(f"{code} "):
        return f"{code} — {label}"
    return label or code or "—"


def _same_date(create_label: str, date_label: str) -> bool:
    create = str(create_label or "").strip()
    doc = str(date_label or "").strip()
    if not create or not doc:
        return True
    return create[:10] == doc[:10]


def build_outgoing_transfers_report(
    date_from,
    date_to,
    *,
    source_warehouses: str = "",
    branch_code: str = "",
    warehouse_code: str = "",
    group_code: str = "",
    status: str = "all",
) -> dict:
    """جدول التحويلات الصادرة من مستودعات المصدر مع حالة الاستلام."""
    d_from, d_to = _validate(date_from, date_to)
    wh_codes = _parse_wh_codes(source_warehouses)
    branch = _norm_code(branch_code)
    warehouse = _norm_code(warehouse_code)
    group = _norm_code(group_code)
    g_bind = _bind_gcode(group) if group else None
    status_key = str(status or "all").strip().lower()
    if status_key not in ("all", "received", "pending", "late"):
        status_key = "all"

    cache_key = (
        f"whout:{_CACHE_VER}:{d_from.isoformat()}:{d_to.isoformat()}:"
        f"{','.join(wh_codes)}:{branch}:{warehouse}:{group}:{status_key}"
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    today = date.today()
    raw = _fetch_outgoing_rows(
        d_from,
        d_to,
        wh_codes=wh_codes,
        branch_code=branch,
        warehouse_code=warehouse,
    )
    sers = [row.get("TR_SER") for row in raw if row.get("TR_SER") is not None]
    groups_map = _fetch_groups_by_ser(sers)
    recv_map = _fetch_recv_dates_by_ser(sers)
    branch_names = _branch_names()

    rows: list[dict] = []
    kpi = {"total": 0, "received": 0, "pending": 0, "late": 0}

    for row in raw:
        ser = _norm_code(row.get("TR_SER"))
        groups = groups_map.get(ser) or []
        if group:
            want = _norm_code(g_bind if g_bind is not None else group)
            if not any(_norm_code(g.get("code")) == want for g in groups):
                continue

        received = int(row.get("PROCESSED") or 0) == 1
        create_on = _as_day(row.get("AD_DATE")) or _as_day(row.get("TR_DATE"))
        recv_on = recv_map.get(ser)
        if received:
            # يتوقف العدد عند تاريخ الاستلام (أو تاريخ الإنشاء إن لم يُعثر على وارد)
            stop_on = recv_on or create_on
        else:
            stop_on = today
        days = _elapsed_days(create_on, stop_on)
        st = _status(received, days)
        kpi["total"] += 1
        if st["kind"] == "ok":
            kpi["received"] += 1
        elif st["kind"] == "late":
            kpi["late"] += 1
        else:
            kpi["pending"] += 1

        if status_key == "received" and st["kind"] != "ok":
            continue
        if status_key == "pending" and st["kind"] != "pending":
            continue
        if status_key == "late" and st["kind"] != "late":
            continue

        group_label = " · ".join(g["name"] for g in groups[:4]) if groups else "—"
        if len(groups) > 4:
            group_label += f" (+{len(groups) - 4})"

        src = _norm_code(row.get("SRC_WH"))
        dst = _norm_code(row.get("DST_WH"))
        src_name = str(row.get("SRC_WH_NAME") or "").strip() or src
        dst_name = str(row.get("DST_WH_NAME") or "").strip() or dst
        src_wh_label = _wh_display(src, src_name)
        dst_wh_label = _wh_display(dst, dst_name)
        dst_brn = _norm_code(row.get("DST_BRN"))
        branch_label = branch_names.get(dst_brn) or branch_names.get(str(row.get("DST_BRN") or "").strip()) or dst_brn or "—"
        qty = _f(row.get("QTY_TOTAL"))
        items = int(row.get("ITEM_COUNT") or 0)
        date_label = str(row.get("TR_DATE_LABEL") or "").strip()
        create_label = str(row.get("CREATE_LABEL") or "").strip() or date_label
        show_doc_date = not _same_date(create_label, date_label)

        rows.append(
            {
                "tr_no": _norm_code(row.get("TR_NO")),
                "tr_ser": ser,
                "tr_name": str(row.get("TR_NAME") or "").strip() or "—",
                "date_label": date_label if show_doc_date else "",
                "create_label": create_label,
                "recv_label": recv_on.isoformat() if recv_on else "",
                "days": days,
                "delay_days": st["delay_days"],
                "status_label": st["label"],
                "status_kind": st["kind"],
                "received": received,
                "src_wh": src,
                "src_wh_name": src_name,
                "src_wh_label": src_wh_label,
                "dst_wh": dst,
                "dst_wh_name": dst_name,
                "dst_wh_label": dst_wh_label,
                "dst_brn": dst_brn,
                "branch_label": branch_label,
                "qty": qty,
                "qty_display": _fmt_qty(qty),
                "item_count": items,
                "groups": groups,
                "group_label": group_label,
            }
        )

    report = {
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "source_warehouses": wh_codes,
        "branch_code": branch,
        "warehouse_code": warehouse,
        "group_code": group,
        "status": status_key,
        "late_days": _LATE_DAYS,
        "kpis": {
            "total": kpi["total"],
            "received": kpi["received"],
            "pending": kpi["pending"],
            "late": kpi["late"],
            "shown": len(rows),
        },
        "rows": rows,
    }
    cache.set(cache_key, report, _CACHE_TTL)
    return report


def _xls_status_class(kind: str) -> str:
    if kind == "ok":
        return "ok"
    if kind == "late":
        return "late"
    return "pending"


def build_outgoing_transfers_excel(report: dict[str, Any]) -> HttpResponse:
    """تصدير جدول التحويلات الصادرة إلى Excel."""
    rows = report.get("rows") or []
    kpis = report.get("kpis") or {}
    buf = io.StringIO()
    buf.write("\ufeff")
    buf.write(
        '<html xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:x="urn:schemas-microsoft-com:office:excel" '
        'xmlns="http://www.w3.org/TR/REC-html40">'
        "<head><meta charset=\"utf-8\">"
        "<!--[if gte mso 9]><xml><x:ExcelWorkbook><x:ExcelWorksheets>"
        "<x:ExcelWorksheet><x:Name>تحويلات</x:Name>"
        "<x:WorksheetOptions><x:DisplayRightToLeft/></x:WorksheetOptions>"
        "</x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
        "<style>"
        "table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:11px;}"
        "th,td{border:1px solid #94a3b8;padding:4px 7px;white-space:nowrap;vertical-align:middle;}"
        "th{background:#d9e2f3;color:#1e293b;font-weight:700;}"
        "td.int{mso-number-format:'\\#\\,\\#\\#0';text-align:center;}"
        "td.ok{background:#c6efce;color:#14532d;font-weight:700;}"
        "td.pending{background:#ffeb9c;color:#7a4d00;font-weight:700;}"
        "td.late{background:#ffc7ce;color:#7f1d1d;font-weight:700;}"
        "tr.even td{background:#f8fafc;}"
        "tr.even td.ok{background:#c6efce;}"
        "tr.even td.pending{background:#ffeb9c;}"
        "tr.even td.late{background:#ffc7ce;}"
        "caption{font-family:Tahoma,Arial;font-size:13px;font-weight:700;margin:8px 0;text-align:right;}"
        ".sub{font-size:10px;color:#475569;font-weight:400;}"
        "</style></head><body dir=\"rtl\">"
    )
    buf.write(
        f"<table><caption>حركة التحويلات الصادرة"
        f"<br><span class=\"sub\">{escape(str(report.get('period_label') or ''))}"
        f" · {int(kpis.get('shown') or len(rows))} تحويل</span></caption>"
        "<thead><tr>"
        "<th>هل تم الاستلام</th>"
        "<th>رقم التحويل</th>"
        "<th>بيان التحويل</th>"
        "<th>فرع المحول له</th>"
        "<th>تأخير استلام</th>"
        "<th>المخزن المحول منه</th>"
        "<th>المخزن المحول له</th>"
        "<th>تاريخ الإنشاء</th>"
        "<th>الكمية</th>"
        "<th>عدد الأصناف</th>"
        "<th>المجموعة</th>"
        "</tr></thead><tbody>"
    )
    for i, row in enumerate(rows, 1):
        kind = _xls_status_class(str(row.get("status_kind") or ""))
        even = ' class="even"' if i % 2 == 0 else ""
        buf.write(f"<tr{even}>")
        buf.write(
            f'<td class="{kind}">{escape(str(row.get("status_label") or ""))}</td>'
        )
        buf.write(f'<td class="int">{escape(str(row.get("tr_no") or ""))}</td>')
        buf.write(f"<td>{escape(str(row.get('tr_name') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('branch_label') or ''))}</td>")
        buf.write(f'<td class="int">{int(row.get("days") or 0)}</td>')
        buf.write(f"<td>{escape(str(row.get('src_wh_label') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('dst_wh_label') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('create_label') or ''))}</td>")
        buf.write(f'<td class="int">{escape(str(row.get("qty_display") or ""))}</td>')
        buf.write(f'<td class="int">{int(row.get("item_count") or 0)}</td>')
        buf.write(f"<td>{escape(str(row.get('group_label') or ''))}</td>")
        buf.write("</tr>")
    buf.write("</tbody></table></body></html>")

    resp = HttpResponse(
        buf.getvalue().encode("utf-8"),
        content_type="application/vnd.ms-excel; charset=utf-8",
    )
    resp["Content-Disposition"] = 'attachment; filename="wh-transfers.xls"'
    return resp
