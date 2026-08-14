"""رصيد في مخازن محددة بلا حركة مبيعات خلال الفترة.

المسار السريع:
- الكمية من IAS_ITM_WCODE عبر فهرس W_CODE (تسعة مخازن فقط)
- المبيعات: قيادة POS بالتاريخ ثم W_CODE على رأس الفاتورة — بلا بحث بكود الصنف
- فواتير النظام من IAS_BILL_DTL عبر W_CODE
- الاستبعاد في بايثون + كاش أسبوعي للمبيعات وكاش للرصيد
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.cache import cache

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _bill_mst_ok,
    _bind_gcode,
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
_CACHE_VER = "v11"
_PAGE_SIZE = 80
_NAME_BATCH = 80
_STOCK_WAREHOUSES = (60, 1, 1201, 1901, 1801, 2001, 701, 800, 30)
_WH_LABEL = "60، 1، 1201، 1901، 1801، 2001، 701، 800، 30"
_EXCLUDED_GCODE = (46,)  # مجموعة التغليف
_EXCLUDED_ICODES = (
    "0479",
    "101200562244576538257",
    "101200562244576538258",
    "1012064",
    "101350",
    "4410770",
)


def excluded_unsold_group_codes() -> set[str]:
    return {str(code) for code in _EXCLUDED_GCODE}


def excluded_unsold_item_codes() -> set[str]:
    return set(_EXCLUDED_ICODES)


def _fold_ar(text: str) -> str:
    out = str(text or "")
    for src, dst in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي")):
        out = out.replace(src, dst)
    return " ".join(out.split())


def _is_excluded_packaging_name(name: str) -> bool:
    """اكياس تعبئة / اكياس تغليف / اكياس طبعة الرشيد — ليس كل صنف فيه كلمة كيس."""
    folded = _fold_ar(name)
    if "اكياس تعبئه" in folded or "اكياس تغليف" in folded:
        return True
    if "اكياس" in folded and ("الرشيد" in folded or "طبعه اسواق" in folded):
        return True
    return False


def _money(value: Any) -> str:
    return f"{float(value or 0):,.2f}"


def _qty(value: Any) -> str:
    num = float(value or 0)
    if abs(num - round(num)) < 1e-9:
        return f"{int(round(num)):,}"
    return f"{num:,.2f}"


def _wh_code(value: Any) -> str:
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


def _wh_in_sql(codes: list[int] | tuple[int, ...] | None = None) -> str:
    return ", ".join(str(int(code)) for code in (codes or _STOCK_WAREHOUSES))


def _pair_key(warehouse: Any, item: Any) -> tuple[str, str]:
    return (_wh_code(warehouse), _item_code(item))


def _warehouse_meta() -> dict[str, dict[str, str]]:
    cache_key = f"unsold:whmeta:{_CACHE_VER}"
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and cached:
        return cached
    names = _branch_names()
    out: dict[str, dict[str, str]] = {}
    try:
        rows = _fetch_all(
            f"""
            SELECT W_CODE, W_NAME, CONN_BRN_NO
            FROM {_schema()}.WAREHOUSE_DETAILS
            WHERE W_CODE IN ({_wh_in_sql()})
            """
        )
    except Exception:
        rows = []
    for row in rows:
        warehouse = _wh_code(row.get("W_CODE"))
        if not warehouse:
            continue
        branch = _wh_code(row.get("CONN_BRN_NO"))
        out[warehouse] = {
            "name": str(row.get("W_NAME") or "").strip() or warehouse,
            "branch": branch,
            "branch_name": names.get(branch) or branch or "—",
        }
    for code in _STOCK_WAREHOUSES:
        warehouse = str(int(code))
        out.setdefault(
            warehouse,
            {"name": warehouse, "branch": "", "branch_name": "—"},
        )
    try:
        cache.set(cache_key, out, _CACHE_TTL)
    except Exception:
        pass
    return out


def _warehouses_for_branch(branch: str) -> list[int]:
    if not branch:
        return list(_STOCK_WAREHOUSES)
    wanted = _wh_code(branch)
    meta = _warehouse_meta()
    matched = [
        int(code)
        for code in _STOCK_WAREHOUSES
        if meta.get(str(int(code)), {}).get("branch") == wanted
    ]
    return matched


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


def _stock_sql(schema: str, item_sql: str, warehouses: list[int]) -> str:
    return f"""
        SELECT /*+ INDEX(w IASITMWCODE_WCODE_FK) */
               w.W_CODE, w.I_CODE, i.G_CODE,
               MAX(i.I_NAME) AS I_NAME,
               ROUND(SUM(NVL(w.AVL_QTY, 0)), 4) AS QTY,
               ROUND(
                 SUM(NVL(w.AVL_QTY, 0) * NVL(w.I_CWTAVG, w.PRIMARY_COST)),
                 2
               ) AS VAL
        FROM {schema}.IAS_ITM_WCODE w
        JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = w.I_CODE
        WHERE w.W_CODE IN ({_wh_in_sql(warehouses)})
          AND NVL(w.AVL_QTY, 0) > 0
          AND NVL(i.SERVICE_ITM, 0) = 0
          {item_sql}
        GROUP BY w.W_CODE, w.I_CODE, i.G_CODE
    """


def _pos_sold_sql(pos: str, warehouses: list[int]) -> str:
    return f"""
        SELECT /*+ LEADING(m d) USE_NL(d)
                   INDEX(m POSBILLMST_BILLDATEUSRBRN)
                   INDEX(d IAS_POS_INDX_BILL_DTL) */
               m.W_CODE, d.I_CODE
        FROM {pos}.IAS_POS_BILL_MST m
        JOIN {pos}.IAS_POS_BILL_DTL d ON d.BILL_NO = m.BILL_NO
        WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
          AND {_hung_ok("m")}
          AND m.W_CODE IN ({_wh_in_sql(warehouses)})
          AND d.I_CODE IS NOT NULL
        GROUP BY m.W_CODE, d.I_CODE
    """


def _bill_sold_sql(schema: str, warehouses: list[int]) -> str:
    return f"""
        SELECT d.W_CODE, d.I_CODE
        FROM {schema}.IAS_BILL_DTL d
        JOIN {schema}.IAS_BILL_MST b ON b.BILL_SER = d.BILL_SER
        WHERE d.W_CODE IN ({_wh_in_sql(warehouses)})
          AND b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
          AND {_bill_mst_ok("b")}
          AND b.BILL_DOC_TYPE IN (1, 4, 5, 8)
          AND d.I_CODE IS NOT NULL
        GROUP BY d.W_CODE, d.I_CODE
    """


def _item_sql(group: str, query: str, params: dict[str, Any]) -> str:
    excl = ", ".join(str(int(code)) for code in _EXCLUDED_GCODE)
    sql = f" AND (i.G_CODE IS NULL OR i.G_CODE NOT IN ({excl}))"
    if _EXCLUDED_ICODES:
        quoted = ", ".join(
            "'" + str(code).replace("'", "''") + "'" for code in _EXCLUDED_ICODES
        )
        sql += f" AND i.I_CODE NOT IN ({quoted})"
    if group:
        params["gcode"] = _bind_gcode(group)
        sql += " AND i.G_CODE = :gcode"
    if query:
        params["q_like"] = f"%{query}%"
        sql += (
            " AND (UPPER(TO_CHAR(w.I_CODE)) LIKE UPPER(:q_like)"
            " OR UPPER(NVL(i.I_NAME, ' ')) LIKE UPPER(:q_like))"
        )
    return sql


def _rows_to_keys(rows: list[dict]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for row in rows or []:
        key = _pair_key(row.get("W_CODE"), row.get("I_CODE"))
        if key[0] and key[1]:
            out.append(key)
    return out


def _sold_jobs(d_from, d_to) -> tuple[str, list]:
    """مهام المبيعات المتوازية + مفتاح الكاش النهائي."""
    dates = _date_params(d_from, d_to)
    cache_key = f"unsold:sold:{_CACHE_VER}:{d_from}:{d_to}"
    pos = _pos_owner()
    schema = _schema()
    warehouses = list(_STOCK_WAREHOUSES)
    slices = _pos_slices(d_from, dates["d_to_excl"])

    def _job_bill():
        bill_key = f"unsold:bill:{_CACHE_VER}:{d_from}:{d_to}"
        hit = cache.get(bill_key)
        if isinstance(hit, list):
            return hit
        rows = _rows_to_keys(_fetch_all(_bill_sold_sql(schema, warehouses), dates))
        try:
            cache.set(bill_key, rows, _CACHE_TTL)
        except Exception:
            pass
        return rows

    def _job_pos_slice(slice_from, slice_to):
        slice_key = f"unsold:pos:{_CACHE_VER}:{slice_from}:{slice_to}"
        hit = cache.get(slice_key)
        if isinstance(hit, list):
            return hit
        params = {"d_from": slice_from, "d_to_excl": slice_to}
        rows = _rows_to_keys(_fetch_all(_pos_sold_sql(pos, warehouses), params))
        try:
            cache.set(slice_key, rows, _CACHE_TTL)
        except Exception:
            pass
        return rows

    jobs = [_job_bill]
    for sl_from, sl_to in slices:
        jobs.append(lambda sl_from=sl_from, sl_to=sl_to: _job_pos_slice(sl_from, sl_to))
    return cache_key, jobs


def _merge_sold(parts: list) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for part in parts:
        keys.update(part or [])
    return keys


def _load_stock_rows(
    *,
    branch_code: str = "",
    group_code: str = "",
    q: str = "",
) -> list[dict]:
    branch = str(branch_code or "").strip()
    group = str(group_code or "").strip()
    query = str(q or "").strip()[:80]
    warehouses = _warehouses_for_branch(branch)
    if not warehouses:
        return []
    cache_key = (
        f"unsold:stock:{_CACHE_VER}:{','.join(str(w) for w in warehouses)}"
        f":{group}:{query.lower()}"
    )
    cached = cache.get(cache_key)
    if isinstance(cached, list):
        return cached

    params: dict[str, Any] = {}
    item_sql = _item_sql(group, query, params)
    raw = _fetch_all(_stock_sql(_schema(), item_sql, warehouses), params)
    meta = _warehouse_meta()
    rows: list[dict] = []
    for row in raw:
        warehouse = _wh_code(row.get("W_CODE"))
        item = _item_code(row.get("I_CODE"))
        qty = round(float(row.get("QTY") or 0), 4)
        if not warehouse or not item or qty <= 0:
            continue
        info = meta.get(warehouse) or {}
        g_code = _item_code(row.get("G_CODE"))
        if g_code in excluded_unsold_group_codes():
            continue
        if item in excluded_unsold_item_codes():
            continue
        if _is_excluded_packaging_name(str(row.get("I_NAME") or "")):
            continue
        rows.append(
            {
                "warehouse_code": warehouse,
                "warehouse_name": info.get("name") or warehouse,
                "branch_code": info.get("branch") or "",
                "branch_name": info.get("branch_name") or "—",
                "item_code": item,
                "group_code": g_code,
                "qty": qty,
                "val": round(float(row.get("VAL") or 0), 2),
            }
        )
    try:
        cache.set(cache_key, rows, _CACHE_TTL)
    except Exception:
        pass
    return rows


def _slim_cache_key(d_from, d_to, branch: str, group: str, query: str) -> str:
    return f"unsold:slim:{_CACHE_VER}:{d_from}:{d_to}:{branch}:{group}:{query.lower()}"


def _load_unsold_slim(
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
    cache_key = _slim_cache_key(d_from, d_to, branch, group, query)
    cached = cache.get(cache_key)
    if isinstance(cached, list):
        return cached

    sold_cache_key, sold_jobs = _sold_jobs(d_from, d_to)
    sold_cached = cache.get(sold_cache_key)
    jobs = [
        lambda: _load_stock_rows(branch_code=branch, group_code=group, q=query)
    ]
    if not isinstance(sold_cached, list):
        jobs.extend(sold_jobs)

    results = _run_parallel(jobs, max_workers=min(4, len(jobs)), timeout_sec=100)
    stock_rows = results[0]
    if isinstance(sold_cached, list):
        sold = {(_wh_code(a), _item_code(b)) for a, b in sold_cached}
    else:
        sold = _merge_sold(results[1:])
        try:
            cache.set(sold_cache_key, list(sold), _CACHE_TTL)
        except Exception:
            pass
    unsold = [
        row
        for row in stock_rows or []
        if (row["warehouse_code"], row["item_code"]) not in sold
    ]
    unsold.sort(key=lambda r: (-float(r.get("val") or 0), r.get("item_code") or ""))
    try:
        cache.set(cache_key, unsold, _CACHE_TTL)
    except Exception:
        pass
    return unsold


def _hydrate(slim_rows: list[dict]) -> list[dict]:
    if not slim_rows:
        return []
    codes: list[str] = []
    seen: set[str] = set()
    for row in slim_rows:
        code = row.get("item_code") or ""
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    names: dict[str, str] = {}
    groups: dict[str, tuple[str, str]] = {}
    schema = _schema()
    for start in range(0, len(codes), _NAME_BATCH):
        chunk = codes[start : start + _NAME_BATCH]
        params = {}
        keys = []
        for index, code in enumerate(chunk):
            key = f"c{index}"
            keys.append(f":{key}")
            params[key] = str(code)
        for row in _fetch_all(
            f"""
            SELECT i.I_CODE,
                   NVL(NULLIF(TRIM(i.I_NAME), ''), TO_CHAR(i.I_CODE)) AS I_NAME,
                   NVL(TO_CHAR(i.G_CODE), '') AS G_CODE,
                   NVL(g.G_A_NAME, NVL(TO_CHAR(i.G_CODE), '—')) AS G_NAME
            FROM {schema}.IAS_ITM_MST i
            LEFT JOIN {schema}.GROUP_DETAILS g ON g.G_CODE = i.G_CODE
            WHERE i.I_CODE IN ({", ".join(keys)})
            """,
            params,
        ):
            code = _item_code(row.get("I_CODE"))
            names[code] = str(row.get("I_NAME") or "").strip() or code
            groups[code] = (
                _item_code(row.get("G_CODE")),
                str(row.get("G_NAME") or "").strip() or "—",
            )
    out: list[dict] = []
    for row in slim_rows:
        code = row["item_code"]
        if code in excluded_unsold_item_codes():
            continue
        item_name = names.get(code) or code
        if _is_excluded_packaging_name(item_name):
            continue
        g_code, g_name = groups.get(code, (row.get("group_code") or "", "—"))
        qty = float(row.get("qty") or 0)
        val = float(row.get("val") or 0)
        out.append(
            {
                "warehouse_code": row["warehouse_code"],
                "warehouse_name": row.get("warehouse_name") or row["warehouse_code"],
                "branch_code": row.get("branch_code") or "",
                "branch_name": row.get("branch_name") or "—",
                "item_code": code,
                "item_name": item_name,
                "group_code": g_code or row.get("group_code") or "",
                "group_name": g_name,
                "qty": qty,
                "qty_display": _qty(qty),
                "purchased_qty": qty,
                "val": val,
                "net_display": _money(val),
                "purchased_net": val,
            }
        )
    return out


def fetch_unsold_items(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
    q: str = "",
    offset: int = 0,
    limit: int = _PAGE_SIZE,
) -> list[dict]:
    """صفحة أصناف من الكاش (أو مسح أوراكل عند أول طلب)."""
    try:
        offset = max(0, int(offset))
        limit = min(max(1, int(limit)), 500)
    except (TypeError, ValueError):
        offset, limit = 0, _PAGE_SIZE
    rows = _load_unsold_slim(
        date_from,
        date_to,
        branch_code=branch_code,
        group_code=group_code,
        q=q,
    )
    return _hydrate(rows[offset : offset + limit])


def build_unsold_report(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    group_code: str = "",
    q: str = "",
) -> dict[str, Any]:
    """ملخص KPIs + تجميع حسب الفرع + أول صفحة أصناف."""
    d_from, d_to = _validate(date_from, date_to)
    branch = str(branch_code or "").strip()
    group = str(group_code or "").strip()
    query = str(q or "").strip()[:80]
    report_key = (
        f"unsold:rep:{_CACHE_VER}:{d_from}:{d_to}:{branch}:{group}:{query.lower()}"
    )
    cached = cache.get(report_key)
    if isinstance(cached, dict):
        return cached

    slim = _load_unsold_slim(
        d_from, d_to, branch_code=branch, group_code=group, q=query
    )
    by_brn: dict[str, dict[str, Any]] = {}
    item_codes: set[str] = set()
    qty_total = 0.0
    net_total = 0.0
    for row in slim:
        brn = row["branch_code"] or "—"
        bucket = by_brn.setdefault(
            brn,
            {
                "branch_code": row["branch_code"],
                "branch_name": row["branch_name"],
                "item_codes": set(),
                "line_count": 0,
                "qty_total": 0.0,
                "net_total": 0.0,
            },
        )
        bucket["item_codes"].add(row["item_code"])
        bucket["line_count"] += 1
        bucket["qty_total"] = round(bucket["qty_total"] + float(row["qty"] or 0), 2)
        bucket["net_total"] = round(bucket["net_total"] + float(row["val"] or 0), 2)
        item_codes.add(row["item_code"])
        qty_total += float(row["qty"] or 0)
        net_total += float(row["val"] or 0)

    qty_total = round(qty_total, 2)
    net_total = round(net_total, 2)
    max_net = max((b["net_total"] for b in by_brn.values()), default=0.0)
    branch_rows = []
    for bucket in sorted(by_brn.values(), key=lambda b: -b["net_total"]):
        net = bucket["net_total"]
        qty = bucket["qty_total"]
        branch_rows.append(
            {
                "branch_code": bucket["branch_code"],
                "branch_name": bucket["branch_name"],
                "item_count": len(bucket["item_codes"]),
                "line_count": bucket["line_count"],
                "qty_total": qty,
                "qty_display": _qty(qty),
                "net_total": net,
                "net_display": _money(net),
                "bar_pct": round(net / max_net * 100.0, 1) if max_net > 0 else 0.0,
            }
        )

    line_count = len(slim)
    result = {
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "page_size": _PAGE_SIZE,
        "wh_label": _WH_LABEL,
        "q": query,
        "kpis": {
            "item_count": len(item_codes),
            "branch_count": len(by_brn),
            "line_count": line_count,
            "line_count_display": f"{line_count:,}",
            "qty_total": qty_total,
            "qty_display": _qty(qty_total),
            "net_total": net_total,
            "net_display": _money(net_total),
        },
        "branch_rows": branch_rows,
        "rows": _hydrate(slim[:_PAGE_SIZE]),
        "filters": {"branch": branch, "group": group, "q": query},
    }
    try:
        cache.set(report_key, result, _CACHE_TTL)
    except Exception:
        pass
    return result
