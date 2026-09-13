"""أصناف لها رصيد في المخزن ولم تخرج بتحويل صادر خلال الفترة."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from django.core.cache import cache

from .oracle_stock import (
    OracleStockError,
    _PENDING_SALES_LOOKBACK_DAYS,
    _as_date,
    _bind_gcode,
    _date_params,
    _fetch_all,
    _fmt_inv_money,
    _hung_ok,
    _pos_owner,
    _schema,
    expected_stock_qty,
    fetch_item_max_pack_map,
    oracle_enabled,
)

_CACHE_TTL = 600
_CACHE_VER = "v3"
_NAME_BATCH = 800
_MAX_ROWS = 2500
_MAX_DAYS = 730
_LAST_TR_LOOKBACK_DAYS = 3650  # ~10 سنوات لآخر تحويل صادر


def _f(value: Any, digits: int = 3) -> float:
    try:
        return round(float(value or 0), digits)
    except (TypeError, ValueError):
        return 0.0


def _fmt_qty(value: Any) -> str:
    num = float(value or 0)
    if abs(num - round(num)) < 1e-9:
        return f"{int(round(num)):,}"
    return f"{num:,.3f}".rstrip("0").rstrip(".")


def _to_pack_qty(qty: float, pack_size: float) -> float:
    pack = float(pack_size or 0)
    if pack <= 1:
        return _f(qty)
    return _f(float(qty or 0) / pack)


def _norm_code(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].replace("-", "", 1).isdigit():
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


def _fetch_wh_qty(
    warehouse: str,
    *,
    group_code: str = "",
) -> tuple[dict[str, float], dict[str, float]]:
    """كمية موجبة + متوسط تكلفة وحدة المخزون (I_CWTAVG) لكل صنف."""
    schema = _schema()
    params: dict[str, Any] = {"wh": _bind_wh(warehouse)}
    joins = ""
    filters = [
        "w.W_CODE = :wh",
        "NVL(w.AVL_QTY, 0) > 0",
        "w.I_CODE IS NOT NULL",
    ]
    gcode = _bind_gcode(group_code) if group_code else None
    if gcode not in ("", None):
        params["gcode"] = gcode
        joins = (
            f" JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = w.I_CODE"
            " AND i.G_CODE = :gcode"
        )
    where = " AND ".join(filters)
    cost = "NVL(w.I_CWTAVG, w.PRIMARY_COST)"
    rows = _fetch_all(
        f"""
        SELECT /*+ INDEX(w IASITMWCODE_WCODE_FK) */
               w.I_CODE AS I_CODE,
               ROUND(SUM(NVL(w.AVL_QTY, 0)), 4) AS QTY,
               ROUND(SUM(NVL(w.AVL_QTY, 0) * {cost}), 4) AS COST_VAL
        FROM {schema}.IAS_ITM_WCODE w
        {joins}
        WHERE {where}
        GROUP BY w.I_CODE
        """,
        params,
    )
    qty_out: dict[str, float] = {}
    cost_out: dict[str, float] = {}
    for row in rows or []:
        code = _norm_code(row.get("I_CODE"))
        if not code:
            continue
        qty = _f(row.get("QTY"), 4)
        cost_val = _f(row.get("COST_VAL"), 4)
        qty_out[code] = qty_out.get(code, 0.0) + qty
        cost_out[code] = cost_out.get(code, 0.0) + cost_val
    unit_cost: dict[str, float] = {}
    for code, qty in qty_out.items():
        if qty > 0:
            unit_cost[code] = round(float(cost_out.get(code) or 0) / qty, 6)
    return qty_out, unit_cost


def _fetch_wh_pending(warehouse: str) -> dict[str, float]:
    pos = _pos_owner()
    days = int(_PENDING_SALES_LOOKBACK_DAYS)
    if days < 1:
        days = 400
    params: dict[str, Any] = {"wh": _bind_wh(warehouse), "days": days}
    qty = "NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(d.P_SIZE, 1))"
    hung = _hung_ok("m")
    wh_ok = "(d.W_CODE = :wh OR (d.W_CODE IS NULL AND m.W_CODE = :wh))"

    sales = _fetch_all(
        f"""
        SELECT /*+ LEADING(m) USE_NL(d) INDEX(m POSBILLMST_BILLDATEUSRBRN) */
               d.I_CODE AS I_CODE,
               ROUND(SUM({qty}), 4) AS QTY
        FROM {pos}.IAS_POS_BILL_MST m
        JOIN {pos}.IAS_POS_BILL_DTL d
          ON d.BILL_NO = m.BILL_NO
        WHERE m.BILL_DATE >= TRUNC(SYSDATE) - :days
          AND (m.POSTED IS NULL OR m.POSTED = 0)
          AND {hung}
          AND {wh_ok}
          AND d.I_CODE IS NOT NULL
        GROUP BY d.I_CODE
        """,
        params,
    )
    returns = _fetch_all(
        f"""
        SELECT /*+ LEADING(m) USE_NL(d) */
               d.I_CODE AS I_CODE,
               ROUND(SUM({qty}), 4) AS QTY
        FROM {pos}.IAS_POS_RT_BILL_MST m
        JOIN {pos}.IAS_POS_RT_BILL_DTL d
          ON d.RT_BILL_NO = m.RT_BILL_NO
         AND d.BRN_NO = m.BRN_NO
        WHERE m.RT_BILL_DATE >= TRUNC(SYSDATE) - :days
          AND (m.POSTED IS NULL OR m.POSTED = 0)
          AND {hung}
          AND {wh_ok}
          AND d.I_CODE IS NOT NULL
        GROUP BY d.I_CODE
        """,
        params,
    )
    out: dict[str, float] = {}
    for row in sales or []:
        code = _norm_code(row.get("I_CODE"))
        if code:
            out[code] = out.get(code, 0.0) + _f(row.get("QTY"))
    for row in returns or []:
        code = _norm_code(row.get("I_CODE"))
        if code:
            out[code] = out.get(code, 0.0) - _f(row.get("QTY"))
    return out


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


def _fmt_day(value: Any) -> str:
    day = _as_day(value)
    return day.isoformat() if day else "—"


def _fetch_outgoing_last_dates(
    warehouse: str,
    *,
    date_to: date,
) -> dict[str, date]:
    """آخر تاريخ تحويل صادر لكل صنف من المخزن (نافذة طويلة حتى نهاية الفترة)."""
    schema = _schema()
    lookback = date_to - timedelta(days=_LAST_TR_LOOKBACK_DAYS)
    dates = _date_params(lookback, date_to)
    params: dict[str, Any] = {
        "d_from": dates["d_from"],
        "d_to_excl": dates["d_to_excl"],
        "wh": _bind_wh(warehouse),
    }
    rows = _fetch_all(
        f"""
        SELECT /*+ LEADING(m d) USE_NL(d) INDEX(d INDX_SER_WHTRNS_DTL) */
               d.I_CODE AS I_CODE,
               MAX(m.TR_DATE) AS LAST_TR
        FROM {schema}.IAS_WHTRNS_MST m
        JOIN {schema}.IAS_WHTRNS_DTL d
          ON d.TR_SER = m.TR_SER
        WHERE m.TR_DATE >= :d_from
          AND m.TR_DATE < :d_to_excl
          AND m.TR_INOUT_TYPE = 1
          AND {_hung_ok("m")}
          AND m.F_W_CODE = :wh
          AND d.I_CODE IS NOT NULL
        GROUP BY d.I_CODE
        """,
        params,
    )
    out: dict[str, date] = {}
    for row in rows or []:
        code = _norm_code(row.get("I_CODE"))
        day = _as_day(row.get("LAST_TR"))
        if code and day:
            out[code] = day
    return out


def _hydrate_items(codes: list[str]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not codes:
        return out
    schema = _schema()
    for start in range(0, len(codes), _NAME_BATCH):
        chunk = codes[start : start + _NAME_BATCH]
        params: dict[str, Any] = {}
        keys: list[str] = []
        for i, code in enumerate(chunk):
            key = f"c{i}"
            keys.append(f":{key}")
            params[key] = code
        rows = _fetch_all(
            f"""
            SELECT i.I_CODE,
                   NVL(NULLIF(TRIM(i.I_NAME), ''), TO_CHAR(i.I_CODE)) AS I_NAME,
                   TO_CHAR(i.G_CODE) AS G_CODE,
                   NVL(NULLIF(TRIM(g.G_A_NAME), ''), TO_CHAR(i.G_CODE)) AS G_NAME
            FROM {schema}.IAS_ITM_MST i
            LEFT JOIN {schema}.GROUP_DETAILS g ON g.G_CODE = i.G_CODE
            WHERE i.I_CODE IN ({", ".join(keys)})
            """,
            params,
        )
        for row in rows or []:
            code = _norm_code(row.get("I_CODE"))
            if not code:
                continue
            out[code] = {
                "name": str(row.get("I_NAME") or code).strip() or code,
                "g_code": _norm_code(row.get("G_CODE")),
                "g_name": str(row.get("G_NAME") or "—").strip() or "—",
            }
    return out


def build_wh_no_transfer_report(
    *,
    warehouse: str,
    date_from,
    date_to,
    group_code: str = "",
    qty_basis: str = "avail",
    warehouse_name: str = "",
) -> dict[str, Any]:
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    wh = _norm_code(warehouse)
    if not wh:
        raise OracleStockError("حدد المخزن.")
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")
    if (d_to - d_from).days > _MAX_DAYS:
        raise OracleStockError(f"الفترة القصوى {_MAX_DAYS} يوم.")

    basis = str(qty_basis or "avail").strip().lower()
    if basis not in ("avail", "avl"):
        basis = "avail"
    gcode = str(group_code or "").strip()
    wh_label = str(warehouse_name or "").strip() or wh

    cache_key = (
        f"whnotr:{_CACHE_VER}:{wh}:{d_from}:{d_to}:{gcode}:{basis}"
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached

    qty_map, unit_cost_map = _fetch_wh_qty(wh, group_code=gcode)
    if basis == "avail":
        pend = _fetch_wh_pending(wh)
        qty_map = {
            code: expected_stock_qty(qty, pend.get(code) or 0)
            for code, qty in qty_map.items()
        }
        qty_map = {c: q for c, q in qty_map.items() if q > 0}

    last_tr_map = _fetch_outgoing_last_dates(wh, date_to=d_to)
    moved = {c for c, day in last_tr_map.items() if day >= d_from}
    idle_codes = [c for c in qty_map if c not in moved]
    idle_codes.sort(key=lambda c: (-float(qty_map.get(c) or 0), c))

    matched = len(idle_codes)
    truncated = matched > _MAX_ROWS
    show_codes = idle_codes[:_MAX_ROWS]
    meta = _hydrate_items(show_codes)
    packs = fetch_item_max_pack_map(show_codes)

    rows: list[dict[str, Any]] = []
    total_base = 0.0
    total_pack = 0.0
    total_cost = 0.0
    for code in show_codes:
        base_qty = _f(qty_map.get(code) or 0)
        pack_info = packs.get(code) or {}
        pack_size = _f(pack_info.get("pack") or 1, 4)
        if pack_size < 1:
            pack_size = 1.0
        unit = str(pack_info.get("unit") or "").strip() or "—"
        pack_qty = _to_pack_qty(base_qty, pack_size)
        info = meta.get(code) or {}
        last_day = last_tr_map.get(code)
        u_cost = float(unit_cost_map.get(code) or 0)
        line_cost = round(base_qty * u_cost, 2) if u_cost > 0 and base_qty > 0 else 0.0
        total_base += base_qty
        total_pack += pack_qty
        total_cost += line_cost
        rows.append(
            {
                "item_code": code,
                "item_name": info.get("name") or code,
                "g_code": info.get("g_code") or "",
                "g_name": info.get("g_name") or "—",
                "unit": unit,
                "pack_size": pack_size if pack_size > 1 else 1,
                "qty": pack_qty,
                "qty_display": _fmt_qty(pack_qty),
                "qty_base": base_qty,
                "qty_base_display": _fmt_qty(base_qty),
                "unit_cost": u_cost,
                "cost": line_cost,
                "cost_display": _fmt_inv_money(line_cost) if line_cost > 0 else "—",
                "last_transfer_date": last_day.isoformat() if last_day else "",
                "last_transfer_display": _fmt_day(last_day),
            }
        )

    basis_label = "المتوفّر" if basis == "avail" else "الرصيد الحالي"
    report = {
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "filters": {
            "warehouse": wh,
            "warehouse_name": wh_label,
            "group_code": gcode,
            "qty_basis": basis,
            "qty_basis_label": basis_label,
        },
        "kpis": {
            "item_count": len(rows),
            "matched_count": matched,
            "stock_count": len(qty_map),
            "moved_count": len(moved),
            "max_rows": _MAX_ROWS,
            "truncated": truncated,
            "qty_display": _fmt_qty(total_pack),
            "cost_display": _fmt_inv_money(total_cost),
        },
        "totals": {
            "qty_display": _fmt_qty(total_pack),
            "qty_base_display": _fmt_qty(total_base),
            "cost_display": _fmt_inv_money(total_cost),
        },
        "rows": rows,
    }
    try:
        cache.set(cache_key, report, _CACHE_TTL)
    except Exception:
        pass
    return report
