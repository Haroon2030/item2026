"""تقارير المخزون — شراء المجموعات مقابل مبيعاتها (للإدارة فقط).

قراءة فقط (SELECT). يكشف هل تشتري المجموعة أكثر مما تبيعه الشركة منها، لكل مجموعة في الفترة:
- المشتريات: فواتير الشراء (IAS_PI_BILL_*) غير المعلّقة ناقص مرتجعات الشراء (IAS_PR_BILL_*)،
  بالقيمة قبل الضريبة (الكمية × السعر − الخصم).
- المبيعات: صافي المبيعات بعد المرتجع (نقاط البيع + الجملة النقدي 1/5 بلا خلاصات الترحيل + الآجل 4/8)
  بنفس أساس «دراسة مجموعة».
- تكلفة المبيعات: صافي الكمية المباعة × متوسط التكلفة الحالي للصنف — وهي ما يُقارن بالمشتريات
  (الشراء والتكلفة كلاهما بسعر التكلفة).
- نسبة الشراء إلى تكلفة المبيعات: فوق HIGH_PCT تعني أن المجموعة تشتري أكثر مما تبيع.
- المخزون الحالي: الرصيد المرحّل في كل المخازن عدا المستثناة × متوسط التكلفة.
- المخزون الزائد: لكل صنف الرصيد فوق تغطية COVER_DAYS يوماً من متوسط بيعه اليومي في الفترة × متوسط التكلفة.
- النمو: المشتريات والمبيعات مقارنة بالفترة السابقة المماثلة (بنفس عدد الأيام قبلها مباشرة).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from django.core.cache import cache

from .oracle_group_study import (
    STOCK_QTY,
    _bill_returns,
    _bill_sales,
    _brn_filter,
    _code,
    _f,
    _money,
    _pct,
    _pct_display,
    _qty,
)
from .oracle_order_qty import EXCLUDED_WAREHOUSES, _month_windows
from .oracle_stock import (
    OracleStockError,
    _as_date,
    _date_params,
    _fetch_all,
    _norm_brn_code,
    _pos_dtl_net_sql,
    _pos_owner,
    _run_parallel,
    _schema,
    fetch_item_max_pack_map,
    fetch_warehouse_options,
    oracle_enabled,
    oracle_session,
)
from .oracle_vendor_commitment import _main_units

logger = logging.getLogger(__name__)

_CACHE_TTL = 600
_CACHE_VER = "v2"
MAX_DAYS = 200
COVER_DAYS = 30  # ما فوق بيع 30 يوماً يُعدّ مخزوناً زائداً
LOW_PCT = 90
HIGH_PCT = 110
ITEM_PAGE_ROWS = 100
ALL = "1 = 1"


# ===== الاستعلامات =====

def _pos_iv(params: dict, flt: str = "") -> list[dict]:
    """نقاط البيع لكل صنف: الكمية والقيمة فقط (أخف استعلام لكل الأصناف)."""
    pos = _pos_owner()
    return _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS I, SUM({STOCK_QTY}) AS Q, ROUND(SUM({_pos_dtl_net_sql("d")}), 2) AS V
        FROM {pos}.IAS_POS_BILL_DTL d
        JOIN {pos}.IAS_POS_BILL_MST m
          ON m.BILL_NO = d.BILL_NO AND m.BRN_NO = d.BRN_NO AND NVL(m.BILL_SRL, 0) = NVL(d.BILL_SRL, 0)
        WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl AND (m.HUNG IS NULL OR m.HUNG = 0) {flt}
        GROUP BY TO_CHAR(d.I_CODE)
        """,
        params,
    )


def _pos_returns_iv(params: dict, flt: str = "") -> list[dict]:
    pos = _pos_owner()
    return _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS I, SUM({STOCK_QTY}) AS Q, ROUND(SUM({_pos_dtl_net_sql("d")}), 2) AS V
        FROM {pos}.IAS_POS_RT_BILL_DTL d
        JOIN {pos}.IAS_POS_RT_BILL_MST m ON m.RT_BILL_NO = d.RT_BILL_NO AND m.BRN_NO = d.BRN_NO
        WHERE m.RT_BILL_DATE >= :d_from AND m.RT_BILL_DATE < :d_to_excl AND (m.HUNG IS NULL OR m.HUNG = 0) {flt}
        GROUP BY TO_CHAR(d.I_CODE)
        """,
        params,
    )


def _purchases_iv(params: dict, flt: str = "") -> list[dict]:
    """فواتير الشراء لكل صنف: الكمية والقيمة قبل الضريبة وعدد الفواتير."""
    sch = _schema()
    return _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS I, SUM({STOCK_QTY}) AS Q,
               ROUND(SUM(NVL(d.I_QTY, 0) * NVL(d.I_PRICE, 0) - NVL(d.DIS_AMT, 0)), 2) AS V
        FROM {sch}.IAS_PI_BILL_DTL d
        JOIN {sch}.IAS_PI_BILL_MST m
          ON m.BILL_NO = d.BILL_NO AND m.BILL_SER = d.BILL_SER AND m.BILL_DOC_TYPE = d.BILL_DOC_TYPE
        WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl AND NVL(m.HUNG, 0) = 0 {flt}
        GROUP BY TO_CHAR(d.I_CODE)
        """,
        params,
    )


def _purchase_returns_iv(params: dict, flt: str = "") -> list[dict]:
    sch = _schema()
    return _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS I, SUM({STOCK_QTY}) AS Q,
               ROUND(SUM(NVL(d.I_QTY, 0) * NVL(d.I_PRICE, 0) - NVL(d.DIS_AMT, 0)), 2) AS V
        FROM {sch}.IAS_PR_BILL_DTL d
        JOIN {sch}.IAS_PR_BILL_MST r
          ON r.RT_BILL_NO = d.RT_BILL_NO AND r.RT_BILL_SER = d.RT_BILL_SER AND r.RT_BILL_DOC_TYPE = d.RT_BILL_DOC_TYPE
        WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl AND NVL(r.HUNG, 0) = 0 {flt}
        GROUP BY TO_CHAR(d.I_CODE)
        """,
        params,
    )


def _stock_all(whs: list[str] | None = None) -> list[dict]:
    """الرصيد الحالي ومتوسط التكلفة لكل صنف: في المخازن المحددة، وإلا كل المخازن عدا المستثناة."""
    sch = _schema()
    params: dict[str, Any] = {}
    if whs is None:
        wh_sql = f"w.W_CODE NOT IN ({', '.join(str(int(w)) for w in EXCLUDED_WAREHOUSES)})"
    elif not whs:
        return []
    else:
        for n, wh in enumerate(whs):
            params[f"w{n}"] = int(wh) if wh.isdigit() else wh
        wh_sql = f"w.W_CODE IN ({', '.join(f':w{n}' for n in range(len(whs)))})"
    return _fetch_all(
        f"""
        SELECT TO_CHAR(w.I_CODE) AS I, SUM(NVL(w.AVL_QTY, 0)) AS Q,
               AVG(CASE WHEN NVL(w.I_CWTAVG, w.PRIMARY_COST) > 0 THEN NVL(w.I_CWTAVG, w.PRIMARY_COST) END) AS UC
        FROM {sch}.IAS_ITM_WCODE w
        WHERE {wh_sql}
        GROUP BY TO_CHAR(w.I_CODE)
        """,
        params,
    )


def _items_map() -> list[dict]:
    sch = _schema()
    return _fetch_all(f"SELECT TO_CHAR(i.I_CODE) AS I, TO_CHAR(i.G_CODE) AS G, TO_CHAR(i.MNG_CODE) AS MNG, i.I_NAME AS N FROM {sch}.IAS_ITM_MST i", {})


def _group_names() -> dict[str, str]:
    rows = _fetch_all(f"SELECT TO_CHAR(G_CODE) AS G, G_A_NAME AS N FROM {_schema()}.GROUP_DETAILS", {})
    return {_code(r["G"]): str(r.get("N") or "").strip() for r in rows}


# ===== الحساب =====

def _weeks(d_from: date, d_to: date) -> list[tuple[date, date]]:
    """أسبوع في كل استعلام نقاط بيع (شهر كامل لكل الأصناف ثقيل) — تُشغَّل بالتوازي."""
    return [
        (start, min(end, start + timedelta(days=6)))
        for m_start, end in _month_windows(d_from, d_to)
        for start in (m_start + timedelta(days=k) for k in range(0, (end - m_start).days + 1, 7))
    ]


def _level(pct: float | None) -> tuple[str, str]:
    if pct is None:
        return "بلا مبيعات", "risk"
    if pct > HIGH_PCT:
        return "يشتري أكثر من المبيعات", "risk"
    if pct >= LOW_PCT:
        return "متوازن", "ok"
    return "يشتري أقل من المبيعات", "watch"


def _growth(cur: float, prev: float) -> float | None:
    return round((cur - prev) * 100.0 / abs(prev), 1) if prev else None


def _pack_text(pack: float, unit: str, base: str) -> str:
    """«كرتون = 12 كيلو»: العبوة الكبرى ثم ما تحويه من الوحدة الرئيسية."""
    unit = unit.replace("ـ", "").strip().rstrip("0123456789").strip()  # «كرتون1» في الكرت = كرتون
    inner = f"{_qty(pack)} {base}".strip()
    return f"{unit} = {inner}" if unit else inner


def _signed(value: float | None) -> str:
    return "—" if value is None else f"{value:+.1f}%"


def build_group_overbuying(
    date_from, date_to, *, group_code: str = "", subgroup_code: str = "", branch_code: str = "", warehouse_code: str = "",
) -> dict[str, Any]:
    """لكل مجموعة: المشتريات مقابل المبيعات وتكلفتها، المخزون الحالي والزائد، ونمو الشراء والبيع.
    الفرع والمخزن يقيّدان الحركة والرصيد معاً؛ ومع group_code (و subgroup_code) تفصيل أصناف المجموعة."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    d_from, d_to = _as_date(date_from), _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد تاريخ النهاية.")
    d_to = min(d_to, date.today())
    days = (d_to - d_from).days + 1
    if days > MAX_DAYS:
        raise OracleStockError(f"الفترة أطول من {MAX_DAYS} يوماً — اختر فترة أقصر.")
    p_to = d_from - timedelta(days=1)
    p_from = p_to - timedelta(days=days - 1)
    gsel = _code(group_code)
    sub = str(subgroup_code or "").strip() if gsel else ""
    brn = _norm_brn_code(branch_code)
    wh = _code(warehouse_code)

    key = f"group_overbuy:{_CACHE_VER}:{d_from}:{d_to}:{brn}:{wh}"
    try:
        base = cache.get(key)
    except Exception:  # noqa: BLE001
        base = None
    if base is None:
        base = _collect(d_from, d_to, p_from, p_to, brn, wh)
        try:
            cache.set(key, base, _CACHE_TTL)
        except Exception:  # noqa: BLE001
            pass
    return _shape(base, d_from, d_to, p_from, p_to, days, gsel, sub)


def _scope_warehouses(brn: str, wh: str) -> list[str] | None:
    """مخازن الرصيد: المخزن المختار، وإلا مخازن الفرع عدا المستثناة، وإلا None (كل المخازن عدا المستثناة)."""
    if wh:
        return [wh]
    if not brn:
        return None
    excluded = {str(w) for w in EXCLUDED_WAREHOUSES}
    return [
        _code(w.get("code")) for w in fetch_warehouse_options(active_only=False) or []
        if _norm_brn_code(w.get("branch_code")) == brn and _code(w.get("code")) and _code(w.get("code")) not in excluded
    ]


def _collect(d_from: date, d_to: date, p_from: date, p_to: date, brn: str = "", wh: str = "") -> dict[str, Any]:
    """يجلب الأرقام الخام لكل صنف للفترتين (قابلة للتخزين المؤقت ثم تُشكَّل لكل مجموعة)."""
    def job(fn, *args, **kwargs):
        def run():
            with oracle_session():
                return fn(*args, **kwargs)
        return run

    extra: dict[str, Any] = {}
    brn_sql = _brn_filter("{a}", brn, extra)  # AND {a}.BRN_NO = :brn
    pos_flt = brn_sql.replace("{a}", "m")
    pur_flt = brn_sql.replace("{a}", "m")
    ret_flt = brn_sql.replace("{a}", "r")
    bill_sql = brn_sql
    if wh:
        extra["wh"] = int(wh) if wh.isdigit() else wh
        pos_flt += " AND m.W_CODE = :wh"
        pur_flt += " AND NVL(d.W_CODE, m.W_CODE) = :wh"
        ret_flt += " AND NVL(d.W_CODE, r.W_CODE) = :wh"
        bill_sql += " AND d.W_CODE = :wh"
    with oracle_session():
        whs = _scope_warehouses(brn, wh)
    periods = {"cur": extra | _date_params(d_from, d_to), "prev": extra | _date_params(p_from, p_to)}
    jobs, tags = [], []
    for tag, (a, b) in (("cur", (d_from, d_to)), ("prev", (p_from, p_to))):
        prm = periods[tag]
        for wa, wb in _weeks(a, b):
            jobs.append(job(_pos_iv, extra | _date_params(wa, wb), pos_flt))
            tags.append((tag, "sales", 1))
        for fn, sign, kind in (
            (lambda p: _bill_sales(p, bill_sql, (1, 5), exclude_summary=True, grp=ALL), 1, "sales"),
            (lambda p: _bill_sales(p, bill_sql, (4, 8), exclude_summary=False, grp=ALL), 1, "sales"),
            (lambda p: _pos_returns_iv(p, pos_flt), -1, "sales"),
            (lambda p: _bill_returns(p, bill_sql, (1, 5), exclude_summary=True, grp=ALL), -1, "sales"),
            (lambda p: _bill_returns(p, bill_sql, (4, 8), exclude_summary=False, grp=ALL), -1, "sales"),
            (lambda p: _purchases_iv(p, pur_flt), 1, "buy"),
            (lambda p: _purchase_returns_iv(p, ret_flt), -1, "buy"),
        ):
            jobs.append(job(fn, prm))
            tags.append((tag, kind, sign))
    jobs += [job(_stock_all, whs), job(_stock_all, None), job(_items_map), job(_group_names)]
    results = _run_parallel(jobs, max_workers=6, timeout_sec=480)
    stock_rows, cost_rows, item_rows, gnames = results[-4:]

    # data[tag][kind][item] = [qty, value]
    data: dict[str, dict[str, dict[str, list[float]]]] = {
        t: {"sales": {}, "buy": {}} for t in ("cur", "prev")
    }
    for (tag, kind, sign), rows in zip(tags, results[:len(tags)]):
        bucket = data[tag][kind]
        for r in rows:
            acc = bucket.setdefault(_code(r["I"]), [0.0, 0.0])
            acc[0] += sign * _f(r["Q"])
            acc[1] += sign * _f(r["V"])
    return {
        "data": data,
        # الرصيد من نطاق الفرع/المخزن، والتكلفة من متوسط كل المخازن حتى يُقيَّم بيع الفرع بتكلفة الصنف
        "stock": {_code(r["I"]): (_f(r["Q"]), _f(r["UC"])) for r in stock_rows},
        "costs": {_code(r["I"]): _f(r["UC"]) for r in cost_rows},
        "items": {_code(r["I"]): (_code(r["G"]), str(r.get("N") or "").strip(), _code(r.get("MNG"))) for r in item_rows},
        "gnames": gnames,
    }


def _shape(base, d_from, d_to, p_from, p_to, days, gsel, sub="") -> dict[str, Any]:
    data, stock, items, gnames = base["data"], base["stock"], base["items"], base["gnames"]
    costs = base["costs"]
    cur_s, cur_b = data["cur"]["sales"], data["cur"]["buy"]
    prev_s, prev_b = data["prev"]["sales"], data["prev"]["buy"]
    keys = ("buy_v", "sales_v", "cost", "stock_v", "excess_v", "prev_buy_v", "prev_sales_v")
    groups: dict[str, dict[str, Any]] = {}
    item_rows: list[dict] = []
    codes = set(cur_s) | set(cur_b) | set(prev_s) | set(prev_b) | {c for c, (q, _) in stock.items() if q > 0}
    for c in codes:
        g, name, mng = items.get(c, ("", "", ""))
        if not g or (sub and g == gsel and mng != sub):
            continue
        sq, sv = cur_s.get(c, (0.0, 0.0))
        bq, bv = cur_b.get(c, (0.0, 0.0))
        st_q, scope_uc = stock.get(c, (0.0, 0.0))
        uc = costs.get(c) or scope_uc
        st_q = max(st_q, 0.0)
        daily = max(sq, 0.0) / days
        excess_q = max(0.0, st_q - COVER_DAYS * daily)
        row = {
            "buy_v": bv, "sales_v": sv, "cost": sq * uc, "stock_v": st_q * uc, "excess_v": excess_q * uc,
            "prev_buy_v": prev_b.get(c, (0.0, 0.0))[1], "prev_sales_v": prev_s.get(c, (0.0, 0.0))[1],
        }
        acc = groups.setdefault(g, {k: 0.0 for k in keys} | {"items": 0, "items_excess": 0})
        for k in keys:
            acc[k] += row[k]
        acc["items"] += 1
        acc["items_excess"] += 1 if excess_q > 0 and uc > 0 else 0
        if gsel and g == gsel:
            cover = (st_q / daily) if daily > 0 else None
            item_rows.append(row | {
                "code": c, "name": name, "sales_q": sq, "buy_q": bq, "stock_q": st_q, "excess_q": excess_q,
                "unit_cost": uc, "cover": cover,
            })

    def finish(r: dict[str, Any]) -> dict[str, Any]:
        ratio = _pct(r["buy_v"], r["cost"]) if r["cost"] > 0 else None
        label, cls = _level(ratio) if (r["buy_v"] > 0 or r["cost"] > 0) else ("بلا حركة", "")
        bg, sg = _growth(r["buy_v"], r["prev_buy_v"]), _growth(r["sales_v"], r["prev_sales_v"])
        gap = None if bg is None or sg is None else round(bg - sg, 1)
        return r | {
            "ratio": ratio, "ratio_display": _pct_display(ratio), "level_label": label, "level_class": cls,
            "buy_growth": bg, "sales_growth": sg, "gap": gap,
            "buy_growth_display": _signed(bg), "sales_growth_display": _signed(sg),
            "gap_display": "—" if gap is None else f"{gap:+.1f} نقطة",
            "over_buy_v": max(0.0, r["buy_v"] - r["cost"]),
            "excess_share": _pct(r["excess_v"], r["stock_v"]),
        } | {f"{k}_display": _money(r[k]) for k in keys} | {
            "over_buy_v_display": _money(max(0.0, r["buy_v"] - r["cost"])),
            "excess_share_display": _pct_display(_pct(r["excess_v"], r["stock_v"])),
        }

    rows = [
        finish(v | {"code": g, "name": gnames.get(g, "") or f"مجموعة {g}"})
        for g, v in groups.items()
        if v["buy_v"] or v["sales_v"] or v["stock_v"]
    ]
    # الأخطر أولاً: الشراء الزائد على التكلفة، ثم المخزون الزائد
    rows.sort(key=lambda r: (-r["over_buy_v"], -r["excess_v"]))
    mx = max([max(r["buy_v"], r["cost"]) for r in rows] + [1.0])
    for r in rows:  # أطوال أشرطة المقارنة (المشتريات مقابل تكلفة المبيعات)
        r["buy_w"] = round(max(r["buy_v"], 0.0) * 100.0 / mx, 1)
        r["cost_w"] = round(max(r["cost"], 0.0) * 100.0 / mx, 1)
    T = finish({k: sum(r[k] for r in rows) for k in keys} | {
        "items": sum(r["items"] for r in rows), "items_excess": sum(r["items_excess"] for r in rows),
    })
    T |= {
        "groups": len(rows),
        "groups_over": sum(1 for r in rows if r["level_class"] == "risk" and r["buy_v"] > 0),
        "groups_faster": sum(1 for r in rows if (r["gap"] or 0) > 0),
    }

    group = None
    items_out: list[dict] = []
    if gsel:
        group = next((r for r in rows if r["code"] == gsel), None)
        item_rows.sort(key=lambda r: (-r["excess_v"], -(r["buy_v"] - r["cost"])))
        # الوحدة الرئيسية (أساس الكميات) والعبوة الكبرى «كرتون (24 حبة)»
        codes = [r["code"] for r in item_rows]
        with oracle_session():
            bases = _main_units(codes) if codes else {}
            packs = fetch_item_max_pack_map(codes) if codes else {}
        for r in item_rows:
            ratio = _pct(r["buy_v"], r["cost"]) if r["cost"] > 0 else None
            base_u = bases.get(r["code"], "")
            pk = packs.get(r["code"]) or {}
            pack = _f(pk.get("pack"))
            items_out.append(r | {
                "unit": base_u or "—",
                "pack": pack,
                "pack_label": _pack_text(pack, str(pk.get("unit") or ""), base_u) if pack > 1 else "—",
                "ratio_display": _pct_display(ratio),
                "sales_q_display": _qty(r["sales_q"]), "buy_q_display": _qty(r["buy_q"]),
                "stock_q_display": _qty(r["stock_q"]), "excess_q_display": _qty(r["excess_q"]),
                "unit_cost_display": _money(r["unit_cost"]),
                "cover_display": "—" if r["cover"] is None else f"{r['cover']:,.0f} يوم",
            } | {f"{k}_display": _money(r[k]) for k in ("buy_v", "sales_v", "cost", "stock_v", "excess_v")})

    return {
        "date_from": d_from, "date_to": d_to, "prev_from": p_from, "prev_to": p_to, "days": days,
        "cover_days": COVER_DAYS, "low_pct": LOW_PCT, "high_pct": HIGH_PCT,
        "rows": rows, "totals": T, "group": group, "items": items_out, "group_code": gsel,
    }


def build_group_overbuying_excel(report: dict[str, Any]):
    from django.http import HttpResponse
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    head_fill = PatternFill("solid", fgColor="1F3A5F")
    head_font = Font(bold=True, color="FFFFFF")

    def sheet(ws, headers, data, pct_cols=()):
        ws.sheet_view.rightToLeft = True
        ws.append(headers)
        for cell in ws[1]:
            cell.fill, cell.font = head_fill, head_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for row in data:
            ws.append(row)
        for col in pct_cols:
            for (cell,) in ws.iter_rows(min_row=2, min_col=col, max_col=col):
                cell.number_format = "0.0%"
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = 16
        ws.column_dimensions["B"].width = 34
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    def frac(v):
        return None if v is None else round(v / 100.0, 4)

    ws = wb.active
    ws.title = "المجموعات"
    sheet(
        ws,
        ["الكود", "المجموعة", "المشتريات", "المبيعات", "تكلفة المبيعات", "الشراء ÷ التكلفة", "التقييم",
         "شراء زائد عن التكلفة", "المخزون الحالي", "المخزون الزائد", "نسبة الزائد",
         "نمو المشتريات", "نمو المبيعات", "الفرق (نقطة)"],
        [
            [r["code"], r["name"], round(r["buy_v"], 2), round(r["sales_v"], 2), round(r["cost"], 2),
             frac(r["ratio"]), r["level_label"], round(r["over_buy_v"], 2), round(r["stock_v"], 2),
             round(r["excess_v"], 2), frac(r["excess_share"]), frac(r["buy_growth"]), frac(r["sales_growth"]), r["gap"]]
            for r in report["rows"]
        ],
        pct_cols=(6, 11, 12, 13),
    )
    if report.get("items"):
        sheet(
            wb.create_sheet("أصناف المجموعة"),
            ["الكود", "الصنف", "الوحدة", "العبوة", "كمية المبيعات", "كمية الشراء", "الرصيد", "تغطية (يوم)", "الرصيد الزائد",
             "متوسط التكلفة", "المشتريات", "تكلفة المبيعات", "قيمة المخزون", "قيمة الزائد"],
            [
                [x["code"], x["name"], x["unit"], x["pack_label"], round(x["sales_q"], 2), round(x["buy_q"], 2), round(x["stock_q"], 2),
                 None if x["cover"] is None else round(x["cover"]), round(x["excess_q"], 2), round(x["unit_cost"], 2),
                 round(x["buy_v"], 2), round(x["cost"], 2), round(x["stock_v"], 2), round(x["excess_v"], 2)]
                for x in report["items"]
            ],
        )

    from io import BytesIO

    buf = BytesIO()
    wb.save(buf)
    suffix = f"_{report['group_code']}" if report.get("group_code") else ""
    name = f"group_overbuying{suffix}_{report['date_from']:%Y%m%d}_{report['date_to']:%Y%m%d}.xlsx"
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp
