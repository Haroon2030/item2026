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
_CACHE_VER = "v1"


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


def _parse_wh_codes(raw: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    text = str(raw or "").replace("،", ",")
    for part in text.split(","):
        code = part.strip()
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
               TO_CHAR(m.TR_SER) AS TR_SER,
               TO_CHAR(m.TR_DATE, 'YYYY-MM-DD') AS TR_DATE,
               TO_CHAR(m.F_W_CODE) AS SRC_WH_CODE,
               TO_CHAR(m.T_W_CODE) AS DST_WH_CODE,
               TO_CHAR(NVL(tw.CONN_BRN_NO, NVL(m.DOC_BRN_NO, m.BRN_NO))) AS DST_BRN,
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
        GROUP BY m.TR_SER, m.TR_DATE, m.F_W_CODE, m.T_W_CODE,
                 NVL(tw.CONN_BRN_NO, NVL(m.DOC_BRN_NO, m.BRN_NO))
        HAVING ROUND(SUM(NVL(d.I_QTY, 0)), 2) <> 0
            OR ROUND(SUM(NVL(d.I_QTY, 0) * NVL(d.I_PRICE, 0)), 2) <> 0
        """,
        params,
    )


def build_warehouse_expense_distribution(
    date_from,
    date_to,
    *,
    source_warehouses: str,
    expense_total: float = 0.0,
    posted_only: bool = True,
) -> dict[str, Any]:
    d_from, d_to = _validate(date_from, date_to)
    wh_codes = _parse_wh_codes(source_warehouses)
    expense = _f(expense_total)
    cache_key = (
        f"wh-exp:{_CACHE_VER}:{d_from}:{d_to}:{','.join(wh_codes)}:"
        f"{int(posted_only)}:{expense:.2f}"
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached

    raw = _fetch_transfer_rows(
        d_from,
        d_to,
        wh_codes=wh_codes,
        posted_only=posted_only,
    )
    branch_names = _branch_names()
    branch_buckets: dict[str, dict[str, Any]] = {}
    transfers: list[dict] = []
    source_stats: dict[str, dict[str, Any]] = {}
    total_amt = 0.0
    total_qty = 0.0
    total_transfers = 0

    for row in raw:
        tr_ser = str(row.get("TR_SER") or "").strip()
        if not tr_ser:
            continue
        src_wh = str(row.get("SRC_WH_CODE") or "").strip()
        dst_brn = str(row.get("DST_BRN") or "").strip()
        dst_name = branch_names.get(dst_brn) or (f"فرع {dst_brn}" if dst_brn else "غير محدد")
        amount = _f(row.get("AMT_TOTAL"))
        qty = _f(row.get("QTY_TOTAL"))
        item_count = int(row.get("ITEM_COUNT") or 0)

        transfers.append(
            {
                "tr_ser": tr_ser,
                "tr_date": str(row.get("TR_DATE") or "").strip(),
                "source_wh_code": src_wh,
                "dest_wh_code": str(row.get("DST_WH_CODE") or "").strip(),
                "dest_branch_code": dst_brn,
                "dest_branch_name": dst_name,
                "amount": amount,
                "amount_display": _fmt_money(amount),
                "qty_total": qty,
                "qty_display": _fmt_qty(qty),
                "item_count": item_count,
            }
        )

        branch = branch_buckets.setdefault(
            dst_brn,
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
            {"warehouse_code": src_wh, "transfer_count": 0, "amount_total": 0.0},
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
        key=lambda r: (-r["amount_total"], -r["transfer_count"], r["branch_code"])
    )
    transfers.sort(key=lambda r: (r["tr_date"], r["tr_ser"]), reverse=True)

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

    result = {
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "filters": {
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat(),
            "source_warehouses": ", ".join(wh_codes),
            "posted_only": posted_only,
            "expense_total": expense,
            "expense_display": _fmt_money(expense),
        },
        "kpis": {
            "transfer_count": total_transfers,
            "branch_count": len(by_branch),
            "amount_total": total_amt,
            "amount_display": _fmt_money(total_amt),
            "qty_total": total_qty,
            "qty_display": _fmt_qty(total_qty),
            "ratio_basis_label": "المبلغ" if total_amt > 0 else "عدد التحويلات",
            "allocated_total": _fmt_money(sum(r["allocated_expense"] for r in by_branch)),
        },
        "by_branch": by_branch,
        "by_source": source_rows,
        "transfers": transfers[:300],
    }
    try:
        cache.set(cache_key, result, _CACHE_TTL)
    except Exception:
        pass
    return result
