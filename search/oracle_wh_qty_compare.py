"""مقارنة كميات الأصناف بين مستودعين — رصيد IAS_ITM_WCODE (قراءة فقط)."""

from __future__ import annotations

import io
from html import escape
from typing import Any, Callable

from django.core.cache import cache
from django.http import HttpResponse

from .oracle_stock import (
    OracleStockError,
    _PENDING_SALES_LOOKBACK_DAYS,
    _bind_gcode,
    _fetch_all,
    _hung_ok,
    _pos_owner,
    _run_parallel,
    _schema,
    expected_stock_qty,
    fetch_item_max_pack_map,
    oracle_enabled,
    oracle_session,
)

_CACHE_TTL = 600
_CACHE_VER = "v7"
_NAME_BATCH = 800
_EPS = 0.0005
_MAX_ROWS = 2500

_MODES = {
    "all": "كل أصناف المستودع الأول",
    "zero_b": "في الأول ولا يتوفر في الثاني",
    "both": "موجود في الاثنين",
    "diff": "كمية مختلفة بين المستودعين",
}

_QTY_BASES = {
    "avail": "المتوفّر",
    "avl": "الرصيد الحالي",
}


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
    """تحويل كمية وحدة المخزون إلى أكبر عبوة."""
    pack = float(pack_size or 0)
    if pack <= 1:
        return _f(qty)
    return _f(float(qty or 0) / pack)

def _norm_code(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].replace("-", "").isdigit():
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


def _norm_mode(raw: str | None) -> str:
    key = str(raw or "all").strip().lower()
    if key in ("gap", "a_only"):
        key = "zero_b"
    if key == "b_only":
        key = "all"
    return key if key in _MODES else "all"


def _norm_qty_basis(raw: str | None) -> str:
    key = str(raw or "avail").strip().lower()
    if key in ("available", "expected", "after", "متوفر", "المتوفّر", "المتوفر"):
        key = "avail"
    if key in ("current", "stock", "balance", "رصيد", "الحالي"):
        key = "avl"
    return key if key in _QTY_BASES else "avail"


def _validate(wh_a: str, wh_b: str) -> tuple[str, str]:
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    a = _norm_code(wh_a)
    b = _norm_code(wh_b)
    if not a or not b:
        raise OracleStockError("حدد مستودعين للمقارنة.")
    if a == b:
        raise OracleStockError("اختر مستودعين مختلفين.")
    return a, b


def _cache_key(
    wh_a: str,
    wh_b: str,
    *,
    group_code: str,
    item_q: str,
    qty_basis: str,
) -> str:
    return (
        f"whqty:{_CACHE_VER}:{wh_a}:{wh_b}:{group_code}:{item_q}:{qty_basis}"
    )


def _fetch_wh_qty(
    warehouse: str,
    *,
    group_code: str = "",
    item_code: str = "",
) -> dict[str, float]:
    """رصيد موجب لكل صنف في المخزن — قيادة بـ W_CODE."""
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

    icode = _norm_code(item_code)
    if icode:
        params["icode"] = icode
        filters.append("w.I_CODE = :icode")

    where = " AND ".join(filters)
    rows = _fetch_all(
        f"""
        SELECT /*+ INDEX(w IASITMWCODE_WCODE_FK) */
               w.I_CODE AS I_CODE,
               ROUND(SUM(NVL(w.AVL_QTY, 0)), 4) AS QTY
        FROM {schema}.IAS_ITM_WCODE w
        {joins}
        WHERE {where}
        GROUP BY w.I_CODE
        """,
        params,
    )
    out: dict[str, float] = {}
    for row in rows or []:
        code = _norm_code(row.get("I_CODE"))
        if not code:
            continue
        out[code] = out.get(code, 0.0) + _f(row.get("QTY"))
    return out


def _fetch_wh_pending(warehouse: str) -> dict[str, float]:
    """صافي POS غير مرحّل لمخزن — قيادة بتاريخ الفاتورة غير المرحّلة."""
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
        if not code:
            continue
        out[code] = out.get(code, 0.0) + max(0.0, _f(row.get("QTY"), 4))
    for row in returns or []:
        code = _norm_code(row.get("I_CODE"))
        if not code:
            continue
        out[code] = out.get(code, 0.0) - max(0.0, _f(row.get("QTY"), 4))
    for code, qty_val in list(out.items()):
        cleaned = round(max(0.0, float(qty_val or 0)), 4)
        if cleaned <= _EPS:
            out.pop(code, None)
        else:
            out[code] = cleaned
    return out


def _apply_qty_basis(
    qty_map: dict[str, float],
    pend_map: dict[str, float],
    *,
    qty_basis: str,
    codes: set[str] | None = None,
) -> dict[str, float]:
    """يحسب الكمية المعروضة؛ لـ avail يمرّ على codes حتى لو صارت صفراً بعد الخصم."""
    if qty_basis != "avail":
        if codes is None:
            return dict(qty_map)
        return {c: _f(qty_map.get(c)) for c in codes if _f(qty_map.get(c)) > _EPS}

    src = codes if codes is not None else set(qty_map.keys())
    out: dict[str, float] = {}
    for code in src:
        expected = expected_stock_qty(qty_map.get(code) or 0, pend_map.get(code) or 0)
        out[code] = expected
    return out


def _resolve_item_q(item_q: str) -> tuple[str, str | None, set[str] | None]:
    """(label, exact_code|None, name_codes|None)."""
    raw = str(item_q or "").strip()
    if not raw:
        return "", None, None
    schema = _schema()
    code_like = (
        " " not in raw
        and len(raw) <= 48
        and all(ord(ch) < 128 for ch in raw)
        and any(ch.isdigit() for ch in raw)
    )
    if code_like:
        hit = _fetch_all(
            f"""
            SELECT i.I_CODE FROM {schema}.IAS_ITM_MST i
            WHERE i.I_CODE = :c AND ROWNUM <= 1
            """,
            {"c": raw},
        )
        code = _norm_code((hit[0].get("I_CODE") if hit else raw))
        return raw, code, {code} if code else None

    like = f"%{raw}%"
    rows = _fetch_all(
        f"""
        SELECT * FROM (
          SELECT i.I_CODE
          FROM {schema}.IAS_ITM_MST i
          WHERE UPPER(i.I_CODE) LIKE UPPER(:q)
             OR UPPER(NVL(i.I_NAME, '')) LIKE UPPER(:q)
          ORDER BY i.I_CODE
        ) WHERE ROWNUM <= 80
        """,
        {"q": like},
    )
    codes = {_norm_code(r.get("I_CODE")) for r in (rows or []) if _norm_code(r.get("I_CODE"))}
    if not codes:
        raise OracleStockError("لا يوجد صنف مطابق لنص البحث.")
    if len(codes) == 1:
        only = next(iter(codes))
        return raw, only, codes
    return raw, None, codes


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
                "item_name": str(row.get("I_NAME") or code).strip(),
                "g_code": _norm_code(row.get("G_CODE")),
                "g_name": str(row.get("G_NAME") or "").strip(),
            }
    return out


def _row_kind(qa: float, qb: float) -> str:
    """من منظور أصناف المستودع الأول فقط."""
    if qb <= _EPS:
        return "zero_b"
    if abs(qa - qb) > _EPS:
        return "diff"
    return "both"


def _kind_label(kind: str, *, wh_b_label: str) -> str:
    b = str(wh_b_label or "المستودع الثاني").strip() or "المستودع الثاني"
    return {
        "zero_b": f"لا يتوفر في {b}",
        "diff": "كمية مختلفة",
        "both": "متطابق",
    }.get(kind, kind)


def _row_matches(kind: str, mode: str) -> bool:
    if mode == "all":
        return True
    if mode == "zero_b":
        return kind == "zero_b"
    if mode == "both":
        return kind in ("both", "diff")
    if mode == "diff":
        return kind in ("diff", "zero_b")
    return True


def build_wh_qty_compare_report(
    *,
    warehouse_a: str,
    warehouse_b: str,
    group_code: str = "",
    item_q: str = "",
    mode: str = "all",
    qty_basis: str = "avail",
    wh_a_name: str = "",
    wh_b_name: str = "",
) -> dict[str, Any]:
    wh_a, wh_b = _validate(warehouse_a, warehouse_b)
    gcode = _norm_code(group_code)
    mode_key = _norm_mode(mode)
    basis_key = _norm_qty_basis(qty_basis)
    item_raw = str(item_q or "").strip()

    q_label = ""
    exact: str | None = None
    code_set: set[str] | None = None
    if item_raw:
        with oracle_session():
            q_label, exact, code_set = _resolve_item_q(item_raw)

    cache_key = _cache_key(
        wh_a,
        wh_b,
        group_code=gcode,
        item_q=q_label or "",
        qty_basis=basis_key,
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return _apply_mode(cached, mode_key)

    def _job_qty(wh: str) -> Callable[[], dict[str, float]]:
        def run() -> dict[str, float]:
            with oracle_session():
                return _fetch_wh_qty(wh, group_code=gcode, item_code=exact or "")

        return run

    qty_a_raw, qty_b_raw = _run_parallel(
        [_job_qty(wh_a), _job_qty(wh_b)],
        max_workers=2,
        timeout_sec=120.0,
    )
    qty_a_raw = qty_a_raw or {}
    qty_b_raw = qty_b_raw or {}

    # القيادة بأصناف المستودع الأول ذات الرصيد الحالي الموجب
    all_codes = set(qty_a_raw.keys())
    if code_set is not None and not exact:
        all_codes &= code_set
    elif exact:
        if exact in qty_a_raw:
            all_codes = {exact}
        else:
            all_codes = set()

    pend_a: dict[str, float] = {}
    pend_b: dict[str, float] = {}
    if basis_key == "avail" and all_codes:

        def _job_pend(wh: str) -> Callable[[], dict[str, float]]:
            def run() -> dict[str, float]:
                with oracle_session():
                    return _fetch_wh_pending(wh)

            return run

        pend_a, pend_b = _run_parallel(
            [_job_pend(wh_a), _job_pend(wh_b)],
            max_workers=2,
            timeout_sec=120.0,
        )
        pend_a = pend_a or {}
        pend_b = pend_b or {}

    qty_a_full = _apply_qty_basis(
        qty_a_raw, pend_a, qty_basis=basis_key, codes=all_codes
    )
    qty_b_full = _apply_qty_basis(
        qty_b_raw, pend_b, qty_basis=basis_key, codes=all_codes
    )

    # للمتوفّر: استبعد ما أصبح صفراً بعد خصم غير المرحّل من الأول
    if basis_key == "avail":
        all_codes = {c for c in all_codes if _f(qty_a_full.get(c)) > _EPS}

    with oracle_session():
        meta = _hydrate_items(sorted(all_codes))
        pack_map = fetch_item_max_pack_map(sorted(all_codes)) or {}

    rows: list[dict[str, Any]] = []
    for code in all_codes:
        qa_stock = _f(qty_a_full.get(code))
        if qa_stock <= _EPS:
            continue
        qb_stock = _f(qty_b_full.get(code))
        kind = _row_kind(qa_stock, qb_stock)
        info = meta.get(code) or {}
        pack_info = pack_map.get(code) or {}
        try:
            pack_size = float(pack_info.get("pack") or 0)
        except (TypeError, ValueError):
            pack_size = 0.0
        unit = str(pack_info.get("unit") or "").strip()
        qa = _to_pack_qty(qa_stock, pack_size)
        qb = _to_pack_qty(qb_stock, pack_size)
        gap_stock = _f(abs(qa_stock - qb_stock))
        gap = _f(abs(qa - qb))
        rows.append(
            {
                "item_code": code,
                "item_name": info.get("item_name") or code,
                "g_code": info.get("g_code") or "",
                "g_name": info.get("g_name") or "",
                "unit": unit or "—",
                "pack_size": pack_size if pack_size > 1 else 1.0,
                "qty_a": qa,
                "qty_b": qb,
                "qty_a_stock": qa_stock,
                "qty_b_stock": qb_stock,
                "qty_a_display": _fmt_qty(qa),
                "qty_b_display": _fmt_qty(qb),
                "qty_gap": gap,
                "qty_gap_stock": gap_stock,
                "qty_gap_display": _fmt_qty(gap),
                "kind": kind,
                "kind_label": _kind_label(kind, wh_b_label=wh_b_name or wh_b),
            }
        )

    rows.sort(
        key=lambda r: (
            0 if r["kind"] == "zero_b" else 1 if r["kind"] == "diff" else 2,
            -float(r.get("qty_gap_stock") or r.get("qty_gap") or 0),
            str(r.get("item_code") or ""),
        )
    )

    base = {
        "filters": {
            "warehouse_a": wh_a,
            "warehouse_b": wh_b,
            "wh_a_name": wh_a_name or wh_a,
            "wh_b_name": wh_b_name or wh_b,
            "group": gcode,
            "item_q": q_label,
            "mode": "all",
            "mode_label": _MODES["all"],
            "qty_basis": basis_key,
            "qty_basis_label": _QTY_BASES[basis_key],
        },
        "rows": rows,
        "modes": [{"code": k, "label": v} for k, v in _MODES.items()],
        "qty_bases": [{"code": k, "label": v} for k, v in _QTY_BASES.items()],
    }
    base.update(_totals(rows))
    try:
        cache.set(cache_key, base, _CACHE_TTL)
    except Exception:  # noqa: BLE001
        pass
    return _apply_mode(base, mode_key)


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    zero_b = both = diff = 0
    sum_a = sum_b = 0.0
    for row in rows:
        kind = row.get("kind")
        if kind == "zero_b":
            zero_b += 1
        elif kind == "both":
            both += 1
        elif kind == "diff":
            diff += 1
        sum_a += float(row.get("qty_a") or 0)
        sum_b += float(row.get("qty_b") or 0)
    kpis = {
        "item_count": len(rows),
        "zero_b_count": zero_b,
        "both_count": both,
        "diff_count": diff,
        "mismatch_count": zero_b + diff,
        "qty_a": _f(sum_a),
        "qty_b": _f(sum_b),
        "qty_a_display": _fmt_qty(sum_a),
        "qty_b_display": _fmt_qty(sum_b),
    }
    totals = {
        "qty_a_display": _fmt_qty(sum_a),
        "qty_b_display": _fmt_qty(sum_b),
        "qty_gap_display": _fmt_qty(abs(sum_a - sum_b)),
    }
    return {"kpis": kpis, "totals": totals}


def _apply_mode(report: dict[str, Any], mode: str) -> dict[str, Any]:
    mode_key = _norm_mode(mode)
    all_rows = list(report.get("rows") or [])
    base_kpis = report.get("kpis") or {}
    if "zero_b_count_all" not in base_kpis:
        full = _totals(all_rows)["kpis"]
        for key in (
            "item_count",
            "zero_b_count",
            "both_count",
            "diff_count",
            "mismatch_count",
        ):
            base_kpis[f"{key}_all"] = full.get(key, 0)
        for key, val in full.items():
            base_kpis.setdefault(key, val)
        report = dict(report)
        report["kpis"] = base_kpis

    rows = [r for r in all_rows if _row_matches(str(r.get("kind") or ""), mode_key)]
    if mode_key in ("zero_b", "diff"):
        rows.sort(
            key=lambda r: (
                -float(r.get("qty_gap_stock") or r.get("qty_gap") or 0),
                -float(r.get("qty_a_stock") or r.get("qty_a") or 0),
                str(r.get("item_code") or ""),
            )
        )
    truncated = False
    if len(rows) > _MAX_ROWS:
        rows = rows[:_MAX_ROWS]
        truncated = True
    stats = _totals(rows)
    kpis = stats["kpis"]
    for key in (
        "item_count",
        "zero_b_count",
        "both_count",
        "diff_count",
        "mismatch_count",
    ):
        kpis[f"{key}_all"] = base_kpis.get(f"{key}_all", 0)
    kpis["truncated"] = truncated
    kpis["max_rows"] = _MAX_ROWS
    matched_n = sum(
        1 for r in all_rows if _row_matches(str(r.get("kind") or ""), mode_key)
    )
    kpis["matched_count"] = matched_n
    kpis["item_count"] = len(rows)

    out = dict(report)
    filters = dict(out.get("filters") or {})
    filters["mode"] = mode_key
    filters["mode_label"] = _MODES.get(mode_key, mode_key)
    basis = _norm_qty_basis(filters.get("qty_basis"))
    filters["qty_basis"] = basis
    filters["qty_basis_label"] = _QTY_BASES.get(basis, basis)
    out["filters"] = filters
    out["rows"] = rows
    out["kpis"] = kpis
    out["totals"] = stats["totals"]
    out["modes"] = [{"code": k, "label": v} for k, v in _MODES.items()]
    out["qty_bases"] = [{"code": k, "label": v} for k, v in _QTY_BASES.items()]
    return out


def build_wh_qty_compare_excel(report: dict[str, Any]) -> HttpResponse:
    rows = report.get("rows") or []
    filters = report.get("filters") or {}
    kpis = report.get("kpis") or {}
    a_code = filters.get("warehouse_a") or "أ"
    b_code = filters.get("warehouse_b") or "ب"
    a_label = f"مخزن {a_code}"
    b_label = f"مخزن {b_code}"
    basis_label = filters.get("qty_basis_label") or _QTY_BASES["avail"]
    buf = io.StringIO()
    buf.write("\ufeff")
    buf.write(
        "<html xmlns:o=\"urn:schemas-microsoft-com:office:office\" "
        "xmlns:x=\"urn:schemas-microsoft-com:office:excel\" "
        "xmlns=\"http://www.w3.org/TR/REC-html40\">"
        "<head><meta charset=\"utf-8\">"
        "<!--[if gte mso 9]><xml><x:ExcelWorkbook><x:ExcelWorksheets>"
        "<x:ExcelWorksheet><x:Name>مقارنة كميات</x:Name>"
        "<x:WorksheetOptions><x:DisplayRightToLeft/></x:WorksheetOptions>"
        "</x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
        "<style>"
        "table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:11px;}"
        "th,td{border:1px solid #94a3b8;padding:4px 6px;white-space:nowrap;}"
        "th{background:#1e3a5f;color:#fff;font-weight:700;}"
        "th.a{background:#166534;} th.b{background:#9a3412;}"
        "td.num{mso-number-format:'\\#\\,\\#\\#0\\.000';text-align:left;}"
        "td.int{mso-number-format:'\\#\\,\\#\\#0';text-align:left;}"
        "tr.even td{background:#f8fafc;}"
        "tr.foot td{background:#dbeafe;font-weight:800;}"
        "caption{font-family:Tahoma,Arial;font-size:13px;font-weight:700;"
        "text-align:right;margin:8px 0;}"
        ".sub{font-size:10px;color:#475569;font-weight:400;}"
        "</style></head><body dir=\"rtl\">"
    )
    buf.write(
        "<table><caption>مقارنة كميات بين مستودعين"
        f'<br><span class="sub">{escape(str(a_label))} ↔ {escape(str(b_label))}'
        f" · {escape(str(basis_label))}"
        f" · {escape(str(filters.get('mode_label') or ''))}"
        f" · {escape(str(kpis.get('item_count') or 0))} صنف</span></caption>"
        "<thead><tr>"
        "<th>#</th><th>رقم الصنف</th><th>اسم الصنف</th><th>المجموعة</th>"
        "<th>الوحدة</th><th>الحالة</th>"
        f"<th class=\"a\">{escape(str(a_label))} — {escape(str(basis_label))}</th>"
        f"<th class=\"b\">{escape(str(b_label))} — {escape(str(basis_label))}</th>"
        "<th>الفرق</th>"
        "</tr></thead><tbody>"
    )
    for i, row in enumerate(rows, 1):
        even = ' class="even"' if i % 2 == 0 else ""
        buf.write(f"<tr{even}>")
        buf.write(f'<td class="int">{i}</td>')
        buf.write(f"<td>{escape(str(row.get('item_code') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('item_name') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('g_name') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('unit') or ''))}</td>")
        buf.write(f"<td>{escape(str(row.get('kind_label') or ''))}</td>")
        buf.write(f'<td class="num">{_f(row.get("qty_a"))}</td>')
        buf.write(f'<td class="num">{_f(row.get("qty_b"))}</td>')
        buf.write(f'<td class="num">{_f(row.get("qty_gap"))}</td>')
        buf.write("</tr>")
    buf.write(
        '<tr class="foot"><td></td><td></td><td>الإجمالي</td><td></td><td></td><td></td>'
        f'<td class="num">{_f(kpis.get("qty_a"))}</td>'
        f'<td class="num">{_f(kpis.get("qty_b"))}</td>'
        f'<td class="num">{_f(abs(_f(kpis.get("qty_a")) - _f(kpis.get("qty_b"))))}</td>'
        "</tr></tbody></table></body></html>"
    )
    payload = buf.getvalue().encode("utf-8")
    filename = (
        f"wh-qty-compare-{filters.get('warehouse_a') or ''}"
        f"-{filters.get('warehouse_b') or ''}.xls"
    )
    resp = HttpResponse(payload, content_type="application/vnd.ms-excel; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp
