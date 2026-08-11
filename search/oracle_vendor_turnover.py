"""دوران مخزون الموردين — كمية واردة مقابل كمية مباعة خلال الفترة (قراءة فقط)."""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any
from xml.sax.saxutils import escape

from django.core.cache import cache
from django.http import HttpResponse

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _bind_brn,
    _fetch_all,
    _hung_ok,
    _pos_owner,
    _run_parallel,
    _schema,
    oracle_enabled,
    oracle_session,
)

_CACHE_TTL = 1800
_PACK_CACHE_TTL = 86400


def _turnover_cache_key(
    d_from,
    d_to,
    *,
    branch_code: str = "",
    vendor_code: str = "",
    limit: int = 1500,
) -> str:
    brn = str(branch_code or "").strip()
    vendor = str(vendor_code or "").strip()
    lim = max(1, min(int(limit or 1500), 5000))
    return (
        f"vendor:turnover:v8:{d_from.isoformat()}:{d_to.isoformat()}:"
        f"{brn}:{vendor}:{lim}"
    )


def peek_vendor_turnover(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    vendor_code: str = "",
    limit: int = 1500,
) -> dict[str, Any] | None:
    """قراءة تقرير دوران محفوظ دون الاتصال بأوراكل."""
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    cached = cache.get(
        _turnover_cache_key(
            d_from,
            d_to,
            branch_code=branch_code,
            vendor_code=vendor_code,
            limit=limit,
        )
    )
    return cached if isinstance(cached, dict) else None


def peek_vendor_item_detail(
    date_from,
    date_to,
    *,
    vendor_code: str,
    branch_code: str = "",
) -> dict[str, Any] | None:
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    vendor = str(vendor_code or "").strip()
    brn = str(branch_code or "").strip()
    cache_key = (
        f"vendor:turnover:items:v1:{d_from.isoformat()}:{d_to.isoformat()}:"
        f"{brn}:{vendor}"
    )
    cached = cache.get(cache_key)
    return cached if isinstance(cached, dict) else None

# عتبات استحقاق السداد حسب نسبة دوران الكمية
_SETTLE_PCT = 80.0
_PARTIAL_PCT = 40.0

# كمية الأساس (حبة): P_QTY أو I_QTY×P_SIZE
_BASE_QTY_SQL = (
    "NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(NULLIF(d.P_SIZE, 0), 1))"
)


def _qty(value: Any) -> str:
    number = float(value or 0)
    if abs(number - round(number)) < 1e-9:
        return f"{int(round(number)):,}"
    return f"{number:,.2f}"


def _money(value: Any) -> str:
    return f"{float(value or 0):,.2f}"


def format_qty(value: Any) -> str:
    return _qty(value)


def _item_max_pack_map() -> dict[str, float]:
    """أكبر عبوة لكل صنف (الوحدة الكبيرة / الكرتون) من IAS_ITM_DTL."""
    cache_key = "vendor:turnover:maxpack:v1"
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and cached:
        return cached
    schema = _schema()
    with oracle_session():
        rows = _fetch_all(
            f"""
            SELECT I_CODE AS I_CODE,
                   MAX(P_SIZE) AS MAX_P
            FROM {schema}.IAS_ITM_DTL
            WHERE NVL(P_SIZE, 0) > 0
            GROUP BY I_CODE
            """,
            {},
        )
    out: dict[str, float] = {}
    for row in rows or []:
        code = _code_str(row.get("I_CODE"))
        try:
            psz = float(row.get("MAX_P") or 0)
        except (TypeError, ValueError):
            psz = 0.0
        if code and psz > 0:
            out[code] = psz
    cache.set(cache_key, out, _PACK_CACHE_TTL)
    return out


def _to_carton(base_qty: float, item_code: str, packs: dict[str, float]) -> float:
    """حوّل كمية الأساس إلى كرتون (÷ أكبر P_SIZE).

    إذا لم تُعرَّف عبوة كبيرة (P_SIZE≤1) نُرجِع 0 — لا نعدّ الحبة كرتونًا.
    """
    base = float(base_qty or 0)
    if base == 0:
        return 0.0
    pack = float(packs.get(item_code) or 0)
    if pack <= 1:
        return 0.0
    return base / pack


def _bind_vendor(vendor_code: str):
    s = str(vendor_code or "").strip()
    if not s:
        return s
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return s
    return s


def _code_str(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0") and text.replace(".", "", 1).isdigit():
        return text[:-2]
    return text


def _decision(turnover_pct: float) -> dict[str, str]:
    if turnover_pct >= _SETTLE_PCT:
        return {
            "key": "settle",
            "label": "يستحق سداد",
            "hint": f"دوران ≥ {_SETTLE_PCT:.0f}%",
        }
    if turnover_pct >= _PARTIAL_PCT:
        return {
            "key": "partial",
            "label": "دفعة بسيطة",
            "hint": f"دوران {_PARTIAL_PCT:.0f}–{_SETTLE_PCT:.0f}%",
        }
    return {
        "key": "hold",
        "label": "لا يستحق سداد",
        "hint": f"دوران < {_PARTIAL_PCT:.0f}%",
    }


def _pi_filters(brn: str, vendor: str) -> tuple[list[str], dict[str, Any]]:
    filters = [
        "m.BILL_DATE >= :d_from",
        "m.BILL_DATE < :d_to_excl",
        "NVL(m.HUNG, 0) = 0",
        "m.V_CODE IS NOT NULL",
        "d.I_CODE IS NOT NULL",
    ]
    params: dict[str, Any] = {}
    if brn:
        params["brn"] = _bind_brn(brn)
        filters.append("m.BRN_NO = :brn")
    if vendor:
        params["vendor"] = _bind_vendor(vendor)
        filters.append("m.V_CODE = :vendor")
    return filters, params


def _pr_filters(brn: str, vendor: str) -> tuple[list[str], dict[str, Any]]:
    filters = [
        "r.RT_BILL_DATE >= :d_from",
        "r.RT_BILL_DATE < :d_to_excl",
        "NVL(r.HUNG, 0) = 0",
        "r.V_CODE IS NOT NULL",
        "d.I_CODE IS NOT NULL",
    ]
    params: dict[str, Any] = {}
    if brn:
        params["brn"] = _bind_brn(brn)
        filters.append("r.BRN_NO = :brn")
    if vendor:
        params["vendor"] = _bind_vendor(vendor)
        filters.append("r.V_CODE = :vendor")
    return filters, params


def _pos_filters(brn: str) -> tuple[list[str], dict[str, Any]]:
    filters = [
        "m.BILL_DATE >= :d_from",
        "m.BILL_DATE < :d_to_excl",
        _hung_ok("m"),
        "d.I_CODE IS NOT NULL",
    ]
    params: dict[str, Any] = {}
    if brn:
        params["brn"] = _bind_brn(brn)
        filters.append("m.BRN_NO = :brn")
    return filters, params


def _rt_filters(brn: str) -> tuple[list[str], dict[str, Any]]:
    filters = [
        "m.RT_BILL_DATE >= :d_from",
        "m.RT_BILL_DATE < :d_to_excl",
        _hung_ok("m"),
        "d.I_CODE IS NOT NULL",
    ]
    params: dict[str, Any] = {}
    if brn:
        params["brn"] = _bind_brn(brn)
        filters.append("m.BRN_NO = :brn")
    return filters, params


def _fetch_pi_rows(date_params: dict, brn: str, vendor: str) -> list[dict]:
    schema = _schema()
    filters, extra = _pi_filters(brn, vendor)
    where = " AND ".join(filters)
    with oracle_session():
        return _fetch_all(
            f"""
            SELECT m.V_CODE AS V_CODE,
                   MAX(NVL(m.V_NAME, TO_CHAR(m.V_CODE))) AS V_NAME,
                   d.I_CODE AS I_CODE,
                   SUM({_BASE_QTY_SQL}) AS QTY
            FROM {schema}.IAS_PI_BILL_MST m
            JOIN {schema}.IAS_PI_BILL_DTL d
              ON d.BILL_NO = m.BILL_NO
             AND d.BILL_SER = m.BILL_SER
             AND d.BILL_DOC_TYPE = m.BILL_DOC_TYPE
            WHERE {where}
            GROUP BY m.V_CODE, d.I_CODE
            """,
            {**date_params, **extra},
        )


def _fetch_pr_rows(date_params: dict, brn: str, vendor: str) -> list[dict]:
    schema = _schema()
    filters, extra = _pr_filters(brn, vendor)
    where = " AND ".join(filters)
    with oracle_session():
        return _fetch_all(
            f"""
            SELECT r.V_CODE AS V_CODE,
                   MAX(NVL(r.V_NAME, TO_CHAR(r.V_CODE))) AS V_NAME,
                   d.I_CODE AS I_CODE,
                   SUM({_BASE_QTY_SQL}) AS QTY
            FROM {schema}.IAS_PR_BILL_MST r
            JOIN {schema}.IAS_PR_BILL_DTL d
              ON d.RT_BILL_NO = r.RT_BILL_NO
             AND d.RT_BILL_SER = r.RT_BILL_SER
             AND d.RT_BILL_DOC_TYPE = r.RT_BILL_DOC_TYPE
            WHERE {where}
            GROUP BY r.V_CODE, d.I_CODE
            """,
            {**date_params, **extra},
        )


def _fetch_pos_sold(date_params: dict, brn: str) -> list[dict]:
    """مسح واحد لمبيعات الفترة حسب الصنف — أسرع من IN على آلاف الأصناف."""
    pos = _pos_owner()
    filters, extra = _pos_filters(brn)
    where = " AND ".join(filters)
    with oracle_session():
        return _fetch_all(
            f"""
            SELECT d.I_CODE AS I_CODE,
                   SUM({_BASE_QTY_SQL}) AS QTY
            FROM {pos}.IAS_POS_BILL_DTL d
            JOIN {pos}.IAS_POS_BILL_MST m
              ON m.BILL_NO = d.BILL_NO
             AND m.BRN_NO = d.BRN_NO
             AND NVL(m.BILL_SRL, 0) = NVL(d.BILL_SRL, 0)
            WHERE {where}
            GROUP BY d.I_CODE
            """,
            {**date_params, **extra},
        )


def _fetch_pos_returns(date_params: dict, brn: str) -> list[dict]:
    pos = _pos_owner()
    filters, extra = _rt_filters(brn)
    where = " AND ".join(filters)
    with oracle_session():
        return _fetch_all(
            f"""
            SELECT d.I_CODE AS I_CODE,
                   SUM({_BASE_QTY_SQL}) AS QTY
            FROM {pos}.IAS_POS_RT_BILL_DTL d
            JOIN {pos}.IAS_POS_RT_BILL_MST m
              ON m.RT_BILL_NO = d.RT_BILL_NO
             AND m.BRN_NO = d.BRN_NO
            WHERE {where}
            GROUP BY d.I_CODE
            """,
            {**date_params, **extra},
        )


def _item_pack_detail_map(item_codes: list[str]) -> dict[str, dict[str, Any]]:
    """أكبر عبوة + اسم الوحدة لكل صنف من القائمة."""
    codes = [_code_str(c) for c in item_codes if _code_str(c)]
    if not codes:
        return {}
    schema = _schema()
    out: dict[str, dict[str, Any]] = {}
    # دفعات لتفادي حدود IN
    chunk = 900
    with oracle_session():
        for i in range(0, len(codes), chunk):
            part = codes[i : i + chunk]
            binds = {f"c{n}": part[n] for n in range(len(part))}
            in_list = ", ".join(f":c{n}" for n in range(len(part)))
            rows = _fetch_all(
                f"""
                SELECT TO_CHAR(x.I_CODE) AS I_CODE,
                       x.P_SIZE AS P_SIZE,
                       NVL(TRIM(x.ITM_UNT), '') AS ITM_UNT
                FROM (
                    SELECT d.I_CODE,
                           d.P_SIZE,
                           d.ITM_UNT,
                           ROW_NUMBER() OVER (
                             PARTITION BY d.I_CODE
                             ORDER BY NVL(d.P_SIZE, 0) DESC, d.ITM_UNT
                           ) AS RN
                    FROM {schema}.IAS_ITM_DTL d
                    WHERE TO_CHAR(d.I_CODE) IN ({in_list})
                      AND NVL(d.P_SIZE, 0) > 0
                ) x
                WHERE x.RN = 1
                """,
                binds,
            )
            for row in rows or []:
                code = _code_str(row.get("I_CODE"))
                if not code:
                    continue
                try:
                    psz = float(row.get("P_SIZE") or 0)
                except (TypeError, ValueError):
                    psz = 0.0
                out[code] = {
                    "pack": psz,
                    "unit": str(row.get("ITM_UNT") or "").strip(),
                }
    return out


def _item_name_map(item_codes: list[str]) -> dict[str, str]:
    codes = [_code_str(c) for c in item_codes if _code_str(c)]
    if not codes:
        return {}
    schema = _schema()
    out: dict[str, str] = {}
    chunk = 900
    with oracle_session():
        for i in range(0, len(codes), chunk):
            part = codes[i : i + chunk]
            binds = {f"c{n}": part[n] for n in range(len(part))}
            in_list = ", ".join(f":c{n}" for n in range(len(part)))
            rows = _fetch_all(
                f"""
                SELECT TO_CHAR(I_CODE) AS I_CODE,
                       NVL(NULLIF(TRIM(I_NAME), ''), TO_CHAR(I_CODE)) AS I_NAME
                FROM {schema}.IAS_ITM_MST
                WHERE TO_CHAR(I_CODE) IN ({in_list})
                """,
                binds,
            )
            for row in rows or []:
                code = _code_str(row.get("I_CODE"))
                if code:
                    out[code] = str(row.get("I_NAME") or code).strip() or code
    return out


def _net_recv_rows(
    pi_rows: list[dict] | None,
    pr_rows: list[dict] | None,
) -> list[dict[str, Any]]:
    recv_map: dict[tuple[str, str], dict[str, Any]] = {}
    for row in pi_rows or []:
        v_code = _code_str(row.get("V_CODE"))
        i_code = _code_str(row.get("I_CODE"))
        qty = float(row.get("QTY") or 0)
        if not v_code or not i_code or qty == 0:
            continue
        key = (v_code, i_code)
        bucket = recv_map.get(key)
        if bucket is None:
            bucket = {
                "v_code": v_code,
                "v_name": str(row.get("V_NAME") or v_code).strip() or v_code,
                "i_code": i_code,
                "qty": 0.0,
            }
            recv_map[key] = bucket
        bucket["qty"] += qty
        if row.get("V_NAME"):
            bucket["v_name"] = str(row.get("V_NAME")).strip() or bucket["v_name"]

    for row in pr_rows or []:
        v_code = _code_str(row.get("V_CODE"))
        i_code = _code_str(row.get("I_CODE"))
        qty = float(row.get("QTY") or 0)
        if not v_code or not i_code or qty == 0:
            continue
        key = (v_code, i_code)
        bucket = recv_map.get(key)
        if bucket is None:
            bucket = {
                "v_code": v_code,
                "v_name": str(row.get("V_NAME") or v_code).strip() or v_code,
                "i_code": i_code,
                "qty": 0.0,
            }
            recv_map[key] = bucket
        bucket["qty"] -= qty

    return [b for b in recv_map.values() if round(float(b["qty"]), 2) > 0]


def _sold_by_item_cartons(
    sold_rows: list[dict] | None,
    ret_rows: list[dict] | None,
    packs: dict[str, float],
) -> dict[str, float]:
    sold_by_item: dict[str, float] = {}
    for row in sold_rows or []:
        code = _code_str(row.get("I_CODE"))
        if code:
            sold_by_item[code] = sold_by_item.get(code, 0.0) + float(row.get("QTY") or 0)
    for row in ret_rows or []:
        code = _code_str(row.get("I_CODE"))
        if code:
            sold_by_item[code] = sold_by_item.get(code, 0.0) - float(row.get("QTY") or 0)
    for code in list(sold_by_item.keys()):
        sold_by_item[code] = _to_carton(sold_by_item[code], code, packs)
    return sold_by_item


def _fetch_vendor_due_map(date_params: dict, brn: str, vendor: str) -> dict[str, float]:
    """رصيد المورد المستحق (دائن − مدين) من قيود الموردين — الرصيد الحالي.

    يُقيَّد بموردي فواتير الشراء في نفس فترة/فرع التقرير لتسريع الاستعلام.
    """
    schema = _schema()
    pi_filters, pi_extra = _pi_filters(brn, vendor)
    # للمورد يكفي وجوده في توريدات الفترة (بدون تفاصيل أصناف)
    pi_filters = [f for f in pi_filters if f != "d.I_CODE IS NOT NULL"]
    pi_where = " AND ".join(pi_filters)
    due_filters = [
        "p.AC_DTL_TYP = 4",
        "p.AC_CODE_DTL IS NOT NULL",
        f"""EXISTS (
              SELECT 1
              FROM {schema}.IAS_PI_BILL_MST m
              WHERE m.V_CODE = p.AC_CODE_DTL
                AND {pi_where}
            )""",
    ]
    params: dict[str, Any] = {**date_params, **pi_extra}
    if brn:
        params["due_brn"] = _bind_brn(brn)
        due_filters.append("p.BRN_NO = :due_brn")
    if vendor:
        params["due_vendor"] = _bind_vendor(vendor)
        due_filters.append("p.AC_CODE_DTL = :due_vendor")
    where = " AND ".join(due_filters)
    with oracle_session():
        rows = _fetch_all(
            f"""
            SELECT TO_CHAR(p.AC_CODE_DTL) AS V_CODE,
                   ROUND(SUM(NVL(p.CR_AMT, 0) - NVL(p.DR_AMT, 0)), 2) AS DUE_AMT
            FROM {schema}.IAS_V_POST_DTL_VNDR_YR p
            WHERE {where}
            GROUP BY TO_CHAR(p.AC_CODE_DTL)
            """,
            params,
        )
    out: dict[str, float] = {}
    for row in rows or []:
        code = _code_str(row.get("V_CODE"))
        if code:
            out[code] = round(float(row.get("DUE_AMT") or 0), 2)
    return out


def build_vendor_turnover(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    vendor_code: str = "",
    limit: int = 1500,
) -> dict[str, Any]:
    """إجمالي دوران الكمية لكل مورد خلال الفترة — بالكرتون (الوحدة الكبيرة).

    وارد = صافي كمية فواتير الشراء (PI − PR) محوّلة لكرتون.
    مباع منسوب = مبيعات أصناف المورد في نفس الفترة بنسبة حصته من توريد الصنف.
    نسبة الدوران = المباع المنسوب ÷ الوارد.
    """
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")

    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    brn = str(branch_code or "").strip()
    vendor = str(vendor_code or "").strip()
    lim = max(1, min(int(limit or 1500), 5000))
    cache_key = _turnover_cache_key(
        d_from, d_to, branch_code=brn, vendor_code=vendor, limit=lim
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    date_params = {
        "d_from": d_from,
        "d_to_excl": d_to + timedelta(days=1),
    }

    # عمال أقل لتقليل ضغط الاتصالات على الشبكات البطيئة / VPN
    pi_rows, pr_rows, sold_rows, ret_rows, due_map, packs = _run_parallel(
        [
            lambda: _fetch_pi_rows(date_params, brn, vendor),
            lambda: _fetch_pr_rows(date_params, brn, vendor),
            lambda: _fetch_pos_sold(date_params, brn),
            lambda: _fetch_pos_returns(date_params, brn),
            lambda: _fetch_vendor_due_map(date_params, brn, vendor),
            _item_max_pack_map,
        ],
        max_workers=3,
        timeout_sec=240.0,
    )
    if not isinstance(packs, dict):
        packs = {}
    if not isinstance(due_map, dict):
        due_map = {}

    recv_rows = _net_recv_rows(pi_rows, pr_rows)
    if not recv_rows:
        empty = _empty_result(d_from, d_to)
        cache.set(cache_key, empty, _CACHE_TTL)
        return empty

    # تحويل الأساس → كرتون (÷ أكبر P_SIZE)
    for row in recv_rows:
        row["qty"] = _to_carton(float(row["qty"]), row["i_code"], packs)

    sold_by_item = _sold_by_item_cartons(sold_rows, ret_rows, packs)

    item_recv_total: dict[str, float] = {}
    for row in recv_rows:
        code = row["i_code"]
        qty = float(row["qty"])
        item_recv_total[code] = item_recv_total.get(code, 0.0) + qty

    vendors: dict[str, dict[str, Any]] = {}
    for row in recv_rows:
        v_code = row["v_code"]
        i_code = row["i_code"]
        recv_qty = float(row["qty"])
        bucket = vendors.get(v_code)
        if bucket is None:
            bucket = {
                "code": v_code,
                "name": row["v_name"],
                "recv_qty": 0.0,
                "sold_qty": 0.0,
                "item_count": 0,
            }
            vendors[v_code] = bucket
        bucket["recv_qty"] += recv_qty
        bucket["item_count"] += 1
        total_item = item_recv_total.get(i_code) or 0.0
        sold_item = max(0.0, sold_by_item.get(i_code) or 0.0)
        if total_item > 0 and sold_item > 0:
            attributed = sold_item * (recv_qty / total_item)
            bucket["sold_qty"] += min(recv_qty, attributed)

    rows_out: list[dict[str, Any]] = []
    for bucket in vendors.values():
        recv = round(float(bucket["recv_qty"]), 2)
        sold_attr = round(float(bucket["sold_qty"]), 2)
        turnover = round((sold_attr / recv) * 100.0, 1) if recv > 0 else 0.0
        decision = _decision(turnover)
        due_amt = round(float(due_map.get(bucket["code"]) or 0), 2)
        rows_out.append(
            {
                "vendor_code": bucket["code"],
                "vendor_name": bucket["name"],
                "item_count": int(bucket["item_count"]),
                "item_count_display": f"{int(bucket['item_count']):,}",
                "recv_qty": recv,
                "recv_qty_display": _qty(recv),
                "sold_qty": sold_attr,
                "sold_qty_display": _qty(sold_attr),
                "remain_qty": round(max(recv - sold_attr, 0.0), 2),
                "remain_qty_display": _qty(max(recv - sold_attr, 0.0)),
                "due_amt": due_amt,
                "due_amt_display": _money(due_amt),
                "turnover_pct": turnover,
                "turnover_display": f"{turnover:.1f}%",
                "decision_key": decision["key"],
                "decision_label": decision["label"],
                "decision_hint": decision["hint"],
            }
        )

    _decision_rank = {"settle": 0, "partial": 1, "hold": 2}
    rows_out.sort(
        key=lambda r: (
            _decision_rank.get(str(r.get("decision_key") or ""), 9),
            -float(r.get("due_amt") or 0),
            -float(r.get("turnover_pct") or 0),
            -float(r.get("recv_qty") or 0),
            str(r.get("vendor_name") or ""),
        )
    )

    settle_n = sum(1 for r in rows_out if r["decision_key"] == "settle")
    partial_n = sum(1 for r in rows_out if r["decision_key"] == "partial")
    hold_n = sum(1 for r in rows_out if r["decision_key"] == "hold")
    tot_recv = round(sum(float(r["recv_qty"]) for r in rows_out), 2)
    tot_sold = round(sum(float(r["sold_qty"]) for r in rows_out), 2)
    tot_due = round(sum(float(r["due_amt"]) for r in rows_out), 2)
    tot_turn = round((tot_sold / tot_recv) * 100.0, 1) if tot_recv > 0 else 0.0
    vendor_total = len(rows_out)
    if len(rows_out) > lim:
        rows_out = rows_out[:lim]

    result = {
        "unit_label": "كرتون",
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "thresholds": {
            "settle": _SETTLE_PCT,
            "partial": _PARTIAL_PCT,
        },
        "rows": rows_out,
        "kpis": {
            "vendor_count": vendor_total,
            "vendor_count_display": f"{vendor_total:,}",
            "shown_count": len(rows_out),
            "recv_qty": tot_recv,
            "recv_qty_display": _qty(tot_recv),
            "sold_qty": tot_sold,
            "sold_qty_display": _qty(tot_sold),
            "due_amt": tot_due,
            "due_amt_display": _money(tot_due),
            "turnover_pct": tot_turn,
            "turnover_display": f"{tot_turn:.1f}%",
            "settle_count": settle_n,
            "partial_count": partial_n,
            "hold_count": hold_n,
        },
    }
    cache.set(cache_key, result, _CACHE_TTL)
    return result


def _empty_result(d_from, d_to) -> dict[str, Any]:
    return {
        "unit_label": "كرتون",
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "thresholds": {"settle": _SETTLE_PCT, "partial": _PARTIAL_PCT},
        "rows": [],
        "kpis": {
            "vendor_count": 0,
            "vendor_count_display": "0",
            "shown_count": 0,
            "recv_qty": 0.0,
            "recv_qty_display": "0",
            "sold_qty": 0.0,
            "sold_qty_display": "0",
            "due_amt": 0.0,
            "due_amt_display": "0.00",
            "turnover_pct": 0.0,
            "turnover_display": "0.0%",
            "settle_count": 0,
            "partial_count": 0,
            "hold_count": 0,
        },
    }


def build_vendor_item_detail(
    date_from,
    date_to,
    *,
    vendor_code: str,
    branch_code: str = "",
) -> dict[str, Any]:
    """تفاصيل أصناف مورد واحد: الاسم، الكود، أكبر عبوة، الحصة من وارد المورد."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    vendor = str(vendor_code or "").strip()
    if not vendor:
        raise OracleStockError("اختر مورداً لعرض الأصناف.")

    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    brn = str(branch_code or "").strip()
    cache_key = (
        f"vendor:turnover:items:v1:{d_from.isoformat()}:{d_to.isoformat()}:"
        f"{brn}:{vendor}"
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    date_params = {
        "d_from": d_from,
        "d_to_excl": d_to + timedelta(days=1),
    }
    # جلب كل توريدات الفترة (لإسناد المبيعات) + مبيعات + عبوات
    pi_rows, pr_rows, sold_rows, ret_rows, packs_all = _run_parallel(
        [
            lambda: _fetch_pi_rows(date_params, brn, ""),
            lambda: _fetch_pr_rows(date_params, brn, ""),
            lambda: _fetch_pos_sold(date_params, brn),
            lambda: _fetch_pos_returns(date_params, brn),
            _item_max_pack_map,
        ],
        max_workers=3,
        timeout_sec=240.0,
    )
    if not isinstance(packs_all, dict):
        packs_all = {}
    packs_all = dict(packs_all)

    all_recv = _net_recv_rows(pi_rows, pr_rows)
    vend_key = _code_str(vendor)
    recv_rows = [r for r in all_recv if _code_str(r["v_code"]) == vend_key]
    vendor_name = vendor
    for row in recv_rows:
        vendor_name = row["v_name"]
        break

    if not recv_rows:
        result = {
            "vendor_code": vend_key or vendor,
            "vendor_name": vendor_name,
            "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
            "unit_label": "كرتون",
            "rows": [],
            "kpis": {
                "item_count": 0,
                "item_count_display": "0",
                "recv_qty": 0.0,
                "recv_qty_display": "0",
                "sold_qty": 0.0,
                "sold_qty_display": "0",
                "turnover_pct": 0.0,
                "turnover_display": "0.0%",
            },
        }
        cache.set(cache_key, result, _CACHE_TTL)
        return result

    item_codes = [r["i_code"] for r in recv_rows]
    pack_detail, names = _run_parallel(
        [
            lambda: _item_pack_detail_map(item_codes),
            lambda: _item_name_map(item_codes),
        ],
        max_workers=2,
        timeout_sec=120.0,
    )
    if not isinstance(pack_detail, dict):
        pack_detail = {}
    if not isinstance(names, dict):
        names = {}
    for code, info in pack_detail.items():
        psz = float((info or {}).get("pack") or 0)
        if psz > 0:
            packs_all[code] = psz

    item_recv_total: dict[str, float] = {}
    for row in all_recv:
        code = row["i_code"]
        qty = _to_carton(float(row["qty"]), code, packs_all)
        item_recv_total[code] = item_recv_total.get(code, 0.0) + qty

    for row in recv_rows:
        row["qty"] = _to_carton(float(row["qty"]), row["i_code"], packs_all)

    sold_by_item = _sold_by_item_cartons(sold_rows, ret_rows, packs_all)
    vendor_recv_total = round(sum(float(r["qty"]) for r in recv_rows), 2)
    rows_out: list[dict[str, Any]] = []
    for row in recv_rows:
        i_code = row["i_code"]
        recv = round(float(row["qty"]), 2)
        total_item = item_recv_total.get(i_code) or 0.0
        sold_item = max(0.0, sold_by_item.get(i_code) or 0.0)
        sold_attr = 0.0
        if total_item > 0 and sold_item > 0 and recv > 0:
            sold_attr = min(recv, sold_item * (recv / total_item))
        sold_attr = round(sold_attr, 2)
        remain = round(max(recv - sold_attr, 0.0), 2)
        turnover = round((sold_attr / recv) * 100.0, 1) if recv > 0 else 0.0
        share = (
            round((recv / vendor_recv_total) * 100.0, 1) if vendor_recv_total > 0 else 0.0
        )
        pd = pack_detail.get(i_code) or {}
        pack_size = float(pd.get("pack") or packs.get(i_code) or 0)
        pack_unit = str(pd.get("unit") or "").strip()
        if pack_size <= 1:
            pack_label = "—"
            pack_display = "—"
        else:
            pack_display = _qty(pack_size)
            pack_label = f"{pack_display} {pack_unit}".strip() if pack_unit else pack_display
        rows_out.append(
            {
                "item_code": i_code,
                "item_name": names.get(i_code) or i_code,
                "pack_size": pack_size,
                "pack_size_display": pack_display,
                "pack_unit": pack_unit or "—",
                "pack_label": pack_label,
                "recv_qty": recv,
                "recv_qty_display": _qty(recv),
                "share_pct": share,
                "share_display": f"{share:.1f}%",
                "sold_qty": sold_attr,
                "sold_qty_display": _qty(sold_attr),
                "remain_qty": remain,
                "remain_qty_display": _qty(remain),
                "turnover_pct": turnover,
                "turnover_display": f"{turnover:.1f}%",
            }
        )

    rows_out.sort(
        key=lambda r: (-float(r["recv_qty"]), r["item_name"], r["item_code"])
    )
    tot_sold = round(sum(float(r["sold_qty"]) for r in rows_out), 2)
    tot_turn = (
        round((tot_sold / vendor_recv_total) * 100.0, 1) if vendor_recv_total > 0 else 0.0
    )
    result = {
        "vendor_code": vend_key or vendor,
        "vendor_name": vendor_name,
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "unit_label": "كرتون",
        "rows": rows_out,
        "kpis": {
            "item_count": len(rows_out),
            "item_count_display": f"{len(rows_out):,}",
            "recv_qty": vendor_recv_total,
            "recv_qty_display": _qty(vendor_recv_total),
            "sold_qty": tot_sold,
            "sold_qty_display": _qty(tot_sold),
            "turnover_pct": tot_turn,
            "turnover_display": f"{tot_turn:.1f}%",
        },
    }
    cache.set(cache_key, result, _CACHE_TTL)
    return result


def build_vendor_item_detail_excel(detail: dict[str, Any]) -> HttpResponse:
    """تصدير أصناف المورد إلى Excel."""
    rows = detail.get("rows") or []
    kpis = detail.get("kpis") or {}
    period = escape(str(detail.get("period_label") or ""))
    vendor = escape(
        f"{detail.get('vendor_name') or ''} ({detail.get('vendor_code') or ''})"
    )
    buf = io.StringIO()
    buf.write("\ufeff")
    buf.write(
        "<html xmlns:o=\"urn:schemas-microsoft-com:office:office\" "
        "xmlns:x=\"urn:schemas-microsoft-com:office:excel\" "
        "xmlns=\"http://www.w3.org/TR/REC-html40\">"
        "<head><meta charset=\"utf-8\">"
        "<!--[if gte mso 9]><xml><x:ExcelWorkbook><x:ExcelWorksheets>"
        "<x:ExcelWorksheet><x:Name>أصناف المورد</x:Name>"
        "<x:WorksheetOptions><x:DisplayRightToLeft/></x:WorksheetOptions>"
        "</x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
        "<style>"
        "table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:11px;}"
        "th,td{border:1px solid #94a3b8;padding:3px 6px;white-space:nowrap;}"
        "th{background:#1e3a5f;color:#fff;font-weight:700;}"
        "td.num{mso-number-format:'\\#\\,\\#\\#0\\.00';text-align:left;}"
        "td.pct{mso-number-format:'0\\.0';text-align:left;}"
        "td.int{mso-number-format:'\\#\\,\\#\\#0';text-align:left;}"
        "tr.foot td{background:#e2e8f0;font-weight:700;}"
        "caption{font-size:13px;font-weight:700;margin-bottom:6px;text-align:right;}"
        "</style></head><body dir=\"rtl\">"
    )
    buf.write(f"<caption>أصناف المورد {vendor} — {period}</caption>")
    buf.write(
        "<table><thead><tr>"
        "<th>#</th><th>رقم الصنف</th><th>الاسم</th>"
        "<th>أكبر عبوة</th><th>وحدة العبوة</th>"
        "<th>وارد كرتون</th><th>نسبة من المورد %</th>"
        "<th>مباع كرتون</th><th>متبقي</th><th>الدوران %</th>"
        "</tr></thead><tbody>"
    )
    for i, row in enumerate(rows, 1):
        buf.write("<tr>")
        buf.write(f'<td class="int">{i}</td>')
        buf.write(f"<td>{escape(str(row.get('item_code') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('item_name') or ''))}</td>")
        pack = float(row.get("pack_size") or 0)
        if pack <= 1:
            buf.write("<td>—</td><td>—</td>")
        else:
            buf.write(f'<td class="num">{pack:.2f}</td>')
            buf.write(f"<td>{escape(str(row.get('pack_unit') or ''))}</td>")
        buf.write(f'<td class="num">{float(row.get("recv_qty") or 0):.2f}</td>')
        buf.write(f'<td class="pct">{float(row.get("share_pct") or 0):.1f}</td>')
        buf.write(f'<td class="num">{float(row.get("sold_qty") or 0):.2f}</td>')
        buf.write(f'<td class="num">{float(row.get("remain_qty") or 0):.2f}</td>')
        buf.write(f'<td class="pct">{float(row.get("turnover_pct") or 0):.1f}</td>')
        buf.write("</tr>")
    buf.write(
        '<tr class="foot"><td></td><td></td><td>الإجمالي</td><td></td><td></td>'
        f'<td class="num">{float(kpis.get("recv_qty") or 0):.2f}</td>'
        '<td class="pct">100.0</td>'
        f'<td class="num">{float(kpis.get("sold_qty") or 0):.2f}</td>'
        "<td></td>"
        f'<td class="pct">{float(kpis.get("turnover_pct") or 0):.1f}</td>'
        "</tr>"
    )
    buf.write("</tbody></table></body></html>")
    code = str(detail.get("vendor_code") or "vendor")
    filename = f"vendor_items_{code}_{period.replace(' ', '').replace('→', '_')}.xls"
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.ms-excel; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


def apply_decision_filter(report: dict[str, Any], decision: str) -> dict[str, Any]:
    key = str(decision or "").strip().lower()
    if not key or not report:
        return report
    rows = [r for r in (report.get("rows") or []) if r.get("decision_key") == key]
    settle_n = sum(1 for r in rows if r.get("decision_key") == "settle")
    partial_n = sum(1 for r in rows if r.get("decision_key") == "partial")
    hold_n = sum(1 for r in rows if r.get("decision_key") == "hold")
    tot_recv = round(sum(float(r.get("recv_qty") or 0) for r in rows), 2)
    tot_sold = round(sum(float(r.get("sold_qty") or 0) for r in rows), 2)
    tot_due = round(sum(float(r.get("due_amt") or 0) for r in rows), 2)
    tot_turn = round((tot_sold / tot_recv) * 100.0, 1) if tot_recv > 0 else 0.0
    kpis = dict(report.get("kpis") or {})
    kpis.update(
        {
            "vendor_count": len(rows),
            "vendor_count_display": f"{len(rows):,}",
            "shown_count": len(rows),
            "recv_qty": tot_recv,
            "recv_qty_display": _qty(tot_recv),
            "sold_qty": tot_sold,
            "sold_qty_display": _qty(tot_sold),
            "due_amt": tot_due,
            "due_amt_display": _money(tot_due),
            "turnover_pct": tot_turn,
            "turnover_display": f"{tot_turn:.1f}%",
            "settle_count": settle_n,
            "partial_count": partial_n,
            "hold_count": hold_n,
        }
    )
    return {**report, "rows": rows, "kpis": kpis}


def build_vendor_turnover_excel(report: dict[str, Any]) -> HttpResponse:
    """ملف Excel (HTML/XML) بجدول مضغوط مرتب الخلايا — يفتح مباشرة في Excel."""
    rows = report.get("rows") or []
    kpis = report.get("kpis") or {}
    period = escape(str(report.get("period_label") or ""))
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM لعربية Excel
    buf.write(
        "<html xmlns:o=\"urn:schemas-microsoft-com:office:office\" "
        "xmlns:x=\"urn:schemas-microsoft-com:office:excel\" "
        "xmlns=\"http://www.w3.org/TR/REC-html40\">"
        "<head><meta charset=\"utf-8\">"
        "<!--[if gte mso 9]><xml><x:ExcelWorkbook><x:ExcelWorksheets>"
        "<x:ExcelWorksheet><x:Name>دوران الموردين</x:Name>"
        "<x:WorksheetOptions><x:DisplayRightToLeft/></x:WorksheetOptions>"
        "</x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
        "<style>"
        "table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:11px;}"
        "th,td{border:1px solid #94a3b8;padding:3px 6px;white-space:nowrap;}"
        "th{background:#1e3a5f;color:#fff;font-weight:700;}"
        "td.num{mso-number-format:'\\#\\,\\#\\#0\\.00';text-align:left;}"
        "td.pct{mso-number-format:'0\\.0';text-align:left;}"
        "td.int{mso-number-format:'\\#\\,\\#\\#0';text-align:left;}"
        "tr.settle td.dec{background:#d1fae5;color:#065f46;font-weight:700;}"
        "tr.partial td.dec{background:#fef3c7;color:#92400e;font-weight:700;}"
        "tr.hold td.dec{background:#fee2e2;color:#991b1b;font-weight:700;}"
        "tr.foot td{background:#e2e8f0;font-weight:700;}"
        "caption{font-size:13px;font-weight:700;margin-bottom:6px;text-align:right;}"
        "</style></head><body dir=\"rtl\">"
    )
    buf.write(f"<caption>دوران مخزون الموردين (كرتون) — {period}</caption>")
    buf.write(
        "<table>"
        "<thead><tr>"
        "<th>#</th><th>كود المورد</th><th>المورد</th>"
        "<th>الاستحقاق</th><th>المستحق</th><th>الدوران %</th>"
        "<th>وارد كرتون</th><th>مباع كرتون</th><th>متبقي كرتون</th>"
        "<th>أصناف</th>"
        "</tr></thead><tbody>"
    )
    for i, row in enumerate(rows, 1):
        key = escape(str(row.get("decision_key") or "hold"))
        buf.write(f'<tr class="{key}">')
        buf.write(f'<td class="int">{i}</td>')
        buf.write(f"<td>{escape(str(row.get('vendor_code') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('vendor_name') or ''))}</td>")
        buf.write(
            f'<td class="dec">{escape(str(row.get("decision_label") or ""))}</td>'
        )
        buf.write(f'<td class="num">{float(row.get("due_amt") or 0):.2f}</td>')
        buf.write(f'<td class="pct">{float(row.get("turnover_pct") or 0):.1f}</td>')
        buf.write(f'<td class="num">{float(row.get("recv_qty") or 0):.2f}</td>')
        buf.write(f'<td class="num">{float(row.get("sold_qty") or 0):.2f}</td>')
        buf.write(f'<td class="num">{float(row.get("remain_qty") or 0):.2f}</td>')
        buf.write(f'<td class="int">{int(row.get("item_count") or 0)}</td>')
        buf.write("</tr>")
    buf.write(
        '<tr class="foot">'
        "<td></td><td></td><td>الإجمالي</td><td></td>"
        f'<td class="num">{float(kpis.get("due_amt") or 0):.2f}</td>'
        f'<td class="pct">{float(kpis.get("turnover_pct") or 0):.1f}</td>'
        f'<td class="num">{float(kpis.get("recv_qty") or 0):.2f}</td>'
        f'<td class="num">{float(kpis.get("sold_qty") or 0):.2f}</td>'
        "<td></td><td></td></tr>"
    )
    buf.write("</tbody></table></body></html>")

    filename = f"vendor_turnover_{period.replace(' ', '').replace('→', '_').replace(':', '-')}.xls"
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.ms-excel; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp
