"""
قائمة الدخل — مطابقة تقرير أونكس «الأرصدة مع الحركة» عبر IAS_POST_DTL.

جدول الحسابات (افتتاحي / حركة / ختامي) + KPIs.
"""

from __future__ import annotations

from .oracle_sqlutil import num_bind as _num_bind

import logging
import math
from datetime import date, timedelta
from typing import Any

from django.core.cache import cache

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _bill_mst_ok,
    _branch_names,
    _fetch_all,
    _hung_ok,
    _item_unit_cost_map,
    _item_wh_unit_cost_map,
    _norm_brn_code,
    _pos_owner,
    _rt_bill_mst_ok,
    _run_parallel,
    _schema,
    oracle_session,
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


def _fmt_compact(value: float) -> str:
    """عرض مختصر للمخططات — ملايين/آلاف بدون قطع الأرقام."""
    n = _f(value)
    sign = "-" if n < 0 else ""
    v = abs(n)
    if v >= 1_000_000:
        return f"{sign}{v / 1_000_000:,.1f} م"
    if v >= 1_000:
        return f"{sign}{v / 1_000:,.1f} ألف"
    return f"{sign}{v:,.0f}"


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
        parts.append("p.BRN_NO = CASE WHEN REGEXP_LIKE(:brn, '^ *[0-9]+ *$') THEN TO_NUMBER(:brn) END")
        params["brn"] = brn
    cc = str(cc_code or "").strip()
    if cc:
        parts.append("p.CC_CODE = :cc")
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
    gross_profit = round(revenue - cogs, 2)
    if revenue:
        gross_margin_pct = round((gross_profit / revenue) * 100.0, 2)
        cogs_pct = round((cogs / revenue) * 100.0, 2)
        expense_pct = round((expense / revenue) * 100.0, 2)
    else:
        gross_margin_pct = 0.0
        cogs_pct = 0.0
        expense_pct = 0.0
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
        "gross_profit": gross_profit,
        "gross_margin_pct": gross_margin_pct,
        "cogs_pct": cogs_pct,
        "expense_pct": expense_pct,
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
        "gross_profit_display": _fmt_money(gross_profit),
        "gross_margin_pct_display": f"{gross_margin_pct:,.2f}%",
        "cogs_pct_display": f"{cogs_pct:,.2f}%",
        "expense_pct_display": f"{expense_pct:,.2f}%",
        "net_display": _fmt_money(net),
        "net_abs_display": _fmt_money(net_abs),
        "net_pct_display": f"{net_pct:,.2f}%",
        "account_count": len(by_account),
    }
    return by_account, kpis


_AR_MONTHS = (
    "",
    "يناير",
    "فبراير",
    "مارس",
    "أبريل",
    "مايو",
    "يونيو",
    "يوليو",
    "أغسطس",
    "سبتمبر",
    "أكتوبر",
    "نوفمبر",
    "ديسمبر",
)


def _month_label(d: date) -> str:
    return f"{_AR_MONTHS[d.month]} {d.year}"


def _prior_period_bounds(d_from: date, d_to: date) -> tuple[date, date]:
    """نفس طول الفترة مباشرة قبل تاريخ البداية."""
    span = (d_to - d_from).days + 1
    prior_to = d_from - timedelta(days=1)
    prior_from = prior_to - timedelta(days=span - 1)
    return prior_from, prior_to


def _delta_pct(current: float, prior: float) -> float | None:
    cur = _f(current)
    prv = _f(prior)
    if abs(prv) < 0.005:
        if abs(cur) < 0.005:
            return 0.0
        return None
    return round(((cur - prv) / abs(prv)) * 100.0, 1)


def _kpis_from_kind_rows(rows: list[dict]) -> dict:
    """KPIs من صفوف مجمّعة حسب جذر الحساب (3/4/5) + إجمالي مدين/دائن للصافي."""
    revenue = cogs = expense = 0.0
    gross_dr = gross_cr = 0.0
    for row in rows:
        root = str(row.get("ROOT") or "").strip()[:1]
        norm = _f(row.get("NORM_AMT"))
        gross_dr = round(gross_dr + _f(row.get("MV_DR")), 2)
        gross_cr = round(gross_cr + _f(row.get("MV_CR")), 2)
        if root == "3":
            revenue = round(revenue + norm, 2)
        elif root == "4":
            cogs = round(cogs + norm, 2)
        elif root == "5":
            expense = round(expense + norm, 2)
    net = round(gross_cr - gross_dr, 2)
    net_abs = abs(net)
    if net > 0:
        net_kind, net_title = "profit", "صافي الربح"
    elif net < 0:
        net_kind, net_title = "loss", "صافي الخسارة"
    else:
        net_kind, net_title = "zero", "الصافي"
    gross_profit = round(revenue - cogs, 2)
    if revenue:
        gross_margin_pct = round((gross_profit / revenue) * 100.0, 2)
        net_pct = round((net / revenue) * 100.0, 2)
        cogs_pct = round((cogs / revenue) * 100.0, 2)
        expense_pct = round((expense / revenue) * 100.0, 2)
    else:
        gross_margin_pct = net_pct = cogs_pct = expense_pct = 0.0
    return {
        "revenue": revenue,
        "cogs": cogs,
        "expense": expense,
        "gross_profit": gross_profit,
        "gross_margin_pct": gross_margin_pct,
        "cogs_pct": cogs_pct,
        "expense_pct": expense_pct,
        "net": net,
        "net_abs": net_abs,
        "net_kind": net_kind,
        "net_title": net_title,
        "net_pct": net_pct,
        "revenue_display": _fmt_money(revenue),
        "cogs_display": _fmt_money(cogs),
        "expense_display": _fmt_money(expense),
        "gross_profit_display": _fmt_money(gross_profit),
        "gross_margin_pct_display": f"{gross_margin_pct:,.2f}%",
        "net_display": _fmt_money(net),
        "net_abs_display": _fmt_money(net_abs),
        "net_pct_display": f"{net_pct:,.2f}%",
    }


def _fetch_income_kind_rows(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    cc_code: str = "",
    posted_only: bool = False,
    by_month: bool = False,
) -> list[dict]:
    """
    حركة الفترة فقط (بدون افتتاحي) مجمّعة حسب جذر الحساب، واختياريًا حسب الشهر.
    يقودها DOC_DATE ضمن الفترة — مسار أخف من مسح الأرصدة الافتتاحية.
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
    sane = "ABS(NVL(p.AMT, 0)) < 1000000000"
    month_sel = "TRUNC(p.DOC_DATE, 'MM') AS YM," if by_month else ""
    month_grp = "TRUNC(p.DOC_DATE, 'MM')," if by_month else ""

    return _fetch_all(
        f"""
        SELECT /*+ USE_HASH(p a) */
               {month_sel}
               SUBSTR(TO_CHAR(a.A_CODE), 1, 1) AS ROOT,
               ROUND(SUM(
                 CASE
                   WHEN NVL(a.DR, 0) = 0 THEN -NVL(p.AMT, 0)
                   ELSE NVL(p.AMT, 0)
                 END
               ), 2) AS NORM_AMT,
               ROUND(SUM(NVL(p.DR_AMT, 0)), 2) AS MV_DR,
               ROUND(SUM(NVL(p.CR_AMT, 0)), 2) AS MV_CR
        FROM {sch}.IAS_POST_DTL p
        JOIN {sch}.ACCOUNT a
          ON a.A_CODE = p.A_CODE
        WHERE {extra}
          AND p.DOC_TYPE <> 0
          AND p.DOC_DATE >= :dfrom
          AND p.DOC_DATE <= :dto
          AND {sane}
        GROUP BY {month_grp} SUBSTR(TO_CHAR(a.A_CODE), 1, 1)
        """,
        params,
    )


def _iter_months(d_from: date, d_to: date) -> list[date]:
    cur = date(d_from.year, d_from.month, 1)
    end = date(d_to.year, d_to.month, 1)
    out: list[date] = []
    while cur <= end:
        out.append(cur)
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
    return out


def _ym_key(value: Any) -> date | None:
    if isinstance(value, date):
        return date(value.year, value.month, 1)
    if hasattr(value, "date"):
        try:
            d = value.date()
            return date(d.year, d.month, 1)
        except Exception:
            return None
    text = str(value or "").strip()[:10]
    if len(text) >= 7 and text[4] == "-":
        try:
            y = int(text[0:4])
            m = int(text[5:7])
            return date(y, m, 1)
        except ValueError:
            return None
    return None


def _build_monthly_trend(d_from: date, d_to: date, rows: list[dict]) -> dict:
    """سلسلة شهرية أفقية: شريط صافي واضح + ملخص إيراد/تكلفة/مصروف."""
    buckets: dict[date, dict[str, float]] = {}
    for row in rows:
        ym = _ym_key(row.get("YM"))
        if ym is None:
            continue
        bucket = buckets.setdefault(
            ym,
            {"revenue": 0.0, "cogs": 0.0, "expense": 0.0, "dr": 0.0, "cr": 0.0},
        )
        root = str(row.get("ROOT") or "").strip()[:1]
        norm = _f(row.get("NORM_AMT"))
        bucket["dr"] = round(bucket["dr"] + _f(row.get("MV_DR")), 2)
        bucket["cr"] = round(bucket["cr"] + _f(row.get("MV_CR")), 2)
        if root == "3":
            bucket["revenue"] = round(bucket["revenue"] + norm, 2)
        elif root == "4":
            bucket["cogs"] = round(bucket["cogs"] + norm, 2)
        elif root == "5":
            bucket["expense"] = round(bucket["expense"] + norm, 2)

    months = _iter_months(d_from, d_to)
    series: list[dict] = []
    peak_net = 0.01
    peak_rev = 0.01
    peak_cogs = 0.01
    peak_exp = 0.01
    for ym in months:
        b = buckets.get(ym) or {
            "revenue": 0.0,
            "cogs": 0.0,
            "expense": 0.0,
            "dr": 0.0,
            "cr": 0.0,
        }
        net = round(_f(b["cr"]) - _f(b["dr"]), 2)
        revenue = _f(b["revenue"])
        cogs = _f(b["cogs"])
        expense = _f(b["expense"])
        peak_net = max(peak_net, abs(net))
        peak_rev = max(peak_rev, abs(revenue))
        peak_cogs = max(peak_cogs, abs(cogs))
        peak_exp = max(peak_exp, abs(expense))
        if net > 0:
            net_kind = "profit"
        elif net < 0:
            net_kind = "loss"
        else:
            net_kind = "zero"
        series.append(
            {
                "ym": ym.isoformat(),
                "label": _month_label(ym),
                "label_short": _AR_MONTHS[ym.month],
                "revenue": revenue,
                "cogs": cogs,
                "expense": expense,
                "net": net,
                "net_kind": net_kind,
                "revenue_display": _fmt_money(revenue),
                "cogs_display": _fmt_money(cogs),
                "expense_display": _fmt_money(expense),
                "net_display": _fmt_money(net),
                "net_abs_display": _fmt_money(abs(net)),
                "revenue_compact": _fmt_compact(revenue),
                "cogs_compact": _fmt_compact(cogs),
                "expense_compact": _fmt_compact(expense),
                "net_compact": _fmt_compact(net),
            }
        )

    def bar_pct(value: float, peak: float) -> str:
        """نسبة CSS آمنة بنقطة عشرية (لا فاصلة محلية تكسر width)."""
        if not peak or abs(value) <= 0.005:
            return "0"
        pct = min(100.0, abs(value) / peak * 100.0)
        return f"{pct:.1f}"

    for row in series:
        row["net_pct"] = bar_pct(row["net"], peak_net)
        row["revenue_pct"] = bar_pct(row["revenue"], peak_rev)
        row["cogs_pct"] = bar_pct(row["cogs"], peak_cogs)
        row["expense_pct"] = bar_pct(row["expense"], peak_exp)

    return {
        "months": series,
        "month_count": len(series),
        "peak": peak_net,
        "peak_display": _fmt_compact(peak_net),
        "has_data": any(
            abs(r["revenue"]) + abs(r["cogs"]) + abs(r["expense"]) + abs(r["net"]) > 0.005
            for r in series
        ),
    }


def _build_executive_alerts(
    *,
    kpis: dict,
    by_branch_profit: list[dict],
    top_accounts: list[dict],
    monthly_trend: dict,
) -> dict:
    """
    لوحة انتباه للمدير التنفيذي — بطاقات استثناء فقط:
    فروع خاسرة · تركّز الربح · ضغط الهامش · مصروف مهيمن.
    """
    alerts: list[dict] = []

    losers = [b for b in (by_branch_profit or []) if str(b.get("net_kind") or "") == "loss"]
    if losers:
        loss_sum = round(sum(abs(_f(b.get("net"))) for b in losers), 2)
        top_names = [str(b.get("branch_name") or b.get("branch_code") or "") for b in losers[:3]]
        hint = " · ".join(n for n in top_names if n)
        if len(losers) > 3:
            hint = f"{hint} +{len(losers) - 3}" if hint else f"+{len(losers) - 3}"
        alerts.append(
            {
                "key": "losing_branches",
                "severity": "high",
                "title": "فروع خاسرة",
                "metric": f"{len(losers)} فرع",
                "detail": f"إجمالي الخسارة {_fmt_compact(loss_sum)}",
                "hint": hint,
            }
        )

    winners = [b for b in (by_branch_profit or []) if _f(b.get("net")) > 0]
    profit_pool = round(sum(_f(b.get("net")) for b in winners), 2)
    if profit_pool > 0 and len(winners) >= 2:
        top2 = sorted(winners, key=lambda b: -_f(b.get("net")))[:2]
        share = round(sum(_f(b.get("net")) for b in top2) / profit_pool * 100.0, 1)
        if share >= 60.0:
            alerts.append(
                {
                    "key": "concentration",
                    "severity": "medium",
                    "title": "تركّز الربح",
                    "metric": f"{share:.0f}%".replace(",", "."),
                    "detail": "أعلى فرعين من إجمالي ربح الفروع",
                    "hint": " · ".join(
                        f"{b.get('branch_name') or b.get('branch_code')} {_fmt_compact(_f(b.get('net')))}"
                        for b in top2
                    ),
                }
            )

    months = [
        m
        for m in (monthly_trend or {}).get("months") or []
        if abs(_f(m.get("revenue"))) > 0.005
    ]
    if len(months) >= 3:

        def _net_margin(m: dict) -> float:
            rev = _f(m.get("revenue"))
            if abs(rev) < 0.005:
                return 0.0
            return round(_f(m.get("net")) / rev * 100.0, 2)

        earlier = months[:-1]
        last = months[-1]
        avg_earlier = sum(_net_margin(m) for m in earlier) / len(earlier)
        last_m = _net_margin(last)
        drop = round(avg_earlier - last_m, 1)
        if drop >= 1.0:
            alerts.append(
                {
                    "key": "margin_pressure",
                    "severity": "medium",
                    "title": "ضغط الهامش",
                    "metric": f"{last_m:.1f}%".replace(",", "."),
                    "detail": (
                        f"صافي {last.get('label_short') or last.get('label')} "
                        f"أقل من متوسط الأشهر السابقة بـ {drop:.1f} نقطة"
                    ).replace(",", "."),
                    "hint": f"متوسط سابق {avg_earlier:.1f}%".replace(",", "."),
                }
            )

    expense_total = abs(_f(kpis.get("expense")))
    if top_accounts and expense_total > 0:
        top1 = top_accounts[0]
        impact = abs(_f(top1.get("impact")))
        share = round(impact / expense_total * 100.0, 1)
        if share >= 25.0:
            name = str(top1.get("account_name") or top1.get("account_code") or "مصروف")
            alerts.append(
                {
                    "key": "expense_spike",
                    "severity": "high" if share >= 40.0 else "medium",
                    "title": "مصروف مهيمن",
                    "metric": f"{share:.0f}%".replace(",", "."),
                    "detail": f"{name} — {_fmt_compact(impact)}",
                    "hint": "من إجمالي مصروفات الفترة",
                }
            )

    severity_rank = {"high": 0, "medium": 1, "info": 2}
    alerts.sort(key=lambda a: (severity_rank.get(str(a.get("severity")), 9), a.get("key") or ""))
    count = len(alerts)
    return {
        "alerts": alerts,
        "count": count,
        "has_alerts": count > 0,
        "summary": f"{count} تنبيه" if count else "لا تنبيهات",
        "ok_message": "لا تنبيهات ضمن الفلتر — الربحية موزعة بدون استثناءات حادة",
    }


def _build_period_compare(current: dict, prior: dict, prior_from: date, prior_to: date) -> dict:
    """مقارنة KPIs الفترة الحالية مع فترة سابقة بنفس الطول."""
    metrics = [
        ("revenue", "الإيرادات", "revenue"),
        ("cogs", "التكلفة", "cogs"),
        ("expense", "المصروفات", "expense"),
        ("net", str(current.get("net_title") or "الصافي"), "net"),
    ]
    rows: list[dict] = []
    for key, label, tone in metrics:
        cur = _f(current.get(key))
        prv = _f(prior.get(key))
        delta = round(cur - prv, 2)
        pct = _delta_pct(cur, prv)
        if key == "net":
            # ارتفاع الصافي أفضل دائماً (ربح أعلى أو خسارة أقل)
            if delta > 0:
                delta_kind = "up"
            elif delta < 0:
                delta_kind = "down"
            else:
                delta_kind = "flat"
        elif key in ("cogs", "expense"):
            if delta > 0:
                delta_kind = "down"
            elif delta < 0:
                delta_kind = "up"
            else:
                delta_kind = "flat"
        else:
            if delta > 0:
                delta_kind = "up"
            elif delta < 0:
                delta_kind = "down"
            else:
                delta_kind = "flat"
        peak = max(abs(cur), abs(prv), 0.01)
        rows.append(
            {
                "key": key,
                "label": label,
                "tone": tone,
                "current": cur,
                "prior": prv,
                "delta": delta,
                "delta_pct": pct,
                "delta_kind": delta_kind,
                "current_display": _fmt_money(cur),
                "prior_display": _fmt_money(prv),
                "delta_display": _fmt_money(delta),
                "delta_pct_display": "—" if pct is None else f"{pct:+,.1f}%",
                "current_pct": f"{min(100.0, abs(cur) / peak * 100.0):.1f}",
                "prior_pct": f"{min(100.0, abs(prv) / peak * 100.0):.1f}",
            }
        )
    return {
        "prior_label": f"{prior_from.isoformat()} → {prior_to.isoformat()}",
        "rows": rows,
        "has_data": any(
            abs(r["current"]) + abs(r["prior"]) > 0.005 for r in rows
        ),
    }


def _pnl_graphic_panels(kpis: dict) -> dict:
    """لوحات رسومية من KPIs الفترة — بدون حقول أوراكل جديدة."""
    revenue = _f(kpis.get("revenue"))
    cogs = _f(kpis.get("cogs"))
    expense = _f(kpis.get("expense"))
    net = _f(kpis.get("net"))
    net_abs = abs(net)
    gross_profit = _f(kpis.get("gross_profit"))
    gross_margin_pct = _f(kpis.get("gross_margin_pct"))
    net_pct = _f(kpis.get("net_pct"))
    peak = max(revenue, cogs, expense, net_abs, 0.01)

    def bar(value: float) -> float:
        return round(min(100.0, abs(value) / peak * 100.0), 1)

    def share_of_rev(value: float) -> tuple[float, str]:
        pct = round((abs(value) / revenue) * 100.0, 1) if revenue else 0.0
        return pct, f"{pct:.1f}%"

    rev_share, rev_share_disp = share_of_rev(revenue)
    cogs_share, cogs_share_disp = share_of_rev(cogs)
    exp_share, exp_share_disp = share_of_rev(expense)
    net_share, net_share_disp = share_of_rev(net_abs)

    structure = [
        {
            "key": "revenue",
            "label": "الإيرادات",
            "tone": "rev",
            "value": revenue,
            "display": _fmt_money(revenue),
            "bar_pct": bar(revenue),
            "share_pct": rev_share,
            "share_display": rev_share_disp,
        },
        {
            "key": "cogs",
            "label": "التكلفة",
            "tone": "cogs",
            "value": cogs,
            "display": _fmt_money(cogs),
            "bar_pct": bar(cogs),
            "share_pct": cogs_share,
            "share_display": cogs_share_disp,
        },
        {
            "key": "expense",
            "label": "المصروفات",
            "tone": "exp",
            "value": expense,
            "display": _fmt_money(expense),
            "bar_pct": bar(expense),
            "share_pct": exp_share,
            "share_display": exp_share_disp,
        },
        {
            "key": "net",
            "label": str(kpis.get("net_title") or "الصافي"),
            "tone": str(kpis.get("net_kind") or "zero"),
            "value": net,
            "display": _fmt_money(net),
            "bar_pct": bar(net_abs),
            "share_pct": net_share,
            "share_display": net_share_disp,
        },
    ]

    gross_level = revenue - cogs
    op_level = gross_level - expense
    other = net - op_level
    steps = [
        ("revenue", "الإيرادات", "rev", "total", 0.0, revenue, revenue, rev_share_disp),
        ("cogs", "التكلفة", "cogs", "minus", revenue, gross_level, -cogs, cogs_share_disp),
        ("gross", "مجمل الربح", "rev", "total", 0.0, gross_level, gross_level, share_of_rev(gross_level)[1]),
        ("expense", "المصروفات", "exp", "minus", gross_level, op_level, -expense, exp_share_disp),
    ]
    if abs(other) >= max(revenue, 1.0) * 0.0005:
        steps.append(
            ("other", "بنود أخرى (الفرق)", "other", "plus" if other > 0 else "minus", op_level, net, other, share_of_rev(other)[1])
        )
    steps.append(
        ("net", str(kpis.get("net_title") or "الصافي"), str(kpis.get("net_kind") or "zero"), "total", 0.0, net, net, net_share_disp)
    )
    levels = [0.0] + [v for st in steps for v in (st[4], st[5])]
    lo, hi = min(levels), max(levels)
    span = (hi - lo) or 1.0

    def pct(v: float) -> float:
        return round((v - lo) / span * 100.0, 2)

    waterfall = []
    for key, label, tone, kind, a_val, b_val, signed, share_disp in steps:
        start, end = sorted((a_val, b_val))
        waterfall.append(
            {
                "key": key,
                "label": label,
                "tone": tone,
                "kind": kind,
                "display": _fmt_money(signed),
                "share_display": share_disp,
                "left_pct": pct(start),
                "width_pct": max(round((end - start) / span * 100.0, 2), 0.6),
            }
        )
    waterfall_axis = {
        "zero_pct": pct(0.0),
        "ticks": [
            {"pct": p, "label": "0" if abs(lo + span * p / 100.0) < 0.5 else f"{(lo + span * p / 100.0) / 1_000_000:,.0f} م"}
            for p in (0, 25, 50, 75, 100)
        ],
    }

    margin_peak = max(abs(gross_margin_pct), abs(net_pct), 0.01)
    margin_bars = [
        {
            "key": "gross",
            "label": "هامش إجمالي",
            "tone": "rev" if gross_profit >= 0 else "loss",
            "display": f"{gross_margin_pct:,.2f}%",
            "meta": _fmt_money(gross_profit),
            "bar_pct": round(min(100.0, abs(gross_margin_pct) / margin_peak * 100.0), 1),
        },
        {
            "key": "net",
            "label": "هامش صافي",
            "tone": str(kpis.get("net_kind") or "zero"),
            "display": f"{net_pct:,.2f}%",
            "meta": _fmt_money(net),
            "bar_pct": round(min(100.0, abs(net_pct) / margin_peak * 100.0), 1),
        },
        {
            "key": "cogs_share",
            "label": "التكلفة من الإيراد",
            "tone": "cogs",
            "display": str(kpis.get("cogs_pct_display") or "0.00%"),
            "meta": _fmt_money(cogs),
            "bar_pct": round(min(100.0, _f(kpis.get("cogs_pct"))), 1),
        },
        {
            "key": "exp_share",
            "label": "المصروف من الإيراد",
            "tone": "exp",
            "display": str(kpis.get("expense_pct_display") or "0.00%"),
            "meta": _fmt_money(expense),
            "bar_pct": round(min(100.0, _f(kpis.get("expense_pct"))), 1),
        },
    ]
    return {
        "structure": structure,
        "waterfall": waterfall,
        "waterfall_axis": waterfall_axis,
        "margin_bars": margin_bars,
        "composition": _pnl_composition_donut(kpis),
    }


def _share_display(share: float) -> str:
    if share >= 10:
        return f"{round(share):.0f}%".replace(",", ".")
    if abs(share - round(share)) > 0.05:
        return f"{share:.1f}%".replace(",", ".")
    return f"{int(round(share))}%".replace(",", ".")


def _annular_slices_from_parts(parts: list[dict]) -> tuple[list[dict], float]:
    """يبني مسارات حلقة سميكة من أجزاء {name, amount, color, ...}."""
    total = round(sum(float(p.get("amount") or 0) for p in parts), 2)
    slices: list[dict] = []
    for part in parts:
        amt = float(part.get("amount") or 0)
        if amt <= 0 and total > 0:
            continue
        share = (amt / total * 100.0) if total else 0.0
        slices.append(
            {
                **part,
                "amount": amt,
                "amount_display": part.get("amount_display") or _fmt_money(amt),
                "amount_compact": part.get("amount_compact") or _fmt_compact(amt),
                "share_pct": round(share, 1),
                "share_display": _share_display(share),
            }
        )

    r_out = 48.0
    r_in = 26.8
    r_label = (r_out + r_in) / 2.0
    cx = cy = 50.0
    gap_deg = 1.8
    frac_acc = 0.0

    def _pt(angle_deg: float, radius: float) -> tuple[float, float]:
        a = math.radians(angle_deg)
        return (cx + radius * math.sin(a), cy - radius * math.cos(a))

    def _annular_path(start_deg: float, end_deg: float) -> str:
        sweep = end_deg - start_deg
        if sweep <= 0.05:
            return ""
        large = 1 if sweep > 180 else 0
        x0, y0 = _pt(start_deg, r_out)
        x1, y1 = _pt(end_deg, r_out)
        x2, y2 = _pt(end_deg, r_in)
        x3, y3 = _pt(start_deg, r_in)
        return (
            f"M {x0:.3f} {y0:.3f} "
            f"A {r_out:.3f} {r_out:.3f} 0 {large} 1 {x1:.3f} {y1:.3f} "
            f"L {x2:.3f} {y2:.3f} "
            f"A {r_in:.3f} {r_in:.3f} 0 {large} 0 {x3:.3f} {y3:.3f} Z"
        )

    for sl in slices:
        amt = float(sl["amount"])
        pct = (amt / total) if total else 0.0
        span = pct * 360.0
        pad = gap_deg if span > gap_deg * 2.5 else max(0.35, span * 0.07)
        start = frac_acc * 360.0 + pad / 2.0
        end = frac_acc * 360.0 + span - pad / 2.0
        if end < start:
            end = start
        mid = (start + end) / 2.0
        lx, ly = _pt(mid, r_label)
        ox, oy = _pt(mid, 49.8)
        sl["path_d"] = _annular_path(start, end)
        sl["label_x"] = f"{lx:.2f}"
        sl["label_y"] = f"{ly:.2f}"
        sl["anchor_x"] = f"{ox:.2f}"
        sl["anchor_y"] = f"{oy:.2f}"
        sl["show_label"] = pct >= 0.045
        frac_acc += pct

    return slices, total


def _build_expense_mix_donut(by_account: list[dict], kpis: dict, *, head: int = 5) -> dict:
    """
    مزيج المصروفات للمدير: أعلى بنود + أخرى + قرار باريتو.
    المركز = إجمالي المصروف · التسمية = أين تتركز الرقابة.
    """
    ranked: list[dict] = []
    for row in by_account or []:
        if str(row.get("kind") or "") != "expense":
            continue
        impact = abs(round(_f(row.get("mv_cr")) - _f(row.get("mv_dr")), 2))
        if impact <= 0:
            continue
        ranked.append(
            {
                "account_code": str(row.get("account_code") or ""),
                "account_name": str(row.get("account_name") or row.get("account_code") or ""),
                "impact": impact,
            }
        )
    ranked.sort(key=lambda r: (-r["impact"], r["account_code"]))
    total_expense = abs(_f(kpis.get("expense")))
    if not total_expense:
        total_expense = round(sum(r["impact"] for r in ranked), 2)

    colors = ("#FB7185", "#FBBF24", "#22D3EE", "#A78BFA", "#34D399", "#64748B")
    head_n = max(1, min(int(head or 5), 8))
    head_rows = ranked[:head_n]
    rest_impact = round(sum(r["impact"] for r in ranked[head_n:]), 2)

    parts: list[dict] = []
    for i, row in enumerate(head_rows):
        name = row["account_name"]
        parts.append(
            {
                "key": f"exp-{row['account_code'] or i}",
                "name": name,
                "full_name": name,
                "account_code": row["account_code"],
                "amount": row["impact"],
                "amount_display": _fmt_money(row["impact"]),
                "amount_compact": _fmt_compact(row["impact"]),
                "color": colors[i % len(colors)],
                "tone": "exp",
            }
        )
    if rest_impact > 0:
        parts.append(
            {
                "key": "other",
                "name": "أخرى",
                "full_name": f"باقي بنود المصروف ({max(0, len(ranked) - head_n)} حساب)",
                "account_code": "",
                "amount": rest_impact,
                "amount_display": _fmt_money(rest_impact),
                "amount_compact": _fmt_compact(rest_impact),
                "color": colors[-1],
                "tone": "other",
            }
        )

    slices, total = _annular_slices_from_parts(parts)
    if total <= 0 and total_expense > 0:
        total = total_expense

    cum = 0.0
    top3_names: list[str] = []
    for i, sl in enumerate(slices):
        if sl.get("key") == "other":
            continue
        cum = round(cum + float(sl.get("share_pct") or 0), 1)
        sl["cum_share_pct"] = cum
        sl["cum_share_display"] = _share_display(cum)
        if i < 3:
            top3_names.append(str(sl.get("name") or ""))

    top1 = float(slices[0]["share_pct"]) if slices else 0.0
    top3_share = 0.0
    named = [s for s in slices if s.get("key") != "other"]
    for s in named[:3]:
        top3_share = round(top3_share + float(s.get("share_pct") or 0), 1)

    if not slices:
        decision = "لا مصروفات ضمن الفلتر للمراجعة."
        decision_tone = "ok"
    elif top1 >= 40.0:
        decision = (
            f"تركيز عالٍ: «{slices[0].get('full_name') or slices[0].get('name')}» "
            f"= {_share_display(top1)} من المصروف — أولوية مراجعة العقود والالتزامات الثابتة."
        )
        decision_tone = "high"
    elif top3_share >= 60.0:
        names = " · ".join(top3_names[:3])
        decision = (
            f"أعلى 3 بنود = {_share_display(top3_share)} من المصروف "
            f"({names}) — راجعها قبل باقي البنود."
        )
        decision_tone = "medium"
    else:
        decision = "توزيع المصروف متوازن نسبيًا — راقب الانحراف الشهري لا التركيز."
        decision_tone = "ok"

    return {
        "slices": slices,
        "center": {
            "label": "المصروفات",
            "value_display": _fmt_compact(total_expense or total),
            "value_full": _fmt_money(total_expense or total),
        },
        "total": total_expense or total,
        "total_display": _fmt_money(total_expense or total),
        "total_compact": _fmt_compact(total_expense or total),
        "top3_share": top3_share,
        "top3_share_display": _share_display(top3_share),
        "decision": decision,
        "decision_tone": decision_tone,
        "has_data": bool(slices),
        "item_count": len(ranked),
    }


def _pnl_composition_donut(kpis: dict) -> dict:
    """شرائح حلقة سميكة (أسلوب المرجع) — النسب داخل الشريحة · المركز = الإيراد."""
    revenue = _f(kpis.get("revenue"))
    cogs = abs(_f(kpis.get("cogs")))
    expense = abs(_f(kpis.get("expense")))
    net = _f(kpis.get("net"))
    net_abs = abs(net)
    net_kind = str(kpis.get("net_kind") or "zero")
    net_title = str(kpis.get("net_title") or "الصافي")
    parts = [
        {
            "key": "cogs",
            "name": "التكلفة",
            "amount": cogs,
            "amount_display": _fmt_money(cogs),
            "color": "#7c5cbf",
            "tone": "cogs",
        },
        {
            "key": "expense",
            "name": "المصروفات",
            "amount": expense,
            "amount_display": _fmt_money(expense),
            "color": "#22d3ee",
            "tone": "exp",
        },
        {
            "key": "net",
            "name": net_title,
            "amount": net_abs,
            "amount_display": _fmt_money(net),
            "color": "#0f9f8f" if net_kind == "profit" else (
                "#94a3b8" if net_kind == "zero" else "#e11d48"
            ),
            "tone": net_kind,
        },
    ]
    total = round(sum(float(p["amount"]) for p in parts), 2)
    slices: list[dict] = []
    for part in parts:
        amt = float(part["amount"])
        if amt <= 0 and total > 0:
            continue
        share = (amt / total * 100.0) if total else 0.0
        slices.append(
            {
                **part,
                "share_pct": round(share, 1),
                "share_display": f"{round(share):.0f}%" if share >= 10 else (
                    f"{share:.1f}%" if abs(share - round(share)) > 0.05 else f"{int(round(share))}%"
                ),
            }
        )

    r_out = 48.0
    r_in = 26.8
    r_label = (r_out + r_in) / 2.0
    cx = cy = 50.0
    gap_deg = 1.8
    frac_acc = 0.0

    def _pt(angle_deg: float, radius: float) -> tuple[float, float]:
        a = math.radians(angle_deg)
        return (cx + radius * math.sin(a), cy - radius * math.cos(a))

    def _annular_path(start_deg: float, end_deg: float) -> str:
        sweep = end_deg - start_deg
        if sweep <= 0.05:
            return ""
        large = 1 if sweep > 180 else 0
        x0, y0 = _pt(start_deg, r_out)
        x1, y1 = _pt(end_deg, r_out)
        x2, y2 = _pt(end_deg, r_in)
        x3, y3 = _pt(start_deg, r_in)
        return (
            f"M {x0:.3f} {y0:.3f} "
            f"A {r_out:.3f} {r_out:.3f} 0 {large} 1 {x1:.3f} {y1:.3f} "
            f"L {x2:.3f} {y2:.3f} "
            f"A {r_in:.3f} {r_in:.3f} 0 {large} 0 {x3:.3f} {y3:.3f} Z"
        )

    for sl in slices:
        amt = float(sl["amount"])
        pct = (amt / total) if total else 0.0
        span = pct * 360.0
        pad = gap_deg if span > gap_deg * 2.5 else max(0.35, span * 0.07)
        start = frac_acc * 360.0 + pad / 2.0
        end = frac_acc * 360.0 + span - pad / 2.0
        if end < start:
            end = start
        mid = (start + end) / 2.0
        lx, ly = _pt(mid, r_label)
        ox, oy = _pt(mid, 49.8)
        sl["path_d"] = _annular_path(start, end)
        sl["angle_deg"] = round(mid, 2)
        # إحداثيات كنص بنقطة عشرية — تجنّب فاصلة التعريب في SVG
        sl["label_x"] = f"{lx:.2f}"
        sl["label_y"] = f"{ly:.2f}"
        sl["anchor_x"] = f"{ox:.2f}"
        sl["anchor_y"] = f"{oy:.2f}"
        sl["callout_y"] = f"{oy:.1f}"
        sl["show_label"] = pct >= 0.03
        frac_acc += pct

    return {
        "slices": slices,
        "center": {
            "label": "الإيرادات",
            "value_display": _fmt_money(revenue),
            "hint": str(kpis.get("gross_margin_pct_display") or ""),
            "net_pct_display": str(kpis.get("net_pct_display") or ""),
        },
        "total": total,
        "total_display": _fmt_money(total),
    }


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
        master_where += " AND CONN_BRN_NO = CASE WHEN REGEXP_LIKE(:mbrn, '^ *[0-9]+ *$') THEN TO_NUMBER(:mbrn) END"
        master_params["mbrn"] = brn
    if wanted:
        in_binds = {f"c{i}": n for i, n in enumerate(wanted)}
        in_clause = ", ".join(_num_bind(f":{k}") for k in in_binds)
        master_where += f" AND CASH_NO IN ({in_clause})"
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
            "chart": [],
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
        in_clause = ", ".join(_num_bind(f":{k}") for k in in_binds)
        cash_filter_sql += f" AND p.CASH_NO IN ({in_clause})"
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
          ON p.CASH_NO = c.CASH_NO
         AND p.A_CODE = c.A_CODE
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

    # مخطط عمودي: أعلى صناديق حسب |الرصيد الختامي|
    ranked = sorted(out_rows, key=lambda r: abs(_f(r.get("close_bal"))), reverse=True)
    peak = abs(_f(ranked[0].get("close_bal"))) if ranked else 0.0
    chart: list[dict] = []
    for row in ranked[:18]:
        close = _f(row.get("close_bal"))
        # لون العمود حسب إشارة الرصيد الختامي (موجب/سالب)، لا حالة المطابقة
        if close < 0:
            tone = "loss"
        elif close > 0:
            tone = "profit"
        else:
            tone = "zero"
        chart.append(
            {
                "cash_no": row["cash_no"],
                "cash_name": row["cash_name"],
                "close_bal": close,
                "close_display": row["close_display"],
                "ok": row["ok"],
                "status": row["status"],
                "tone": tone,
                "bar_pct": round((abs(close) / peak) * 100.0, 1) if peak else 0.0,
            }
        )

    return {
        "rows": out_rows,
        "chart": chart,
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


def _build_top_accounts(by_account: list[dict], limit: int = 25) -> tuple[list[dict], dict, dict]:
    """أعلى حسابات المصروفات (|صافي الحركة|) من الأعلى إلى الأدنى + إجماليات + محور المخطط."""
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
    avg_impact = (impact_sum / len(out)) if out else 0.0
    peak = max_impact if max_impact else 0.01

    def _axis_bar(value: float) -> float:
        return round(min(100.0, abs(value) / peak * 100.0), 1) if peak else 0.0

    totals = {
        "count": len(out),
        "net": net_sum,
        "impact": impact_sum,
        "net_display": _fmt_money(net_sum),
        "impact_display": _fmt_money(impact_sum),
    }
    expense_axis = {
        "ref_pct": _axis_bar(avg_impact),
        "peak_display": _fmt_money(peak),
        "ticks": [
            {"pct": 0, "label": "0"},
            {"pct": 25, "label": _fmt_money(peak * 0.25)},
            {"pct": 50, "label": _fmt_money(peak * 0.5)},
            {"pct": 75, "label": _fmt_money(peak * 0.75)},
            {"pct": 100, "label": _fmt_money(peak)},
        ],
    }
    return out, totals, expense_axis


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
        bar_pct = f"{min(100.0, abs(net) / max_abs * 100.0):.1f}" if max_abs else "0"
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


# ─── مبيعات نقاط البيع التي لم تُنقل لفواتير المبيعات بعد (خارج الدفتر) ───
# POSTED=1 على رأس POS يعني نُقلت لـ IAS_BILL ثم إلى IAS_POST_DTL؛ غير ذلك لا يظهر
# في الدفتر. تُعرض في بطاقة مستقلة ولا تُضاف لأرقام القائمة.
# الشرائح الأقدم من نافذة الحداثة تُخزَّن طويلاً لأن ترحيل فواتير قديمة جداً نادر.
_UNPOSTED_RECENT_DAYS = 45
_UNPOSTED_RECENT_SLICE_DAYS = 15
_UNPOSTED_OLD_TTL = 6 * 3600
_UNPOSTED_WORKERS = 4
_UNPOSTED_TIMEOUT_SEC = 110
_UNPOSTED_STOCK_QTY = "NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(d.P_SIZE, 1))"


def _unposted_slices(d_from: date, d_to: date, today: date) -> list[tuple[date, date, bool]]:
    """(بداية، نهاية شاملة، قابلة للتخزين): أشهر كاملة قبل نافذة الحداثة، ثم شرائح قصيرة."""
    recent_start = today - timedelta(days=_UNPOSTED_RECENT_DAYS)
    out: list[tuple[date, date, bool]] = []
    cur = d_from
    while cur <= d_to:
        if cur >= recent_start:
            end = min(d_to, cur + timedelta(days=_UNPOSTED_RECENT_SLICE_DAYS - 1))
            out.append((cur, end, False))
        else:
            if cur.month == 12:
                month_end = date(cur.year, 12, 31)
            else:
                month_end = date(cur.year, cur.month + 1, 1) - timedelta(days=1)
            end = min(month_end, d_to, recent_start - timedelta(days=1))
            out.append((cur, end, True))
        cur = end + timedelta(days=1)
    return out


def _unposted_pos_sql() -> str:
    """رؤوس POS غير المنقولة (قيادة بفهرس التاريخ) ثم تفاصيلها بالمفتاح — مسح واحد عبر MATERIALIZE."""
    pos = _pos_owner()
    return f"""
        WITH h AS (
            SELECT /*+ MATERIALIZE INDEX(m POSBILLMST_BILLDATEUSRBRN) */
                   m.BILL_NO, m.BRN_NO, m.W_CODE, m.BILL_DATE, m.BILL_AMT
            FROM {pos}.IAS_POS_BILL_MST m
            WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
              AND {_hung_ok("m")}
              AND (m.POSTED IS NULL OR m.POSTED = 0)
        )
        SELECT 'H' AS K, h.BRN_NO AS BRN, TRUNC(h.BILL_DATE) AS D,
               NULL AS ITEM_CODE, NULL AS W_CODE,
               COUNT(*) AS N, SUM(NVL(h.BILL_AMT, 0)) AS AMT, 0 AS STOCK_QTY
        FROM h
        GROUP BY h.BRN_NO, TRUNC(h.BILL_DATE)
        UNION ALL
        SELECT /*+ LEADING(h d) USE_NL(d) INDEX(d IAS_POS_INDX_BILL_DTL) */
               'L', h.BRN_NO, TRUNC(h.BILL_DATE, 'MM'),
               TO_CHAR(d.I_CODE), TO_CHAR(NVL(d.W_CODE, h.W_CODE)),
               COUNT(*), 0, SUM({_UNPOSTED_STOCK_QTY})
        FROM h
        JOIN {pos}.IAS_POS_BILL_DTL d
          ON d.BILL_NO = h.BILL_NO AND d.BRN_NO = h.BRN_NO
        WHERE d.I_CODE IS NOT NULL
        GROUP BY h.BRN_NO, TRUNC(h.BILL_DATE, 'MM'), d.I_CODE, NVL(d.W_CODE, h.W_CODE)
    """


def _unposted_pos_return_sql() -> str:
    """مرتجع POS غير المنقول — جدول صغير بلا فهرس تاريخ، مسح واحد للفترة كلها."""
    pos = _pos_owner()
    return f"""
        WITH h AS (
            SELECT /*+ MATERIALIZE */
                   m.RT_BILL_NO, m.BRN_NO, m.W_CODE, m.RT_BILL_DATE, m.RT_BILL_AMT
            FROM {pos}.IAS_POS_RT_BILL_MST m
            WHERE m.RT_BILL_DATE >= :d_from AND m.RT_BILL_DATE < :d_to_excl
              AND {_hung_ok("m")}
              AND (m.POSTED IS NULL OR m.POSTED = 0)
        )
        SELECT 'H' AS K, h.BRN_NO AS BRN, TRUNC(h.RT_BILL_DATE) AS D,
               NULL AS ITEM_CODE, NULL AS W_CODE,
               COUNT(*) AS N, SUM(NVL(h.RT_BILL_AMT, 0)) AS AMT, 0 AS STOCK_QTY
        FROM h
        GROUP BY h.BRN_NO, TRUNC(h.RT_BILL_DATE)
        UNION ALL
        SELECT /*+ LEADING(h d) USE_NL(d) INDEX(d IAS_POS_INDX_RTBILL_DTL) */
               'L', h.BRN_NO, TRUNC(h.RT_BILL_DATE, 'MM'),
               TO_CHAR(d.I_CODE), TO_CHAR(NVL(d.W_CODE, h.W_CODE)),
               COUNT(*), 0, SUM({_UNPOSTED_STOCK_QTY})
        FROM h
        JOIN {pos}.IAS_POS_RT_BILL_DTL d
          ON d.RT_BILL_NO = h.RT_BILL_NO AND d.BRN_NO = h.BRN_NO
        WHERE d.I_CODE IS NOT NULL
        GROUP BY h.BRN_NO, TRUNC(h.RT_BILL_DATE, 'MM'), d.I_CODE, NVL(d.W_CODE, h.W_CODE)
    """


def _as_plain_date(value: Any) -> date | None:
    if value is None:
        return None
    if hasattr(value, "date") and callable(value.date):
        try:
            return value.date()
        except Exception:
            return None
    if isinstance(value, date):
        return value
    return None


def _unposted_unit_cost_fn():
    cmap = _item_unit_cost_map()
    wh_cmap = _item_wh_unit_cost_map()

    def unit_cost(item: str, wh: str) -> float:
        by_wh = wh_cmap.get(item)
        if by_wh:
            if wh and wh in by_wh:
                return float(by_wh[wh] or 0)
            for val in by_wh.values():
                if val:
                    return float(val)
        return float(cmap.get(item) or 0)

    return unit_cost


def _aggregate_unposted(sales_rows: list[dict], return_rows: list[dict], unit_cost) -> dict:
    """صافي (بيع − مرتجع) لكل فرع. المبلغ بدون ضريبة؛ التكلفة بمتوسط التكلفة الحالي."""
    by_branch: dict[str, dict] = {}
    missing_cost: set[str] = set()

    def bucket(brn: str) -> dict:
        return by_branch.setdefault(
            brn,
            {"amount": 0.0, "cost": 0.0, "bills": 0, "returns": 0, "oldest": None},
        )

    for rows, sign in ((sales_rows, 1.0), (return_rows, -1.0)):
        for row in rows:
            brn = _norm_brn_code(row.get("BRN"))
            day = _as_plain_date(row.get("D"))
            if not brn or day is None:
                continue
            b = bucket(brn)
            if str(row.get("K") or "") == "H":
                b["amount"] += _f(row.get("AMT")) * sign
                if sign > 0:
                    b["bills"] += int(row.get("N") or 0)
                else:
                    b["returns"] += int(row.get("N") or 0)
                if b["oldest"] is None or day < b["oldest"]:
                    b["oldest"] = day
            else:
                item = str(row.get("ITEM_CODE") or "").strip()
                wh = str(row.get("W_CODE") or "").strip()
                qty = float(row.get("STOCK_QTY") or 0)
                uc = unit_cost(item, wh)
                if not uc and qty:
                    missing_cost.add(item)
                b["cost"] += qty * uc * sign

    for b in by_branch.values():
        b["amount"] = round(b["amount"], 2)
        b["cost"] = round(b["cost"], 2)
    return {"by_branch": by_branch, "missing_cost_items": len(missing_cost)}


def fetch_unposted_pos_sales(date_from, date_to, *, today: date | None = None) -> dict:
    """مبيعات POS غير المنقولة لكل الفروع ضمن الفترة (تُصفّى بالفرع لاحقاً في بايثون)."""
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    today = today or date.today()
    if d_from > today:
        return _aggregate_unposted([], [], lambda _i, _w: 0.0)

    last_day = min(d_to, today)
    sales_sql = _unposted_pos_sql()
    parts: list[list[dict] | None] = []
    jobs = []
    job_slots: list[tuple[int, str | None]] = []
    for start, end, cacheable in _unposted_slices(d_from, last_day, today):
        key = f"income:unposted:v2:{start}:{end}" if cacheable else None
        hit = cache.get(key) if key else None
        if isinstance(hit, list):
            parts.append(hit)
            continue
        parts.append(None)
        bind = {"d_from": start, "d_to_excl": end + timedelta(days=1)}

        def job(b=bind):
            with oracle_session():
                return _fetch_all(sales_sql, b)

        jobs.append(job)
        job_slots.append((len(parts) - 1, key))

    return_sql = _unposted_pos_return_sql()
    return_bind = {"d_from": d_from, "d_to_excl": last_day + timedelta(days=1)}

    def return_job():
        with oracle_session():
            return _fetch_all(return_sql, return_bind)

    results = _run_parallel(
        [*jobs, return_job],
        max_workers=_UNPOSTED_WORKERS,
        timeout_sec=_UNPOSTED_TIMEOUT_SEC,
    )
    for (slot, key), rows in zip(job_slots, results[:-1]):
        parts[slot] = rows or []
        if key:
            try:
                cache.set(key, parts[slot], _UNPOSTED_OLD_TTL)
            except Exception:
                pass
    sales_rows = [row for part in parts for row in (part or [])]
    return _aggregate_unposted(sales_rows, results[-1] or [], _unposted_unit_cost_fn())


# ─── فواتير الآجل (نظام المبيعات) التي لم تصل لدفتر IAS_POST_DTL بعد ───
# BILL_POST على IAS_BILL_MST غير موثوق: فحص فعلي أظهر فواتير BILL_POST=0
# لها بالفعل قيود في IAS_POST_DTL (أي محسوبة أصلاً ضمن «الإيرادات»). المعيار
# الصحيح هو عدم وجود أي قيد يطابق رقم/سلسلة/نوع الفاتورة في IAS_POST_DTL
# (NOT EXISTS) — هذا ما يعنيه «لم تصل للدفتر» فعلياً. تقتصر على الآجل (4/8)؛
# النقدي (1/5) مستبعد عمداً لأنه مرآة لمبيعات نقاط البيع ولا توجد حالياً
# قاعدة موثوقة لربطه بفاتورة POS الأصل دون تكرار.
_UNPOSTED_BILL_DOC_TYPES = (4, 8)


def _unposted_bill_sql() -> str:
    """رؤوس فواتير الآجل بلا أي قيد في IAS_POST_DTL بعد، ثم تفاصيلها للتكلفة (STK_COST جاهزة)."""
    sch = _schema()
    types = ", ".join(str(t) for t in _UNPOSTED_BILL_DOC_TYPES)
    return f"""
        WITH h AS (
            SELECT /*+ MATERIALIZE */
                   m.BILL_NO, m.BILL_SER, m.BILL_DOC_TYPE, m.BRN_NO,
                   m.BILL_DATE, m.BILL_AMT
            FROM {sch}.IAS_BILL_MST m
            WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
              AND m.BILL_DOC_TYPE IN ({types})
              AND {_bill_mst_ok("m")}
              AND NOT EXISTS (
                SELECT 1 FROM {sch}.IAS_POST_DTL p
                WHERE p.DOC_NO = m.BILL_NO
                  AND p.DOC_SER = m.BILL_SER
                  AND p.DOC_TYPE = m.BILL_DOC_TYPE
              )
        )
        SELECT 'H' AS K, h.BRN_NO AS BRN, TRUNC(h.BILL_DATE) AS D,
               NULL AS ITEM_CODE, 0 AS COST,
               COUNT(*) AS N, SUM(NVL(h.BILL_AMT, 0)) AS AMT
        FROM h
        GROUP BY h.BRN_NO, TRUNC(h.BILL_DATE)
        UNION ALL
        SELECT 'L', h.BRN_NO, TRUNC(h.BILL_DATE, 'MM'), TO_CHAR(d.I_CODE),
               SUM(NVL(d.STK_COST, 0) * NVL(d.I_QTY, 0)), 0, 0
        FROM h
        JOIN {sch}.IAS_BILL_DTL d
          ON d.BILL_NO = h.BILL_NO
         AND d.BILL_SER = h.BILL_SER
         AND d.BILL_DOC_TYPE = h.BILL_DOC_TYPE
        WHERE d.I_CODE IS NOT NULL
        GROUP BY h.BRN_NO, TRUNC(h.BILL_DATE, 'MM'), d.I_CODE
    """


def _unposted_bill_return_sql() -> str:
    """مرتجع الآجل بلا أي قيد في IAS_POST_DTL — نفس منطق الرأس/التفاصيل."""
    sch = _schema()
    types = ", ".join(str(t) for t in _UNPOSTED_BILL_DOC_TYPES)
    return f"""
        WITH h AS (
            SELECT /*+ MATERIALIZE */
                   m.RT_BILL_NO, m.RT_BILL_SER, m.RT_BILL_DOC_TYPE, m.BRN_NO,
                   m.RT_BILL_DATE, m.BILL_AMT
            FROM {sch}.IAS_RT_BILL_MST m
            WHERE m.RT_BILL_DATE >= :d_from AND m.RT_BILL_DATE < :d_to_excl
              AND m.RT_BILL_DOC_TYPE IN ({types})
              AND {_rt_bill_mst_ok("m")}
              AND NOT EXISTS (
                SELECT 1 FROM {sch}.IAS_POST_DTL p
                WHERE p.DOC_NO = m.RT_BILL_NO
                  AND p.DOC_SER = m.RT_BILL_SER
                  AND p.DOC_TYPE = m.RT_BILL_DOC_TYPE
              )
        )
        SELECT 'H' AS K, h.BRN_NO AS BRN, TRUNC(h.RT_BILL_DATE) AS D,
               NULL AS ITEM_CODE, 0 AS COST,
               COUNT(*) AS N, SUM(NVL(h.BILL_AMT, 0)) AS AMT
        FROM h
        GROUP BY h.BRN_NO, TRUNC(h.RT_BILL_DATE)
        UNION ALL
        SELECT 'L', h.BRN_NO, TRUNC(h.RT_BILL_DATE, 'MM'), TO_CHAR(d.I_CODE),
               SUM(NVL(d.STK_COST, 0) * NVL(d.I_QTY, 0)), 0, 0
        FROM h
        JOIN {sch}.IAS_RT_BILL_DTL d
          ON d.RT_BILL_NO = h.RT_BILL_NO
         AND d.RT_BILL_SER = h.RT_BILL_SER
         AND d.RT_BILL_DOC_TYPE = h.RT_BILL_DOC_TYPE
        WHERE d.I_CODE IS NOT NULL
        GROUP BY h.BRN_NO, TRUNC(h.RT_BILL_DATE, 'MM'), d.I_CODE
    """


def _aggregate_unposted_bill(sales_rows: list[dict], return_rows: list[dict]) -> dict:
    """صافي (بيع − مرتجع) لكل فرع لفواتير الآجل غير المرحّلة. التكلفة من STK_COST مباشرة."""
    by_branch: dict[str, dict] = {}

    def bucket(brn: str) -> dict:
        return by_branch.setdefault(
            brn,
            {"amount": 0.0, "cost": 0.0, "bills": 0, "returns": 0, "oldest": None},
        )

    for rows, sign in ((sales_rows, 1.0), (return_rows, -1.0)):
        for row in rows:
            brn = _norm_brn_code(row.get("BRN"))
            day = _as_plain_date(row.get("D"))
            if not brn or day is None:
                continue
            b = bucket(brn)
            if str(row.get("K") or "") == "H":
                b["amount"] += _f(row.get("AMT")) * sign
                if sign > 0:
                    b["bills"] += int(row.get("N") or 0)
                else:
                    b["returns"] += int(row.get("N") or 0)
                if b["oldest"] is None or day < b["oldest"]:
                    b["oldest"] = day
            else:
                b["cost"] += _f(row.get("COST")) * sign

    for b in by_branch.values():
        b["amount"] = round(b["amount"], 2)
        b["cost"] = round(b["cost"], 2)
    return {"by_branch": by_branch}


def fetch_unposted_bill_sales(date_from, date_to) -> dict:
    """فواتير الآجل (نظام المبيعات) غير المرحّلة محاسبياً — كل الفروع ضمن الفترة."""
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    d_to_excl = d_to + timedelta(days=1)
    sales_sql = _unposted_bill_sql()
    return_sql = _unposted_bill_return_sql()
    bind = {"d_from": d_from, "d_to_excl": d_to_excl}

    def sales_job():
        with oracle_session():
            return _fetch_all(sales_sql, bind)

    def return_job():
        with oracle_session():
            return _fetch_all(return_sql, bind)

    results = _run_parallel(
        [sales_job, return_job],
        max_workers=2,
        timeout_sec=_UNPOSTED_TIMEOUT_SEC,
    )
    return _aggregate_unposted_bill(results[0] or [], results[1] or [])


def _merge_unposted_sources(pos_unposted: dict, bill_unposted: dict) -> dict:
    """يجمع غير مرحّل نقاط البيع + الآجل في نتيجة واحدة، مع إجمالي كل مصدر لعرضه في التلميح."""
    by_branch: dict[str, dict] = {}
    pos_total = bill_total = 0.0

    for source, total_key in ((pos_unposted, "pos"), (bill_unposted, "bill")):
        for brn, b in (source.get("by_branch") or {}).items():
            merged = by_branch.setdefault(
                brn,
                {"amount": 0.0, "cost": 0.0, "bills": 0, "returns": 0, "oldest": None},
            )
            merged["amount"] += _f(b.get("amount"))
            merged["cost"] += _f(b.get("cost"))
            merged["bills"] += int(b.get("bills") or 0)
            merged["returns"] += int(b.get("returns") or 0)
            oldest = b.get("oldest")
            if oldest and (merged["oldest"] is None or oldest < merged["oldest"]):
                merged["oldest"] = oldest
            if total_key == "pos":
                pos_total += _f(b.get("amount"))
            else:
                bill_total += _f(b.get("amount"))

    for b in by_branch.values():
        b["amount"] = round(b["amount"], 2)
        b["cost"] = round(b["cost"], 2)
    return {
        "by_branch": by_branch,
        "missing_cost_items": int(pos_unposted.get("missing_cost_items") or 0),
        "pos_amount": round(pos_total, 2),
        "bill_amount": round(bill_total, 2),
    }


def _unposted_summary(unposted: dict, branch_code: str) -> dict:
    """بيانات بطاقة «مبيعات لم تُرحّل» بعد تصفية الفرع."""
    names = _branch_names()
    brn = _norm_brn_code(branch_code)
    amount = cost = 0.0
    bills = returns = 0
    oldest: date | None = None
    rows: list[dict] = []
    for code, b in (unposted.get("by_branch") or {}).items():
        if brn and code != brn:
            continue
        if not (b["amount"] or b["cost"] or b["bills"] or b["returns"]):
            continue
        amount += b["amount"]
        cost += b["cost"]
        bills += b["bills"]
        returns += b["returns"]
        if b["oldest"] and (oldest is None or b["oldest"] < oldest):
            oldest = b["oldest"]
        rows.append(
            {
                "branch_code": code,
                "branch_name": names.get(code, code),
                "amount": b["amount"],
                "amount_display": _fmt_money(b["amount"]),
            }
        )
    rows.sort(key=lambda r: -r["amount"])
    amount = round(amount, 2)
    cost = round(cost, 2)
    gross = round(amount - cost, 2)
    margin_pct = round(gross / amount * 100.0, 2) if amount else 0.0
    pos_amount = round(_f(unposted.get("pos_amount")), 2)
    bill_amount = round(_f(unposted.get("bill_amount")), 2)
    return {
        "available": True,
        "has_data": bool(rows),
        "amount": amount,
        "cost": cost,
        "gross": gross,
        "bills": bills,
        "returns": returns,
        "oldest": oldest.isoformat() if oldest else "",
        "missing_cost_items": int(unposted.get("missing_cost_items") or 0),
        "amount_display": _fmt_money(amount),
        "cost_display": _fmt_money(cost),
        "gross_display": _fmt_money(gross),
        "margin_pct_display": f"{margin_pct:,.2f}%",
        "bills_display": f"{bills:,}",
        "returns_display": f"{returns:,}",
        "branch_count": len(rows),
        "top_branches": rows[:3],
        "pos_amount": pos_amount,
        "pos_amount_display": _fmt_money(pos_amount),
        "bill_amount": bill_amount,
        "bill_amount_display": _fmt_money(bill_amount),
    }


def _cache_key(
    d_from: date,
    d_to: date,
    branch_code: str,
    cc_code: str,
    posted_only: bool,
) -> str:
    return (
        f"income:stmt:v56:{d_from}:{d_to}:"
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
    top_accounts, top_accounts_totals, expense_axis = _build_top_accounts(by_account, limit=25)
    expense_mix = _build_expense_mix_donut(by_account, kpis, head=5)
    graphic = _pnl_graphic_panels(kpis)

    monthly_rows = _fetch_income_kind_rows(
        d_from,
        d_to,
        branch_code=brn,
        cc_code=cc,
        posted_only=use_posted,
        by_month=True,
    )
    monthly_trend = _build_monthly_trend(d_from, d_to, monthly_rows)
    executive_alerts = _build_executive_alerts(
        kpis=kpis,
        by_branch_profit=by_branch_profit,
        top_accounts=top_accounts,
        monthly_trend=monthly_trend,
    )

    ttl = _INCOME_CACHE_TTL
    unposted: dict = {"available": False, "has_data": False}
    if cc:
        unposted["reason"] = "المبيعات غير المرحّلة بلا مركز تكلفة"
    else:
        try:
            merged = _merge_unposted_sources(
                fetch_unposted_pos_sales(d_from, d_to),
                fetch_unposted_bill_sales(d_from, d_to),
            )
            unposted = _unposted_summary(merged, brn)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unposted sales unavailable: %s", exc)
            unposted["error"] = "تعذّرت قراءة المبيعات غير المرحّلة."
            ttl = 120

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
        "structure": graphic["structure"],
        "waterfall": graphic["waterfall"],
        "waterfall_axis": graphic.get("waterfall_axis") or {},
        "margin_bars": graphic["margin_bars"],
        "composition": graphic.get("composition") or {},
        "monthly_trend": monthly_trend,
        "executive_alerts": executive_alerts,
        "by_account": by_account,
        "by_account_total_count": len(by_account),
        "account_totals": account_totals,
        "by_branch_profit": by_branch_profit,
        "reconciliation": reconciliation,
        "kpi_reconciliation": kpi_reconciliation,
        "top_accounts": top_accounts,
        "top_accounts_totals": top_accounts_totals,
        "expense_axis": expense_axis,
        "expense_mix": expense_mix,
        "unposted": unposted,
    }
    try:
        cache.set(key, result, ttl)
    except Exception:
        pass
    return result
