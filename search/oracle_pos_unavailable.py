"""كميات نقاط البيع الغير متوفرة — صافي POS غير المرحّل (كتسوية المخزون في أونكس)."""

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
    _date_params,
    _fetch_all,
    _pos_owner,
    _schema,
    oracle_enabled,
)

_CACHE_TTL = 600
_CACHE_VER = "v2"
_MAX_DAYS = 366
_BATCH = 400


def _f(value: Any, digits: int = 4) -> float:
    try:
        return round(float(value or 0), digits)
    except (TypeError, ValueError):
        return 0.0


def _fmt_qty(value: Any) -> str:
    num = float(value or 0)
    if abs(num - round(num)) < 1e-9:
        return f"{int(round(num)):,}"
    return f"{num:,.3f}".rstrip("0").rstrip(".")


def _fmt_money(value: Any, digits: int = 2) -> str:
    return f"{_f(value, digits):,.{digits}f}"


def _norm_code(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].replace("-", "").isdigit():
        text = text[:-2]
    return text


def _parse_wh_codes(raw: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    text = str(raw or "").replace("،", ",").replace("-", ",")
    for part in text.split(","):
        code = _norm_code(part)
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _validate(date_from, date_to, wh_codes: list[str]):
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")
    if (d_to - d_from).days > _MAX_DAYS:
        raise OracleStockError(f"الفترة القصوى {_MAX_DAYS} يوم.")
    if not wh_codes:
        raise OracleStockError("حدد مخزناً واحداً على الأقل.")
    if len(wh_codes) > 40:
        raise OracleStockError("عدد المخازن كبير جداً — اختصر القائمة.")
    return d_from, d_to


def _wh_in_clause(wh_codes: list[str], params: dict[str, Any], prefix: str = "w") -> str:
    keys: list[str] = []
    for i, code in enumerate(wh_codes):
        key = f"{prefix}{i}"
        params[key] = code
        keys.append(f":{key}")
    return ", ".join(keys)


def _qty_expr(alias: str = "d") -> str:
    return f"NVL({alias}.P_QTY, NVL({alias}.I_QTY, 0) * NVL({alias}.P_SIZE, 1))"


def _fetch_unposted_pos_net(
    date_from: date,
    date_to: date,
    *,
    wh_codes: list[str],
) -> list[dict]:
    """صافي كمية POS غير مرحّلة (مبيعات − مرتجع) لكل صنف×مخزن."""
    pos = _pos_owner()
    dates = _date_params(date_from, date_to)
    params: dict[str, Any] = {
        "d_from": dates["d_from"],
        "d_to_excl": dates["d_to_excl"],
    }
    wh_sql = _wh_in_clause(wh_codes, params, "wh")
    qty = _qty_expr("d")
    hung = "(m.HUNG IS NULL OR m.HUNG = 0)"

    return _fetch_all(
        f"""
        SELECT I_CODE, W_CODE,
               ROUND(SUM(QTY), 4) AS NET_QTY,
               ROUND(SUM(SALE_QTY), 4) AS SALE_QTY,
               ROUND(SUM(RT_QTY), 4) AS RT_QTY,
               MAX(ITM_UNT) AS ITM_UNT,
               MAX(P_SIZE) AS P_SIZE
        FROM (
          SELECT /*+ LEADING(m d) USE_NL(d) INDEX(m POSBILLMST_BILLDATEUSRBRN) */
                 TO_CHAR(d.I_CODE) AS I_CODE,
                 TO_CHAR(NVL(d.W_CODE, m.W_CODE)) AS W_CODE,
                 SUM({qty}) AS QTY,
                 SUM({qty}) AS SALE_QTY,
                 0 AS RT_QTY,
                 MAX(NVL(NULLIF(TRIM(TO_CHAR(d.ITM_UNT)), ''), '')) AS ITM_UNT,
                 MAX(NVL(d.P_SIZE, 1)) AS P_SIZE
          FROM {pos}.IAS_POS_BILL_MST m
          JOIN {pos}.IAS_POS_BILL_DTL d
            ON d.BILL_NO = m.BILL_NO
           AND d.BRN_NO = m.BRN_NO
           AND NVL(d.BILL_SRL, 0) = NVL(m.BILL_SRL, 0)
          WHERE m.BILL_DATE >= :d_from
            AND m.BILL_DATE < :d_to_excl
            AND NVL(m.POSTED, 0) = 0
            AND {hung}
            AND TO_CHAR(NVL(d.W_CODE, m.W_CODE)) IN ({wh_sql})
          GROUP BY TO_CHAR(d.I_CODE), TO_CHAR(NVL(d.W_CODE, m.W_CODE))
          UNION ALL
          SELECT /*+ LEADING(m d) USE_NL(d) */
                 TO_CHAR(d.I_CODE) AS I_CODE,
                 TO_CHAR(NVL(d.W_CODE, m.W_CODE)) AS W_CODE,
                 -SUM({qty}) AS QTY,
                 0 AS SALE_QTY,
                 SUM({qty}) AS RT_QTY,
                 MAX(NVL(NULLIF(TRIM(TO_CHAR(d.ITM_UNT)), ''), '')) AS ITM_UNT,
                 MAX(NVL(d.P_SIZE, 1)) AS P_SIZE
          FROM {pos}.IAS_POS_RT_BILL_MST m
          JOIN {pos}.IAS_POS_RT_BILL_DTL d
            ON d.RT_BILL_NO = m.RT_BILL_NO
           AND d.BRN_NO = m.BRN_NO
          WHERE m.RT_BILL_DATE >= :d_from
            AND m.RT_BILL_DATE < :d_to_excl
            AND NVL(m.POSTED, 0) = 0
            AND {hung}
            AND TO_CHAR(NVL(d.W_CODE, m.W_CODE)) IN ({wh_sql})
          GROUP BY TO_CHAR(d.I_CODE), TO_CHAR(NVL(d.W_CODE, m.W_CODE))
        )
        GROUP BY I_CODE, W_CODE
        HAVING ROUND(SUM(QTY), 4) <> 0
        """,
        params,
    )


def _chunked(items: list[str], size: int = _BATCH):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _fetch_item_meta(codes: list[str]) -> dict[str, dict[str, str]]:
    """اسم الصنف ورمز/اسم المجموعة."""
    if not codes:
        return {}
    schema = _schema()
    out: dict[str, dict[str, str]] = {}
    for batch in _chunked(codes):
        params: dict[str, Any] = {}
        keys = _wh_in_clause(batch, params, "c")
        rows = _fetch_all(
            f"""
            SELECT TO_CHAR(m.I_CODE) AS I_CODE,
                   NVL(NULLIF(TRIM(m.I_NAME), ''), TO_CHAR(m.I_CODE)) AS I_NAME,
                   NVL(TO_CHAR(m.G_CODE), '') AS G_CODE,
                   NVL(NULLIF(TRIM(g.G_A_NAME), ''), NVL(TO_CHAR(m.G_CODE), '')) AS G_NAME
            FROM {schema}.IAS_ITM_MST m
            LEFT JOIN {schema}.GROUP_DETAILS g
              ON g.G_CODE = m.G_CODE
            WHERE TO_CHAR(m.I_CODE) IN ({keys})
            """,
            params,
        )
        for row in rows or []:
            code = _norm_code(row.get("I_CODE"))
            if not code:
                continue
            g_code = _norm_code(row.get("G_CODE"))
            out[code] = {
                "name": str(row.get("I_NAME") or "").strip() or code,
                "g_code": g_code,
                "g_name": str(row.get("G_NAME") or "").strip() or g_code or "—",
            }
    return out


def _fetch_item_names(codes: list[str]) -> dict[str, str]:
    return {
        code: meta.get("name") or code
        for code, meta in _fetch_item_meta(codes).items()
    }

def _fetch_stock_meta(
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """رصيد ومتوسط تكلفة ووحدة لكل صنف×مخزن (صف وحدة أساسي)."""
    if not pairs:
        return {}
    schema = _schema()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    by_wh: dict[str, list[str]] = {}
    for icode, wcode in pairs:
        by_wh.setdefault(wcode, []).append(icode)

    for wcode, codes in by_wh.items():
        for batch in _chunked(sorted(set(codes))):
            params: dict[str, Any] = {"wh": wcode}
            keys = _wh_in_clause(batch, params, "c")
            rows = _fetch_all(
                f"""
                SELECT I_CODE, W_CODE, ITM_UNT, P_SIZE, AVL_QTY, I_CWTAVG
                FROM (
                  SELECT TO_CHAR(w.I_CODE) AS I_CODE,
                         TO_CHAR(w.W_CODE) AS W_CODE,
                         NVL(NULLIF(TRIM(TO_CHAR(w.ITM_UNT)), ''), '') AS ITM_UNT,
                         NVL(w.P_SIZE, 1) AS P_SIZE,
                         NVL(w.AVL_QTY, 0) AS AVL_QTY,
                         NVL(w.I_CWTAVG, 0) AS I_CWTAVG,
                         ROW_NUMBER() OVER (
                           PARTITION BY w.I_CODE, w.W_CODE
                           ORDER BY NVL(w.P_SIZE, 1), w.ITM_UNT
                         ) AS RN
                  FROM {schema}.IAS_ITM_WCODE w
                  WHERE TO_CHAR(w.W_CODE) = :wh
                    AND TO_CHAR(w.I_CODE) IN ({keys})
                )
                WHERE RN = 1
                """,
                params,
            )
            for row in rows or []:
                ic = _norm_code(row.get("I_CODE"))
                wc = _norm_code(row.get("W_CODE"))
                if not ic or not wc:
                    continue
                out[(ic, wc)] = {
                    "unit": str(row.get("ITM_UNT") or "").strip() or "—",
                    "p_size": _f(row.get("P_SIZE"), 4) or 1.0,
                    "avl_qty": _f(row.get("AVL_QTY"), 4),
                    "avg_cost": _f(row.get("I_CWTAVG"), 6),
                }
    return out


def build_pos_unavailable_report(
    date_from,
    date_to,
    *,
    warehouse_codes: str,
    sign: str = "all",
    group_code: str = "",
) -> dict[str, Any]:
    wh_codes = _parse_wh_codes(warehouse_codes)
    d_from, d_to = _validate(date_from, date_to, wh_codes)
    sign_key = str(sign or "all").strip().lower()
    if sign_key not in ("all", "pos", "neg", "positive", "negative"):
        sign_key = "all"
    if sign_key == "positive":
        sign_key = "pos"
    if sign_key == "negative":
        sign_key = "neg"
    want_group = _norm_code(group_code)

    cache_key = (
        f"pos-unavl:{_CACHE_VER}:{d_from}:{d_to}:"
        f"{','.join(wh_codes)}:{sign_key}:g={want_group or 'ALL'}"
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached

    raw = _fetch_unposted_pos_net(d_from, d_to, wh_codes=wh_codes)
    pairs: list[tuple[str, str]] = []
    codes: list[str] = []
    for row in raw or []:
        ic = _norm_code(row.get("I_CODE"))
        wc = _norm_code(row.get("W_CODE"))
        if not ic or not wc:
            continue
        pairs.append((ic, wc))
        codes.append(ic)

    item_meta = _fetch_item_meta(codes)
    stock = _fetch_stock_meta(pairs)

    rows_out: list[dict[str, Any]] = []
    in_cost = 0.0
    out_cost = 0.0
    pos_count = 0
    neg_count = 0

    for row in raw or []:
        ic = _norm_code(row.get("I_CODE"))
        wc = _norm_code(row.get("W_CODE"))
        if not ic or not wc:
            continue
        meta_item = item_meta.get(ic) or {}
        g_code = _norm_code(meta_item.get("g_code"))
        if want_group and g_code != want_group:
            continue
        qty = _f(row.get("NET_QTY"), 4)
        if sign_key == "pos" and qty <= 0:
            continue
        if sign_key == "neg" and qty >= 0:
            continue

        meta = stock.get((ic, wc)) or {}
        unit = str(row.get("ITM_UNT") or "").strip() or meta.get("unit") or "—"
        p_size = _f(row.get("P_SIZE"), 4) or _f(meta.get("p_size"), 4) or 1.0
        avl = _f(meta.get("avl_qty"), 4)
        avg_cost = _f(meta.get("avg_cost"), 6)
        line_cost = round(qty * avg_cost, 6)
        if qty > 0:
            in_cost = round(in_cost + line_cost, 6)
            pos_count += 1
        else:
            out_cost = round(out_cost + abs(line_cost), 6)
            neg_count += 1

        rows_out.append(
            {
                "item_code": ic,
                "item_name": meta_item.get("name") or ic,
                "g_code": g_code,
                "g_name": meta_item.get("g_name") or g_code or "—",
                "unit": unit,
                "p_size": p_size,
                "p_size_display": _fmt_qty(p_size),
                "wh_code": wc,
                "avg_cost": avg_cost,
                "avg_cost_display": _fmt_money(avg_cost, 4),
                "avl_qty": avl,
                "avl_display": _fmt_qty(avl),
                "qty": qty,
                "qty_display": _fmt_qty(qty),
                "sale_qty": _f(row.get("SALE_QTY"), 4),
                "rt_qty": _f(row.get("RT_QTY"), 4),
                "line_cost": line_cost,
                "line_cost_display": _fmt_money(line_cost, 4),
                "sign": "pos" if qty > 0 else "neg",
            }
        )

    rows_out.sort(key=lambda r: (-abs(r["qty"]), r["item_code"], r["wh_code"]))

    result = {
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "filters": {
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat(),
            "warehouses": ", ".join(wh_codes),
            "sign": sign_key,
            "group_code": want_group,
        },
        "kpis": {
            "item_count": len(rows_out),
            "pos_count": pos_count,
            "neg_count": neg_count,
            "in_cost": in_cost,
            "in_cost_display": _fmt_money(in_cost, 4),
            "out_cost": out_cost,
            "out_cost_display": _fmt_money(out_cost, 4),
            "net_cost": round(in_cost - out_cost, 6),
            "net_cost_display": _fmt_money(in_cost - out_cost, 4),
        },
        "rows": rows_out,
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
        return "0" + ("." + ("0" * digits) if digits else "")


def build_pos_unavailable_excel(report: dict[str, Any]) -> HttpResponse:
    rows = report.get("rows") or []
    filters = report.get("filters") or {}
    kpis = report.get("kpis") or {}
    period = report.get("period_label") or ""
    buf = io.StringIO()
    buf.write("\ufeff")
    buf.write(
        "<html xmlns:o=\"urn:schemas-microsoft-com:office:office\" "
        "xmlns:x=\"urn:schemas-microsoft-com:office:excel\" "
        "xmlns=\"http://www.w3.org/TR/REC-html40\">"
        "<head><meta charset=\"utf-8\">"
        "<!--[if gte mso 9]><xml><x:ExcelWorkbook><x:ExcelWorksheets>"
        "<x:ExcelWorksheet><x:Name>POS غير متوفر</x:Name>"
        "<x:WorksheetOptions><x:DisplayRightToLeft/></x:WorksheetOptions>"
        "</x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
        "<style>"
        "table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:11px;}"
        "th,td{border:1px solid #94a3b8;padding:4px 7px;white-space:nowrap;vertical-align:middle;}"
        "th{background:#1e3a5f;color:#fff;font-weight:700;}"
        "th.qty{background:#5b21b6;}"
        "th.avl{background:#0e7490;}"
        "th.cost{background:#166534;}"
        "td.txt{mso-number-format:'\\@';}"
        "td.num{mso-number-format:'\\#\\,\\#\\#0\\.000';text-align:left;}"
        "td.money{mso-number-format:'\\#\\,\\#\\#0\\.0000';text-align:left;}"
        "td.int{mso-number-format:'\\#\\,\\#\\#0';text-align:left;}"
        "td.qty{background:#f5f3ff;color:#5b21b6;font-weight:700;}"
        "td.avl{background:#ecfeff;color:#0e7490;font-weight:700;}"
        "td.cost{background:#ecfdf3;color:#166534;font-weight:700;}"
        "td.neg{background:#fff7ed;color:#9a3412;font-weight:700;}"
        "tr.even td{background:#f8fafc;}"
        "tr.even td.qty{background:#ede9fe;}"
        "tr.even td.avl{background:#cffafe;}"
        "tr.even td.cost{background:#dcfce7;}"
        "tr.even td.neg{background:#ffedd5;}"
        "tr.foot td{background:#dbeafe;font-weight:800;}"
        "caption{font-family:Tahoma,Arial;font-size:13px;font-weight:700;text-align:right;margin:8px 0;}"
        ".sub{font-size:10px;color:#475569;font-weight:400;}"
        "</style></head><body dir=\"rtl\">"
    )
    buf.write(
        "<table><caption>كميات نقاط البيع الغير متوفرة — صافي غير مرحّل"
        f'<br><span class="sub">{escape(str(period))}'
        f" · مخازن {escape(str(filters.get('warehouses') or ''))}"
        f" · {escape(str(kpis.get('item_count') or 0))} صنف</span></caption>"
        "<thead><tr>"
        "<th>#</th>"
        "<th>رقم الصنف</th>"
        "<th>اسم الصنف</th>"
        "<th>المجموعة</th>"
        "<th>الوحدة</th>"
        "<th>العبوة</th>"
        "<th>المخزن</th>"
        "<th class=\"cost\">متوسط التكلفة</th>"
        "<th class=\"avl\">الكمية المتوفرة</th>"
        "<th class=\"qty\">الكمية</th>"
        "<th class=\"cost\">التكلفة</th>"
        "</tr></thead><tbody>"
    )
    for i, row in enumerate(rows, 1):
        even = ' class="even"' if i % 2 == 0 else ""
        qty_cls = "neg" if float(row.get("qty") or 0) < 0 else "qty"
        buf.write(f"<tr{even}>")
        buf.write(f'<td class="int">{i}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("item_code") or ""))}</td>')
        buf.write(f"<td>{escape(str(row.get('item_name') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('g_name') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('unit') or ''))}</td>")
        buf.write(f'<td class="num">{_xls_num(row.get("p_size"))}</td>')
        buf.write(f'<td class="txt">{escape(str(row.get("wh_code") or ""))}</td>')
        buf.write(f'<td class="money cost">{_xls_num(row.get("avg_cost"))}</td>')
        buf.write(f'<td class="num avl">{_xls_num(row.get("avl_qty"))}</td>')
        buf.write(f'<td class="num {qty_cls}">{_xls_num(row.get("qty"))}</td>')
        buf.write(f'<td class="money cost">{_xls_num(row.get("line_cost"))}</td>')
        buf.write("</tr>")
    buf.write(
        '<tr class="foot">'
        "<td></td><td></td><td>الإجمالي</td><td></td><td></td><td></td><td></td><td></td><td></td><td></td>"
        f'<td class="money">{_xls_num(kpis.get("net_cost"))}</td>'
        "</tr>"
        '<tr class="foot">'
        "<td></td><td></td>"
        f"<td>وارد {_xls_num(kpis.get('in_cost'))} · منصرف {_xls_num(kpis.get('out_cost'))}</td>"
        "<td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td>"
        "</tr>"
    )
    buf.write("</tbody></table></body></html>")
    payload = buf.getvalue().encode("utf-8")
    filename = (
        f"pos-unavailable-{filters.get('date_from') or ''}"
        f"-{filters.get('date_to') or ''}.xls"
    )
    resp = HttpResponse(payload, content_type="application/vnd.ms-excel; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp
