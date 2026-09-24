"""دوران المخزون: صافي بيع الفترة ÷ الرصيد الحالي.

المقام رصيد اليوم بعد خصم كميات نقطة البيع غير المرحلة، وليس متوسط
مخزون تاريخي. الكمية بوحدة المخزون (P_QTY أو I_QTY × P_SIZE).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _bind_brn,
    _bind_gcode,
    _branch_names,
    _date_params,
    _fetch_all,
    _fmt_inv_money,
    _fmt_inv_qty,
    _hung_ok,
    _inv_expected_qty_sql,
    _inventory_stock_filters,
    _norm_brn_code,
    _pending_pos_net_sql,
    _pos_owner,
    _sales_cache_get,
    _sales_cache_set,
    _schema,
    fetch_inventory_by_group,
    oracle_enabled,
)

logger = logging.getLogger(__name__)

FAST_COVER_DAYS = 14
BALANCED_COVER_DAYS = 45
ITEM_ROW_CAP = 400
_STOCK_UNIT_QTY = "NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(d.P_SIZE, 1))"

_BAND_LABEL = {
    "dead": "راكد",
    "slow": "بطيء",
    "ok": "متوازن",
    "fast": "سريع",
    "out": "نفاد",
}
_BAND_RANK = {"dead": 0, "slow": 1, "ok": 2, "fast": 3, "out": 4}
STATUS_OPTIONS = (
    ("", "الكل"),
    ("dead", "مخزن"),
    ("out", "نفذ"),
)
_STATUS_BANDS = {code for code, _label in STATUS_OPTIONS if code}


def period_days(date_from, date_to) -> int:
    return (_as_date(date_to) - _as_date(date_from)).days + 1


def _code(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def cover_days(stock_qty: float, sold_qty: float, days: int) -> float | None:
    """أيام تغطية الرصيد الحالي بمعدل البيع اليومي. بلا معنى إن لم يكن هناك بيع ورصيد."""
    if days <= 0 or stock_qty <= 0 or sold_qty <= 0:
        return None
    return stock_qty / (sold_qty / days)


def turnover_times(sold_qty: float, stock_qty: float) -> float | None:
    if stock_qty <= 0:
        return None
    return sold_qty / stock_qty


def delay_days(cover: float | None) -> float | None:
    """أيام زيادة عدد التغطية عن الحد المتوازن. بلا تغطية محسوبة لا يوجد تأخير عددي."""
    if cover is None:
        return None
    return max(0.0, cover - BALANCED_COVER_DAYS)


def _fmt_delay(delay: float | None, band: str) -> str:
    if delay is None:
        return "راكد" if band == "dead" else "—"
    return _fmt_days(delay)


def band_for(stock_qty: float, sold_qty: float, days: int) -> str:
    if stock_qty <= 0 and sold_qty > 0:
        return "out"
    if stock_qty > 0 and sold_qty <= 0:
        return "dead"
    cover = cover_days(stock_qty, sold_qty, days)
    if cover is None:
        return "dead" if stock_qty > 0 else "out"
    if cover <= FAST_COVER_DAYS:
        return "fast"
    if cover <= BALANCED_COVER_DAYS:
        return "ok"
    return "slow"


def _fmt_times(value: float | None) -> str:
    if value is None:
        return "—"
    if value != 0 and abs(value) < 0.01:
        return "<0.01" if value > 0 else ">-0.01"
    whole = round(value)
    if abs(value - whole) < 1e-9:
        return f"{int(whole):,}"
    return f"{value:,.2f}"


def _fmt_days(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}"


def assemble_turnover_rows(
    stock_rows: list[dict],
    sales_qty: dict[str, float],
    *,
    period_days_count: int,
    names: dict[str, str] | None = None,
    extras: dict[str, dict] | None = None,
    purchase_qty: dict[str, float] | None = None,
    row_cap: int | None = None,
) -> dict[str, Any]:
    """يدمج الرصيد مع صافي البيع والشراء. صفوف بلا رصيد وبلا بيع تُسقط.

    الشراء كمية الفترة فقط. لا يُطرح من البيع لإنتاج الرصيد:
    الرصيد رصيد اليوم، والشراء لا يشمل الافتتاحي ولا التحويل.
    """
    names = names or {}
    merged: dict[str, dict] = {}
    for row in stock_rows:
        code = _code(row.get("code"))
        if not code:
            continue
        merged[code] = {
            "code": code,
            "name": str(row.get("name") or names.get(code) or code).strip() or code,
            "stock_qty": round(float(row.get("qty") or 0), 2),
            "stock_value": round(float(row.get("value") or 0), 2),
            "item_count": int(row.get("item_count") or 0),
        }
    for code, qty in sales_qty.items():
        key = _code(code)
        if not key:
            continue
        slot = merged.get(key)
        if slot is None:
            slot = {
                "code": key,
                "name": str(names.get(key) or key).strip() or key,
                "stock_qty": 0.0,
                "stock_value": 0.0,
                "item_count": 0,
            }
            merged[key] = slot
        slot["sold_qty"] = round(float(qty or 0), 2)
    for code, qty in (purchase_qty or {}).items():
        key = _code(code)
        slot = merged.get(key)
        if slot is None:
            continue
        slot["purchased_qty"] = round(float(qty or 0), 2)

    rows: list[dict] = []
    for slot in merged.values():
        stock_qty = float(slot.get("stock_qty") or 0)
        sold_qty = round(float(slot.get("sold_qty") or 0), 2)
        purchased_qty = round(float(slot.get("purchased_qty") or 0), 2)
        if stock_qty <= 0 and sold_qty <= 0:
            continue
        band = band_for(stock_qty, sold_qty, period_days_count)
        times = turnover_times(sold_qty, stock_qty)
        cover = cover_days(stock_qty, sold_qty, period_days_count)
        delay = delay_days(cover)
        value = float(slot["stock_value"])
        times_shown = None if times is None else round(times, 4)
        extra = (extras or {}).get(slot["code"]) or {}
        rows.append(
            {
                "code": slot["code"],
                "item_code": str(extra.get("item_code") or slot["code"]),
                "name": slot["name"],
                "barcode": str(extra.get("barcode") or ""),
                "branch_code": str(extra.get("branch_code") or ""),
                "branch_name": str(extra.get("branch_name") or ""),
                "stock_qty": stock_qty,
                "stock_qty_display": _fmt_inv_qty(stock_qty),
                "sold_qty": sold_qty,
                "sold_qty_display": _fmt_inv_qty(sold_qty),
                "purchased_qty": purchased_qty,
                "purchased_qty_display": _fmt_inv_qty(purchased_qty),
                "stock_value": value,
                "stock_value_display": _fmt_inv_money(value),
                "turnover": times_shown,
                "turnover_display": _fmt_times(times),
                "cover_days": None if cover is None else round(cover, 1),
                "cover_display": _fmt_days(cover),
                "delay_days": None if delay is None else round(delay, 1),
                "delay_display": _fmt_delay(delay, band),
                "band": band,
                "band_label": _BAND_LABEL[band],
                "item_count": int(slot.get("item_count") or 0),
            }
        )

    rows.sort(
        key=lambda row: (
            _BAND_RANK[row["band"]],
            -(row["cover_days"] if row["cover_days"] is not None else 10**12),
            -row["stock_value"],
            row["name"],
            row["code"],
        )
    )
    total_count = len(rows)
    truncated = False
    if row_cap is not None and total_count > row_cap:
        rows = rows[:row_cap]
        truncated = True

    stock_qty_sum = round(sum(r["stock_qty"] for r in rows if not truncated), 2)
    # المؤشرات من كل الصفوف قبل القص حتى لا يتغير الرقم مع حد العرض
    source = rows if not truncated else None
    return {
        "rows": rows,
        "total_count": total_count,
        "truncated": truncated,
        "shown_count": len(rows),
        "_all_for_kpi": source,
    }


def _kpis_from_rows(rows: list[dict], *, period_days_count: int) -> dict[str, Any]:
    stock_qty = round(sum(float(r["stock_qty"]) for r in rows), 2)
    stock_value = round(sum(float(r["stock_value"]) for r in rows), 2)
    sold_qty = round(sum(float(r["sold_qty"]) for r in rows), 2)
    purchased_qty = round(sum(float(r.get("purchased_qty") or 0) for r in rows), 2)
    dead = [r for r in rows if r["band"] == "dead"]
    dead_value = round(sum(float(r["stock_value"]) for r in dead), 2)
    times = turnover_times(sold_qty, stock_qty)
    cover = cover_days(stock_qty, sold_qty, period_days_count)
    delay = delay_days(cover)
    return {
        "stock_qty": stock_qty,
        "stock_qty_display": _fmt_inv_qty(stock_qty),
        "stock_value": stock_value,
        "stock_value_display": _fmt_inv_money(stock_value),
        "sold_qty": sold_qty,
        "sold_qty_display": _fmt_inv_qty(sold_qty),
        "purchased_qty": purchased_qty,
        "purchased_qty_display": _fmt_inv_qty(purchased_qty),
        "turnover": None if times is None else round(times, 4),
        "turnover_display": _fmt_times(times),
        "cover_days": None if cover is None else round(cover, 1),
        "cover_display": _fmt_days(cover),
        "delay_days": None if delay is None else round(delay, 1),
        "delay_display": _fmt_delay(delay, "ok" if delay is not None else "out"),
        "balanced_cover_days": BALANCED_COVER_DAYS,
        "dead_count": len(dead),
        "dead_value": dead_value,
        "dead_value_display": _fmt_inv_money(dead_value),
        "row_count": len(rows),
    }


def rows_for_status(rows: list[dict], status: str) -> list[dict]:
    """فلتر الراكد: الكل، مخزن (رصيد بلا صافي بيع)، نفذ (بيع بلا رصيد)."""
    code = str(status or "").strip()
    if code not in _STATUS_BANDS:
        return rows
    return [row for row in rows if row.get("band") == code]


def _movement_sql(
    *,
    kind: str,
    by_item: bool,
    branch_code: str,
    group_code: str,
) -> tuple[str, dict]:
    pos = _pos_owner()
    schema = _schema()
    if kind == "sale":
        master = f"{pos}.IAS_POS_BILL_MST"
        detail = f"{pos}.IAS_POS_BILL_DTL"
        date_col = "BILL_DATE"
        join_on = (
            "d.BILL_NO = m.BILL_NO AND d.BRN_NO = m.BRN_NO "
            "AND d.BILL_SRL = m.BILL_SRL"
        )
    else:
        master = f"{pos}.IAS_POS_RT_BILL_MST"
        detail = f"{pos}.IAS_POS_RT_BILL_DTL"
        date_col = "RT_BILL_DATE"
        join_on = "d.RT_BILL_NO = m.RT_BILL_NO AND d.BRN_NO = m.BRN_NO"
    filters = [
        f"m.{date_col} >= :d_from",
        f"m.{date_col} < :d_to_excl",
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
    if by_item:
        select_code = "d.I_CODE"
        select_name = "MAX(i.I_NAME) AS ITEM_NAME, m.BRN_NO AS BRN_NO,"
        group_by = "d.I_CODE, m.BRN_NO"
    else:
        select_code = "i.G_CODE"
        select_name = ""
        group_by = "i.G_CODE"
    sql = f"""
        SELECT /*+ LEADING(m) */
            {select_code} AS CODE,
            {select_name}
            ROUND(SUM({_STOCK_UNIT_QTY}), 4) AS QTY
        FROM {master} m
        JOIN {detail} d
          ON {join_on}
        JOIN {schema}.IAS_ITM_MST i
          ON i.I_CODE = d.I_CODE
        WHERE {" AND ".join(filters)}
        GROUP BY {group_by}
    """
    return sql, bind


def _fetch_net_qty(
    date_from,
    date_to,
    *,
    branch_code: str,
    group_code: str,
    by_item: bool,
) -> tuple[dict[str, float], dict[str, str]]:
    """صافي كمية نقطة البيع (بيع − مرتجع) مجمّعاً بالمجموعة أو بالصنف."""
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    level = "item-brn" if by_item else "group"
    cache_key = (
        f"turn:netqty:v2:{level}:{d_from.isoformat()}:{d_to.isoformat()}:"
        f"{branch_code}:{group_code}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        qty_map = {str(k): float(v) for k, v in (cached.get("qty") or {}).items()}
        names = {str(k): str(v) for k, v in (cached.get("names") or {}).items()}
        return qty_map, names

    date_bind = _date_params(d_from, d_to)
    qty_map: dict[str, float] = {}
    names: dict[str, str] = {}
    for kind, sign in (("sale", 1.0), ("return", -1.0)):
        sql, extra = _movement_sql(
            kind=kind,
            by_item=by_item,
            branch_code=branch_code,
            group_code=group_code,
        )
        params = {**date_bind, **extra}
        for row in _fetch_all(sql, params):
            code = _code(row.get("CODE"))
            if not code:
                continue
            key = code
            if by_item:
                key = f"{code}|{_norm_brn_code(row.get('BRN_NO'))}"
            qty_map[key] = qty_map.get(key, 0.0) + sign * float(row.get("QTY") or 0)
            name = str(row.get("ITEM_NAME") or "").strip()
            if name and key not in names:
                names[key] = name
    payload = {
        "qty": {k: round(v, 4) for k, v in qty_map.items()},
        "names": names,
    }
    _sales_cache_set(cache_key, payload, ttl=900, date_from=d_from, date_to=d_to)
    return payload["qty"], names


def _purchase_sql(
    *,
    kind: str,
    by_item: bool,
    branch_code: str,
    group_code: str,
) -> tuple[str, dict]:
    """شراء أو مرتجع شراء بقيادة تاريخ الرأس ثم BILL_SER."""
    schema = _schema()
    if kind == "purchase":
        master = f"{schema}.IAS_PI_BILL_MST"
        detail = f"{schema}.IAS_PI_BILL_DTL"
        alias = "m"
        date_col = "BILL_DATE"
        join_on = "d.BILL_SER = m.BILL_SER"
        hint = "LEADING(m d) USE_NL(d) INDEX(d INDX_SER_PI_BILL_DTL)"
    else:
        master = f"{schema}.IAS_PR_BILL_MST"
        detail = f"{schema}.IAS_PR_BILL_DTL"
        alias = "r"
        date_col = "RT_BILL_DATE"
        join_on = "d.RT_BILL_SER = r.RT_BILL_SER"
        hint = "LEADING(r d) USE_NL(d)"
    filters = [
        f"{alias}.{date_col} >= :d_from",
        f"{alias}.{date_col} < :d_to_excl",
        _hung_ok(alias),
        "d.I_CODE IS NOT NULL",
    ]
    bind: dict[str, Any] = {}
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    if brn:
        bind["brn"] = _bind_brn(brn)
        filters.append(f"{alias}.BRN_NO = :brn")
    if gcode:
        bind["gcode"] = _bind_gcode(gcode)
        filters.append("i.G_CODE = :gcode")
    if by_item:
        select_code = "d.I_CODE"
        select_brn = f", {alias}.BRN_NO AS BRN_NO"
        group_by = f"d.I_CODE, {alias}.BRN_NO"
    else:
        select_code = "i.G_CODE"
        select_brn = ""
        group_by = "i.G_CODE"
    sql = f"""
        SELECT /*+ {hint} */
            {select_code} AS CODE{select_brn},
            ROUND(SUM({_STOCK_UNIT_QTY}), 4) AS QTY
        FROM {master} {alias}
        JOIN {detail} d
          ON {join_on}
        JOIN {schema}.IAS_ITM_MST i
          ON i.I_CODE = d.I_CODE
        WHERE {" AND ".join(filters)}
        GROUP BY {group_by}
    """
    return sql, bind


def _fetch_purchase_qty(
    date_from,
    date_to,
    *,
    branch_code: str,
    group_code: str,
    by_item: bool,
) -> dict[str, float]:
    """صافي شراء الفترة (فاتورة − مرتجع) بوحدة المخزون. ليس رصيداً."""
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    level = "item-brn" if by_item else "group"
    cache_key = (
        f"turn:purchqty:v1:{level}:{d_from.isoformat()}:{d_to.isoformat()}:"
        f"{branch_code}:{group_code}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return {str(k): float(v) for k, v in cached.items()}

    date_bind = _date_params(d_from, d_to)
    qty_map: dict[str, float] = {}
    for kind, sign in (("purchase", 1.0), ("return", -1.0)):
        sql, extra = _purchase_sql(
            kind=kind,
            by_item=by_item,
            branch_code=branch_code,
            group_code=group_code,
        )
        for row in _fetch_all(sql, {**date_bind, **extra}):
            code = _code(row.get("CODE"))
            if not code:
                continue
            key = code
            if by_item:
                key = f"{code}|{_norm_brn_code(row.get('BRN_NO'))}"
            qty_map[key] = qty_map.get(key, 0.0) + sign * float(row.get("QTY") or 0)
    payload = {k: round(v, 4) for k, v in qty_map.items()}
    _sales_cache_set(cache_key, payload, ttl=900, date_from=d_from, date_to=d_to)
    return payload


def _fetch_item_stock(*, branch_code: str, group_code: str) -> list[dict]:
    schema = _schema()
    where, params = _inventory_stock_filters(
        group_code=group_code,
        branch_code=branch_code,
    )
    qty_sql = _inv_expected_qty_sql()
    pend_sql = _pending_pos_net_sql()
    cache_key = f"turn:itemstock:v2:{branch_code}:{group_code}"
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached
    rows = _fetch_all(
        f"""
        SELECT
            w.I_CODE AS ITEM_CODE,
            wh.CONN_BRN_NO AS BRN_NO,
            MAX(m.I_NAME) AS ITEM_NAME,
            ROUND(SUM({qty_sql}), 2) AS QTY_TOTAL,
            ROUND(SUM({qty_sql} * NVL(w.I_CWTAVG, w.PRIMARY_COST)), 2) AS STOCK_VALUE
        FROM {schema}.IAS_ITM_WCODE w
        JOIN {schema}.IAS_ITM_MST m ON m.I_CODE = w.I_CODE
        LEFT JOIN {schema}.WAREHOUSE_DETAILS wh
          ON TO_CHAR(wh.W_CODE) = TO_CHAR(w.W_CODE)
        LEFT JOIN {pend_sql} pend
          ON pend.I_CODE = TO_CHAR(w.I_CODE)
         AND pend.W_CODE = TO_CHAR(w.W_CODE)
        WHERE {where}
        GROUP BY w.I_CODE, wh.CONN_BRN_NO
        HAVING ROUND(SUM({qty_sql}), 2) > 0
        """,
        params,
    )
    branch_names = _branch_names()
    out = []
    for row in rows:
        item = _code(row.get("ITEM_CODE"))
        if not item:
            continue
        brn = _norm_brn_code(row.get("BRN_NO"))
        out.append(
            {
                "code": f"{item}|{brn}",
                "item_code": item,
                "name": str(row.get("ITEM_NAME") or "").strip(),
                "branch_code": brn,
                "branch_name": branch_names.get(brn) or brn,
                "qty": round(float(row.get("QTY_TOTAL") or 0), 2),
                "value": round(float(row.get("STOCK_VALUE") or 0), 2),
                "item_count": 1,
            }
        )
    _sales_cache_set(cache_key, out, ttl=900)
    return out


def _item_branch_extras(
    stock_rows: list[dict],
    sales_qty: dict[str, float],
) -> dict[str, dict]:
    """مفتاح الصنف|الفرع حتى لا يختلط رصيد فرعين لنفس الصنف."""
    branch_names = _branch_names()
    extras: dict[str, dict] = {}
    for row in stock_rows:
        key = str(row.get("code") or "")
        if not key:
            continue
        brn = str(row.get("branch_code") or "")
        item = str(row.get("item_code") or key.split("|", 1)[0])
        extras[key] = {
            "item_code": item,
            "branch_code": brn,
            "branch_name": str(row.get("branch_name") or branch_names.get(brn) or brn),
        }
    for key in sales_qty:
        if key in extras:
            continue
        item, _, brn = str(key).partition("|")
        extras[key] = {
            "item_code": item,
            "branch_code": brn,
            "branch_name": branch_names.get(brn) or brn,
        }
    return extras


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
    """باركود الوحدة من IAS_ITM_UNT_BARCODE، لا كل الرموز المسجلة للصنف."""
    codes = {str(row.get("item_code") or "").strip() for row in rows}
    codes.discard("")
    chosen: dict[str, str] = {}
    if codes and oracle_enabled():
        schema = _schema()
        code_list = list(codes)
        batch = 200
        for start in range(0, len(code_list), batch):
            chunk = code_list[start : start + batch]
            binds = {f"c{i}": code for i, code in enumerate(chunk)}
            in_sql = ", ".join(f":c{i}" for i in range(len(chunk)))
            fetched = _fetch_all(
                f"""
                SELECT b.I_CODE, b.BARCODE, b.MAIN_BARCODE, b.P_SIZE
                FROM {schema}.IAS_ITM_UNT_BARCODE b
                WHERE b.I_CODE IN ({in_sql})
                """,
                binds,
            )
            grouped: dict[str, list[tuple[float | None, int, str]]] = {}
            for rec in fetched:
                item_code = _code(rec.get("I_CODE"))
                barcode = str(rec.get("BARCODE") or "").strip()
                if not item_code or not barcode:
                    continue
                pack_raw = rec.get("P_SIZE")
                try:
                    pack = float(pack_raw) if pack_raw is not None else None
                except (TypeError, ValueError):
                    pack = None
                if pack is not None and pack <= 0:
                    pack = None
                main = int(rec.get("MAIN_BARCODE") or 0)
                grouped.setdefault(item_code, []).append((pack, main, barcode))
            for item_code, candidates in grouped.items():
                chosen[item_code] = _choose_barcode(candidates)
    for row in rows:
        item_code = str(row.get("item_code") or "").strip()
        barcode = chosen.get(item_code, "")
        row["barcode"] = barcode
        row["barcode_display"] = barcode or "—"


def build_stock_turnover_report(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
    status: str = "",
) -> dict[str, Any]:
    """تقرير دوران: مجموعات، أو أصناف المجموعة عند تحديد مجموعة."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")
    days = period_days(d_from, d_to)
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    by_item = bool(gcode)

    sales_qty, sales_names = _fetch_net_qty(
        d_from,
        d_to,
        branch_code=brn,
        group_code=gcode,
        by_item=by_item,
    )
    purchase_qty = _fetch_purchase_qty(
        d_from,
        d_to,
        branch_code=brn,
        group_code=gcode,
        by_item=by_item,
    )
    if by_item:
        stock_rows = _fetch_item_stock(branch_code=brn, group_code=gcode)
        grain = "item"
        cap: int | None = ITEM_ROW_CAP
        extras = _item_branch_extras(stock_rows, sales_qty)
    else:
        stock_rows = [
            {
                "code": _code(row.get("code")),
                "name": str(row.get("name") or "").strip(),
                "qty": float(row.get("qty_total") or 0),
                "value": float(row.get("stock_value") or 0),
                "item_count": int(row.get("item_count") or 0),
            }
            for row in fetch_inventory_by_group(branch_code=brn, group_code=gcode)
        ]
        grain = "group"
        cap = None
        extras = None

    assembled = assemble_turnover_rows(
        stock_rows,
        sales_qty,
        period_days_count=days,
        names=sales_names,
        extras=extras,
        purchase_qty=purchase_qty,
        row_cap=None,
    )
    matched = rows_for_status(assembled["rows"], status)
    shown = matched if cap is None else matched[:cap]
    report = {
        "rows": shown,
        "total_count": len(matched),
        "truncated": cap is not None and len(matched) > cap,
        "shown_count": len(shown),
        "kpis": _kpis_from_rows(matched, period_days_count=days),
    }
    if grain == "item":
        _attach_item_barcodes(report["rows"])
    report["grain"] = grain
    report["period_days"] = days
    report["period_label"] = f"{d_from.isoformat()} → {d_to.isoformat()}"
    report["date_from"] = d_from
    report["date_to"] = d_to
    return report
