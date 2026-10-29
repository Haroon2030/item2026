"""كمية الطلب — احتياج الشهر القادم لكل صنف من متوسط بيعه اليومي في فترة، لتفادي المخزون العالي.

قراءة فقط (SELECT). صافي الكمية المباعة = نقاط البيع + الجملة النقدي 1/5 (بلا خلاصات ترحيل نقاط البيع)
+ الآجل 4/8 ناقص مرتجعاتها — نفس استعلامات «دراسة مجموعة» فتتطابق الكميات.
المتوسط اليومي = صافي الكمية ÷ أيام الفترة (حتى اليوم إن كانت الفترة ممتدة للمستقبل)،
واحتياج الشهر القادم = المتوسط اليومي × 30 يوماً. كل الكميات بأكبر وحدة للصنف (كرتون/ربطة: الكمية ÷ حجمها)،
والصنف بلا وحدة كبرى بوحدته الرئيسية؛ كمية الطلب مقرّبة لأعلى بتلك الوحدة.
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Any

from django.core.cache import cache

from .oracle_group_study import (
    GROUP_SQL,
    MAX_DAYS,
    STOCK_QTY,
    _base_units,
    _bill_returns,
    _bill_sales,
    _brn_filter,
    _code,
    _f,
    _group_name,
    _pack_label,
    _pos_returns,
    _qty,
    _subgroup_name,
)
from .oracle_stock import (
    OracleStockError,
    _as_date,
    _bind_gcode,
    _branch_names,
    _date_params,
    _fetch_all,
    _norm_brn_code,
    _pos_owner,
    _run_parallel,
    _schema,
    fetch_item_max_pack_map,
    fetch_warehouse_options,
    oracle_enabled,
    oracle_session,
)

logger = logging.getLogger(__name__)

_CACHE_TTL = 600
_CACHE_VER = "v8"
NEXT_DAYS = 30  # الشهر القادم
ORDER_QTY_PAGE_ROWS = 1000  # صفوف الشاشة؛ ملف Excel فيه كل الأصناف
ALL_GROUPS = "all"
# «كل المجموعات» تستثني الطازج وما يُطلب يومياً: الأسماك، اللحوم (الملحمة، ملحمة خارجية، ملحمه بلدي)،
# المجمدات، الفرش، مجموعة الطازج، الأجبان، المخبز والمخبوزات الخارجية، المحمصة، الشعبيات، مياه عادي، الدواجن والبيض؛
# وما لا يُطلب بهذا الحساب: الملابس (الملابس، مجموعة الملابس بشير)، الأحذية، اسر منتجة، المكينة، كارلاء، خدمات، فلتر، قسم المواليد، وافل وايسكريم
EXCLUDED_GROUPS = (15, 16, 42, 43, 6, 5, 26, 9, 14, 35, 11, 12, 52, 29, 38, 31, 17, 40, 21, 53, 28, 45, 30, 33)
ALL_GROUPS_SQL = f"i.G_CODE NOT IN ({', '.join(str(g) for g in EXCLUDED_GROUPS)})"


def _pos_qty(params: dict, brn_sql: str, grp: str) -> list[dict]:
    """كمية نقاط البيع لكل صنف فقط (بلا قيمة ولا عدّ فواتير) — أخف من استعلام الدراسة لفترات طويلة وكل المجموعات."""
    pos, sch = _pos_owner(), _schema()
    return _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS I, SUM({STOCK_QTY}) AS Q
        FROM {pos}.IAS_POS_BILL_DTL d
        JOIN {pos}.IAS_POS_BILL_MST m
          ON m.BILL_NO = d.BILL_NO AND m.BRN_NO = d.BRN_NO AND NVL(m.BILL_SRL, 0) = NVL(d.BILL_SRL, 0)
        JOIN {sch}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
          AND (m.HUNG IS NULL OR m.HUNG = 0) AND {grp} {brn_sql.replace('{a}', 'm')}
        GROUP BY TO_CHAR(d.I_CODE)
        """,
        params,
    )


def _month_windows(d_from: date, d_to: date) -> list[tuple[date, date]]:
    """الفترة مقسّمة أشهراً (أول وآخر يوم من كل جزء) حتى يُجلب كل شهر في استعلام مستقل بالتوازي."""
    out, start = [], d_from
    while start <= d_to:
        nxt = (start.replace(day=1) + timedelta(days=32)).replace(day=1)
        end = min(d_to, nxt - timedelta(days=1))
        out.append((start, end))
        start = end + timedelta(days=1)
    return out


def _stock(brn: str, sub: str, grp: str, group: Any) -> dict[str, float]:
    """الرصيد الحالي المرحّل (AVL_QTY بالوحدة الرئيسية) لكل صنف في مخازن الفرع، أو كل المخازن بلا فرع."""
    sch = _schema()
    params: dict[str, Any] = ({"g": group} if ":g" in grp else {}) | ({"mng": sub} if sub else {})
    wh_sql = ""
    if brn:
        whs = [
            _code(w.get("code")) for w in fetch_warehouse_options(active_only=False) or []
            if _norm_brn_code(w.get("branch_code")) == brn and _code(w.get("code"))
        ]
        if not whs:
            return {}
        for n, wh in enumerate(whs):
            params[f"w{n}"] = int(wh) if wh.isdigit() else wh
        wh_sql = f"AND w.W_CODE IN ({', '.join(f':w{n}' for n in range(len(whs)))})"
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(w.I_CODE) AS I, SUM(NVL(w.AVL_QTY, 0)) AS Q
        FROM {sch}.IAS_ITM_WCODE w
        JOIN {sch}.IAS_ITM_MST i ON i.I_CODE = w.I_CODE
        WHERE {grp} {"AND TO_CHAR(i.MNG_CODE) = :mng" if sub else ""} {wh_sql}
        GROUP BY TO_CHAR(w.I_CODE)
        """,
        params,
    )
    return {_code(r["I"]): _f(r["Q"]) for r in rows}


def _item_names(group: Any, sub: str, grp: str = GROUP_SQL) -> dict[str, tuple[str, str]]:
    """اسم الصنف ومجموعته «39 - البان مستورده» لكل صنف."""
    sub_sql = "AND TO_CHAR(i.MNG_CODE) = :mng" if sub else ""
    sch = _schema()
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(i.I_CODE) AS I, i.I_NAME AS N, TO_CHAR(i.G_CODE) AS G, g.G_A_NAME AS GN
        FROM {sch}.IAS_ITM_MST i
        LEFT JOIN {sch}.GROUP_DETAILS g ON g.G_CODE = i.G_CODE
        WHERE {grp} {sub_sql}
        """,
        ({"g": group} if ":g" in grp else {}) | ({"mng": sub} if sub else {}),
    )
    out: dict[str, tuple[str, str]] = {}
    for r in rows:
        gc, gn = _code(r.get("G")), str(r.get("GN") or "").strip()
        out[_code(r["I"])] = (str(r.get("N") or "").strip(), f"{gc} - {gn}" if gn else gc)
    return out


def build_order_qty(date_from, date_to, *, group_code: str, branch_code: str = "", subgroup_code: str = "") -> dict[str, Any]:
    """كمية الطلب: لكل صنف مباع في الفترة صافي كميته، متوسطه اليومي والشهري، واحتياج 30 يوماً بالوحدة والعبوة.
    group_code = "all": كل المجموعات عدا EXCLUDED_GROUPS (ولا فرعية)."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    gcode = str(group_code or "").strip()
    if not gcode:
        raise OracleStockError("اختر المجموعة.")
    d_from, d_to = _as_date(date_from), _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد تاريخ النهاية.")
    if (d_to - d_from).days + 1 > MAX_DAYS:
        raise OracleStockError(f"الفترة أطول من {MAX_DAYS} يوماً — اختر فترة أقصر.")
    days = max(1, (min(d_to, date.today()) - d_from).days + 1)
    brn = _norm_brn_code(branch_code)
    all_groups = gcode == ALL_GROUPS
    sub = "" if all_groups else str(subgroup_code or "").strip()

    key = f"order_qty:{_CACHE_VER}:{d_from}:{d_to}:{gcode}:{sub}:{brn}"
    try:
        cached = cache.get(key)
    except Exception:  # noqa: BLE001
        cached = None
    if cached is not None:
        return cached

    group = None if all_groups else _bind_gcode(gcode)
    grp = ALL_GROUPS_SQL if all_groups else GROUP_SQL
    params: dict[str, Any] = dict(_date_params(d_from, d_to)) | ({} if all_groups else {"g": group})
    brn_sql = _brn_filter("{a}", brn, params)
    if sub:
        params["mng"] = sub
        brn_sql += " AND TO_CHAR(i.MNG_CODE) = :mng"

    def job(fn, *args, **kwargs):
        def run():
            with oracle_session():
                return fn(*args, **kwargs)
        return run

    # نقاط البيع هي الأثقل: شهر في كل استعلام بالتوازي (6 أشهر لكل المجموعات تتجاوز مهلة الاستعلام الواحد)
    windows = _month_windows(d_from, min(d_to, date.today()))
    pos_jobs = [job(_pos_qty, params | _date_params(a, b), brn_sql, grp) for a, b in windows]
    results = _run_parallel(
        pos_jobs + [
            job(_bill_sales, params, brn_sql, (1, 5), exclude_summary=True, grp=grp),
            job(_bill_sales, params, brn_sql, (4, 8), exclude_summary=False, grp=grp),
            job(_pos_returns, params, brn_sql, grp),
            job(_bill_returns, params, brn_sql, (1, 5), exclude_summary=True, grp=grp),
            job(_bill_returns, params, brn_sql, (4, 8), exclude_summary=False, grp=grp),
            job(_item_names, group, sub, grp),
            job(_stock, brn, sub, grp, group),
        ],
        max_workers=6,
        timeout_sec=420,
    )
    pos_rows = [r for part in results[:len(pos_jobs)] for r in part]
    cash_rows, credit_rows, pos_ret, cash_ret, credit_ret, names, stock = results[len(pos_jobs):]

    sold: dict[str, float] = {}
    returned: dict[str, float] = {}
    for rows in (pos_rows, cash_rows, credit_rows):
        for r in rows:
            sold[_code(r["I"])] = sold.get(_code(r["I"]), 0.0) + _f(r["Q"])
    for rows in (pos_ret, cash_ret, credit_ret):
        for r in rows:
            returned[_code(r["I"])] = returned.get(_code(r["I"]), 0.0) + _f(r["Q"])

    codes = sorted(c for c in set(sold) | set(returned) if sold.get(c, 0.0) - returned.get(c, 0.0) > 0)
    with oracle_session():
        packs = fetch_item_max_pack_map(codes) if codes else {}
        bases = _base_units(group, sub, grp) if codes else {}
        group_name = "كل المجموعات" if all_groups else (_group_name(group) or gcode)
        sub_name = (_subgroup_name(group, sub) or sub) if sub else ""
        branch_name = _branch_names().get(brn, brn) if brn else ""

    rows_out: list[dict[str, Any]] = []
    for code in codes:
        pk = packs.get(code) or {}
        pack = max(_f(pk.get("pack")), 1.0)
        base = bases.get(code, "")
        # أكبر وحدة للصنف: «كرتون (144 حبة)»؛ بلا وحدة كبرى تبقى الرئيسية (حجمها 1)
        unit = _pack_label(pack, str(pk.get("unit") or ""), base) if pack > 1 else (base or "—")
        sold_q = sold.get(code, 0.0) / pack
        ret_q = returned.get(code, 0.0) / pack
        net_q = sold_q - ret_q
        daily = net_q / days
        need = daily * NEXT_DAYS
        rows_out.append({
            "code": code,
            "name": (names.get(code) or (code, ""))[0] or code,
            "group": (names.get(code) or ("", "—"))[1] or "—",
            "unit": unit, "pack": pack,
            "sold_q": sold_q, "ret_q": ret_q, "net_q": net_q,
            "daily": daily, "need": need,
            "order_q": math.ceil(round(need, 4)),  # بأكبر وحدة مقرّباً لأعلى
            "stock_q": stock.get(code, 0.0) / pack,  # الرصيد الحالي للمقارنة فقط (لا يُخصم من الطلب)
        })
    rows_out.sort(key=lambda r: (-r["need"], r["code"]))  # الأعلى احتياجاً بأكبر وحدة
    for r in rows_out:
        r.update({
            "sold_q_display": _qty(r["sold_q"]), "ret_q_display": _qty(r["ret_q"]), "net_q_display": _qty(r["net_q"]),
            "daily_display": _qty(r["daily"]), "need_display": _qty(r["need"]),
            "order_q_display": f"{r['order_q']:,}",
            "stock_q_display": _qty(r["stock_q"]),
        })

    report = {
        "from": d_from.isoformat(), "to": d_to.isoformat(), "days": days, "next_days": NEXT_DAYS,
        "group_code": gcode, "group_name": group_name, "all_groups": all_groups, "subgroup_code": sub, "subgroup_name": sub_name,
        "branch": brn, "branch_name": branch_name,
        "rows": rows_out,
        # وحدات الأصناف مختلفة (كرتون 144 وكرتون 12…): الإجمالي عدد العبوات المطلوبة فقط
        "totals": {
            "items": len(rows_out),
            "items_display": f"{len(rows_out):,}",
            "order_q_display": f"{sum(r['order_q'] for r in rows_out):,}",
        },
    }
    try:
        cache.set(key, report, _CACHE_TTL)
    except Exception:  # noqa: BLE001
        pass
    return report


def build_order_qty_excel(report: dict[str, Any]):
    """ملف xlsx لكمية الطلب بأعمدة الشاشة وأرقام قابلة للحساب."""
    from io import BytesIO

    from django.http import HttpResponse
    from openpyxl import Workbook

    from .oracle_pos_hours import _XL_COLORS, _xl_sheet

    sub = f" · فرعية {report['subgroup_code']} - {report['subgroup_name']}" if report.get("subgroup_code") else ""
    period = (
        f"{report['group_name'] if report.get('all_groups') else report['group_code'] + ' - ' + report['group_name']}{sub}"
        f" · {report.get('branch_name') or 'كل الفروع'}"
        f" · من {report['from']} إلى {report['to']} ({report['days']} يوم) · الاحتياج لـ {report['next_days']} يوماً"
    )
    wb = Workbook()
    wb.remove(wb.active)
    columns = [
        ("الكود", "name", 18), ("الصنف", "name", 36), ("الوحدة", "text", 18), ("المجموعة", "text", 22),
        ("الكمية المباعة", "avg", 13), ("المرتجع", "avg", 11), ("صافي الكمية", "avg", 13),
        ("المتوسط اليومي", "avg", 13), (f"احتياج {report['next_days']} يوماً", "avg", 15), ("كمية الطلب", "count", 13),
        ("الرصيد الحالي", "avg", 13),
    ]
    rows = [
        [
            x["code"], x["name"], x["unit"], x.get("group", ""), round(x["sold_q"], 2), round(x["ret_q"], 2),
            round(x["net_q"], 2), round(x["daily"], 2), round(x["need"], 2), x["order_q"], round(x.get("stock_q", 0.0), 2),
        ]
        for x in report["rows"]
    ]
    items = report["rows"]
    total = ["", "الإجمالي (عدد العبوات)", "", "", None, None, None, None, None, sum(x["order_q"] for x in items), None]
    ws = _xl_sheet(wb, "كمية الطلب", period, columns, rows, [(total, _XL_COLORS["total"])])
    ws.page_setup.orientation = "landscape"

    buf = BytesIO()
    wb.save(buf)
    stamp = f"{report['from']}_{report['to']}".replace("-", "")
    code = f"{report['group_code']}_{report['subgroup_code']}" if report.get("subgroup_code") else report["group_code"]
    response = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="order_qty_{code}_{stamp}.xlsx"'
    return response
