"""توزيع مصاريف المستودع بناءً على تحويلات المخازن إلى الفروع."""

from __future__ import annotations

from datetime import date
from typing import Any

from django.core.cache import cache

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _branch_names,
    _date_params,
    _fetch_all,
    _hung_ok,
    _schema,
    oracle_enabled,
)

_CACHE_TTL = 900
_CACHE_VER = "v4"
_DEFAULT_CC = "103"


def _f(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _fmt_money(value: Any) -> str:
    return f"{_f(value):,.2f}"


def _fmt_qty(value: Any) -> str:
    qty = float(value or 0)
    if abs(qty - round(qty)) < 1e-9:
        return f"{int(round(qty)):,}"
    return f"{qty:,.2f}"


def _norm_code(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].replace("-", "").isdigit():
        text = text[:-2]
    return text


def _parse_wh_codes(raw: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    text = str(raw or "").replace("،", ",")
    for part in text.split(","):
        code = _norm_code(part)
        if not code:
            continue
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _validate(date_from, date_to):
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")
    if (d_to - d_from).days > 366:
        raise OracleStockError("الفترة القصوى سنة واحدة.")
    return d_from, d_to


def _fetch_wh_names(wh_codes: list[str]) -> dict[str, str]:
    if not wh_codes:
        return {}
    schema = _schema()
    params: dict[str, Any] = {}
    keys: list[str] = []
    for i, code in enumerate(wh_codes):
        key = f"w{i}"
        params[key] = code
        keys.append(f":{key}")
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(w.W_CODE) AS W_CODE,
               NVL(NULLIF(TRIM(w.W_NAME), ''), TO_CHAR(w.W_CODE)) AS W_NAME
        FROM {schema}.WAREHOUSE_DETAILS w
        WHERE TO_CHAR(w.W_CODE) IN ({", ".join(keys)})
        """,
        params,
    )
    out: dict[str, str] = {}
    for row in rows or []:
        code = _norm_code(row.get("W_CODE"))
        if code:
            out[code] = str(row.get("W_NAME") or "").strip() or code
    return out


def _fetch_transfer_rows(
    date_from: date,
    date_to: date,
    *,
    wh_codes: list[str],
    posted_only: bool,
) -> list[dict]:
    if not wh_codes:
        return []
    schema = _schema()
    dates = _date_params(date_from, date_to)
    params: dict[str, Any] = {
        "d_from": dates["d_from"],
        "d_to_excl": dates["d_to_excl"],
    }
    wh_keys: list[str] = []
    for i, code in enumerate(wh_codes):
        key = f"w{i}"
        params[key] = code
        wh_keys.append(f":{key}")
    posted_sql = "AND NVL(m.PROCESSED, 0) = 1" if posted_only else ""
    return _fetch_all(
        f"""
        SELECT /*+ LEADING(m d) USE_NL(d) INDEX(d INDX_SER_WHTRNS_DTL) */
               TO_CHAR(m.TR_NO) AS TR_NO,
               TO_CHAR(m.TR_SER) AS TR_SER,
               TO_CHAR(m.TR_DATE, 'YYYY-MM-DD') AS TR_DATE,
               TO_CHAR(m.F_W_CODE) AS SRC_WH_CODE,
               TO_CHAR(m.T_W_CODE) AS DST_WH_CODE,
               TO_CHAR(tw.CONN_BRN_NO) AS DST_BRN,
               NVL(NULLIF(TRIM(tw.W_NAME), ''), TO_CHAR(m.T_W_CODE)) AS DST_WH_NAME,
               ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL,
               ROUND(SUM(NVL(d.I_QTY, 0) * NVL(d.I_PRICE, 0)), 2) AS AMT_TOTAL,
               COUNT(DISTINCT d.I_CODE) AS ITEM_COUNT
        FROM {schema}.IAS_WHTRNS_MST m
        JOIN {schema}.IAS_WHTRNS_DTL d
          ON d.TR_SER = m.TR_SER
        LEFT JOIN {schema}.WAREHOUSE_DETAILS tw
          ON tw.W_CODE = m.T_W_CODE
        WHERE m.TR_DATE >= :d_from
          AND m.TR_DATE < :d_to_excl
          AND m.TR_INOUT_TYPE = 1
          AND {_hung_ok("m")}
          {posted_sql}
          AND TO_CHAR(m.F_W_CODE) IN ({", ".join(wh_keys)})
        GROUP BY m.TR_NO, m.TR_SER, m.TR_DATE, m.F_W_CODE, m.T_W_CODE,
                 tw.CONN_BRN_NO, tw.W_NAME
        HAVING ROUND(SUM(NVL(d.I_QTY, 0)), 2) <> 0
            OR ROUND(SUM(NVL(d.I_QTY, 0) * NVL(d.I_PRICE, 0)), 2) <> 0
        """,
        params,
    )


def _fetch_cc_name(cc_code: str) -> str:
    code = _norm_code(cc_code)
    if not code:
        return ""
    schema = _schema()
    rows = _fetch_all(
        f"""
        SELECT NVL(NULLIF(TRIM(CC_A_NAME), ''), TO_CHAR(CC_CODE)) AS CC_NAME
        FROM {schema}.COST_CENTERS
        WHERE TO_CHAR(CC_CODE) = :cc
          AND ROWNUM = 1
        """,
        {"cc": code},
    )
    if not rows:
        return code
    return str(rows[0].get("CC_NAME") or "").strip() or code


def _fetch_cc_expense_total(
    date_from: date,
    date_to: date,
    *,
    cc_code: str,
) -> dict[str, Any]:
    """إجمالي مصاريف (حسابات تبدأ بـ 5) على مركز التكلفة في الفترة."""
    code = _norm_code(cc_code) or _DEFAULT_CC
    schema = _schema()
    dates = _date_params(date_from, date_to)
    # فهرس CC ثم التاريخ — INDX_IASPOSTDTL_CCCODE / INDX_IASPOSTDTL_CS
    rows = _fetch_all(
        f"""
        SELECT /*+ INDEX(p INDX_IASPOSTDTL_CCCODE) */
               ROUND(SUM(NVL(p.DR_AMT, 0) - NVL(p.CR_AMT, 0)), 2) AS EXP_NET,
               ROUND(SUM(NVL(p.DR_AMT, 0)), 2) AS EXP_DR,
               COUNT(*) AS LINE_COUNT
        FROM {schema}.IAS_POST_DTL p
        WHERE p.DOC_DATE >= :d_from
          AND p.DOC_DATE < :d_to_excl
          AND TO_CHAR(p.CC_CODE) = :cc
          AND TO_CHAR(p.A_CODE) LIKE '5%'
          AND ABS(NVL(p.AMT, 0)) < 1000000000
        """,
        {
            "d_from": dates["d_from"],
            "d_to_excl": dates["d_to_excl"],
            "cc": code,
        },
    )
    row = (rows or [{}])[0] or {}
    exp_net = _f(row.get("EXP_NET"))
    exp_dr = _f(row.get("EXP_DR"))
    # صافي مدين للمصروف؛ إن سالب نأخذ إجمالي المدين
    amount = exp_net if exp_net > 0 else max(exp_dr, 0.0)
    return {
        "cc_code": code,
        "cc_name": _fetch_cc_name(code),
        "amount": amount,
        "amount_display": _fmt_money(amount),
        "line_count": int(row.get("LINE_COUNT") or 0),
        "exp_net": exp_net,
        "exp_dr": exp_dr,
    }


def build_warehouse_expense_distribution(
    date_from,
    date_to,
    *,
    source_warehouses: str,
    expense_total: float = 0.0,
    posted_only: bool = True,
    source_wh_filter: str = "",
    cc_code: str = _DEFAULT_CC,
) -> dict[str, Any]:
    d_from, d_to = _validate(date_from, date_to)
    wh_codes = _parse_wh_codes(source_warehouses)
    manual_expense = _f(expense_total)
    cc = _norm_code(cc_code) or _DEFAULT_CC
    src_filter = _norm_code(source_wh_filter)
    if src_filter and src_filter not in wh_codes:
        src_filter = ""

    cache_key = (
        f"wh-exp:{_CACHE_VER}:{d_from}:{d_to}:{','.join(wh_codes)}:"
        f"{int(posted_only)}:man={manual_expense:.2f}:cc={cc}:src={src_filter or 'ALL'}"
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached

    cc_expense = _fetch_cc_expense_total(d_from, d_to, cc_code=cc)
    if manual_expense > 0:
        expense = manual_expense
        expense_source = "manual"
    else:
        expense = _f(cc_expense.get("amount"))
        expense_source = "cost_center"

    raw = _fetch_transfer_rows(
        d_from,
        d_to,
        wh_codes=wh_codes,
        posted_only=posted_only,
    )
    branch_names = _branch_names()
    wh_names = _fetch_wh_names(wh_codes)

    # خيارات فلتر المخزن المصدر (من كل التحويلات قبل الفلتر)
    source_options_map: dict[str, dict[str, Any]] = {}
    for row in raw:
        src_wh = _norm_code(row.get("SRC_WH_CODE"))
        if not src_wh:
            continue
        bucket = source_options_map.setdefault(
            src_wh,
            {
                "code": src_wh,
                "name": wh_names.get(src_wh) or src_wh,
                "transfer_count": 0,
            },
        )
        bucket["transfer_count"] += 1

    if src_filter:
        raw = [r for r in raw if _norm_code(r.get("SRC_WH_CODE")) == src_filter]

    branch_buckets: dict[str, dict[str, Any]] = {}
    source_stats: dict[str, dict[str, Any]] = {}
    total_amt = 0.0
    total_qty = 0.0
    total_transfers = 0

    for row in raw:
        tr_no = _norm_code(row.get("TR_NO"))
        tr_ser = _norm_code(row.get("TR_SER"))
        if not tr_no and not tr_ser:
            continue
        src_wh = _norm_code(row.get("SRC_WH_CODE"))
        dst_wh = _norm_code(row.get("DST_WH_CODE"))
        dst_brn = _norm_code(row.get("DST_BRN"))
        dst_wh_name = str(row.get("DST_WH_NAME") or "").strip() or dst_wh or "—"
        dst_name = (
            branch_names.get(dst_brn)
            or (f"فرع {dst_brn}" if dst_brn else "")
            or dst_wh_name
            or "غير محدد"
        )
        amount = _f(row.get("AMT_TOTAL"))
        qty = _f(row.get("QTY_TOTAL"))
        item_count = int(row.get("ITEM_COUNT") or 0)

        branch_key = dst_brn or f"wh:{dst_wh}" or "unknown"
        branch = branch_buckets.setdefault(
            branch_key,
            {
                "branch_code": dst_brn,
                "branch_name": dst_name,
                "transfer_count": 0,
                "amount_total": 0.0,
                "qty_total": 0.0,
                "item_total": 0,
            },
        )
        branch["transfer_count"] += 1
        branch["amount_total"] = round(branch["amount_total"] + amount, 2)
        branch["qty_total"] = round(branch["qty_total"] + qty, 2)
        branch["item_total"] += item_count

        src_bucket = source_stats.setdefault(
            src_wh,
            {
                "warehouse_code": src_wh,
                "warehouse_name": wh_names.get(src_wh) or src_wh,
                "transfer_count": 0,
                "amount_total": 0.0,
            },
        )
        src_bucket["transfer_count"] += 1
        src_bucket["amount_total"] = round(src_bucket["amount_total"] + amount, 2)

        total_amt = round(total_amt + amount, 2)
        total_qty = round(total_qty + qty, 2)
        total_transfers += 1

    by_branch: list[dict] = []
    for row in branch_buckets.values():
        if total_amt > 0:
            share = row["amount_total"] / total_amt
            ratio_basis = "amount"
        else:
            share = (
                row["transfer_count"] / total_transfers if total_transfers > 0 else 0.0
            )
            ratio_basis = "count"
        allocated = round(expense * share, 2)
        by_branch.append(
            {
                **row,
                "amount_display": _fmt_money(row["amount_total"]),
                "qty_display": _fmt_qty(row["qty_total"]),
                "share_pct": round(share * 100.0, 2),
                "share_display": f"{round(share * 100.0, 2):,.2f}%",
                "allocated_expense": allocated,
                "allocated_display": _fmt_money(allocated),
                "ratio_basis": ratio_basis,
            }
        )
    by_branch.sort(
        key=lambda r: (-r["amount_total"], -r["transfer_count"], r["branch_code"] or "")
    )

    source_rows = []
    for row in sorted(
        source_stats.values(),
        key=lambda r: (-r["amount_total"], -r["transfer_count"], r["warehouse_code"]),
    ):
        source_rows.append(
            {
                **row,
                "amount_display": _fmt_money(row["amount_total"]),
            }
        )

    source_options = sorted(
        source_options_map.values(),
        key=lambda r: (r["name"], r["code"]),
    )

    result = {
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "filters": {
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat(),
            "source_warehouses": ", ".join(wh_codes),
            "source_wh_filter": src_filter,
            "source_wh_filter_name": (
                wh_names.get(src_filter) or src_filter if src_filter else ""
            ),
            "posted_only": posted_only,
            "expense_total": expense,
            "expense_display": _fmt_money(expense),
            "expense_source": expense_source,
            "manual_expense": manual_expense,
            "cc_code": cc_expense.get("cc_code") or cc,
            "cc_name": cc_expense.get("cc_name") or cc,
            "cc_expense_display": cc_expense.get("amount_display") or _fmt_money(0),
            "cc_line_count": int(cc_expense.get("line_count") or 0),
        },
        "source_options": source_options,
        "kpis": {
            "transfer_count": total_transfers,
            "branch_count": len(by_branch),
            "amount_total": total_amt,
            "amount_display": _fmt_money(total_amt),
            "qty_total": total_qty,
            "qty_display": _fmt_qty(total_qty),
            "ratio_basis_label": "المبلغ" if total_amt > 0 else "عدد التحويلات",
            "allocated_total": _fmt_money(sum(r["allocated_expense"] for r in by_branch)),
            "expense_source_label": (
                f"إدخال يدوي"
                if expense_source == "manual"
                else f"مركز {cc_expense.get('cc_code') or cc}"
            ),
        },
        "by_branch": by_branch,
        "by_source": source_rows,
    }
    try:
        cache.set(cache_key, result, _CACHE_TTL)
    except Exception:
        pass
    return result
