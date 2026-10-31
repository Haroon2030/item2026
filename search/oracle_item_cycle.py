"""دورة الصنف — آخر تاريخ بيع وآخر تاريخ توريد لكل صنف، لمتابعة دورانه ورصيده.

قراءة فقط (SELECT) من حركة الأصناف ITEM_MOVEMENT (السنة المالية الحالية؛ تبدأ بأرصدة افتتاحية بتاريخ 1/1):
  • آخر بيع: DOC_TYPE 1 (مبيعات نقدية وآجلة ونقاط بيع)؛
  • آخر توريد من مورد: DOC_TYPE 2 و15 (سند مشتريات/توريد بكود مورد)؛
  • آخر استلام تحويل: DOC_TYPE 8 (وارد من مخزن آخر أو إذن توريد داخلي).
المرتجعات (3 و4) وتعديل التكلفة (17/18) والتجميع (12) خارج الحساب. الفرع = فرع المستند، والمخزن = مخزن السطر.
الرصيد الحالي من IAS_ITM_WCODE (AVL_QTY بالوحدة الرئيسية) في المخازن نفسها المختارة.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from django.core.cache import cache

from .oracle_group_study import _code, _f, _group_name, _qty, _subgroup_name
from .oracle_stock import (
    OracleStockError,
    _bind_gcode,
    _branch_names,
    _fetch_all,
    _norm_brn_code,
    _run_parallel,
    _schema,
    fetch_warehouse_options,
    oracle_enabled,
    oracle_session,
)

logger = logging.getLogger(__name__)

_CACHE_TTL = 300
_CACHE_VER = "v3"
CYCLE_PAGE_ROWS = 300
SALE_DOC = (1,)
VENDOR_DOCS = (2, 15)
TRANSFER_DOCS = (8,)

# ترتيب الحالات في الملخص (من الأسلم إلى الأحوج للمتابعة)
STATUS_ORDER = ("نشط", "بطيء", "راكد", "لم يُبع منذ وروده", "لم يُبع هذا العام", "نفد وهو يُباع")

# مستويات الحالة بأيام آخر بيع
ACTIVE_DAYS = 30
SLOW_DAYS = 90


def _num(value: str) -> int | str:
    return int(value) if str(value).isdigit() else value


def _item_filter(group: Any, sub: str, item: str, barcode: str) -> tuple[str, dict[str, Any]]:
    """شرط الأصناف (على الاسم المستعار i) وقيم ربطه: المجموعة والفرعية ورقم الصنف والباركود."""
    parts, params = ["1 = 1"], {}
    if group is not None:
        parts.append("i.G_CODE = :g")
        params["g"] = group
    if sub:
        parts.append("TO_CHAR(i.MNG_CODE) = :mng")
        params["mng"] = sub
    if item:
        parts.append("TO_CHAR(i.I_CODE) = :ic")
        params["ic"] = item
    if barcode:
        sch = _schema()
        parts.append(f"i.I_CODE IN (SELECT bd.I_CODE FROM {sch}.IAS_ITM_UNT_BARCODE bd WHERE TRIM(bd.BARCODE) = :bc)")
        params["bc"] = barcode
    return " AND ".join(parts), params


def _warehouses_of(brn: str, wh: str) -> list[str]:
    """مخازن الاستعلام: المخزن المختار، وإلا مخازن الفرع، وإلا فارغة (= كل المخازن)."""
    if wh:
        return [wh]
    if not brn:
        return []
    return [
        _code(w.get("code")) for w in fetch_warehouse_options(active_only=False) or []
        if _norm_brn_code(w.get("branch_code")) == brn and _code(w.get("code"))
    ]


def _last_dates(cond: str, params: dict, brn: str, wh: str) -> dict[str, dict[str, Any]]:
    """آخر تاريخ بيع/توريد/استلام تحويل لكل صنف في الشرط."""
    sch = _schema()
    p = dict(params)
    mv = ""
    if brn:
        mv += " AND m.BRN_NO = :mb"
        p["mb"] = _num(brn)
    if wh:
        mv += " AND TO_CHAR(m.W_CODE) = :mw"
        p["mw"] = wh
    ids = lambda t: ", ".join(str(x) for x in t)  # noqa: E731
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(m.I_CODE) AS I,
               MAX(CASE WHEN m.DOC_TYPE IN ({ids(SALE_DOC)}) THEN m.I_DATE END) AS LS,
               MAX(CASE WHEN m.DOC_TYPE IN ({ids(VENDOR_DOCS)}) THEN m.I_DATE END) AS LV,
               MAX(CASE WHEN m.DOC_TYPE IN ({ids(TRANSFER_DOCS)}) THEN m.I_DATE END) AS LT
        FROM {sch}.ITEM_MOVEMENT m
        JOIN {sch}.IAS_ITM_MST i ON i.I_CODE = m.I_CODE
        WHERE m.DOC_TYPE IN ({ids(SALE_DOC + VENDOR_DOCS + TRANSFER_DOCS)}) AND {cond} {mv}
        GROUP BY TO_CHAR(m.I_CODE)
        """,
        p,
    )
    return {_code(r["I"]): r for r in rows}


def _stock(cond: str, params: dict, whs: list[str]) -> dict[str, float]:
    sch = _schema()
    p = dict(params)
    wh_sql = ""
    if whs:
        for n, w in enumerate(whs):
            p[f"w{n}"] = _num(w)
        wh_sql = f"AND w.W_CODE IN ({', '.join(f':w{n}' for n in range(len(whs)))})"
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(w.I_CODE) AS I, SUM(NVL(w.AVL_QTY, 0)) AS Q
        FROM {sch}.IAS_ITM_WCODE w
        JOIN {sch}.IAS_ITM_MST i ON i.I_CODE = w.I_CODE
        WHERE {cond} {wh_sql}
        GROUP BY TO_CHAR(w.I_CODE)
        """,
        p,
    )
    return {_code(r["I"]): _f(r["Q"]) for r in rows}


def _item_info(cond: str, params: dict) -> dict[str, dict[str, str]]:
    """الاسم والمجموعة والوحدة الرئيسية والباركود لكل صنف في الشرط."""
    sch = _schema()
    names = _fetch_all(
        f"""
        SELECT TO_CHAR(i.I_CODE) AS I, i.I_NAME AS N, TO_CHAR(i.G_CODE) AS G, g.G_A_NAME AS GN
        FROM {sch}.IAS_ITM_MST i
        LEFT JOIN {sch}.GROUP_DETAILS g ON g.G_CODE = i.G_CODE
        WHERE {cond}
        """,
        params,
    )
    units = _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS I, MIN(CASE WHEN d.MAIN_UNIT = 1 THEN TRIM(d.ITM_UNT) END) AS U
        FROM {sch}.IAS_ITM_DTL d
        JOIN {sch}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE {cond}
        GROUP BY TO_CHAR(d.I_CODE)
        """,
        params,
    )
    # لكل صنف باركودات كثيرة (لكل وحدة): نعرض باركود أصغر وحدة
    barcodes = _fetch_all(
        f"""
        SELECT TO_CHAR(b.I_CODE) AS I,
               MIN(TRIM(b.BARCODE)) KEEP (DENSE_RANK FIRST ORDER BY NVL(b.P_SIZE, 0), b.BARCODE) AS B
        FROM {sch}.IAS_ITM_UNT_BARCODE b
        JOIN {sch}.IAS_ITM_MST i ON i.I_CODE = b.I_CODE
        WHERE {cond} AND b.BARCODE IS NOT NULL
        GROUP BY TO_CHAR(b.I_CODE)
        """,
        params,
    )
    out: dict[str, dict[str, str]] = {}
    for r in names:
        gc, gn = _code(r.get("G")), str(r.get("GN") or "").strip()
        out[_code(r["I"])] = {"name": str(r.get("N") or "").strip(), "group": f"{gc} - {gn}" if gn else gc, "unit": "", "barcode": ""}
    for r in units:
        info = out.get(_code(r["I"]))
        if info is not None:
            info["unit"] = str(r.get("U") or "").replace("ـ", "").strip()
    for r in barcodes:
        info = out.get(_code(r["I"]))
        if info is not None:
            info["barcode"] = str(r.get("B") or "").strip()
    return out


def _as_day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _days_since(day: date | None, today: date) -> int | None:
    return None if day is None else max(0, (today - day).days)


def _status(sale_days: int | None, stock: float, received: bool = False) -> str:
    if sale_days is None:
        # وردت للمخزن ولم يُسجَّل لها بيع بعد الورود، أو لا حركة بيع ولا توريد لها هذا العام
        return "لم يُبع منذ وروده" if received else "لم يُبع هذا العام"
    if stock <= 0 and sale_days <= ACTIVE_DAYS:
        return "نفد وهو يُباع"
    if sale_days <= ACTIVE_DAYS:
        return "نشط"
    if sale_days <= SLOW_DAYS:
        return "بطيء"
    return "راكد"


def build_item_cycle(
    *, group_code: str = "", subgroup_code: str = "", branch_code: str = "", warehouse_code: str = "",
    item_code: str = "", barcode: str = "",
) -> dict[str, Any]:
    """لكل صنف: آخر بيع، آخر توريد من مورد، آخر استلام تحويل، الرصيد الحالي، والحالة. الأقدم بيعاً أولاً."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    gcode = str(group_code or "").strip()
    sub = str(subgroup_code or "").strip() if gcode else ""
    item, bc = str(item_code or "").strip(), str(barcode or "").strip()
    if not (gcode or item or bc):
        raise OracleStockError("اختر المجموعة أو أدخل رقم الصنف أو الباركود.")
    brn = _norm_brn_code(branch_code)
    wh = str(warehouse_code or "").strip()
    today = date.today()

    key = f"item_cycle:{_CACHE_VER}:{today}:{gcode}:{sub}:{item}:{bc}:{brn}:{wh}"
    try:
        cached = cache.get(key)
    except Exception:  # noqa: BLE001
        cached = None
    if cached is not None:
        return cached

    group = _bind_gcode(gcode) if gcode else None
    cond, params = _item_filter(group, sub, item, bc)

    def job(fn, *args):
        def run():
            with oracle_session():
                return fn(*args)
        return run

    with oracle_session():
        whs = _warehouses_of(brn, wh)
        if brn and not wh and not whs:
            raise OracleStockError("لا مخازن مربوطة بهذا الفرع.")
        group_name = (_group_name(group) or gcode) if gcode else ""
        sub_name = (_subgroup_name(group, sub) or sub) if sub else ""
        branch_name = _branch_names().get(brn, brn) if brn else ""
        wh_name = next((w["name"] for w in fetch_warehouse_options(active_only=False) or [] if _code(w.get("code")) == wh), wh) if wh else ""

    dates, stock, info = _run_parallel(
        [job(_last_dates, cond, params, brn, wh), job(_stock, cond, params, whs), job(_item_info, cond, params)],
        max_workers=3, timeout_sec=300,
    )

    rows: list[dict[str, Any]] = []
    for code in set(dates) | {c for c, q in stock.items() if q}:
        meta = info.get(code)
        if meta is None:
            continue
        d, qty = dates.get(code) or {}, stock.get(code, 0.0)
        sale, vendor, transfer = _as_day(d.get("LS")), _as_day(d.get("LV")), _as_day(d.get("LT"))
        sale_days = _days_since(sale, today)
        rows.append({
            "code": code, "name": meta["name"] or code, "group": meta["group"] or "—", "unit": meta["unit"] or "—",
            "barcode": meta["barcode"],
            "stock": qty, "stock_display": _qty(qty),
            "last_sale": sale.isoformat() if sale else "", "sale_days": sale_days,
            "last_vendor": vendor.isoformat() if vendor else "", "vendor_days": _days_since(vendor, today),
            "last_transfer": transfer.isoformat() if transfer else "", "transfer_days": _days_since(transfer, today),
            "status": _status(sale_days, qty, received=bool(vendor or transfer)),
            "_sort": sale or date.min,
        })
    # الأقدم بيعاً أولاً (ومن لم يُبع قبله)، ثم الأعلى رصيداً
    rows.sort(key=lambda r: (r["_sort"], -r["stock"], r["code"]))
    for r in rows:
        del r["_sort"]

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    report = {
        "as_of": today.isoformat(),
        "group_code": gcode, "group_name": group_name, "subgroup_code": sub, "subgroup_name": sub_name,
        "branch": brn, "branch_name": branch_name, "warehouse": wh, "warehouse_name": wh_name,
        "item_code": item, "barcode": bc,
        "rows": rows,
        "counts": counts,
        "totals": {"items": len(rows), "items_display": f"{len(rows):,}"},
    }
    try:
        cache.set(key, report, _CACHE_TTL)
    except Exception:  # noqa: BLE001
        pass
    return report


def build_item_cycle_excel(report: dict[str, Any]):
    """ملف xlsx لدورة الصنف بأعمدة الشاشة."""
    from io import BytesIO

    from django.http import HttpResponse
    from openpyxl import Workbook

    from .oracle_pos_hours import _xl_sheet

    scope = [report["group_code"] + " - " + report["group_name"] if report.get("group_code") else ""]
    if report.get("subgroup_code"):
        scope.append(f"فرعية {report['subgroup_code']} - {report['subgroup_name']}")
    if report.get("item_code"):
        scope.append(f"صنف {report['item_code']}")
    if report.get("barcode"):
        scope.append(f"باركود {report['barcode']}")
    scope.append(report.get("branch_name") or "كل الفروع")
    scope.append(report.get("warehouse_name") or "كل المخازن")
    period = " · ".join(s for s in scope if s) + f" · حتى {report['as_of']}"
    wb = Workbook()
    wb.remove(wb.active)
    columns = [
        ("الكود", "name", 20), ("الصنف", "name", 36), ("الباركود", "text", 18), ("المجموعة", "text", 22), ("الوحدة", "text", 10),
        ("الرصيد الحالي", "avg", 13), ("آخر بيع", "text", 12), ("أيام منذ آخر بيع", "count", 12),
        ("آخر توريد من مورد", "text", 14), ("أيام منذ التوريد", "count", 12),
        ("آخر استلام تحويل", "text", 14), ("أيام منذ الاستلام", "count", 12), ("الحالة", "text", 16),
    ]
    rows = [
        [
            x["code"], x["name"], x["barcode"], x["group"], x["unit"], round(x["stock"], 2),
            x["last_sale"] or "—", x["sale_days"] if x["sale_days"] is not None else "—",
            x["last_vendor"] or "—", x["vendor_days"] if x["vendor_days"] is not None else "—",
            x["last_transfer"] or "—", x["transfer_days"] if x["transfer_days"] is not None else "—", x["status"],
        ]
        for x in report["rows"]
    ]
    ws = _xl_sheet(wb, "دورة الصنف", period, columns, rows, [])
    ws.page_setup.orientation = "landscape"
    buf = BytesIO()
    wb.save(buf)
    response = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="item_cycle_{report["as_of"].replace("-", "")}.xlsx"'
    return response
