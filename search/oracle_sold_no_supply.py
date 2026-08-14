"""أصناف تُباع في الفترة بلا مشتريات على الفرع ولا تحويل وارد إليه.

المسار السريع:
- المبيعات: قيادة POS بالتاريخ ثم JOIN التفاصيل على BILL_NO
- المشتريات والتحويلات الواردة: مسح مرة واحدة لكل التاريخ ثم كاش طويل
- الاستبعاد في بايثون حسب (فرع، صنف)
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.cache import cache

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _bill_mst_ok,
    _bind_brn,
    _branch_names,
    _date_params,
    _fetch_all,
    _hung_ok,
    _pos_owner,
    _run_parallel,
    _schema,
    oracle_enabled,
)

_CACHE_TTL = 7200
_CACHE_VER = "v1"
_PAGE_SIZE = 80
_NAME_BATCH = 400


def _money(value: Any) -> str:
    return f"{float(value or 0):,.2f}"


def _qty(value: Any) -> str:
    num = float(value or 0)
    if abs(num - round(num)) < 1e-9:
        return f"{int(round(num)):,}"
    return f"{num:,.2f}"


def _brn_code(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        text = str(value).strip()
        if text.endswith(".0") and text[:-2].lstrip("-").isdigit():
            return text[:-2]
        return text


def _item_code(value: Any) -> str:
    return str(value or "").strip()


def _pair(branch: Any, item: Any) -> tuple[str, str]:
    return (_brn_code(branch), _item_code(item))


def _validate(date_from, date_to):
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")
    return d_from, d_to


def _pos_slices(d_from, d_to_excl) -> list[tuple]:
    slices = []
    cur = d_from
    while cur < d_to_excl:
        nxt = min(cur + timedelta(days=7), d_to_excl)
        slices.append((cur, nxt))
        cur = nxt
    return slices or [(d_from, d_to_excl)]


def _rows_to_pairs(rows: list[dict]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for row in rows or []:
        key = _pair(row.get("BRN"), row.get("I_CODE"))
        if key[0] and key[1]:
            out.append(key)
    return out


def _load_purchased_pairs() -> set[tuple[str, str]]:
    cache_key = f"sns:pi:{_CACHE_VER}"
    cached = cache.get(cache_key)
    if isinstance(cached, list):
        return {(_brn_code(a), _item_code(b)) for a, b in cached}
    schema = _schema()
    rows = _fetch_all(
        f"""
        SELECT /*+ USE_NL(d) INDEX(d INDX_SER_PI_BILL_DTL) */
               m.BRN_NO AS BRN, d.I_CODE
        FROM {schema}.IAS_PI_BILL_MST m
        JOIN {schema}.IAS_PI_BILL_DTL d ON d.BILL_SER = m.BILL_SER
        WHERE {_hung_ok("m")}
          AND d.I_CODE IS NOT NULL
        GROUP BY m.BRN_NO, d.I_CODE
        """
    )
    pairs = _rows_to_pairs(rows)
    try:
        cache.set(cache_key, pairs, _CACHE_TTL)
    except Exception:
        pass
    return set(pairs)


def _load_inbound_pairs() -> set[tuple[str, str]]:
    cache_key = f"sns:in:{_CACHE_VER}"
    cached = cache.get(cache_key)
    if isinstance(cached, list):
        return {(_brn_code(a), _item_code(b)) for a, b in cached}
    schema = _schema()
    rows = _fetch_all(
        f"""
        SELECT /*+ USE_NL(d) INDEX(d INDX_SER_WHTRNS_DTL) */
               m.BRN_NO AS BRN, d.I_CODE
        FROM {schema}.IAS_WHTRNS_MST m
        JOIN {schema}.IAS_WHTRNS_DTL d ON d.TR_SER = m.TR_SER
        WHERE m.TR_INOUT_TYPE = 2
          AND {_hung_ok("m")}
          AND d.I_CODE IS NOT NULL
        GROUP BY m.BRN_NO, d.I_CODE
        """
    )
    pairs = _rows_to_pairs(rows)
    try:
        cache.set(cache_key, pairs, _CACHE_TTL)
    except Exception:
        pass
    return set(pairs)


def _pos_sold_sql(pos: str, brn_sql: str) -> str:
    return f"""
        SELECT /*+ LEADING(m d) USE_NL(d)
                   INDEX(m POSBILLMST_BILLDATEUSRBRN)
                   INDEX(d IAS_POS_INDX_BILL_DTL) */
               m.BRN_NO AS BRN, d.I_CODE,
               SUM(NVL(d.I_QTY, 0)) AS S_QTY
        FROM {pos}.IAS_POS_BILL_MST m
        JOIN {pos}.IAS_POS_BILL_DTL d ON d.BILL_NO = m.BILL_NO
        WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
          AND {_hung_ok("m")}
          AND d.I_CODE IS NOT NULL
          {brn_sql}
        GROUP BY m.BRN_NO, d.I_CODE
        HAVING SUM(NVL(d.I_QTY, 0)) > 0
    """


def _bill_sold_sql(schema: str, brn_sql: str) -> str:
    return f"""
        SELECT /*+ LEADING(b d) USE_NL(d) INDEX(d INDX_SER_BILL_DTL) */
               b.BRN_NO AS BRN, d.I_CODE,
               SUM(NVL(d.I_QTY, 0)) AS S_QTY
        FROM {schema}.IAS_BILL_MST b
        JOIN {schema}.IAS_BILL_DTL d ON d.BILL_SER = b.BILL_SER
        WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
          AND {_bill_mst_ok("b")}
          AND b.BILL_DOC_TYPE IN (1, 4, 5, 8)
          AND d.I_CODE IS NOT NULL
          {brn_sql.replace("m.BRN_NO", "b.BRN_NO")}
        GROUP BY b.BRN_NO, d.I_CODE
        HAVING SUM(NVL(d.I_QTY, 0)) > 0
    """


def _merge_sold_qty(parts: list) -> dict[tuple[str, str], float]:
    qty: dict[tuple[str, str], float] = {}
    for rows in parts:
        for row in rows or []:
            key = _pair(row.get("BRN"), row.get("I_CODE"))
            if not key[0] or not key[1]:
                continue
            qty[key] = qty.get(key, 0.0) + float(row.get("S_QTY") or 0)
    return qty


def _hydrate_codes(codes: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not codes:
        return out
    schema = _schema()
    for start in range(0, len(codes), _NAME_BATCH):
        chunk = codes[start : start + _NAME_BATCH]
        params: dict[str, Any] = {}
        keys = []
        for index, code in enumerate(chunk):
            key = f"c{index}"
            keys.append(f":{key}")
            params[key] = str(code)
        for row in _fetch_all(
            f"""
            SELECT i.I_CODE,
                   NVL(NULLIF(TRIM(i.I_NAME), ''), i.I_CODE) AS I_NAME,
                   i.G_CODE,
                   NVL(g.G_A_NAME, NVL(TO_CHAR(i.G_CODE), '—')) AS G_NAME,
                   NVL(i.SERVICE_ITM, 0) AS SERVICE_ITM
            FROM {schema}.IAS_ITM_MST i
            LEFT JOIN {schema}.GROUP_DETAILS g ON g.G_CODE = i.G_CODE
            WHERE i.I_CODE IN ({", ".join(keys)})
            """,
            params,
        ):
            code = _item_code(row.get("I_CODE"))
            out[code] = {
                "name": str(row.get("I_NAME") or "").strip() or code,
                "group_code": _item_code(row.get("G_CODE")),
                "group_name": str(row.get("G_NAME") or "").strip() or "—",
                "service": float(row.get("SERVICE_ITM") or 0) == 1,
            }
    return out


def _load_rows(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
    q: str = "",
) -> list[dict]:
    d_from, d_to = _validate(date_from, date_to)
    branch = str(branch_code or "").strip()
    group = str(group_code or "").strip()
    query = str(q or "").strip()[:80]
    cache_key = (
        f"sns:rows:{_CACHE_VER}:{d_from}:{d_to}:{branch}:{group}:{query.lower()}"
    )
    cached = cache.get(cache_key)
    if isinstance(cached, list):
        return cached

    dates = _date_params(d_from, d_to)
    sold_params = dict(dates)
    brn_sql = ""
    if branch:
        sold_params["brn"] = _bind_brn(branch)
        brn_sql = "AND m.BRN_NO = :brn"
    pos = _pos_owner()
    schema = _schema()
    slices = _pos_slices(d_from, dates["d_to_excl"])

    def _job_pi():
        return _load_purchased_pairs()

    def _job_in():
        return _load_inbound_pairs()

    def _job_bill():
        bill_key = f"sns:bill:{_CACHE_VER}:{d_from}:{d_to}:{branch}"
        hit = cache.get(bill_key)
        if isinstance(hit, list):
            return hit
        rows = _fetch_all(_bill_sold_sql(schema, brn_sql), sold_params)
        try:
            cache.set(bill_key, rows, _CACHE_TTL)
        except Exception:
            pass
        return rows

    def _job_pos_slice(slice_from, slice_to):
        slice_key = f"sns:pos:{_CACHE_VER}:{slice_from}:{slice_to}:{branch}"
        hit = cache.get(slice_key)
        if isinstance(hit, list):
            return hit
        params = dict(sold_params)
        params["d_from"] = slice_from
        params["d_to_excl"] = slice_to
        rows = _fetch_all(_pos_sold_sql(pos, brn_sql), params)
        try:
            cache.set(slice_key, rows, _CACHE_TTL)
        except Exception:
            pass
        return rows

    jobs = [_job_pi, _job_in, _job_bill]
    for sl_from, sl_to in slices:
        jobs.append(lambda sl_from=sl_from, sl_to=sl_to: _job_pos_slice(sl_from, sl_to))

    results = _run_parallel(jobs, max_workers=min(4, len(jobs)), timeout_sec=100)
    purch = results[0] if isinstance(results[0], set) else set(results[0] or [])
    inbound = results[1] if isinstance(results[1], set) else set(results[1] or [])
    sold_qty = _merge_sold_qty(results[2:])

    names = _branch_names()
    slim: list[dict] = []
    item_codes: list[str] = []
    seen: set[str] = set()
    for key, qty in sold_qty.items():
        if qty <= 0 or key in purch or key in inbound:
            continue
        brn, code = key
        slim.append({"branch_code": brn, "item_code": code, "qty": round(qty, 4)})
        if code not in seen:
            seen.add(code)
            item_codes.append(code)

    meta = _hydrate_codes(item_codes)
    q_up = query.upper()
    rows: list[dict] = []
    for row in slim:
        info = meta.get(row["item_code"]) or {}
        if info.get("service"):
            continue
        g_code = info.get("group_code") or ""
        if group and str(g_code) != str(group):
            continue
        name = info.get("name") or row["item_code"]
        if query and q_up not in name.upper() and q_up not in row["item_code"].upper():
            continue
        qty = float(row["qty"] or 0)
        brn = row["branch_code"]
        rows.append(
            {
                "branch_code": brn,
                "branch_name": names.get(brn) or brn or "—",
                "item_code": row["item_code"],
                "item_name": name,
                "group_code": g_code,
                "group_name": info.get("group_name") or "—",
                "qty": qty,
                "qty_display": _qty(qty),
            }
        )
    rows.sort(key=lambda r: (-r["qty"], r["item_code"]))
    try:
        cache.set(cache_key, rows, _CACHE_TTL)
    except Exception:
        pass
    return rows


def fetch_sold_no_supply_items(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
    q: str = "",
    offset: int = 0,
    limit: int = _PAGE_SIZE,
) -> list[dict]:
    try:
        offset = max(0, int(offset))
        limit = min(max(1, int(limit)), 500)
    except (TypeError, ValueError):
        offset, limit = 0, _PAGE_SIZE
    rows = _load_rows(
        date_from,
        date_to,
        branch_code=branch_code,
        group_code=group_code,
        q=q,
    )
    return rows[offset : offset + limit]


def build_sold_no_supply_report(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
    q: str = "",
) -> dict[str, Any]:
    d_from, d_to = _validate(date_from, date_to)
    branch = str(branch_code or "").strip()
    group = str(group_code or "").strip()
    query = str(q or "").strip()[:80]
    report_key = (
        f"sns:rep:{_CACHE_VER}:{d_from}:{d_to}:{branch}:{group}:{query.lower()}"
    )
    cached = cache.get(report_key)
    if isinstance(cached, dict):
        return cached

    rows = _load_rows(
        d_from, d_to, branch_code=branch, group_code=group, q=query
    )
    by_brn: dict[str, dict[str, Any]] = {}
    item_codes: set[str] = set()
    qty_total = 0.0
    for row in rows:
        brn = row["branch_code"] or "—"
        bucket = by_brn.setdefault(
            brn,
            {
                "branch_code": row["branch_code"],
                "branch_name": row["branch_name"],
                "item_codes": set(),
                "line_count": 0,
                "qty_total": 0.0,
            },
        )
        bucket["item_codes"].add(row["item_code"])
        bucket["line_count"] += 1
        bucket["qty_total"] = round(bucket["qty_total"] + row["qty"], 2)
        item_codes.add(row["item_code"])
        qty_total += row["qty"]

    qty_total = round(qty_total, 2)
    max_qty = max((b["qty_total"] for b in by_brn.values()), default=0.0)
    branch_rows = []
    for bucket in sorted(by_brn.values(), key=lambda b: -b["qty_total"]):
        qty = bucket["qty_total"]
        branch_rows.append(
            {
                "branch_code": bucket["branch_code"],
                "branch_name": bucket["branch_name"],
                "item_count": len(bucket["item_codes"]),
                "line_count": bucket["line_count"],
                "qty_total": qty,
                "qty_display": _qty(qty),
                "bar_pct": round(qty / max_qty * 100.0, 1) if max_qty > 0 else 0.0,
            }
        )

    line_count = len(rows)
    result = {
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "page_size": _PAGE_SIZE,
        "kpis": {
            "item_count": len(item_codes),
            "branch_count": len(by_brn),
            "line_count": line_count,
            "line_count_display": f"{line_count:,}",
            "qty_total": qty_total,
            "qty_display": _qty(qty_total),
        },
        "branch_rows": branch_rows,
        "rows": rows[:_PAGE_SIZE],
        "filters": {"branch": branch, "group": group, "q": query},
    }
    try:
        cache.set(report_key, result, _CACHE_TTL)
    except Exception:
        pass
    return result
