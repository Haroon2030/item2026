"""التزام الموردين — كمية المبيعات مقابل كمية الشراء من المورد لكل صنف ورّده.

قراءة فقط (SELECT). لكل (مورد، صنف) اشتُري منه في الفترة:
- الشراء: فواتير الشراء (IAS_PI_BILL_*) غير المعلّقة من المورد ناقص مرتجعات الشراء إليه (IAS_PR_BILL_*)، بالوحدة الرئيسية.
- المبيعات: صافي الكمية المباعة من الصنف في الفترة نفسها (نقاط البيع + الجملة النقدي 1/5 بلا خلاصات الترحيل
  + الآجل 4/8 ناقص مرتجعاتها) — نفس استعلامات «دراسة مجموعة»، ولكل الموردين معاً (المبيعات لا تُنسب لمورد).
- نسبة الشراء إلى المبيعات = الشراء من المورد ÷ مبيعات الصنف؛ وللمورد: مجموع شرائه ÷ مجموع مبيعات أصنافه.
  الصنف المشترى من أكثر من مورد تُحسب مبيعاته كاملة مع كل مورد، ويُعرض الشراء من الموردين الآخرين.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from django.core.cache import cache

from .oracle_group_study import (
    GROUP_SQL,
    STOCK_QTY,
    _bill_returns,
    _bill_sales,
    _brn_filter,
    _code,
    _f,
    _group_name,
    _pct,
    _pct_display,
    _pos_returns,
    _qty,
    _subgroup_name,
)
from .oracle_order_qty import _month_windows, _pos_qty
from .oracle_stock import (
    OracleStockError,
    _as_date,
    _bind_gcode,
    _branch_names,
    _date_params,
    _fetch_all,
    _norm_brn_code,
    _run_parallel,
    _schema,
    oracle_enabled,
    oracle_session,
)

logger = logging.getLogger(__name__)

_CACHE_TTL = 600
_CACHE_VER = "v4"
MAX_DAYS = 400
VENDOR_PAGE_ROWS = 100  # صفوف الشاشة (الأكثر أصنافاً)؛ ملف Excel فيه كل الموردين
# تصنيف المورد بنسبة شرائه إلى مبيعات أصنافه
LOW_PCT = 90
HIGH_PCT = 110
ALL_ITEMS_SQL = "1 = 1"


def _purchases(params: dict, brn_sql: str, grp: str) -> list[dict]:
    """الشراء لكل (مورد، صنف): كمية الفواتير وعددها وآخر تاريخ."""
    sch = _schema()
    return _fetch_all(
        f"""
        SELECT TO_CHAR(m.V_CODE) AS V, MAX(NVL(m.V_NAME, TO_CHAR(m.V_CODE))) AS VN,
               TO_CHAR(d.I_CODE) AS I, MAX(i.I_NAME) AS IN_,
               SUM({STOCK_QTY}) AS Q, COUNT(DISTINCT m.BILL_SER) AS N, MAX(m.BILL_DATE) AS LAST_D
        FROM {sch}.IAS_PI_BILL_DTL d
        JOIN {sch}.IAS_PI_BILL_MST m
          ON m.BILL_NO = d.BILL_NO AND m.BILL_SER = d.BILL_SER AND m.BILL_DOC_TYPE = d.BILL_DOC_TYPE
        JOIN {sch}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
          AND NVL(m.HUNG, 0) = 0 AND m.V_CODE IS NOT NULL AND {grp} {brn_sql.replace('{a}', 'm')}
        GROUP BY TO_CHAR(m.V_CODE), TO_CHAR(d.I_CODE)
        """,
        params,
    )


def _purchase_returns(params: dict, brn_sql: str, grp: str) -> list[dict]:
    """مرتجعات الشراء لكل (مورد، صنف)."""
    sch = _schema()
    return _fetch_all(
        f"""
        SELECT TO_CHAR(r.V_CODE) AS V, TO_CHAR(d.I_CODE) AS I, SUM({STOCK_QTY}) AS Q
        FROM {sch}.IAS_PR_BILL_DTL d
        JOIN {sch}.IAS_PR_BILL_MST r
          ON r.RT_BILL_NO = d.RT_BILL_NO AND r.RT_BILL_SER = d.RT_BILL_SER AND r.RT_BILL_DOC_TYPE = d.RT_BILL_DOC_TYPE
        JOIN {sch}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
          AND NVL(r.HUNG, 0) = 0 AND r.V_CODE IS NOT NULL AND {grp} {brn_sql.replace('{a}', 'r')}
        GROUP BY TO_CHAR(r.V_CODE), TO_CHAR(d.I_CODE)
        """,
        params,
    )


def _main_units(codes: list[str]) -> dict[str, str]:
    """الوحدة الرئيسية (MAIN_UNIT = 1) لأصناف محددة — لتفصيل مورد واحد."""
    out: dict[str, str] = {}
    sch = _schema()
    for start in range(0, len(codes), 500):
        chunk = codes[start:start + 500]
        binds = {f"c{n}": c for n, c in enumerate(chunk)}
        rows = _fetch_all(
            f"""
            SELECT I, U FROM (
                SELECT TO_CHAR(d.I_CODE) AS I, TRIM(d.ITM_UNT) AS U,
                       ROW_NUMBER() OVER (
                           PARTITION BY d.I_CODE ORDER BY CASE WHEN d.MAIN_UNIT = 1 THEN 0 ELSE 1 END, NVL(d.P_SIZE, 0)
                       ) AS RN
                FROM {sch}.IAS_ITM_DTL d
                WHERE TO_CHAR(d.I_CODE) IN ({', '.join(':' + k for k in binds)}) AND NVL(d.P_SIZE, 0) > 0
            ) WHERE RN = 1
            """,
            binds,
        )
        out.update({_code(r["I"]): str(r.get("U") or "").replace("ـ", "").strip() for r in rows})
    return out


def _level(pct: float | None) -> tuple[str, str]:
    """المورد: شراؤه أقل من مبيعات أصنافه، متوازن، أو أكثر منها."""
    if pct is None:
        return "—", ""
    if pct < LOW_PCT:
        return "أقل من المبيعات", "risk"
    if pct <= HIGH_PCT:
        return "متوازن", "ok"
    return "أكثر من المبيعات", "watch"


def build_vendor_commitment(
    date_from, date_to, *, branch_code: str = "", group_code: str = "", subgroup_code: str = "", vendor_code: str = "",
) -> dict[str, Any]:
    """لكل مورد: الكمية المشتراة منه مقابل مبيعات أصنافه في الفترة، ومع vendor_code تفصيل أصنافه."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    d_from, d_to = _as_date(date_from), _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد تاريخ النهاية.")
    days = (d_to - d_from).days + 1
    if days > MAX_DAYS:
        raise OracleStockError(f"الفترة أطول من {MAX_DAYS} يوماً — اختر فترة أقصر.")
    brn = _norm_brn_code(branch_code)
    gcode = str(group_code or "").strip()
    sub = str(subgroup_code or "").strip() if gcode else ""
    vcode = str(vendor_code or "").strip()

    key = f"vendor_commit:{_CACHE_VER}:{d_from}:{d_to}:{brn}:{gcode}:{sub}:{vcode}"
    try:
        cached = cache.get(key)
    except Exception:  # noqa: BLE001
        cached = None
    if cached is not None:
        return cached

    group = _bind_gcode(gcode) if gcode else ""
    grp = GROUP_SQL if gcode else ALL_ITEMS_SQL
    params: dict[str, Any] = dict(_date_params(d_from, d_to)) | ({"g": group} if gcode else {})
    brn_sql = _brn_filter("{a}", brn, params)
    if sub:
        params["mng"] = sub
        brn_sql += " AND TO_CHAR(i.MNG_CODE) = :mng"

    def job(fn, *args, **kwargs):
        def run():
            with oracle_session():
                return fn(*args, **kwargs)
        return run

    # نقاط البيع لكل الأصناف هي الأثقل (شهر كامل ≈ دقيقة): أسبوع في كل استعلام بالتوازي
    windows = [
        (start, min(end, start + timedelta(days=6)))
        for m_start, end in _month_windows(d_from, min(d_to, date.today()))
        for start in (m_start + timedelta(days=k) for k in range(0, (end - m_start).days + 1, 7))
    ]
    pos_jobs = [job(_pos_qty, params | _date_params(a, b), brn_sql, grp) for a, b in windows]
    results = _run_parallel(
        pos_jobs + [
            job(_bill_sales, params, brn_sql, (1, 5), exclude_summary=True, grp=grp),
            job(_bill_sales, params, brn_sql, (4, 8), exclude_summary=False, grp=grp),
            job(_pos_returns, params, brn_sql, grp),
            job(_bill_returns, params, brn_sql, (1, 5), exclude_summary=True, grp=grp),
            job(_bill_returns, params, brn_sql, (4, 8), exclude_summary=False, grp=grp),
            job(_purchases, params, brn_sql, grp),
            job(_purchase_returns, params, brn_sql, grp),
        ],
        max_workers=6,
        timeout_sec=420,
    )
    pos_rows = [r for part in results[:len(pos_jobs)] for r in part]
    cash_rows, credit_rows, pos_ret, cash_ret, credit_ret, pi_rows, pr_rows = results[len(pos_jobs):]

    sales: dict[str, float] = {}
    for rows, sign in ((pos_rows, 1), (cash_rows, 1), (credit_rows, 1), (pos_ret, -1), (cash_ret, -1), (credit_ret, -1)):
        for r in rows:
            c = _code(r["I"])
            sales[c] = sales.get(c, 0.0) + sign * _f(r["Q"])
    returned = {(_code(r["V"]), _code(r["I"])): _f(r["Q"]) for r in pr_rows}

    pairs: list[dict[str, Any]] = []
    item_total: dict[str, float] = {}
    for r in pi_rows:
        v, i = _code(r["V"]), _code(r["I"])
        ret = returned.get((v, i), 0.0)
        bought = _f(r["Q"]) - ret
        item_total[i] = item_total.get(i, 0.0) + bought
        pairs.append({
            "vendor": v, "vendor_name": str(r.get("VN") or v).strip(), "code": i, "name": str(r.get("IN_") or i).strip(),
            "buy_q": bought, "ret_q": ret, "invoices": int(_f(r["N"])),
            "last_date": _as_date(r["LAST_D"]).isoformat() if r.get("LAST_D") else "",
        })

    vendors: dict[str, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    for x in pairs:
        sold = max(sales.get(x["code"], 0.0), 0.0)
        x.update({
            "sales_q": sold, "other_q": item_total.get(x["code"], 0.0) - x["buy_q"],
            "diff_q": x["buy_q"] - sold, "ratio": _pct(x["buy_q"], sold),
        })
        a = vendors.setdefault(x["vendor"], {
            "code": x["vendor"], "name": x["vendor_name"], "items": 0, "items_short": 0, "items_over": 0, "items_unsold": 0,
            "buy_q": 0.0, "sales_q": 0.0,
        })
        a["items"] += 1
        a["items_unsold"] += 1 if sold <= 0 else 0
        a["items_short"] += 1 if sold > 0 and x["buy_q"] < sold else 0
        a["items_over"] += 1 if sold > 0 and x["buy_q"] > sold else 0
        a["buy_q"] += x["buy_q"]
        a["sales_q"] += sold
        if not vcode or x["vendor"] == vcode:
            items.append(x)

    rows_out: list[dict[str, Any]] = []
    for a in vendors.values():
        ratio = _pct(a["buy_q"], a["sales_q"])
        label, lvl = _level(ratio)
        rows_out.append(a | {"diff_q": a["buy_q"] - a["sales_q"], "ratio": ratio, "level_label": label, "level": lvl})
    rows_out.sort(key=lambda x: (-x["items"], -x["buy_q"], x["code"]))
    for x in rows_out:
        x.update({
            "buy_q_display": _qty(x["buy_q"]), "sales_q_display": _qty(x["sales_q"]), "diff_q_display": _qty(x["diff_q"]),
            "ratio_display": _pct_display(x["ratio"]),
            # الشريط: 100% في المنتصف (الشراء = المبيعات)، والسقف 200%
            "bar": f"{min(x['ratio'] or 0, 200) / 2:.1f}",
        })

    units = {}
    if vcode and items:
        with oracle_session():
            units = _main_units(sorted({it["code"] for it in items}))
    # الأكبر نقصاً (الشراء أقل من البيع) أولاً
    items.sort(key=lambda x: (x["diff_q"], x["code"]))
    for it in items:
        it.update({
            "unit": units.get(it["code"], ""),
            "buy_q_display": _qty(it["buy_q"]), "sales_q_display": _qty(it["sales_q"]), "diff_q_display": _qty(it["diff_q"]),
            "other_q_display": _qty(it["other_q"]), "ret_q_display": _qty(it["ret_q"]),
            "ratio_display": _pct_display(it["ratio"]),
        })

    buy_total = sum(x["buy_q"] for x in rows_out)
    sales_total = sum(x["sales_q"] for x in rows_out)
    levels = {lvl: sum(1 for x in rows_out if x["level"] == lvl) for lvl in ("ok", "watch", "risk")}
    totals = {
        "vendors": len(rows_out), "items": sum(x["items"] for x in rows_out),
        "items_short": sum(x["items_short"] for x in rows_out), "items_over": sum(x["items_over"] for x in rows_out),
        "items_unsold": sum(x["items_unsold"] for x in rows_out),
        "buy_q_display": _qty(buy_total), "sales_q_display": _qty(sales_total), "diff_q_display": _qty(buy_total - sales_total),
        "ratio_display": _pct_display(_pct(buy_total, sales_total)),
        "balanced": levels["ok"], "over": levels["watch"], "under": levels["risk"],
    }

    with oracle_session():
        group_name = (_group_name(group) or gcode) if gcode else ""
        sub_name = (_subgroup_name(group, sub) or sub) if sub else ""
        branch_name = _branch_names().get(brn, brn) if brn else ""
    report = {
        "from": d_from.isoformat(), "to": d_to.isoformat(), "days": days,
        "branch": brn, "branch_name": branch_name, "group_code": gcode, "group_name": group_name,
        "subgroup_code": sub, "subgroup_name": sub_name,
        "vendor_code": vcode, "vendor": next((x for x in rows_out if x["code"] == vcode), None) if vcode else None,
        "rows": rows_out, "items": items, "totals": totals,
        "low_pct": LOW_PCT, "high_pct": HIGH_PCT,
    }
    try:
        cache.set(key, report, _CACHE_TTL)
    except Exception:  # noqa: BLE001
        pass
    return report


def build_vendor_commitment_excel(report: dict[str, Any]):
    """ملف xlsx: ورقة الموردين، وورقة تفصيل الأصناف لكل (مورد، صنف)."""
    from io import BytesIO

    from django.http import HttpResponse
    from openpyxl import Workbook

    from .oracle_pos_hours import _XL_COLORS, _xl_sheet

    scope = " · ".join(filter(None, [
        f"{report['group_code']} - {report['group_name']}" if report.get("group_code") else "كل المجموعات",
        f"فرعية {report['subgroup_code']} - {report['subgroup_name']}" if report.get("subgroup_code") else "",
        report.get("branch_name") or "كل الفروع",
        f"المورد {report['vendor_code']} - {report['vendor']['name']}" if report.get("vendor") else "",
    ]))
    period = f"{scope} · من {report['from']} إلى {report['to']} · الكميات بالوحدة الرئيسية"
    wb = Workbook()
    wb.remove(wb.active)
    t = report["totals"]

    columns = [
        ("رقم المورد", "name", 12), ("المورد", "name", 36), ("الأصناف", "count", 10),
        ("أصناف شراؤها أقل من بيعها", "count", 14), ("أصناف شراؤها أكثر من بيعها", "count", 14),
        ("أصناف بلا مبيعات", "count", 12), ("كمية المبيعات", "count", 14), ("كمية الشراء", "count", 14),
        ("الفرق (شراء − مبيعات)", "count", 15), ("نسبة الشراء إلى المبيعات", "share", 13), ("التقييم", "text", 16),
    ]
    rows = [
        [
            x["code"], x["name"], x["items"], x["items_short"], x["items_over"], x["items_unsold"],
            round(x["sales_q"], 2), round(x["buy_q"], 2), round(x["diff_q"], 2),
            None if x["ratio"] is None else x["ratio"] / 100.0, x["level_label"],
        ]
        for x in report["rows"]
    ]
    buy = sum(x["buy_q"] for x in report["rows"])
    sold = sum(x["sales_q"] for x in report["rows"])
    total = [
        "", "الإجمالي", t["items"], t["items_short"], t["items_over"], t["items_unsold"],
        round(sold, 2), round(buy, 2), round(buy - sold, 2), (buy / sold) if sold else None, "",
    ]
    ws = _xl_sheet(wb, "الموردون", period, columns, rows, [(total, _XL_COLORS["total"])])
    ws.page_setup.orientation = "landscape"

    item_cols = [
        ("رقم المورد", "name", 12), ("المورد", "name", 30), ("الكود", "name", 18), ("الصنف", "name", 36),
        ("الوحدة", "text", 10), ("كمية المبيعات", "count", 13), ("الشراء من المورد", "count", 13),
        ("الفرق (شراء − مبيعات)", "count", 14), ("نسبة الشراء إلى المبيعات", "share", 12),
        ("مرتجع للمورد", "count", 11), ("شراء من موردين آخرين", "count", 13), ("فواتير الشراء", "count", 11),
        ("آخر فاتورة", "text", 12),
    ]
    item_rows = [
        [
            x["vendor"], x["vendor_name"], x["code"], x["name"], x.get("unit", ""), round(x["sales_q"], 2),
            round(x["buy_q"], 2), round(x["diff_q"], 2), None if x["ratio"] is None else x["ratio"] / 100.0,
            round(x["ret_q"], 2), round(x["other_q"], 2), x["invoices"], x["last_date"],
        ]
        for x in sorted(report["items"], key=lambda x: (x["vendor"], x["diff_q"], x["code"]))
    ]
    if report.get("vendor_code"):
        ws = _xl_sheet(wb, "تفصيل الأصناف", period, item_cols, item_rows, [])
        ws.page_setup.orientation = "landscape"
    else:
        # كل الموردين: عشرات آلاف الأسطر — تنسيق كل خلية كان يستغرق أكثر من دقيقة، فالترويسة فقط منسّقة
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        ws = wb.create_sheet("تفصيل الأصناف")
        ws.sheet_view.rightToLeft = True
        ws.append(["تفصيل الأصناف — " + period])
        ws["A1"].font = Font(name="Arial", size=11, bold=True, color="1F2A37")
        ws.append([label for label, _kind, _w in item_cols])
        for i, (_label, _kind, width) in enumerate(item_cols, start=1):
            c = ws.cell(row=2, column=i)
            c.font = Font(name="Arial", size=10, bold=True, color="5A6675")
            c.fill = PatternFill("solid", fgColor=_XL_COLORS["head"])
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.column_dimensions[get_column_letter(i)].width = width
        for row in item_rows:
            ws.append(row)
        for (cell,) in ws.iter_rows(min_row=3, min_col=9, max_col=9):
            cell.number_format = "0.0%"
        ws.freeze_panes = "A3"
        ws.auto_filter.ref = f"A2:{get_column_letter(len(item_cols))}{ws.max_row}"

    buf = BytesIO()
    wb.save(buf)
    stamp = f"{report['from']}_{report['to']}".replace("-", "")
    suffix = f"_{report['vendor_code']}" if report.get("vendor_code") else ""
    response = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="vendor_commitment{suffix}_{stamp}.xlsx"'
    return response
