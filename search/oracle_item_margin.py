"""هامش ربح الصنف — مقارنة سعر البيع بمتوسط التكلفة في مخازن محددة، لكل وحدة مسعّرة.

قراءة فقط (SELECT)، بنفس منطق شاشة الأسعار في أونكس (انظر oracle_below_cost_prices):
  سعر البيع = IAS_ITEM_PRICE (المستوى 1) لمخزن الصف ووحدته، شامل الضريبة؛
  صافي السعر = السعر ÷ (1 + الضريبة/100)، والضريبة 15% للصنف الخاضع (VAT_TYPE = 1) وصفر لغيره؛
  متوسط التكلفة = I_CWTAVG للوحدة الرئيسية في مخزن الصف × حجم عبوة وحدة السعر،
                  وإن كان صفراً فمتوسط بطاقة الصنف؛
  الربح = صافي السعر − التكلفة،
  نسبة الربح على التكلفة = الربح ÷ التكلفة (كما تعرضها أونكس)،
  هامش الربح من سعر البيع = الربح ÷ صافي السعر.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.cache import cache

from .oracle_stock import (
    OracleStockError,
    _fetch_all,
    _schema,
    fetch_warehouse_options,
    oracle_enabled,
    oracle_session,
)

logger = logging.getLogger(__name__)

_CACHE_TTL = 300
_CACHE_VER = "v3"
# المخازن المقارَن بينها (مخازن البيع)
MARGIN_WAREHOUSES = (60, 1, 1201, 1801, 1901, 2001, 20, 701, 800, 900, 30)
_DEFAULT_VAT_PCT = 15.0


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _vat_pct(vat_type: Any, vat_per: Any) -> float:
    """VAT_TYPE هو الفيصل لا VAT_PER: الخاضع (1) بنسبة صنفه أو 15%، وغيره صفر."""
    if int(_f(vat_type)) == 1:
        return _f(vat_per) if _f(vat_per) > 0 else _DEFAULT_VAT_PCT
    return 0.0


def resolve_item(query: str) -> str:
    """رقم الصنف من «رقم الصنف أو الباركود»؛ فارغ إن لم يوجد."""
    q = str(query or "").strip()
    if not q:
        return ""
    sch = _schema()
    rows = _fetch_all(f"SELECT TO_CHAR(I_CODE) AS I FROM {sch}.IAS_ITM_MST WHERE TO_CHAR(I_CODE) = :q", {"q": q})
    if rows:
        return str(rows[0]["I"]).strip()
    rows = _fetch_all(
        f"SELECT TO_CHAR(I_CODE) AS I FROM {sch}.IAS_ITM_UNT_BARCODE WHERE TRIM(BARCODE) = :q AND ROWNUM <= 1",
        {"q": q},
    )
    return str(rows[0]["I"]).strip() if rows else ""


def _price_rows(item: str) -> list[dict]:
    sch = _schema()
    ws = ", ".join(str(w) for w in MARGIN_WAREHOUSES)
    return _fetch_all(
        f"""
        SELECT TO_CHAR(p.W_CODE) AS W, TRIM(p.ITM_UNT) AS U, NVL(p.P_SIZE, 1) AS PS, p.I_PRICE AS PR,
               NVL(w.I_CWTAVG, 0) AS WC, NVL(m.I_CWTAVG, 0) AS MC, m.VAT_TYPE AS VT, m.VAT_PER AS VP,
               NVL(w.AVL_QTY, 0) AS Q, NVL(pu.MAIN_UNIT, 0) AS MU
        FROM {sch}.IAS_ITEM_PRICE p
        JOIN {sch}.IAS_ITM_MST m ON m.I_CODE = p.I_CODE
        JOIN {sch}.IAS_ITM_DTL pu ON pu.I_CODE = p.I_CODE AND pu.ITM_UNT = p.ITM_UNT AND (pu.INACTIVE IS NULL OR pu.INACTIVE = 0)
        LEFT JOIN {sch}.IAS_ITM_DTL md ON md.I_CODE = p.I_CODE AND NVL(md.MAIN_UNIT, 0) = 1
        LEFT JOIN {sch}.IAS_ITM_WCODE w ON w.I_CODE = p.I_CODE AND w.W_CODE = p.W_CODE AND w.ITM_UNT = md.ITM_UNT
        WHERE TO_CHAR(p.I_CODE) = :ic AND p.LEV_NO = 1 AND p.I_PRICE > 0 AND p.W_CODE IN ({ws})
        """,
        {"ic": item},
    )


def _item_head(item: str) -> dict[str, str]:
    sch = _schema()
    rows = _fetch_all(
        f"""
        SELECT i.I_NAME AS N, TO_CHAR(i.G_CODE) AS G, g.G_A_NAME AS GN
        FROM {sch}.IAS_ITM_MST i LEFT JOIN {sch}.GROUP_DETAILS g ON g.G_CODE = i.G_CODE
        WHERE TO_CHAR(i.I_CODE) = :ic
        """,
        {"ic": item},
    )
    r = rows[0] if rows else {}
    gc, gn = str(r.get("G") or "").strip(), str(r.get("GN") or "").strip()
    return {"name": str(r.get("N") or "").strip(), "group": f"{gc} - {gn}" if gn else gc}


def _pct(a: float, b: float) -> float | None:
    return round(a / b * 100, 2) if b else None


def _fmt(value: float | None, nd: int = 2) -> str:
    return "—" if value is None else f"{value:,.{nd}f}"


def build_item_margin(query: str, unit: str = "") -> dict[str, Any]:
    """لكل وحدة مسعّرة جدول بالمخازن: السعر، الصافي، التكلفة، الربح، النسبة والهامش، مع أعلى وأدنى مخزن."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    q = str(query or "").strip()
    if not q:
        raise OracleStockError("أدخل رقم الصنف أو الباركود.")
    unit = str(unit or "").strip()

    key = f"item_margin:{_CACHE_VER}:{q}:{unit}"
    try:
        cached = cache.get(key)
    except Exception:  # noqa: BLE001
        cached = None
    if cached is not None:
        return cached

    with oracle_session():
        item = resolve_item(q)
        if not item:
            raise OracleStockError("لا يوجد صنف بهذا الرقم أو الباركود.")
        head = _item_head(item)
        rows = _price_rows(item)
        whs = {str(w.get("code")): w for w in fetch_warehouse_options(active_only=False) or []}

    # الوحدات المسعّرة مرتبة بحجم العبوة (الأصغر أولاً)
    units: dict[str, dict[str, Any]] = {}
    for r in rows:
        u = str(r["U"] or "").replace("ـ", "").strip()
        units.setdefault(u, {"unit": u, "pack": _f(r["PS"]), "main": bool(int(_f(r["MU"]))), "rows": []})
    unit_options = [{"unit": v["unit"], "pack": v["pack"], "main": v["main"]} for v in sorted(units.values(), key=lambda v: v["pack"])]
    if unit and unit not in units:
        unit = ""

    sections: list[dict[str, Any]] = []
    for u in sorted(units.values(), key=lambda v: v["pack"]):
        if unit and u["unit"] != unit:
            continue
        by_wh = {str(r["W"]): r for r in rows if str(r["U"] or "").replace("ـ", "").strip() == u["unit"]}
        lines: list[dict[str, Any]] = []
        for code in (str(w) for w in MARGIN_WAREHOUSES):
            w = whs.get(code) or {}
            base = {"wh_code": code, "wh_name": w.get("name") or code,
                    "branch": w.get("branch_name") or ""}
            r = by_wh.get(code)
            if r is None:
                lines.append(base | {"priced": False})
                continue
            pack = _f(r["PS"]) or 1.0
            vat = _vat_pct(r["VT"], r["VP"])
            price = _f(r["PR"])
            net = price / (1 + vat / 100)
            wc, mc = _f(r["WC"]), _f(r["MC"])
            unit_cost = (wc if wc > 0 else mc) * pack
            profit = net - unit_cost
            lines.append(base | {
                "priced": True, "price": price, "vat": vat, "net": net, "cost": unit_cost,
                "cost_src": "المخزن" if wc > 0 else "عام", "profit": profit,
                "markup": _pct(profit, unit_cost), "margin": _pct(profit, net),
                "stock": _f(r["Q"]) / pack,
            })
        priced = [x for x in lines if x["priced"] and x["margin"] is not None]
        # الملخص (الأعلى والأدنى والمتوسط) بين المخازن ذات الرصيد: تكلفة المخزن الفارغ قد تكون قديمة فتضلّل
        ranked = [x for x in priced if x["stock"] > 0]
        if len(ranked) < 2:
            ranked = priced
        best = max(ranked, key=lambda x: x["margin"], default=None)
        worst = min(ranked, key=lambda x: x["margin"], default=None)
        for x in lines:
            if not x["priced"]:
                continue
            x.update({
                "price_display": _fmt(x["price"]), "vat_display": f"{x['vat']:g}", "net_display": _fmt(x["net"]),
                "cost_display": _fmt(x["cost"]), "profit_display": _fmt(x["profit"]),
                "markup_display": _fmt(x["markup"]), "margin_display": _fmt(x["margin"]),
                "stock_display": _fmt(x["stock"], 2).rstrip("0").rstrip(".") if x["stock"] else "0",
                "is_best": best is not None and x is best and len(ranked) > 1,
                "is_worst": worst is not None and x is worst and len(ranked) > 1,
            })
        avg_margin = round(sum(x["margin"] for x in ranked) / len(ranked), 2) if ranked else None
        top = max((x["margin"] for x in priced), default=0)
        for x in priced:
            # مؤشرات العرض: رصيد صفر (التكلفة قد تكون قديمة)، بيع بخسارة، وطول شريط الهامش من الأعلى
            x["zero_stock"] = x["stock"] <= 0
            x["loss"] = x["profit"] < 0
            x["bar_pct"] = round(max(x["margin"], 0) / top * 100) if top > 0 else 0
        # الأعلى هامشاً أولاً، والمخازن بلا سعر في الآخر
        lines.sort(key=lambda x: (not x["priced"], -(x.get("margin") or 0) if x["priced"] else 0))
        sections.append({
            "unit": u["unit"], "pack": u["pack"], "main": u["main"], "rows": lines,
            "unpriced": sum(1 for x in lines if not x["priced"]),
            "ranked_count": len(ranked),
            "unpriced_names": [f"{x['wh_code']} - {x['wh_name']}" for x in lines if not x["priced"]],
            "zero_count": sum(1 for x in priced if x["zero_stock"]),
            "loss_count": sum(1 for x in priced if x["loss"]),
            "avg_margin_display": _fmt(avg_margin),
            "spread_display": _fmt(round(best["margin"] - worst["margin"], 2)) if best and worst else "—",
            "best": best, "worst": worst,
        })

    vats = {x["vat"] for sec in sections for x in sec["rows"] if x["priced"]}
    report = {
        "item_code": item, "item_name": head["name"] or item, "group": head["group"], "query": q,
        "vat_display": f"{next(iter(vats)):g}" if len(vats) == 1 else "", "vat_mixed": len(vats) > 1,
        "default_unit": next((sec["unit"] for sec in sections if sec["main"]), sections[0]["unit"] if sections else ""),
        "unit": unit, "unit_options": unit_options, "sections": sections,
        "warehouses_count": len(MARGIN_WAREHOUSES),
    }
    try:
        cache.set(key, report, _CACHE_TTL)
    except Exception:  # noqa: BLE001
        pass
    return report


def build_item_margin_excel(report: dict[str, Any]):
    """ملف xlsx: ورقة واحدة بكل الوحدات والمخازن."""
    from io import BytesIO

    from django.http import HttpResponse
    from openpyxl import Workbook

    from .oracle_pos_hours import _xl_sheet

    wb = Workbook()
    wb.remove(wb.active)
    columns = [
        ("الوحدة", "text", 14), ("المخزن", "name", 30), ("الفرع", "text", 18), ("سعر البيع (شامل الضريبة)", "avg", 16),
        ("الضريبة %", "avg", 10), ("صافي السعر", "avg", 13), ("متوسط التكلفة", "avg", 14), ("مصدر التكلفة", "text", 12),
        ("الربح", "avg", 11), ("نسبة الربح على التكلفة %", "avg", 16), ("هامش الربح من سعر البيع %", "avg", 16),
        ("الرصيد", "avg", 12),
    ]
    rows: list[list] = []
    for s in report["sections"]:
        for x in s["rows"]:
            if not x["priced"]:
                rows.append([s["unit"], x["wh_name"], x.get("branch") or "", None, None, None, None, "لا سعر", None, None, None, None])
                continue
            rows.append([
                s["unit"], x["wh_name"], x.get("branch") or "", round(x["price"], 4), round(x["vat"], 2), round(x["net"], 4),
                round(x["cost"], 4), x["cost_src"], round(x["profit"], 4),
                x["markup"], x["margin"], round(x["stock"], 2),
            ])
    title = f"{report['item_code']} - {report['item_name']}" + (f" · {report['group']}" if report.get("group") else "")
    ws = _xl_sheet(wb, "هامش الربح", title, columns, rows, [])
    ws.page_setup.orientation = "landscape"
    buf = BytesIO()
    wb.save(buf)
    response = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="item_margin_{report["item_code"]}.xlsx"'
    return response
