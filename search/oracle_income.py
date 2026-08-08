"""
قائمة الدخل — مطابقة تقرير أونكس «الأرصدة مع الحركة» عبر IAS_POST_DTL.

جدول الحسابات (افتتاحي / حركة / ختامي) + KPIs.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from django.core.cache import cache

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _branch_names,
    _fetch_all,
    _schema,
)

logger = logging.getLogger(__name__)

_INCOME_CACHE_TTL = 1800
_LOOKUP_TTL = 1800


def _f(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _fmt_money(value: float) -> str:
    return f"{_f(value):,.2f}"


def _split_dr_cr(net: float) -> tuple[float, float]:
    n = _f(net)
    if n >= 0:
        return n, 0.0
    return 0.0, -n


def _period_filters(
    *,
    branch_code: str = "",
    cc_code: str = "",
    posted_only: bool = False,
) -> tuple[str, dict[str, Any]]:
    parts = ["a.A_REPORT = 2"]
    params: dict[str, Any] = {}
    if posted_only:
        parts.append("NVL(p.DOC_POST, 0) = 1")
    brn = str(branch_code or "").strip()
    if brn:
        parts.append("TO_CHAR(p.BRN_NO) = :brn")
        params["brn"] = brn
    cc = str(cc_code or "").strip()
    if cc:
        parts.append("TO_CHAR(p.CC_CODE) = :cc")
        params["cc"] = cc
    return " AND ".join(parts), params


def fetch_income_cost_centers() -> list[dict]:
    """مراكز تكلفة نشطة للفلتر."""
    cache_key = "income:cc:options:v1"
    cached = cache.get(cache_key)
    if isinstance(cached, list):
        return cached
    sch = _schema()
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(CC_CODE) AS CC_CODE, CC_A_NAME
        FROM {sch}.COST_CENTERS
        WHERE NVL(INACTIVE, 0) = 0
        ORDER BY CC_CODE
        """
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
        cache.set(cache_key, out, _LOOKUP_TTL)
    except Exception:
        pass
    return out


def fetch_income_branches() -> list[dict]:
    """فروع من S_BRN للفلتر."""
    names = _branch_names()
    return [
        {"code": code, "name": name}
        for code, name in sorted(names.items(), key=lambda x: x[0])
    ]


def _kind_for_code(code: str) -> str:
    root = (code or "")[:1]
    return {"3": "revenue", "4": "cogs", "5": "expense"}.get(root, "other")


def _fetch_income_base_rows(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    cc_code: str = "",
    posted_only: bool = False,
) -> list[dict]:
    """
    مسح من IAS_POST_DTL مجمّع حسب الحساب.
    مطابق لـ GLS_FETCH_DATA_PKG (افتتاحي / حركة مدين-دائن).
    """
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")

    sch = _schema()
    extra, params = _period_filters(
        branch_code=branch_code,
        cc_code=cc_code,
        posted_only=posted_only,
    )
    params.update({"dfrom": d_from, "dto": d_to})

    # استبعاد قيود تالفة بمبالغ شاذة (مثل فاتورة مبيعات بمليارات مكررة)
    # تُبقي كل القيود المرحّلة وغير المرحّلة الطبيعية
    sane = "ABS(NVL(p.AMT, 0)) < 1000000000"

    return _fetch_all(
        f"""
        SELECT /*+ USE_HASH(p a) */
               TO_CHAR(a.A_CODE) AS A_CODE,
               MAX(a.A_NAME) AS A_NAME,
               NVL(MAX(a.DR), 0) AS DR,
               SUM(
                 CASE
                   WHEN (p.DOC_DATE < :dfrom OR p.DOC_TYPE = 0) AND {sane}
                   THEN NVL(p.AMT, 0)
                   ELSE 0
                 END
               ) AS OPEN_NET,
               SUM(
                 CASE
                   WHEN p.DOC_DATE >= :dfrom
                    AND p.DOC_DATE <= :dto
                    AND p.DOC_TYPE <> 0
                    AND {sane}
                   THEN NVL(p.DR_AMT, 0)
                   ELSE 0
                 END
               ) AS MV_DR,
               SUM(
                 CASE
                   WHEN p.DOC_DATE >= :dfrom
                    AND p.DOC_DATE <= :dto
                    AND p.DOC_TYPE <> 0
                    AND {sane}
                   THEN NVL(p.CR_AMT, 0)
                   ELSE 0
                 END
               ) AS MV_CR,
               SUM(
                 CASE
                   WHEN p.DOC_DATE >= :dfrom
                    AND p.DOC_DATE <= :dto
                    AND p.DOC_TYPE <> 0
                    AND {sane}
                   THEN CASE
                          WHEN NVL(a.DR, 0) = 0 THEN -NVL(p.AMT, 0)
                          ELSE NVL(p.AMT, 0)
                        END
                   ELSE 0
                 END
               ) AS NORM_AMT,
               SUM(
                 CASE
                   WHEN p.DOC_DATE >= :dfrom
                    AND p.DOC_DATE <= :dto
                    AND p.DOC_TYPE <> 0
                    AND {sane}
                   THEN 1
                   ELSE 0
                 END
               ) AS LINE_COUNT
        FROM {sch}.IAS_POST_DTL p
        JOIN {sch}.ACCOUNT a
          ON a.A_CODE = p.A_CODE
        WHERE {extra}
          AND p.DOC_DATE <= :dto
        GROUP BY a.A_CODE
        HAVING
          SUM(
            CASE
              WHEN (p.DOC_DATE < :dfrom OR p.DOC_TYPE = 0) AND {sane}
              THEN NVL(p.AMT, 0)
              ELSE 0
            END
          ) <> 0
          OR SUM(
            CASE
              WHEN p.DOC_DATE >= :dfrom
               AND p.DOC_DATE <= :dto
               AND p.DOC_TYPE <> 0
               AND {sane}
              THEN 1
              ELSE 0
            END
          ) > 0
        """,
        params,
    )


def _row_account(code: str, name: str, open_net: float, mv_dr: float, mv_cr: float) -> dict:
    # أونكس «أرصدة مع الحركة»: تصافي لكل حساب — يظهر المدين أو الدائن فقط
    open_dr, open_cr = _split_dr_cr(open_net)
    mv_net = round(_f(mv_dr) - _f(mv_cr), 2)
    mv_dr_n, mv_cr_n = _split_dr_cr(mv_net)
    close_net = round(_f(open_net) + mv_net, 2)
    close_dr, close_cr = _split_dr_cr(close_net)
    return {
        "account_code": code,
        "account_name": name or code,
        "currency": "SAR",
        "kind": _kind_for_code(code),
        "open_dr": open_dr,
        "open_cr": open_cr,
        "mv_dr": mv_dr_n,
        "mv_cr": mv_cr_n,
        "close_dr": close_dr,
        "close_cr": close_cr,
        "open_dr_display": _fmt_money(open_dr) if open_dr else "",
        "open_cr_display": _fmt_money(open_cr) if open_cr else "",
        "mv_dr_display": _fmt_money(mv_dr_n) if mv_dr_n else "",
        "mv_cr_display": _fmt_money(mv_cr_n) if mv_cr_n else "",
        "close_dr_display": _fmt_money(close_dr) if close_dr else "",
        "close_cr_display": _fmt_money(close_cr) if close_cr else "",
    }


def _aggregate_accounts(base_rows: list[dict]) -> tuple[list[dict], dict]:
    """يبني صفوف الحسابات (مصفاة) + KPIs. الصافي = دائن الحركة − مدين الحركة كأونكس."""
    by_account: list[dict] = []
    revenue = cogs = expense = 0.0
    gross_mv_dr = gross_mv_cr = 0.0

    for row in base_rows:
        code = str(row.get("A_CODE") or "").strip()
        if not code:
            continue
        name = str(row.get("A_NAME") or "").strip() or code
        open_net = _f(row.get("OPEN_NET"))
        mv_dr = _f(row.get("MV_DR"))
        mv_cr = _f(row.get("MV_CR"))
        norm = _f(row.get("NORM_AMT"))
        if not (open_net or mv_dr or mv_cr):
            continue
        by_account.append(_row_account(code, name, open_net, mv_dr, mv_cr))
        gross_mv_dr = round(gross_mv_dr + mv_dr, 2)
        gross_mv_cr = round(gross_mv_cr + mv_cr, 2)
        kind = _kind_for_code(code)
        if kind == "revenue":
            revenue = round(revenue + norm, 2)
        elif kind == "cogs":
            cogs = round(cogs + norm, 2)
        elif kind == "expense":
            expense = round(expense + norm, 2)

    by_account.sort(key=lambda r: r["account_code"])
    # صافي أونكس: مجموع الدائن − مجموع المدين (قبل/بعد التصافي نفس الناتج)
    net = round(gross_mv_cr - gross_mv_dr, 2)
    net_abs = abs(net)
    if net > 0:
        net_kind = "profit"
        net_title = "صافي الربح"
    elif net < 0:
        net_kind = "loss"
        net_title = "صافي الخسارة"
    else:
        net_kind = "zero"
        net_title = "الصافي"
    if revenue:
        net_pct = round((net / revenue) * 100.0, 2)
    else:
        net_pct = 0.0
    # عرض صف الصافي كأونكس: رصيد الفترة بإشارة معاكسة، والرصيد النهائي بالموجب للربح
    if net > 0:
        period_dr, period_cr = net, 0.0
        final_dr, final_cr = 0.0, net
        period_dr_display = _fmt_money(-net)
        period_cr_display = ""
        final_dr_display = ""
        final_cr_display = _fmt_money(net)
    elif net < 0:
        period_dr, period_cr = 0.0, -net
        final_dr, final_cr = -net, 0.0
        period_dr_display = ""
        period_cr_display = _fmt_money(net)  # سالب
        final_dr_display = _fmt_money(-net)
        final_cr_display = ""
    else:
        period_dr = period_cr = final_dr = final_cr = 0.0
        period_dr_display = period_cr_display = final_dr_display = final_cr_display = ""

    kpis = {
        "revenue": revenue,
        "cogs": cogs,
        "expense": expense,
        "net": net,
        "net_abs": net_abs,
        "net_kind": net_kind,
        "net_title": net_title,
        "net_pct": net_pct,
        "period_dr": period_dr,
        "period_cr": period_cr,
        "final_dr": final_dr,
        "final_cr": final_cr,
        "period_dr_display": period_dr_display,
        "period_cr_display": period_cr_display,
        "final_dr_display": final_dr_display,
        "final_cr_display": final_cr_display,
        "revenue_display": _fmt_money(revenue),
        "cogs_display": _fmt_money(cogs),
        "expense_display": _fmt_money(expense),
        "net_display": _fmt_money(net),
        "net_abs_display": _fmt_money(net_abs),
        "net_pct_display": f"{net_pct:,.2f}%",
        "account_count": len(by_account),
    }
    return by_account, kpis


def _sum_account_totals(rows: list[dict]) -> dict:
    open_dr = round(sum(_f(r.get("open_dr")) for r in rows), 2)
    open_cr = round(sum(_f(r.get("open_cr")) for r in rows), 2)
    mv_dr = round(sum(_f(r.get("mv_dr")) for r in rows), 2)
    mv_cr = round(sum(_f(r.get("mv_cr")) for r in rows), 2)
    close_dr = round(sum(_f(r.get("close_dr")) for r in rows), 2)
    close_cr = round(sum(_f(r.get("close_cr")) for r in rows), 2)
    return {
        "open_dr": open_dr,
        "open_cr": open_cr,
        "mv_dr": mv_dr,
        "mv_cr": mv_cr,
        "close_dr": close_dr,
        "close_cr": close_cr,
        "open_dr_display": _fmt_money(open_dr),
        "open_cr_display": _fmt_money(open_cr),
        "mv_dr_display": _fmt_money(mv_dr),
        "mv_cr_display": _fmt_money(mv_cr),
        "close_dr_display": _fmt_money(close_dr),
        "close_cr_display": _fmt_money(close_cr),
    }


def _cash_filter_sql(
    *,
    posted_only: bool = False,
    branch_code: str = "",
    cc_code: str = "",
    prefix: str = "p",
) -> tuple[str, dict[str, Any]]:
    parts: list[str] = [f"ABS(NVL({prefix}.AMT, 0)) < 1000000000"]
    params: dict[str, Any] = {}
    if posted_only:
        parts.append(f"NVL({prefix}.DOC_POST, 0) = 1")
    brn = str(branch_code or "").strip()
    if brn:
        parts.append(f"TO_CHAR({prefix}.BRN_NO) = :brn")
        params["brn"] = brn
    cc = str(cc_code or "").strip()
    if cc:
        parts.append(f"TO_CHAR({prefix}.CC_CODE) = :cc")
        params["cc"] = cc
    return " AND ".join(parts), params


def fetch_cash_box_checks(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    cc_code: str = "",
    posted_only: bool = False,
    cash_nos: tuple[str, ...] | list[str] | None = None,
) -> dict:
    """
    كشف حساب كل الصناديق (CASH_IN_HAND) على حساب الصندوق المرتبط:
    افتتاحي / مدين / دائن / ختامي — مطابق إذا مدين الفترة ≈ دائن الفترة.
    """
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")

    sch = _schema()
    extra_sql, extra_params = _cash_filter_sql(
        posted_only=posted_only,
        branch_code=branch_code,
        cc_code=cc_code,
    )

    wanted = [str(c).strip() for c in (cash_nos or []) if str(c).strip()]
    master_params: dict[str, Any] = {}
    master_where = "NVL(INACTIVE, 0) = 0"
    brn = str(branch_code or "").strip()
    if brn:
        master_where += " AND TO_CHAR(CONN_BRN_NO) = :mbrn"
        master_params["mbrn"] = brn
    if wanted:
        in_binds = {f"c{i}": n for i, n in enumerate(wanted)}
        in_clause = ", ".join(f":{k}" for k in in_binds)
        master_where += f" AND TO_CHAR(CASH_NO) IN ({in_clause})"
        master_params.update(in_binds)

    masters = _fetch_all(
        f"""
        SELECT TO_CHAR(CASH_NO) AS CASH_NO,
               CASH_NAME,
               TO_CHAR(A_CODE) AS A_CODE,
               TO_CHAR(CONN_BRN_NO) AS BRN_NO
        FROM {sch}.CASH_IN_HAND
        WHERE {master_where}
        ORDER BY TO_NUMBER(REGEXP_REPLACE(TO_CHAR(CASH_NO), '[^0-9]', '')),
                 TO_CHAR(CASH_NO)
        """,
        master_params,
    )
    if not masters:
        return {
            "rows": [],
            "matched": 0,
            "total": 0,
            "all_ok": False,
            "summary": "لا صناديق",
            "totals": {
                "count": 0,
                "open_bal": 0.0,
                "open_display": _fmt_money(0),
                "mv_dr": 0.0,
                "mv_dr_display": _fmt_money(0),
                "mv_cr": 0.0,
                "mv_cr_display": _fmt_money(0),
                "close_bal": 0.0,
                "close_display": _fmt_money(0),
                "diff": 0.0,
                "diff_display": _fmt_money(0),
            },
        }

    cash_list = [str(r.get("CASH_NO") or "").strip() for r in masters if r.get("CASH_NO") is not None]
    master_map = {str(r.get("CASH_NO") or "").strip(): r for r in masters if r.get("CASH_NO") is not None}

    cash_filter_sql = "NVL(c.INACTIVE, 0) = 0"
    agg_params: dict[str, Any] = {"dfrom": d_from, "dto": d_to, **extra_params}
    if wanted:
        in_binds = {f"c{i}": n for i, n in enumerate(wanted)}
        in_clause = ", ".join(f":{k}" for k in in_binds)
        cash_filter_sql += f" AND TO_CHAR(p.CASH_NO) IN ({in_clause})"
        agg_params.update(in_binds)

    agg_rows = _fetch_all(
        f"""
        SELECT TO_CHAR(p.CASH_NO) AS CASH_NO,
               ROUND(SUM(
                 CASE
                   WHEN p.DOC_TYPE = 0 OR p.DOC_DATE < :dfrom
                   THEN NVL(p.DR_AMT, 0) - NVL(p.CR_AMT, 0)
                   ELSE 0
                 END
               ), 2) AS OPEN_BAL,
               ROUND(SUM(
                 CASE
                   WHEN p.DOC_TYPE <> 0
                    AND p.DOC_DATE >= :dfrom
                    AND p.DOC_DATE <= :dto
                   THEN NVL(p.DR_AMT, 0)
                   ELSE 0
                 END
               ), 2) AS MV_DR,
               ROUND(SUM(
                 CASE
                   WHEN p.DOC_TYPE <> 0
                    AND p.DOC_DATE >= :dfrom
                    AND p.DOC_DATE <= :dto
                   THEN NVL(p.CR_AMT, 0)
                   ELSE 0
                 END
               ), 2) AS MV_CR
        FROM {sch}.IAS_POST_DTL p
        JOIN {sch}.CASH_IN_HAND c
          ON TO_CHAR(p.CASH_NO) = TO_CHAR(c.CASH_NO)
         AND TO_CHAR(p.A_CODE) = TO_CHAR(c.A_CODE)
        WHERE {cash_filter_sql}
          AND {extra_sql}
          AND (
                p.DOC_TYPE = 0
             OR p.DOC_DATE < :dfrom
             OR (p.DOC_TYPE <> 0 AND p.DOC_DATE >= :dfrom AND p.DOC_DATE <= :dto)
          )
        GROUP BY p.CASH_NO
        """,
        agg_params,
    )
    agg_map = {str(r.get("CASH_NO") or "").strip(): r for r in agg_rows}

    out_rows: list[dict] = []
    for cash_no in cash_list:
        m = master_map.get(cash_no) or {}
        a = agg_map.get(cash_no) or {}
        name = str(m.get("CASH_NAME") or "").strip() or f"صندوق {cash_no}"
        a_code = str(m.get("A_CODE") or "").strip()
        brn = str(m.get("BRN_NO") or "").strip()
        open_bal = _f(a.get("OPEN_BAL"))
        mv_dr = _f(a.get("MV_DR"))
        mv_cr = _f(a.get("MV_CR"))
        close_bal = round(open_bal + mv_dr - mv_cr, 2)
        diff = round(mv_dr - mv_cr, 2)
        ok = abs(diff) <= 0.05
        out_rows.append(
            {
                "cash_no": cash_no,
                "cash_name": name,
                "account_code": a_code,
                "account_name": name,
                "branch_code": brn,
                "kind_label": "صندوق",
                "label": name,
                "note": f"صندوق {cash_no}" + (f" · حساب {a_code}" if a_code else ""),
                "open_bal": open_bal,
                "open_display": _fmt_money(open_bal),
                "close_bal": close_bal,
                "close_display": _fmt_money(close_bal),
                "mv_dr": mv_dr,
                "mv_cr": mv_cr,
                "mv_dr_display": _fmt_money(mv_dr),
                "mv_cr_display": _fmt_money(mv_cr),
                "diff": diff,
                "diff_display": _fmt_money(diff),
                "diff_abs_display": _fmt_money(abs(diff)),
                "ok": ok,
                "status": "مطابق" if ok else "غير مطابق",
            }
        )

    matched = sum(1 for r in out_rows if r["ok"])
    tot_open = round(sum(_f(r.get("open_bal")) for r in out_rows), 2)
    tot_dr = round(sum(_f(r.get("mv_dr")) for r in out_rows), 2)
    tot_cr = round(sum(_f(r.get("mv_cr")) for r in out_rows), 2)
    tot_close = round(sum(_f(r.get("close_bal")) for r in out_rows), 2)
    tot_diff = round(tot_dr - tot_cr, 2)
    return {
        "rows": out_rows,
        "matched": matched,
        "total": len(out_rows),
        "all_ok": bool(out_rows) and matched == len(out_rows),
        "summary": (
            "كل الصناديق متطابقة"
            if out_rows and matched == len(out_rows)
            else (f"{matched}/{len(out_rows)} متطابق" if out_rows else "لا صناديق")
        ),
        "totals": {
            "count": len(out_rows),
            "open_bal": tot_open,
            "open_display": _fmt_money(tot_open),
            "mv_dr": tot_dr,
            "mv_dr_display": _fmt_money(tot_dr),
            "mv_cr": tot_cr,
            "mv_cr_display": _fmt_money(tot_cr),
            "close_bal": tot_close,
            "close_display": _fmt_money(tot_close),
            "diff": tot_diff,
            "diff_display": _fmt_money(tot_diff),
        },
    }


def _box_check(
    label: str,
    box: float,
    mv_dr: float,
    mv_cr: float,
    *,
    expected: float,
    note: str = "",
) -> dict:
    diff = round(_f(box) - _f(expected), 2)
    ok = abs(diff) <= 0.05
    return {
        "label": label,
        "box": _f(box),
        "box_display": _fmt_money(box),
        "mv_dr": _f(mv_dr),
        "mv_cr": _f(mv_cr),
        "mv_dr_display": _fmt_money(mv_dr) if mv_dr else "",
        "mv_cr_display": _fmt_money(mv_cr) if mv_cr else "",
        "expected": _f(expected),
        "expected_display": _fmt_money(expected),
        "diff": diff,
        "diff_display": _fmt_money(diff),
        "ok": ok,
        "status": "مطابق" if ok else "غير مطابق",
        "note": note,
    }


def _build_reconciliation(by_account: list[dict], kpis: dict, account_totals: dict) -> dict:
    """مطابقة كل صندوق مع حالة المدين/الدائن في الجدول."""

    def kind_sides(kind: str) -> tuple[float, float]:
        dr = round(sum(_f(r.get("mv_dr")) for r in by_account if r.get("kind") == kind), 2)
        cr = round(sum(_f(r.get("mv_cr")) for r in by_account if r.get("kind") == kind), 2)
        return dr, cr

    rev_dr, rev_cr = kind_sides("revenue")
    cogs_dr, cogs_cr = kind_sides("cogs")
    exp_dr, exp_cr = kind_sides("expense")
    tot_dr = _f(account_totals.get("mv_dr"))
    tot_cr = _f(account_totals.get("mv_cr"))

    # المتوقع من المدين/الدائن حسب طبيعة الحساب
    rev_exp = round(rev_cr - rev_dr, 2)
    cogs_exp = round(cogs_dr - cogs_cr, 2)
    exp_exp = round(exp_dr - exp_cr, 2)
    net_exp = round(tot_cr - tot_dr, 2)

    rows = [
        _box_check(
            "صندوق الإيرادات",
            _f(kpis.get("revenue")),
            rev_dr,
            rev_cr,
            expected=rev_exp,
            note="متوقع = دائن − مدين",
        ),
        _box_check(
            "صندوق التكلفة",
            _f(kpis.get("cogs")),
            cogs_dr,
            cogs_cr,
            expected=cogs_exp,
            note="متوقع = مدين − دائن",
        ),
        _box_check(
            "صندوق المصروفات",
            _f(kpis.get("expense")),
            exp_dr,
            exp_cr,
            expected=exp_exp,
            note="متوقع = مدين − دائن",
        ),
        _box_check(
            f"صندوق {kpis.get('net_title') or 'الصافي'}",
            _f(kpis.get("net")),
            tot_dr,
            tot_cr,
            expected=net_exp,
            note="متوقع = إجمالي دائن − إجمالي مدين",
        ),
    ]
    matched = sum(1 for r in rows if r["ok"])
    return {
        "rows": rows,
        "matched": matched,
        "total": len(rows),
        "all_ok": matched == len(rows),
        "summary": "كل الصناديق متطابقة" if matched == len(rows) else f"{matched}/{len(rows)} متطابق",
    }


def _build_top_accounts(by_account: list[dict], limit: int = 25) -> tuple[list[dict], dict]:
    """أعلى حسابات المصروفات (|صافي الحركة|) من الأعلى إلى الأدنى + إجماليات."""
    kind_label = {
        "revenue": "إيراد",
        "cogs": "تكلفة",
        "expense": "مصروف",
        "other": "أخرى",
    }
    ranked: list[dict] = []
    for row in by_account:
        if str(row.get("kind") or "") != "expense":
            continue
        net = round(_f(row.get("mv_cr")) - _f(row.get("mv_dr")), 2)
        impact = abs(net)
        if not impact:
            continue
        kind = "expense"
        ranked.append(
            {
                "account_code": row.get("account_code"),
                "account_name": row.get("account_name"),
                "kind": kind,
                "kind_label": kind_label.get(kind, kind),
                "net": net,
                "impact": impact,
                "net_display": _fmt_money(net),
                "impact_display": _fmt_money(impact),
                "mv_dr_display": row.get("mv_dr_display") or "",
                "mv_cr_display": row.get("mv_cr_display") or "",
            }
        )
    ranked.sort(key=lambda r: (-r["impact"], r["account_code"]))
    top = ranked[: max(0, min(int(limit or 25), 50))]
    max_impact = top[0]["impact"] if top else 0.0
    out: list[dict] = []
    for i, row in enumerate(top):
        share = round((row["impact"] / max_impact) * 100.0, 1) if max_impact else 0.0
        out.append(
            {
                **row,
                "rank": i + 1,
                "bar_pct": share,
                "net_kind": "profit" if row["net"] > 0 else ("loss" if row["net"] < 0 else "zero"),
            }
        )
    net_sum = round(sum(_f(r.get("net")) for r in out), 2)
    impact_sum = round(sum(_f(r.get("impact")) for r in out), 2)
    totals = {
        "count": len(out),
        "net": net_sum,
        "impact": impact_sum,
        "net_display": _fmt_money(net_sum),
        "impact_display": _fmt_money(impact_sum),
    }
    return out, totals


def fetch_income_branch_profits(
    date_from,
    date_to,
    *,
    cc_code: str = "",
    posted_only: bool = False,
) -> list[dict]:
    """
    صافي ربح/خسارة لكل فرع (دائن − مدين) لحسابات قائمة الدخل.
    كل الفروع مرتّبة من الأعلى ربحاً — للرسم البياني بجانب الجدول.
    """
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")

    sch = _schema()
    extra, params = _period_filters(
        branch_code="",
        cc_code=cc_code,
        posted_only=posted_only,
    )
    params.update({"dfrom": d_from, "dto": d_to})
    sane = "ABS(NVL(p.AMT, 0)) < 1000000000"

    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(p.BRN_NO) AS BRN_NO,
               ROUND(SUM(NVL(p.DR_AMT, 0)), 2) AS MV_DR,
               ROUND(SUM(NVL(p.CR_AMT, 0)), 2) AS MV_CR,
               ROUND(SUM(NVL(p.CR_AMT, 0)) - SUM(NVL(p.DR_AMT, 0)), 2) AS NET
        FROM {sch}.IAS_POST_DTL p
        JOIN {sch}.ACCOUNT a
          ON a.A_CODE = p.A_CODE
        WHERE {extra}
          AND p.DOC_TYPE <> 0
          AND p.DOC_DATE >= :dfrom
          AND p.DOC_DATE <= :dto
          AND {sane}
        GROUP BY p.BRN_NO
        HAVING SUM(NVL(p.DR_AMT, 0)) <> 0
            OR SUM(NVL(p.CR_AMT, 0)) <> 0
        ORDER BY NET DESC
        """,
        params,
    )
    names = _branch_names()
    nets = [_f(r.get("NET")) for r in rows]
    max_abs = max((abs(n) for n in nets), default=0.0)
    out: list[dict] = []
    for row in rows:
        code = str(row.get("BRN_NO") or "").strip()
        if not code:
            continue
        net = _f(row.get("NET"))
        mv_dr = _f(row.get("MV_DR"))
        mv_cr = _f(row.get("MV_CR"))
        if net > 0:
            kind = "profit"
            title = "ربح"
        elif net < 0:
            kind = "loss"
            title = "خسارة"
        else:
            kind = "zero"
            title = "متعادل"
        bar_pct = round((abs(net) / max_abs) * 100.0, 1) if max_abs else 0.0
        out.append(
            {
                "branch_code": code,
                "branch_name": names.get(code, code),
                "mv_dr": mv_dr,
                "mv_cr": mv_cr,
                "net": net,
                "net_abs": abs(net),
                "net_kind": kind,
                "net_title": title,
                "net_display": _fmt_money(net),
                "net_abs_display": _fmt_money(abs(net)),
                "bar_pct": bar_pct,
            }
        )
    return out


def _cache_key(
    d_from: date,
    d_to: date,
    branch_code: str,
    cc_code: str,
    posted_only: bool,
) -> str:
    return (
        f"income:stmt:v25:{d_from}:{d_to}:"
        f"{branch_code or '-'}:"
        f"{cc_code or '-'}:"
        f"{int(bool(posted_only))}"
    )


def build_income_statement(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    cc_code: str = "",
    posted_only: bool = False,
) -> dict:
    """يبني قائمة الدخل: KPIs + جدول الأرصدة مع الحركة حسب الحساب + ترتيب الفروع."""
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    brn = str(branch_code or "").strip()
    cc = str(cc_code or "").strip()
    use_posted = bool(posted_only)

    key = _cache_key(d_from, d_to, brn, cc, use_posted)
    cached = cache.get(key)
    if isinstance(cached, dict) and cached.get("cash_stmt"):
        return cached

    base = _fetch_income_base_rows(
        d_from,
        d_to,
        branch_code=brn,
        cc_code=cc,
        posted_only=use_posted,
    )
    by_account, kpis = _aggregate_accounts(base)
    account_totals = _sum_account_totals(by_account)
    by_branch_profit = fetch_income_branch_profits(
        d_from,
        d_to,
        cc_code=cc,
        posted_only=use_posted,
    )
    reconciliation = fetch_cash_box_checks(
        d_from,
        d_to,
        branch_code=brn,
        cc_code=cc,
        posted_only=use_posted,
    )
    # احتفاظ بمطابقة صناديق قائمة الدخل للتحليل الداخلي إن لزم
    kpi_reconciliation = _build_reconciliation(by_account, kpis, account_totals)
    top_accounts, top_accounts_totals = _build_top_accounts(by_account, limit=25)

    scope_bits = [f"{d_from.isoformat()} → {d_to.isoformat()}"]
    if brn:
        scope_bits.append(_branch_names().get(brn, brn))
    if cc:
        scope_bits.append(f"مركز {cc}")
    scope_bits.append("مرحّل فقط" if use_posted else "مرحّل + غير مرحّل")

    result = {
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "scope_label": " · ".join(scope_bits),
        "posted_only": use_posted,
        "cash_stmt": True,
        "kpis": kpis,
        "by_account": by_account,
        "by_account_total_count": len(by_account),
        "account_totals": account_totals,
        "by_branch_profit": by_branch_profit,
        "reconciliation": reconciliation,
        "kpi_reconciliation": kpi_reconciliation,
        "top_accounts": top_accounts,
        "top_accounts_totals": top_accounts_totals,
    }
    try:
        span = (d_to - d_from).days + 1
        ttl = _INCOME_CACHE_TTL
        cache.set(key, result, ttl)
    except Exception:
        pass
    return result
