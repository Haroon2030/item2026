"""توزيع المصاريف على الأقسام/المراكز (مراكز التكلفة 202–240)."""

from __future__ import annotations

import logging
from typing import Any

from django.core.cache import cache

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _fetch_all,
    _schema,
)

logger = logging.getLogger(__name__)

_CACHE_TTL = 900
_CC_FROM = 202
_CC_TO = 240


def _f(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _fmt_money(value: float) -> str:
    return f"{_f(value):,.2f}"


def _fmt_pct(value: float) -> str:
    return f"{_f(value):.1f}%"


def _filters(
    *,
    branch_code: str = "",
    posted_only: bool = False,
) -> tuple[str, dict[str, Any]]:
    parts: list[str] = [
        "TO_CHAR(a.A_CODE) LIKE '5%'",
        "p.CC_CODE IS NOT NULL",
        "REGEXP_LIKE(TO_CHAR(p.CC_CODE), '^[0-9]+$')",
        "TO_NUMBER(TO_CHAR(p.CC_CODE)) BETWEEN :cc_from AND :cc_to",
        "p.DOC_TYPE <> 0",
    ]
    params: dict[str, Any] = {"cc_from": _CC_FROM, "cc_to": _CC_TO}
    if posted_only:
        parts.append("NVL(p.DOC_POST, 0) = 1")
    brn = str(branch_code or "").strip()
    if brn:
        parts.append("TO_CHAR(p.BRN_NO) = :brn")
        params["brn"] = brn
    return " AND ".join(parts), params


def fetch_expense_departments() -> list[dict]:
    """أقسام/مراكز التكلفة من 202 إلى 240."""
    cache_key = f"expdist:depts:{_CC_FROM}:{_CC_TO}:v1"
    cached = cache.get(cache_key)
    if isinstance(cached, list):
        return cached
    sch = _schema()
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(CC_CODE) AS CC_CODE, CC_A_NAME
        FROM {sch}.COST_CENTERS
        WHERE NVL(INACTIVE, 0) = 0
          AND REGEXP_LIKE(TO_CHAR(CC_CODE), '^[0-9]+$')
          AND TO_NUMBER(TO_CHAR(CC_CODE)) BETWEEN :a AND :b
        ORDER BY TO_NUMBER(TO_CHAR(CC_CODE))
        """,
        {"a": _CC_FROM, "b": _CC_TO},
    )
    out = []
    for row in rows:
        code = str(row.get("CC_CODE") or "").strip()
        if not code:
            continue
        out.append(
            {
                "code": code,
                "name": str(row.get("CC_A_NAME") or "").strip() or code,
            }
        )
    try:
        cache.set(cache_key, out, 3600)
    except Exception:
        pass
    return out


def build_expense_distribution(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    posted_only: bool = False,
    hide_zero: bool = True,
) -> dict[str, Any]:
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")

    brn = str(branch_code or "").strip()
    key = (
        f"expdist:v1:{d_from.isoformat()}:{d_to.isoformat()}:"
        f"{brn or '-'}:{int(posted_only)}:{int(hide_zero)}"
    )
    cached = cache.get(key)
    if isinstance(cached, dict):
        return cached

    sch = _schema()
    extra, params = _filters(branch_code=brn, posted_only=posted_only)
    params.update({"dfrom": d_from, "dto": d_to})

    raw = _fetch_all(
        f"""
        SELECT /*+ USE_HASH(p a cc) */
               TO_CHAR(p.CC_CODE) AS CC_CODE,
               MAX(NVL(cc.CC_A_NAME, TO_CHAR(p.CC_CODE))) AS CC_NAME,
               TO_CHAR(a.A_CODE) AS A_CODE,
               MAX(a.A_NAME) AS A_NAME,
               SUM(NVL(p.DR_AMT, 0) - NVL(p.CR_AMT, 0)) AS NET_EXP,
               COUNT(*) AS LINE_CNT
        FROM {sch}.IAS_POST_DTL p
        JOIN {sch}.ACCOUNT a
          ON a.A_CODE = p.A_CODE
        LEFT JOIN {sch}.COST_CENTERS cc
          ON TO_CHAR(cc.CC_CODE) = TO_CHAR(p.CC_CODE)
        WHERE {extra}
          AND p.DOC_DATE >= :dfrom
          AND p.DOC_DATE <= :dto
        GROUP BY p.CC_CODE, a.A_CODE
        HAVING ABS(SUM(NVL(p.DR_AMT, 0) - NVL(p.CR_AMT, 0))) > 0.0005
        """,
        params,
    )

    dept_map: dict[str, dict] = {}
    detail_rows: list[dict] = []
    grand = 0.0

    for row in raw:
        cc = str(row.get("CC_CODE") or "").strip()
        if not cc:
            continue
        net = _f(row.get("NET_EXP"))
        if hide_zero and abs(net) < 0.005:
            continue
        code = str(row.get("A_CODE") or "").strip()
        name = str(row.get("A_NAME") or "").strip() or code
        cc_name = str(row.get("CC_NAME") or "").strip() or cc
        lines = int(row.get("LINE_CNT") or 0)

        bucket = dept_map.get(cc)
        if bucket is None:
            bucket = {
                "cc_code": cc,
                "cc_name": cc_name,
                "amount": 0.0,
                "line_count": 0,
                "account_count": 0,
            }
            dept_map[cc] = bucket
        bucket["amount"] = round(bucket["amount"] + net, 2)
        bucket["line_count"] += lines
        bucket["account_count"] += 1
        grand = round(grand + net, 2)

        detail_rows.append(
            {
                "cc_code": cc,
                "cc_name": cc_name,
                "account_code": code,
                "account_name": name,
                "amount": net,
                "amount_display": _fmt_money(net),
                "line_count": lines,
            }
        )

    # Ensure all departments 202-240 appear (even zero) when hide_zero is False
    if not hide_zero:
        for d in fetch_expense_departments():
            code = d["code"]
            if code not in dept_map:
                dept_map[code] = {
                    "cc_code": code,
                    "cc_name": d["name"],
                    "amount": 0.0,
                    "line_count": 0,
                    "account_count": 0,
                }

    dept_rows: list[dict] = []
    for code in sorted(dept_map.keys(), key=lambda x: int(x) if x.isdigit() else x):
        b = dept_map[code]
        amt = _f(b["amount"])
        if hide_zero and abs(amt) < 0.005:
            continue
        pct = round((amt / grand * 100.0), 1) if abs(grand) > 0.0005 else 0.0
        dept_rows.append(
            {
                "cc_code": b["cc_code"],
                "cc_name": b["cc_name"],
                "amount": amt,
                "amount_display": _fmt_money(amt),
                "pct": pct,
                "pct_display": _fmt_pct(pct),
                "line_count": b["line_count"],
                "line_count_display": f"{b['line_count']:,}",
                "account_count": b["account_count"],
                "account_count_display": f"{b['account_count']:,}",
            }
        )

    detail_rows.sort(
        key=lambda r: (
            int(r["cc_code"]) if str(r["cc_code"]).isdigit() else 0,
            str(r["account_code"]),
        )
    )

    top_dept = max(dept_rows, key=lambda r: abs(r["amount"])) if dept_rows else None

    result = {
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "report_title": "توزيع المصاريف على الأقسام (مراكز 202–240)",
        "cc_range_label": f"{_CC_FROM} → {_CC_TO}",
        "currency": "SAR",
        "departments": dept_rows,
        "details": detail_rows,
        "totals": {
            "dept_count": len(dept_rows),
            "dept_count_display": f"{len(dept_rows):,}",
            "detail_count": len(detail_rows),
            "detail_count_display": f"{len(detail_rows):,}",
            "amount": grand,
            "amount_display": _fmt_money(grand),
            "top_dept_code": (top_dept or {}).get("cc_code") or "",
            "top_dept_name": (top_dept or {}).get("cc_name") or "—",
            "top_dept_amount_display": (top_dept or {}).get("amount_display") or "0.00",
        },
        "filters": {
            "branch": brn,
            "posted_only": posted_only,
            "hide_zero": hide_zero,
            "cc_from": _CC_FROM,
            "cc_to": _CC_TO,
        },
    }
    try:
        cache.set(key, result, _CACHE_TTL)
    except Exception:
        pass
    return result


__all__ = [
    "build_expense_distribution",
    "fetch_expense_departments",
]
