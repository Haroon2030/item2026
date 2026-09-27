"""حركة كل فاتورة شراء تراكمياً: كم بيع منها وكم بقي منذ تاريخ شرائها.

لكل صنف في فرع تُرتَّب فواتير شراء الفترة بالتاريخ، ثم يستهلك صافي البيع اليومي
(نقطة البيع + فواتير البيع − مرتجعاتهما) من تاريخ أول فاتورة حتى اليوم الفواتيرَ الأقدم أولاً.
لا تُستهلك فاتورة قبل وصولها، وما بيع زيادة على المستلم يُحسب «من رصيد سابق».
خارج الحساب: رصيد ما قبل الفترة، التحويلات بين الفروع، مرتجعات الشراء.
الكمية بوحدة المخزون وتشمل البونص (FREE_QTY) لأنه يدخل الرف ويباع؛
التكلفة قيمة الفاتورة بعد خصم السطر، فالبونص يخفض تكلفة الوحدة.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from html import escape
from typing import Any

from django.http import HttpResponse

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _bill_mst_ok,
    _bind_brn,
    _bind_gcode,
    _branch_names,
    _date_params,
    _fetch_all,
    _fmt_inv_money,
    _fmt_inv_qty,
    _hung_ok,
    _norm_brn_code,
    _pos_owner,
    _rt_bill_mst_ok,
    _run_parallel,
    _sales_cache_get,
    _sales_cache_set,
    _schema,
    oracle_enabled,
)

logger = logging.getLogger(__name__)

FAST_DAYS = 14
MID_DAYS = 45
SLOW_DAYS = 90
STAGNANT_DAYS = 30
# مسح نقطة البيع من بداية الفترة حتى اليوم؛ كل الفروع لأبعد من هذا يتجاوز مهلة أوراكل.
ALL_BRANCHES_MAX_BACK_DAYS = 45
BRANCH_MAX_BACK_DAYS = 120
DEFAULT_PERIOD_DAYS = 30
PAGE_SIZE = 50
EXPORT_BARCODE_ITEM_CAP = 3000
_EPS = 1e-4
_PARALLEL_TIMEOUT_SEC = 110

_RECEIVED_QTY = (
    "NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(d.P_SIZE, 1))"
    " + NVL(d.FREE_QTY, 0) * NVL(d.P_SIZE, 1)"
)
_POS_QTY = "NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(d.P_SIZE, 1))"
_LINE_COST = "NVL(d.I_QTY, 0) * NVL(d.I_PRICE, 0) - NVL(d.DIS_AMT, 0)"

# الرمز: (الاسم، اللون)
_STATUS_META: dict[str, tuple[str, str]] = {
    "fast_out": ("نفدت سريعاً", "great"),
    "sold_out": ("نفدت", "good"),
    "fast": ("سريعة", "great"),
    "mid": ("متوسطة", "watch"),
    "slow": ("بطيئة", "risk"),
    "very_slow": ("بطيئة جداً", "danger"),
    "stagnant": ("راكدة", "stopped"),
    "waiting": ("لم يبدأ بيعها", "neutral"),
    # فرع يشتري ولا يبيع (مستودع يوزّع بالتحويل): لا تُقاس حركته هنا.
    "no_branch_sales": ("فرع بلا بيع", "neutral"),
}
_SOLD_OUT = {"fast_out", "sold_out"}
STATUS_OPTIONS = (
    ("", "كل الحالات"),
    ("open", "لم تنفد بعد"),
    *((code, label) for code, (label, _tone) in _STATUS_META.items()),
)
STATUS_CODES = {code for code, _label in STATUS_OPTIONS if code}
SORT_OPTIONS = (
    ("value", "قيمة المتبقي"),
    ("sell", "الأقل بيعاً"),
    ("age", "الأقدم شراءً"),
    ("recent", "الأحدث شراءً"),
    ("clear", "الأطول حتى النفاد"),
    ("item", "الصنف ثم التاريخ"),
)
SORT_CODES = {code for code, _label in SORT_OPTIONS}
VIEW_OPTIONS = (
    ("invoice", "الفواتير"),
    ("line", "بنود الفواتير"),
)
VIEW_CODES = {code for code, _label in VIEW_OPTIONS}
DEFAULT_VIEW = "invoice"


def _code(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].lstrip("-").isdigit():
        return text[:-2]
    return text


def max_back_days(branch_code: str) -> int:
    return BRANCH_MAX_BACK_DAYS if str(branch_code or "").strip() else ALL_BRANCHES_MAX_BACK_DAYS


def allocate_fifo(
    invoices: list[tuple[str, date, float]],
    daily: dict[date, float],
) -> dict[str, Any]:
    """توزيع صافي البيع اليومي لصنف في فرع على فواتيره، الأقدم وصولاً أولاً.

    ``invoices``: (مفتاح، تاريخ الوصول، الكمية). ``daily``: يوم → صافي الكمية.
    مرتجع اليوم يعيد الكمية لآخر فاتورة استُهلكت، والزائد عنها يخصم من «رصيد سابق».
    """
    order = sorted(
        (inv for inv in invoices if float(inv[2] or 0) > _EPS),
        key=lambda inv: (inv[1], str(inv[0])),
    )
    count = len(order)
    starts: list[float] = []
    ends: list[float] = []
    running = 0.0
    for _key, _day, qty in order:
        starts.append(running)
        running += float(qty)
        ends.append(running)
    first_sale: list[date | None] = [None] * count
    last_sale: list[date | None] = [None] * count
    sold_out: list[date | None] = [None] * count
    consumed = 0.0
    arrived = 0.0
    prior = 0.0
    next_arrival = 0
    pair_last_sale: date | None = None
    for day in sorted(daily):
        qty = float(daily[day] or 0)
        while next_arrival < count and order[next_arrival][1] <= day:
            arrived += float(order[next_arrival][2])
            next_arrival += 1
        if qty > 0:
            pair_last_sale = day
            take = min(qty, max(0.0, arrived - consumed))
            if take > 0:
                before = consumed
                consumed += take
                for i in range(count):
                    if ends[i] <= before + _EPS or starts[i] >= consumed - _EPS:
                        continue
                    if first_sale[i] is None:
                        first_sale[i] = day
                    last_sale[i] = day
                    if sold_out[i] is None and ends[i] <= consumed + _EPS:
                        sold_out[i] = day
            prior += qty - take
        elif qty < 0:
            back = min(-qty, consumed)
            consumed -= back
            prior = max(0.0, prior - (-qty - back))
            for i in range(count):
                if sold_out[i] is not None and ends[i] > consumed + _EPS:
                    sold_out[i] = None
    lines: dict[str, dict[str, Any]] = {}
    for i, (key, _arrival, qty) in enumerate(order):
        sold = min(max(consumed - starts[i], 0.0), float(qty))
        lines[str(key)] = {
            "sold": sold,
            "cum_qty": ends[i],
            "cum_sold": min(consumed, ends[i]),
            "ahead": max(0.0, starts[i] - consumed),
            "first_sale": first_sale[i],
            "last_sale": last_sale[i],
            "sold_out": sold_out[i],
        }
    return {
        "lines": lines,
        "consumed": consumed,
        "prior": prior,
        "last_sale": pair_last_sale,
    }


def classify(
    *,
    remaining: float,
    sellout_days: int | None,
    age: int,
    clear_days: float | None,
    pair_idle: int | None,
) -> str:
    """الحالة من الأيام: نفاد فعلي، أو مدة متوقعة حتى النفاد = العمر + باقي الطابور ÷ معدل البيع."""
    if remaining <= _EPS:
        if sellout_days is not None and sellout_days <= FAST_DAYS:
            return "fast_out"
        return "sold_out"
    if pair_idle is not None and pair_idle >= STAGNANT_DAYS:
        return "stagnant"
    if clear_days is None:
        return "stagnant" if age >= STAGNANT_DAYS else "waiting"
    projected = age + clear_days
    if projected <= FAST_DAYS:
        return "fast"
    if projected <= MID_DAYS:
        return "mid"
    if projected <= SLOW_DAYS:
        return "slow"
    return "very_slow"


def compute_invoice_lines(
    purchase_rows: list[dict],
    daily_by_pair: dict[str, dict[date, float]],
    *,
    today: date,
    branch_names: dict[str, str] | None = None,
    selling_branches: set[str] | None = None,
) -> list[dict]:
    """سطر لكل صنف في فاتورة، بالمباع منه والمتبقي وتراكمي الصنف في فرعه.

    ``selling_branches``: الفروع التي لها أي بيع في النافذة؛ غيرها يأخذ «فرع بلا بيع».
    """
    branch_names = branch_names or {}
    by_pair: dict[str, list[dict]] = {}
    for row in purchase_rows:
        item = _code(row.get("I_CODE"))
        bill_ser = _code(row.get("BILL_SER"))
        qty = float(row.get("QTY") or 0)
        if not item or not bill_ser or qty <= _EPS or row.get("BILL_DATE") is None:
            continue
        brn = _norm_brn_code(row.get("BRN_NO"))
        pair = f"{item}|{brn}"
        vendor_code = _code(row.get("V_CODE"))
        by_pair.setdefault(pair, []).append(
            {
                "key": f"{bill_ser}|{item}",
                "pair_key": pair,
                "bill_ser": bill_ser,
                "bill_no": _code(row.get("BILL_NO")) or bill_ser,
                "bill_date": _as_date(row.get("BILL_DATE")),
                "vendor_code": vendor_code,
                "vendor_name": str(row.get("V_NAME") or "").strip() or vendor_code or "—",
                "branch_code": brn,
                "branch_name": branch_names.get(brn) or brn,
                "item_code": item,
                "name": str(row.get("I_NAME") or "").strip() or item,
                "group_code": _code(row.get("G_CODE")),
                "qty": qty,
                "cost": float(row.get("COST") or 0),
            }
        )

    lines: list[dict] = []
    for pair, invoices in by_pair.items():
        daily = daily_by_pair.get(pair) or {}
        result = allocate_fifo(
            [(inv["key"], inv["bill_date"], inv["qty"]) for inv in invoices],
            daily,
        )
        first_arrival = min(inv["bill_date"] for inv in invoices)
        window = max(1, (today - first_arrival).days + 1)
        net_total = sum(float(v or 0) for day, v in daily.items() if day >= first_arrival)
        rate = net_total / window if net_total > 0 else 0.0
        last_sale = result["last_sale"]
        pair_idle = (today - (last_sale or first_arrival)).days
        consumed = float(result["consumed"])
        for inv in invoices:
            alloc = result["lines"].get(inv["key"])
            if alloc is None:
                continue
            qty = inv["qty"]
            sold = float(alloc["sold"])
            remaining = max(0.0, qty - sold)
            if remaining <= _EPS:
                remaining = 0.0
            unit_cost = inv["cost"] / qty if qty > 0 else 0.0
            age = max(0, (today - inv["bill_date"]).days)
            sold_out = alloc["sold_out"] if remaining == 0.0 else None
            sellout_days = (sold_out - inv["bill_date"]).days if sold_out else None
            to_clear = float(alloc["cum_qty"]) - consumed
            clear_days = (to_clear / rate) if remaining > 0 and rate > 0 else None
            status = classify(
                remaining=remaining,
                sellout_days=sellout_days,
                age=age,
                clear_days=clear_days,
                pair_idle=pair_idle,
            )
            if (
                status not in _SOLD_OUT
                and selling_branches is not None
                and inv["branch_code"] not in selling_branches
            ):
                status = "no_branch_sales"
            lines.append(
                {
                    **inv,
                    "unit_cost": unit_cost,
                    "sold": round(sold, 4),
                    "remaining": round(remaining, 4),
                    "sold_value": round(sold * unit_cost, 2),
                    "remaining_value": round(remaining * unit_cost, 2),
                    "sell_pct": (sold / qty * 100) if qty > 0 else 0.0,
                    "cum_qty": round(float(alloc["cum_qty"]), 4),
                    "cum_sold": round(float(alloc["cum_sold"]), 4),
                    "queued": sold <= _EPS and float(alloc["ahead"]) > _EPS,
                    "age": age,
                    "first_sale": alloc["first_sale"],
                    "sold_out": sold_out,
                    "sellout_days": sellout_days,
                    "clear_days": None if clear_days is None else round(clear_days, 1),
                    "pair_last_sale": last_sale,
                    "pair_idle": pair_idle,
                    "pair_prior": round(float(result["prior"]), 4),
                    "status": status,
                }
            )
    return lines


def build_invoice_rows(lines: list[dict]) -> list[dict]:
    """تجميع البنود بالفاتورة. الحالة للبند صاحب أكبر قيمة متبقية، أو نفاد إن نفدت كل البنود."""
    grouped: dict[str, list[dict]] = {}
    for line in lines:
        grouped.setdefault(line["bill_ser"], []).append(line)
    rows: list[dict] = []
    for bill_ser, items in grouped.items():
        head = items[0]
        qty = sum(line["qty"] for line in items)
        cost = sum(line["cost"] for line in items)
        sold = sum(line["sold"] for line in items)
        remaining = sum(line["remaining"] for line in items)
        open_lines = [line for line in items if line["remaining"] > 0]
        if open_lines:
            lead = max(open_lines, key=lambda line: (line["remaining_value"], line["remaining"]))
            status = lead["status"]
            known = [line["clear_days"] for line in open_lines if line["clear_days"] is not None]
            clear_days = max(known) if len(known) == len(open_lines) else None
            sellout_days = None
            sold_out = None
        else:
            clear_days = None
            sellout_days = max(line["sellout_days"] or 0 for line in items)
            sold_out = max((line["sold_out"] for line in items if line["sold_out"]), default=None)
            status = "fast_out" if sellout_days <= FAST_DAYS else "sold_out"
        last_sales = [line["pair_last_sale"] for line in items if line["pair_last_sale"]]
        rows.append(
            {
                "key": bill_ser,
                "bill_ser": bill_ser,
                "bill_no": head["bill_no"],
                "bill_date": head["bill_date"],
                "vendor_code": head["vendor_code"],
                "vendor_name": head["vendor_name"],
                "branch_code": head["branch_code"],
                "branch_name": head["branch_name"],
                "line_count": len(items),
                "open_count": len(open_lines),
                "qty": round(qty, 4),
                "cost": round(cost, 2),
                "sold": round(sold, 4),
                "remaining": round(remaining, 4),
                "sold_value": round(sum(line["sold_value"] for line in items), 2),
                "remaining_value": round(sum(line["remaining_value"] for line in items), 2),
                "sell_pct": (sold / qty * 100) if qty > 0 else 0.0,
                "age": head["age"],
                "sold_out": sold_out,
                "sellout_days": sellout_days,
                "clear_days": clear_days,
                "pair_last_sale": max(last_sales) if last_sales else None,
                "status": status,
                "name": "",
                "item_code": "",
            }
        )
    return rows


def rows_for_status(rows: list[dict], status: str) -> list[dict]:
    code = str(status or "").strip()
    if code == "open":
        return [row for row in rows if row["status"] not in _SOLD_OUT]
    if code in _STATUS_META:
        return [row for row in rows if row["status"] == code]
    return rows


def sort_rows(rows: list[dict], sort: str) -> list[dict]:
    code = str(sort or "value").strip()
    if code == "sell":
        return sorted(rows, key=lambda r: (r["sell_pct"], -r["remaining_value"]))
    if code == "age":
        return sorted(rows, key=lambda r: (r["bill_date"], r["bill_ser"], r["name"]))
    if code == "recent":
        return sorted(rows, key=lambda r: (r["bill_date"], r["bill_ser"]), reverse=True)
    if code == "clear":
        def longest(row: dict) -> float:
            if row["remaining"] <= 0:
                return -1.0
            return 10.0**9 if row["clear_days"] is None else float(row["clear_days"])
        return sorted(rows, key=lambda r: (-longest(r), -r["remaining_value"]))
    if code == "item":
        return sorted(rows, key=lambda r: (r["name"], r["item_code"], r["branch_code"], r["bill_date"], r["bill_ser"]))
    return sorted(rows, key=lambda r: (-r["remaining_value"], -r["remaining"], r["bill_date"]))


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}%"


def build_kpis(lines: list[dict]) -> dict[str, Any]:
    qty = sum(line["qty"] for line in lines)
    cost = sum(line["cost"] for line in lines)
    sold = sum(line["sold"] for line in lines)
    sold_value = sum(line["sold_value"] for line in lines)
    remaining = sum(line["remaining"] for line in lines)
    remaining_value = sum(line["remaining_value"] for line in lines)
    sold_out = [line for line in lines if line["status"] in _SOLD_OUT]
    stagnant = [line for line in lines if line["status"] == "stagnant"]
    slow = [line for line in lines if line["status"] in ("slow", "very_slow")]
    sellout = [line["sellout_days"] for line in sold_out if line["sellout_days"] is not None]
    avg_sellout = (sum(sellout) / len(sellout)) if sellout else None
    prior: dict[str, float] = {}
    for line in lines:
        prior[line["pair_key"]] = float(line["pair_prior"] or 0)
    prior_qty = sum(prior.values())
    stagnant_value = sum(line["remaining_value"] for line in stagnant)
    slow_value = sum(line["remaining_value"] for line in slow)
    return {
        "invoice_count": len({line["bill_ser"] for line in lines}),
        "line_count": len(lines),
        "item_count": len({line["item_code"] for line in lines}),
        "qty_display": _fmt_inv_qty(qty),
        "cost_display": _fmt_inv_money(cost),
        "sold_display": _fmt_inv_qty(sold),
        "sold_value_display": _fmt_inv_money(sold_value),
        "remaining_display": _fmt_inv_qty(remaining),
        "remaining_value": round(remaining_value, 2),
        "remaining_value_display": _fmt_inv_money(remaining_value),
        "sell_pct_display": _pct(sold / qty * 100 if qty > 0 else None),
        "value_sell_pct_display": _pct(sold_value / cost * 100 if cost > 0 else None),
        "sold_out_count": len(sold_out),
        "avg_sellout_display": "—" if avg_sellout is None else f"{avg_sellout:,.0f}",
        "stagnant_count": len(stagnant),
        "stagnant_value_display": _fmt_inv_money(stagnant_value),
        "slow_count": len(slow),
        "slow_value_display": _fmt_inv_money(slow_value),
        "prior_qty_display": _fmt_inv_qty(prior_qty),
        "prior_qty": round(prior_qty, 4),
    }


def build_status_counts(rows: list[dict]) -> list[dict]:
    out = []
    for code, (label, tone) in _STATUS_META.items():
        matched = [row for row in rows if row["status"] == code]
        if not matched:
            continue
        out.append(
            {
                "code": code,
                "label": label,
                "tone": tone,
                "count": len(matched),
                "value_display": _fmt_inv_money(sum(row["remaining_value"] for row in matched)),
            }
        )
    return out


def _days(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}"


def decorate(row: dict) -> dict:
    """نصوص العرض للصفوف المعروضة فقط."""
    label, tone = _STATUS_META[row["status"]]
    if row["remaining"] <= 0:
        timing = f"نفدت بعد {_days(row['sellout_days'])} يوم"
    elif row["clear_days"] is not None:
        timing = f"≈ {_days(row['clear_days'])} يوم"
    else:
        timing = "—"
    shown = dict(row)
    shown.update(
        {
            "bill_date_display": row["bill_date"].isoformat(),
            "qty_display": _fmt_inv_qty(row["qty"]),
            "cost_display": _fmt_inv_money(row["cost"]),
            "sold_display": _fmt_inv_qty(row["sold"]),
            "remaining_display": _fmt_inv_qty(row["remaining"]),
            "remaining_value_display": _fmt_inv_money(row["remaining_value"]),
            "sell_pct_display": _pct(row["sell_pct"]),
            "sell_pct_bar": max(0, min(100, round(row["sell_pct"]))),
            "age_display": _days(row["age"]),
            "timing_display": timing,
            "last_sale_display": row["pair_last_sale"].isoformat() if row.get("pair_last_sale") else "—",
            "status_label": label,
            "status_tone": tone,
        }
    )
    if "cum_qty" in row:
        shown["cum_qty_display"] = _fmt_inv_qty(row["cum_qty"])
        shown["cum_sold_display"] = _fmt_inv_qty(row["cum_sold"])
    return shown


def _purchase_lines_sql(*, branch_code: str, group_code: str) -> tuple[str, dict]:
    """بنود شراء الفترة بقيادة تاريخ الرأس ثم BILL_SER."""
    schema = _schema()
    filters = [
        "m.BILL_DATE >= :d_from",
        "m.BILL_DATE < :d_to_excl",
        _hung_ok("m"),
        "d.I_CODE IS NOT NULL",
    ]
    bind: dict[str, Any] = {}
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    if brn:
        bind["brn"] = _bind_brn(brn)
        filters.append("m.BRN_NO = :brn")
    if gcode:
        bind["gcode"] = _bind_gcode(gcode)
        filters.append("i.G_CODE = :gcode")
    sql = f"""
        SELECT /*+ LEADING(m d i) USE_NL(d i) INDEX(d INDX_SER_PI_BILL_DTL) */
               m.BILL_SER AS BILL_SER,
               m.BILL_NO AS BILL_NO,
               m.BILL_DATE AS BILL_DATE,
               m.V_CODE AS V_CODE,
               m.V_NAME AS V_NAME,
               m.BRN_NO AS BRN_NO,
               d.I_CODE AS I_CODE,
               MAX(i.I_NAME) AS I_NAME,
               MAX(i.G_CODE) AS G_CODE,
               ROUND(SUM({_RECEIVED_QTY}), 4) AS QTY,
               ROUND(SUM({_LINE_COST}), 2) AS COST
        FROM {schema}.IAS_PI_BILL_MST m
        JOIN {schema}.IAS_PI_BILL_DTL d
          ON d.BILL_SER = m.BILL_SER
        JOIN {schema}.IAS_ITM_MST i
          ON i.I_CODE = d.I_CODE
        WHERE {" AND ".join(filters)}
        GROUP BY m.BILL_SER, m.BILL_NO, m.BILL_DATE, m.V_CODE, m.V_NAME, m.BRN_NO, d.I_CODE
        HAVING SUM({_RECEIVED_QTY}) > 0
    """
    return sql, bind


def _sales_source(kind: str) -> dict[str, str]:
    """مصادر البيع: نقطة البيع ومرتجعها، وفواتير البيع (الآجل/الجملة) ومرتجعها."""
    schema = _schema()
    pos = _pos_owner()
    if kind == "pos":
        return {
            "master": f"{pos}.IAS_POS_BILL_MST",
            "detail": f"{pos}.IAS_POS_BILL_DTL",
            "date_col": "BILL_DATE",
            "join_on": "d.BILL_NO = m.BILL_NO AND d.BRN_NO = m.BRN_NO AND d.BILL_SRL = m.BILL_SRL",
            "flag": _hung_ok("m"),
            "hint": "LEADING(m d p) USE_NL(d) USE_HASH(p)",
            "qty": _POS_QTY,
        }
    if kind == "pos_return":
        return {
            "master": f"{pos}.IAS_POS_RT_BILL_MST",
            "detail": f"{pos}.IAS_POS_RT_BILL_DTL",
            "date_col": "RT_BILL_DATE",
            "join_on": "d.RT_BILL_NO = m.RT_BILL_NO AND d.BRN_NO = m.BRN_NO",
            "flag": _hung_ok("m"),
            "hint": "LEADING(m d p) USE_NL(d) USE_HASH(p)",
            "qty": _POS_QTY,
        }
    if kind == "bill":
        return {
            "master": f"{schema}.IAS_BILL_MST",
            "detail": f"{schema}.IAS_BILL_DTL",
            "date_col": "BILL_DATE",
            "join_on": "d.BILL_SER = m.BILL_SER",
            "flag": _bill_mst_ok("m"),
            "hint": "LEADING(m d p) USE_NL(d) INDEX(d INDX_SER_BILL_DTL) USE_HASH(p)",
            "qty": _RECEIVED_QTY,
        }
    if kind == "bill_return":
        return {
            "master": f"{schema}.IAS_RT_BILL_MST",
            "detail": f"{schema}.IAS_RT_BILL_DTL",
            "date_col": "RT_BILL_DATE",
            "join_on": "d.RT_BILL_SER = m.RT_BILL_SER",
            "flag": _rt_bill_mst_ok("m"),
            "hint": "LEADING(m d p) USE_NL(d) INDEX(d INDX_SER_RT_BILL_DTL) USE_HASH(p)",
            "qty": _RECEIVED_QTY,
        }
    raise ValueError(kind)


# (المصدر، الإشارة، عدد شرائح الأيام): البيع يستهلك الفواتير والمرتجع يعيد إليها.
# التفاصيل الضخمة تُقسَّم أياماً على اتصالات متوازية لأن القراءة الباردة عشوائية الإدخال.
_SALES_KINDS = (
    ("pos", 1.0, 4),
    ("pos_return", -1.0, 1),
    ("bill", 1.0, 2),
    ("bill_return", -1.0, 1),
)


def _daily_sales_sql(*, kind: str, branch_code: str, group_code: str) -> tuple[str, dict]:
    """صافي كمية يومي لأزواج (صنف، فرع) المشتراة فقط، من يوم أول فاتورة للزوج.

    القيادة بتاريخ رأس المصدر، ثم HASH مع مجموعة الأزواج الصغيرة.
    لا EXISTS/IN بكود الصنف نحو التفاصيل.
    """
    schema = _schema()
    source = _sales_source(kind)
    master = source["master"]
    detail = source["detail"]
    date_col = source["date_col"]
    join_on = source["join_on"]
    p_filters = [
        "pm.BILL_DATE >= :d_from",
        "pm.BILL_DATE < :d_to_excl",
        _hung_ok("pm"),
        "pd.I_CODE IS NOT NULL",
    ]
    s_filters = [
        f"m.{date_col} >= :s_from",
        f"m.{date_col} < :s_to_excl",
        f"m.{date_col} >= p.FIRST_DAY",
        source["flag"],
    ]
    bind: dict[str, Any] = {}
    item_join = ""
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    if brn:
        bind["brn"] = _bind_brn(brn)
        p_filters.append("pm.BRN_NO = :brn")
        s_filters.append("m.BRN_NO = :brn")
    if gcode:
        bind["gcode"] = _bind_gcode(gcode)
        item_join = f"JOIN {schema}.IAS_ITM_MST pi ON pi.I_CODE = pd.I_CODE"
        p_filters.append("pi.G_CODE = :gcode")
    sql = f"""
        WITH p AS (
            SELECT /*+ MATERIALIZE LEADING(pm pd) USE_NL(pd) INDEX(pd INDX_SER_PI_BILL_DTL) */
                   pd.I_CODE AS I_CODE,
                   pm.BRN_NO AS BRN_NO,
                   TRUNC(MIN(pm.BILL_DATE)) AS FIRST_DAY
            FROM {schema}.IAS_PI_BILL_MST pm
            JOIN {schema}.IAS_PI_BILL_DTL pd
              ON pd.BILL_SER = pm.BILL_SER
            {item_join}
            WHERE {" AND ".join(p_filters)}
            GROUP BY pd.I_CODE, pm.BRN_NO
        )
        SELECT /*+ {source["hint"]} */
               d.I_CODE AS I_CODE,
               m.BRN_NO AS BRN_NO,
               TRUNC(m.{date_col}) AS DAY,
               ROUND(SUM({source["qty"]}), 4) AS QTY
        FROM {master} m
        JOIN {detail} d
          ON {join_on}
        JOIN p
          ON p.I_CODE = d.I_CODE
         AND p.BRN_NO = m.BRN_NO
        WHERE {" AND ".join(s_filters)}
        GROUP BY d.I_CODE, m.BRN_NO, TRUNC(m.{date_col})
    """
    return sql, bind


def _merge_daily_net(parts: list[tuple[float, list[dict]]]) -> dict[str, dict[date, float]]:
    out: dict[str, dict[date, float]] = {}
    for sign, rows in parts:
        for row in rows:
            item = _code(row.get("I_CODE"))
            if not item or row.get("DAY") is None:
                continue
            pair = f"{item}|{_norm_brn_code(row.get('BRN_NO'))}"
            day = _as_date(row.get("DAY"))
            slot = out.setdefault(pair, {})
            slot[day] = slot.get(day, 0.0) + sign * float(row.get("QTY") or 0)
    return out


def _selling_branches_sql(kind: str) -> str:
    """فروع لها بيع في النافذة، من الرؤوس فقط.

    نقطة البيع من الفهرس وحده (BILL_DATE, AD_U_ID, BRN_NO) بلا علم التعليق:
    السؤال هنا «هل يبيع الفرع أصلاً» لا كم باع.
    """
    source = _sales_source(kind)
    col = source["date_col"]
    if kind == "pos":
        return f"""
            SELECT /*+ INDEX(m POSBILLMST_BILLDATEUSRBRN) */ m.BRN_NO AS BRN_NO
            FROM {source["master"]} m
            WHERE m.{col} >= :s_from AND m.{col} < :s_to_excl
            GROUP BY m.BRN_NO
        """
    return f"""
        SELECT m.BRN_NO AS BRN_NO
        FROM {source["master"]} m
        WHERE m.{col} >= :s_from AND m.{col} < :s_to_excl
          AND {source["flag"]}
        GROUP BY m.BRN_NO
    """


def _fetch_sales(
    d_from: date,
    d_to: date,
    *,
    today: date,
    branch_code: str,
    group_code: str,
) -> tuple[dict[str, dict[date, float]], set[str]]:
    """صافي البيع اليومي والفروع البائعة؛ كل مصدر على اتصال مستقل بالتوازي
    حتى يكون الزمن البارد زمن أبطأ مصدر لا مجموعها."""
    window = {"s_from": d_from, "s_to_excl": today + timedelta(days=1)}
    period = _date_params(d_from, d_to)
    jobs = []
    signs: list[float] = []
    for kind, sign, parts in _SALES_KINDS:
        sql, extra = _daily_sales_sql(kind=kind, branch_code=branch_code, group_code=group_code)
        for s_from, s_to_excl in day_slices(window["s_from"], window["s_to_excl"], parts):
            bind = {**period, **extra, "s_from": s_from, "s_to_excl": s_to_excl}
            jobs.append(lambda sql=sql, bind=bind: _fetch_all(sql, bind))
            signs.append(sign)
    for kind in ("pos", "bill"):
        jobs.append(lambda sql=_selling_branches_sql(kind): _fetch_all(sql, window))
    results = _run_parallel(jobs, max_workers=5, timeout_sec=_PARALLEL_TIMEOUT_SEC)
    daily = _merge_daily_net(list(zip(signs, results[: len(signs)])))
    selling = {
        _norm_brn_code(row.get("BRN_NO")) for rows in results[len(signs):] for row in rows
    }
    selling.discard("")
    return daily, selling


def day_slices(start: date, end_excl: date, parts: int) -> list[tuple[date, date]]:
    """شرائح أيام متتالية نصف مفتوحة [من، إلى) تغطي النافذة بلا تداخل."""
    days = max(0, (end_excl - start).days)
    parts = max(1, min(int(parts or 1), days or 1))
    out: list[tuple[date, date]] = []
    cursor = start
    for index in range(parts):
        size = days // parts + (1 if index < days % parts else 0)
        nxt = cursor + timedelta(days=size)
        if nxt > cursor or not out:
            out.append((cursor, nxt))
        cursor = nxt
    return out


def load_invoice_lines(
    d_from: date,
    d_to: date,
    *,
    today: date,
    branch_code: str = "",
    group_code: str = "",
) -> list[dict]:
    """بنود الفترة بعد التوزيع. الفلترة بالمورد والصنف بعد التوزيع حتى يبقى الطابور كاملاً."""
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    cache_key = (
        f"turn:fifo:v2:{d_from.isoformat()}:{d_to.isoformat()}:{today.isoformat()}:{brn}:{gcode}"
    )
    cached = _sales_cache_get(cache_key)
    if isinstance(cached, list):
        return cached
    sql, bind = _purchase_lines_sql(branch_code=brn, group_code=gcode)
    purchase_rows = _fetch_all(sql, {**_date_params(d_from, d_to), **bind})
    daily: dict[str, dict[date, float]] = {}
    selling: set[str] | None = None
    if purchase_rows:
        daily, selling = _fetch_sales(d_from, d_to, today=today, branch_code=brn, group_code=gcode)
    lines = compute_invoice_lines(
        purchase_rows,
        daily,
        today=today,
        branch_names=_branch_names(),
        selling_branches=selling,
    )
    _sales_cache_set(cache_key, lines, ttl=900, keep_stale=False)
    return lines


def _barcode_item_codes(query: str) -> set[str]:
    text = str(query or "").strip()
    if len(text) < 6 or not text.isdigit():
        return set()
    try:
        rows = _fetch_all(
            f"SELECT b.I_CODE AS I_CODE FROM {_schema()}.IAS_ITM_UNT_BARCODE b WHERE b.BARCODE = :bc",
            {"bc": text},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Turnover barcode lookup failed: %s", exc)
        return set()
    return {_code(row.get("I_CODE")) for row in rows if row.get("I_CODE") is not None}


def filter_lines(
    lines: list[dict],
    *,
    vendor_code: str = "",
    item_query: str = "",
    barcode_items: set[str] | None = None,
    bill_ser: str = "",
) -> list[dict]:
    vendor = _code(vendor_code)
    out = lines
    bill = _code(bill_ser)
    if bill:
        out = [line for line in out if line["bill_ser"] == bill]
    if vendor:
        out = [line for line in out if line["vendor_code"] == vendor]
    needle = str(item_query or "").strip().casefold()
    if needle:
        items = barcode_items or set()
        out = [
            line
            for line in out
            if line["item_code"] in items
            or needle == line["bill_no"].casefold()
            or needle in f"{line['name']} {line['item_code']} {line['vendor_name']}".casefold()
        ]
    return out


def _choose_barcode(candidates: list[tuple[float | None, int, str]]) -> str:
    """باركود واحد: الرئيسي أولاً، ثم وحدة المخزون (عبوة 1)، ثم الأطول."""
    usable = [(pack, main, barcode) for pack, main, barcode in candidates if barcode]
    if not usable:
        return ""
    mains = [row for row in usable if row[1] == 1]
    pool = mains or usable
    base = [row for row in pool if row[0] == 1]
    pool = base or pool
    _pack, _main, barcode = max(pool, key=lambda row: (len(row[2]), row[2]))
    return barcode


def _attach_item_barcodes(rows: list[dict]) -> None:
    """باركود الوحدة من IAS_ITM_UNT_BARCODE للصفوف المعروضة."""
    codes = sorted({str(row.get("item_code") or "").strip() for row in rows} - {""})
    chosen: dict[str, str] = {}
    if codes and oracle_enabled():
        schema = _schema()
        for start in range(0, len(codes), 200):
            chunk = codes[start:start + 200]
            binds = {f"c{i}": code for i, code in enumerate(chunk)}
            holders = ", ".join(f":c{i}" for i in range(len(chunk)))
            fetched = _fetch_all(
                f"""
                SELECT b.I_CODE, b.BARCODE, b.MAIN_BARCODE, b.P_SIZE
                FROM {schema}.IAS_ITM_UNT_BARCODE b
                WHERE b.I_CODE IN ({holders})
                """,
                binds,
            )
            grouped: dict[str, list[tuple[float | None, int, str]]] = {}
            for rec in fetched:
                item_code = _code(rec.get("I_CODE"))
                barcode = str(rec.get("BARCODE") or "").strip()
                if not item_code or not barcode:
                    continue
                try:
                    pack = float(rec["P_SIZE"]) if rec.get("P_SIZE") is not None else None
                except (TypeError, ValueError):
                    pack = None
                if pack is not None and pack <= 0:
                    pack = None
                grouped.setdefault(item_code, []).append(
                    (pack, int(rec.get("MAIN_BARCODE") or 0), barcode)
                )
            for item_code, candidates in grouped.items():
                chosen[item_code] = _choose_barcode(candidates)
    for row in rows:
        row["barcode"] = chosen.get(str(row.get("item_code") or "").strip(), "")


def validate_period(d_from: date, d_to: date, *, today: date, branch_code: str) -> date:
    """يعيد نهاية الفترة بعد قصها على اليوم، أو يرفض بداية أبعد من حد المسح."""
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")
    if d_from > today:
        raise OracleStockError("بداية الفترة بعد اليوم.")
    limit = max_back_days(branch_code)
    if (today - d_from).days > limit:
        if str(branch_code or "").strip():
            raise OracleStockError(f"بداية الشراء لا تتجاوز {limit} يوماً للخلف.")
        raise OracleStockError(
            f"لكل الفروع تبدأ فترة الشراء خلال آخر {ALL_BRANCHES_MAX_BACK_DAYS} يوماً. "
            f"اختر فرعاً لفترة حتى {BRANCH_MAX_BACK_DAYS} يوماً."
        )
    return min(d_to, today)


def build_stock_turnover_report(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
    vendor_code: str = "",
    item_query: str = "",
    bill_ser: str = "",
    status: str = "",
    sort: str = "",
    view: str = "",
    page: int = 1,
    for_export: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    today = today or date.today()
    d_from = _as_date(date_from)
    d_to = validate_period(d_from, _as_date(date_to), today=today, branch_code=branch_code)
    view_code = view if view in VIEW_CODES else DEFAULT_VIEW
    sort_code = sort if sort in SORT_CODES else "value"
    status_code = status if status in STATUS_CODES else ""

    lines = load_invoice_lines(
        d_from, d_to, today=today, branch_code=branch_code, group_code=group_code
    )
    vendors = sorted(
        {(line["vendor_code"], line["vendor_name"]) for line in lines if line["vendor_code"]},
        key=lambda pair: (pair[1], pair[0]),
    )
    vendor = _code(vendor_code)
    if vendor not in {code for code, _name in vendors}:
        vendor = ""
    query = str(item_query or "").strip()
    scoped = filter_lines(
        lines,
        vendor_code=vendor,
        item_query=query,
        barcode_items=_barcode_item_codes(query) if query else None,
        bill_ser=bill_ser,
    )
    bill = _code(bill_ser)
    bill_label = ""
    if bill and scoped:
        head = scoped[0]
        bill_label = f"{head['bill_no']} · {head['vendor_name']} · {head['branch_name']}"
    base_rows = build_invoice_rows(scoped) if view_code == "invoice" else scoped
    status_counts = build_status_counts(base_rows)
    matched = sort_rows(rows_for_status(base_rows, status_code), sort_code)
    if view_code == "invoice":
        keep = {row["bill_ser"] for row in matched}
        kpi_lines = [line for line in scoped if line["bill_ser"] in keep]
    else:
        kpi_lines = matched

    total = len(matched)
    page_count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    try:
        page_no = int(page or 1)
    except (TypeError, ValueError):
        page_no = 1
    page_no = min(max(page_no, 1), page_count)
    start = (page_no - 1) * PAGE_SIZE
    shown = matched if for_export else matched[start:start + PAGE_SIZE]
    shown = [decorate(row) for row in shown]
    if view_code == "line" and shown:
        if not for_export or len({row["item_code"] for row in shown}) <= EXPORT_BARCODE_ITEM_CAP:
            _attach_item_barcodes(shown)

    return {
        "view": view_code,
        "rows": shown,
        "total": total,
        "page": page_no,
        "page_count": page_count,
        "page_start": 0 if for_export else start,
        "page_size": PAGE_SIZE,
        "kpis": build_kpis(kpi_lines),
        "status_counts": status_counts,
        "vendors": [{"code": code, "name": name} for code, name in vendors],
        "vendor": vendor,
        "bill": bill,
        "bill_label": bill_label,
        "date_from": d_from,
        "date_to": d_to,
        "today": today,
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "sales_label": f"{d_from.isoformat()} → {today.isoformat()}",
    }


def build_turnover_excel(report: dict[str, Any]) -> HttpResponse:
    """تصدير كل صفوف العرض ضمن الفلتر. الأرقام المرجعية نص حتى لا تُقص الأصفار."""
    is_line = report.get("view") == "line"
    headers = ["رقم الفاتورة", "التاريخ", "المورد", "الفرع"]
    if is_line:
        headers += ["الصنف", "رقم الصنف", "الباركود"]
    else:
        headers += ["عدد البنود", "بنود لم تنفد"]
    headers += ["المستلم", "التكلفة"]
    if is_line:
        headers += ["تراكمي المستلم", "المباع منها", "تراكمي المباع"]
    else:
        headers += ["المباع"]
    headers += ["المتبقي", "قيمة المتبقي", "نسبة البيع", "العمر بالأيام", "النفاد", "آخر بيع", "الحالة"]
    text_cols = {0, 5, 6} if is_line else {0}
    body = [
        "<html><head><meta charset=\"utf-8\"></head><body dir=\"rtl\">",
        "<table border=\"1\"><thead><tr>",
    ]
    body.extend(f"<th>{escape(col)}</th>" for col in headers)
    body.append("</tr></thead><tbody>")
    for row in report.get("rows") or []:
        cells = [row["bill_no"], row["bill_date_display"], row["vendor_name"], row["branch_name"]]
        if is_line:
            cells += [row["name"], row["item_code"], row.get("barcode") or ""]
        else:
            cells += [row["line_count"], row["open_count"]]
        cells += [row["qty_display"], row["cost_display"]]
        if is_line:
            cells += [row["cum_qty_display"], row["sold_display"], row["cum_sold_display"]]
        else:
            cells += [row["sold_display"]]
        cells += [
            row["remaining_display"],
            row["remaining_value_display"],
            row["sell_pct_display"],
            row["age_display"],
            row["timing_display"],
            row["last_sale_display"],
            row["status_label"],
        ]
        body.append("<tr>")
        for index, value in enumerate(cells):
            kind = " class=\"txt\" style=\"mso-number-format:'\\@'\"" if index in text_cols else ""
            body.append(f"<td{kind}>{escape(str(value))}</td>")
        body.append("</tr>")
    body.append("</tbody></table></body></html>")
    response = HttpResponse(
        "\ufeff" + "".join(body),
        content_type="application/vnd.ms-excel; charset=utf-8",
    )
    response["Content-Disposition"] = "attachment; filename=purchase-invoice-movement.xls"
    return response
