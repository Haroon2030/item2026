"""ميزان المراجعة — أرصدة نهائية / تفصيلي تحليلي من IAS_POST_DTL (أونكس)."""

from __future__ import annotations

import io
import logging
from html import escape
from typing import Any

from django.core.cache import cache
from django.http import HttpResponse

from .oracle_income import fetch_income_branches
from .oracle_stock import (
    OracleStockError,
    _as_date,
    _branch_names,
    _fetch_all,
    _schema,
)

logger = logging.getLogger(__name__)

_CACHE_TTL = 120
_DETAIL_LIMIT = 8000
# استبعاد مبالغ ≥ مليار (قيود شاذة تضخّم الإجمالي دون أثر على الرصيد)
_SANE_AMT = "ABS(NVL(p.AMT, 0)) < 1000000000"

# أنواع الحساب التحليلي في أونكس (ACCOUNT.AC_DTL_TYP / IAS_POST_DTL.AC_DTL_TYP)
_DTL_TYP_LABELS = {
    "0": "",
    "1": "صندوق",
    "2": "بنك",
    "3": "عميل",
    "4": "مورد",
    "5": "مركز تكلفة",
    "6": "أخرى",
    "7": "موظف",
}


def _dtl_typ_label(dtl_typ: Any) -> str:
    key = str(dtl_typ or "0").strip() or "0"
    return _DTL_TYP_LABELS.get(key, key)


def _f(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _fmt_money(value: float) -> str:
    return f"{_f(value):,.2f}"


def _amt_cell(value: float) -> str:
    return _fmt_money(value) if abs(_f(value)) > 0.0005 else ""


def _split_dr_cr(net: float) -> tuple[float, float]:
    n = _f(net)
    if n >= 0:
        return n, 0.0
    return 0.0, -n


def _book_branch(row: dict, names: dict[str, str]) -> tuple[str, str]:
    """الفرع الدفتري BRN_NO — أساس الرصيد النهائي لكل حساب."""
    brn = str(row.get("BRN_NO") or "").strip()
    if brn:
        return brn, names.get(brn) or f"فرع {brn}"
    return "", "—"


def _benef_branch(row: dict, names: dict[str, str]) -> tuple[str, str]:
    """الفرع المستفيد كأونكس = DOC_BRN_NO (قد يكون فارغاً)."""
    doc_brn = str(row.get("DOC_BRN") or "").strip()
    if doc_brn:
        return doc_brn, names.get(doc_brn) or f"فرع {doc_brn}"
    return "", "—"


def _filters(
    *,
    branch_code: str = "",
    posted_only: bool = False,
) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    params: dict[str, Any] = {}
    if posted_only:
        parts.append("NVL(p.DOC_POST, 0) = 1")
    brn = str(branch_code or "").strip()
    if brn:
        parts.append("TO_CHAR(p.BRN_NO) = :brn")
        params["brn"] = brn
    return (" AND ".join(parts) if parts else "1=1"), params


def _cache_key(
    d_from,
    d_to,
    *,
    branch_code: str,
    posted_only: bool,
    hide_zero: bool,
    mode: str,
) -> str:
    return (
        f"trialbal:v18:{mode}:{d_from.isoformat()}:{d_to.isoformat()}:"
        f"{branch_code or '-'}:{int(posted_only)}:{int(hide_zero)}"
    )


def _normalize_mode(mode: str) -> str:
    m = str(mode or "").strip().lower()
    if m in {
        "detail",
        "detailed",
        "movement",
        "movements",
        "حركة",
        "تفصيلي",
    }:
        return "detail"
    if m in {
        "analytic",
        "analytical",
        "تحليلي",
    }:
        return "analytic"
    return "summary"


def peek_trial_balance(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    posted_only: bool = False,
    hide_zero: bool = True,
    mode: str = "summary",
) -> dict[str, Any] | None:
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    mode_n = _normalize_mode(mode)
    cached = cache.get(
        _cache_key(
            d_from,
            d_to,
            branch_code=str(branch_code or "").strip(),
            posted_only=posted_only,
            hide_zero=hide_zero,
            mode=mode_n,
        )
    )
    return cached if isinstance(cached, dict) else None


def _fetch_tb_summary_rows(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    posted_only: bool = False,
) -> list[dict]:
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")

    sch = _schema()
    extra, params = _filters(branch_code=branch_code, posted_only=posted_only)
    params.update({"dfrom": d_from, "dto": d_to})
    sane = _SANE_AMT

    return _fetch_all(
        f"""
        SELECT /*+ USE_HASH(p a) */
               TO_CHAR(a.A_CODE) AS A_CODE,
               MAX(a.A_NAME) AS A_NAME,
               TO_CHAR(p.BRN_NO) AS BRN_NO,
               NVL(TO_CHAR(p.DOC_BRN_NO), '') AS DOC_BRN,
               NVL(TO_CHAR(p.A_CY), 'SAR') AS A_CY,
               SUM(
                 CASE
                   WHEN (p.DOC_DATE < :dfrom OR p.DOC_TYPE = 0) AND {sane}
                   THEN NVL(p.AMT, 0) ELSE 0 END
               ) AS OPEN_NET,
               SUM(
                 CASE
                   WHEN p.DOC_DATE >= :dfrom AND p.DOC_DATE <= :dto
                    AND p.DOC_TYPE <> 0 AND {sane}
                   THEN NVL(p.DR_AMT, 0) ELSE 0 END
               ) AS MV_DR,
               SUM(
                 CASE
                   WHEN p.DOC_DATE >= :dfrom AND p.DOC_DATE <= :dto
                    AND p.DOC_TYPE <> 0 AND {sane}
                   THEN NVL(p.CR_AMT, 0) ELSE 0 END
               ) AS MV_CR
        FROM {sch}.IAS_POST_DTL p
        JOIN {sch}.ACCOUNT a ON a.A_CODE = p.A_CODE
        WHERE {extra} AND p.DOC_DATE <= :dto
        GROUP BY
          a.A_CODE,
          p.BRN_NO,
          NVL(TO_CHAR(p.DOC_BRN_NO), ''),
          NVL(TO_CHAR(p.A_CY), 'SAR')
        HAVING
          SUM(CASE WHEN (p.DOC_DATE < :dfrom OR p.DOC_TYPE = 0) AND {sane}
                   THEN NVL(p.AMT, 0) ELSE 0 END) <> 0
          OR SUM(CASE WHEN p.DOC_DATE >= :dfrom AND p.DOC_DATE <= :dto
                       AND p.DOC_TYPE <> 0 AND {sane} THEN 1 ELSE 0 END) > 0
        """,
        params,
    )


def _fetch_tb_detail_rows(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    posted_only: bool = False,
) -> list[dict]:
    """تفصيلي/تحليلي: حساب + رقم التحليلي + اسمه حسب نوع الحساب في أونكس.

    نوع التحليلي من دليل الحسابات ACCOUNT.AC_DTL_TYP (مع احتياطي من القيد):
      1 صندوق CASH_IN_HAND · 2 بنك CASH_AT_BANK · 3 عميل CUSTOMER
      4 مورد V_DETAILS · 5 مركز تكلفة COST_CENTERS · 7 موظف S_EMP
    """
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")

    sch = _schema()
    extra, params = _filters(branch_code=branch_code, posted_only=posted_only)
    params.update({"dfrom": d_from, "dto": d_to})
    sane = _SANE_AMT
    # نوع الدليل أولاً — نفس المنطق لكل الحسابات
    dtl_typ_expr = "NVL(NULLIF(a.AC_DTL_TYP, 0), NVL(p.AC_DTL_TYP, 0))"
    dtl_code_expr = f"""
      CASE
        WHEN {dtl_typ_expr} = 0 THEN ''
        ELSE NVL(TO_CHAR(p.AC_CODE_DTL), '')
      END
    """

    return _fetch_all(
        f"""
        SELECT /*+ USE_HASH(p a) */
               TO_CHAR(a.A_CODE) AS A_CODE,
               MAX(a.A_NAME) AS A_NAME,
               TO_CHAR({dtl_typ_expr}) AS AC_DTL_TYP,
               {dtl_code_expr} AS AC_CODE_DTL,
               MAX(
                 CASE
                   WHEN {dtl_typ_expr} = 1 THEN NVL(ch.CASH_NAME, TO_CHAR(p.AC_CODE_DTL))
                   WHEN {dtl_typ_expr} = 2 THEN NVL(bk.BANK_NAME, TO_CHAR(p.AC_CODE_DTL))
                   WHEN {dtl_typ_expr} = 3 THEN NVL(cu.C_A_NAME, TO_CHAR(p.AC_CODE_DTL))
                   WHEN {dtl_typ_expr} = 4 THEN NVL(vd.V_A_NAME, TO_CHAR(p.AC_CODE_DTL))
                   WHEN {dtl_typ_expr} = 5 THEN NVL(cc.CC_A_NAME, TO_CHAR(p.AC_CODE_DTL))
                   WHEN {dtl_typ_expr} = 7 THEN
                     TRIM(NVL(em.EMP_L_NM, '') || ' ' || NVL(em.EMP_F_NM, ''))
                   ELSE TO_CHAR(p.AC_CODE_DTL)
                 END
               ) AS DTL_NAME,
               TO_CHAR(p.BRN_NO) AS BRN_NO,
               NVL(TO_CHAR(p.DOC_BRN_NO), '') AS DOC_BRN,
               NVL(TO_CHAR(p.A_CY), 'SAR') AS A_CY,
               SUM(
                 CASE
                   WHEN (p.DOC_DATE < :dfrom OR p.DOC_TYPE = 0) AND {sane}
                   THEN NVL(p.AMT, 0) ELSE 0 END
               ) AS OPEN_NET,
               SUM(
                 CASE
                   WHEN p.DOC_DATE >= :dfrom AND p.DOC_DATE <= :dto
                    AND p.DOC_TYPE <> 0 AND {sane}
                   THEN NVL(p.DR_AMT, 0) ELSE 0 END
               ) AS MV_DR,
               SUM(
                 CASE
                   WHEN p.DOC_DATE >= :dfrom AND p.DOC_DATE <= :dto
                    AND p.DOC_TYPE <> 0 AND {sane}
                   THEN NVL(p.CR_AMT, 0) ELSE 0 END
               ) AS MV_CR
        FROM {sch}.IAS_POST_DTL p
        JOIN {sch}.ACCOUNT a
          ON a.A_CODE = p.A_CODE
        LEFT JOIN {sch}.CASH_IN_HAND ch
          ON {dtl_typ_expr} = 1
         AND TO_CHAR(ch.CASH_NO) = TO_CHAR(p.AC_CODE_DTL)
        LEFT JOIN {sch}.CASH_AT_BANK bk
          ON {dtl_typ_expr} = 2
         AND TO_CHAR(bk.BANK_NO) = TO_CHAR(p.AC_CODE_DTL)
        LEFT JOIN {sch}.CUSTOMER cu
          ON {dtl_typ_expr} = 3
         AND TO_CHAR(cu.C_CODE) = TO_CHAR(p.AC_CODE_DTL)
        LEFT JOIN {sch}.V_DETAILS vd
          ON {dtl_typ_expr} = 4
         AND TO_CHAR(vd.V_CODE) = TO_CHAR(p.AC_CODE_DTL)
        LEFT JOIN {sch}.COST_CENTERS cc
          ON {dtl_typ_expr} = 5
         AND TO_CHAR(cc.CC_CODE) = TO_CHAR(p.AC_CODE_DTL)
        LEFT JOIN {sch}.S_EMP em
          ON {dtl_typ_expr} = 7
         AND TO_CHAR(em.EMP_NO) = TO_CHAR(p.AC_CODE_DTL)
        WHERE {extra}
          AND p.DOC_DATE <= :dto
        GROUP BY
          a.A_CODE,
          {dtl_typ_expr},
          {dtl_code_expr},
          p.BRN_NO,
          NVL(TO_CHAR(p.DOC_BRN_NO), ''),
          NVL(TO_CHAR(p.A_CY), 'SAR')
        HAVING
          SUM(CASE WHEN (p.DOC_DATE < :dfrom OR p.DOC_TYPE = 0) AND {sane}
                   THEN NVL(p.AMT, 0) ELSE 0 END) <> 0
          OR SUM(CASE WHEN p.DOC_DATE >= :dfrom AND p.DOC_DATE <= :dto
                       AND p.DOC_TYPE <> 0 AND {sane} THEN 1 ELSE 0 END) > 0
        """,
        params,
    )


def _collapse_onix_groups(raw: list[dict], *, detail: bool) -> list[dict]:
    """رصيد نهائي: دمج المستفيد داخل الفرع الدفتري (حساب + دفتر + عملة).

    يمنع تضخيم المدين/الدائن من تحويلات المخزون (DOC_BRN فارغ مقابل فرع مستفيد).
    """
    buckets: dict[tuple, dict] = {}
    for row in raw:
        code = str(row.get("A_CODE") or "").strip()
        if not code:
            continue
        book_brn = str(row.get("BRN_NO") or "").strip()
        cy = str(row.get("A_CY") or "SAR").strip() or "SAR"
        if detail:
            key: tuple = (
                code,
                str(row.get("AC_DTL_TYP") or "0").strip() or "0",
                str(row.get("AC_CODE_DTL") or "").strip(),
                book_brn,
                cy,
            )
        else:
            key = (code, book_brn, cy)
        acc = buckets.get(key)
        if acc is None:
            acc = dict(row)
            acc["A_CODE"] = code
            acc["BRN_NO"] = book_brn
            acc["DOC_BRN"] = ""
            acc["A_CY"] = cy
            acc["OPEN_NET"] = _f(row.get("OPEN_NET"))
            acc["MV_DR"] = _f(row.get("MV_DR"))
            acc["MV_CR"] = _f(row.get("MV_CR"))
            buckets[key] = acc
            continue
        acc["OPEN_NET"] = round(_f(acc.get("OPEN_NET")) + _f(row.get("OPEN_NET")), 2)
        acc["MV_DR"] = round(_f(acc.get("MV_DR")) + _f(row.get("MV_DR")), 2)
        acc["MV_CR"] = round(_f(acc.get("MV_CR")) + _f(row.get("MV_CR")), 2)
        if not str(acc.get("A_NAME") or "").strip():
            acc["A_NAME"] = row.get("A_NAME")
        if detail and not str(acc.get("DTL_NAME") or "").strip():
            acc["DTL_NAME"] = row.get("DTL_NAME")
    return list(buckets.values())


def _gap_rows_from_raw(raw: list[dict]) -> list[dict]:
    """صفوف وهمية لفارق كل دفتر BRN_NO قبل دمج أونكس."""
    out: list[dict] = []
    for row in raw:
        brn = str(row.get("BRN_NO") or "").strip()
        open_net = _f(row.get("OPEN_NET"))
        mv_dr = _f(row.get("MV_DR"))
        mv_cr = _f(row.get("MV_CR"))
        close_dr, close_cr = _split_dr_cr(round(open_net + mv_dr - mv_cr, 2))
        if not close_dr and not close_cr:
            continue
        out.append(
            {
                "book_branch_code": brn,
                "branch_code": brn,
                "debit": close_dr,
                "credit": close_cr,
            }
        )
    return out


def _balance_tuple(tot_dr: float, tot_cr: float) -> tuple[float, str, bool]:
    """فرّق مدين/دائن مع تسامح هللة واحدة لعرض متوازن كأونكس."""
    balance = round(tot_dr - tot_cr, 2)
    if abs(balance) < 0.02:
        return 0.0, "0.00", True
    return balance, _fmt_money(balance), False


def _balance_fields(tot_dr: float, tot_cr: float) -> dict[str, Any]:
    """حقول الرصيد/الفارق لعرض تذييل أونكس (رصيد مدين أو دائن)."""
    balance, balance_display, balanced = _balance_tuple(tot_dr, tot_cr)
    if balanced:
        label = "متوازن"
        bal_dr = bal_cr = 0.0
    elif balance > 0:
        label = "رصيد مدين"
        bal_dr, bal_cr = balance, 0.0
    else:
        label = "رصيد دائن"
        bal_dr, bal_cr = 0.0, -balance
    return {
        "balance": balance,
        "balance_display": balance_display,
        "balanced": balanced,
        "balance_label": label,
        "balance_dr": bal_dr,
        "balance_cr": bal_cr,
        "balance_dr_display": _amt_cell(bal_dr),
        "balance_cr_display": _amt_cell(bal_cr),
        "balance_abs_display": _fmt_money(abs(balance)),
    }


def _branch_gap_summary(rows: list[dict], names: dict[str, str]) -> list[dict]:
    """فارق الرصيد النهائي لكل فرع دفتري (BRN_NO) من صفوف الميزان."""
    buckets: dict[str, list[float]] = {}
    for row in rows:
        code = str(row.get("book_branch_code") or "").strip()
        if not code:
            code = str(row.get("branch_code") or "").strip() or "—"
        dr = _f(row.get("debit"))
        cr = _f(row.get("credit"))
        if code not in buckets:
            buckets[code] = [0.0, 0.0]
        buckets[code][0] = round(buckets[code][0] + dr, 2)
        buckets[code][1] = round(buckets[code][1] + cr, 2)

    out: list[dict] = []
    max_abs = 0.0
    for code, (tot_dr, tot_cr) in buckets.items():
        bal = _balance_fields(tot_dr, tot_cr)
        max_abs = max(max_abs, abs(_f(bal.get("balance"))))
        name = (
            "—"
            if code in ("", "—")
            else (names.get(code) or f"فرع {code}")
        )
        if bal.get("balanced"):
            net_kind = "balanced"
        elif _f(bal.get("balance")) > 0:
            net_kind = "debit"
        else:
            net_kind = "credit"
        out.append(
            {
                "branch_code": "" if code == "—" else code,
                "branch_name": name,
                "debit": tot_dr,
                "credit": tot_cr,
                "debit_display": _fmt_money(tot_dr),
                "credit_display": _fmt_money(tot_cr),
                "net_kind": net_kind,
                "net_display": bal.get("balance_abs_display") or "0.00",
                **bal,
            }
        )
    for row in out:
        abs_bal = abs(_f(row.get("balance")))
        row["bar_pct"] = (
            round(100.0 * abs_bal / max_abs, 1) if max_abs > 0.0005 else 0.0
        )
    out.sort(
        key=lambda r: (
            0 if r.get("balanced") else 1,
            -abs(_f(r.get("balance"))),
            str(r.get("branch_code") or ""),
        )
    )
    return out


def _build_summary(
    raw: list[dict],
    *,
    names: dict[str, str],
    hide_zero: bool,
    d_from,
    d_to,
    brn: str,
    posted_only: bool,
) -> dict[str, Any]:
    """أرصدة نهائية: رقم الحساب + الفرع الدفتري + العملة (رصيد ختامي فقط)."""
    merged = _collapse_onix_groups(raw, detail=False)
    rows_out: list[dict] = []
    tot_dr = tot_cr = 0.0
    for row in merged:
        code = str(row.get("A_CODE") or "").strip()
        if not code:
            continue
        name = str(row.get("A_NAME") or "").strip() or code
        branch_code_row, branch_name = _book_branch(row, names)
        cy = str(row.get("A_CY") or "SAR").strip() or "SAR"
        open_net = _f(row.get("OPEN_NET"))
        mv_dr = _f(row.get("MV_DR"))
        mv_cr = _f(row.get("MV_CR"))
        close_net = round(open_net + mv_dr - mv_cr, 2)
        close_dr, close_cr = _split_dr_cr(close_net)
        if hide_zero and not close_dr and not close_cr:
            continue
        display_name = f"{name} — {branch_name}" if branch_code_row else name
        rows_out.append(
            {
                "account_code": code,
                "branch_code": branch_code_row,
                "branch_name": branch_name,
                "book_branch_code": branch_code_row,
                "account_name": name,
                "display_name": display_name,
                "dtl_typ": "",
                "dtl_typ_label": "",
                "dtl_code": "",
                "dtl_name": "",
                "currency": cy,
                "cheque_display": "",
                "debit": close_dr,
                "credit": close_cr,
                "debit_display": _amt_cell(close_dr),
                "credit_display": _amt_cell(close_cr),
            }
        )
        tot_dr = round(tot_dr + close_dr, 2)
        tot_cr = round(tot_cr + close_cr, 2)

    rows_out.sort(
        key=lambda r: (
            str(r.get("account_code") or ""),
            str(r.get("branch_code") or ""),
            str(r.get("currency") or ""),
        )
    )
    bal = _balance_fields(tot_dr, tot_cr)
    return {
        "mode": "summary",
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "report_title": "كشف حساب إجمالي — أرصدة نهائية",
        "group_by_label": "رقم الحساب · الفرع الدفتري · العملة",
        "currency": "SAR",
        "rows": rows_out,
        "totals": {
            "row_count": len(rows_out),
            "row_count_display": f"{len(rows_out):,}",
            "debit": tot_dr,
            "credit": tot_cr,
            "debit_display": _fmt_money(tot_dr),
            "credit_display": _fmt_money(tot_cr),
            "open_dr": 0.0,
            "open_cr": 0.0,
            "open_dr_display": "",
            "open_cr_display": "",
            "mv_dr": 0.0,
            "mv_cr": 0.0,
            "mv_dr_display": "",
            "mv_cr_display": "",
            **bal,
        },
        "filters": {
            "branch": brn,
            "posted_only": posted_only,
            "hide_zero": hide_zero,
            "mode": "summary",
        },
    }


def _build_analytic(
    raw: list[dict],
    *,
    names: dict[str, str],
    hide_zero: bool,
    d_from,
    d_to,
    brn: str,
    posted_only: bool,
) -> dict[str, Any]:
    """أرصدة نهائية تحليلية: صف لكل حساب+تحليلي+فرع مستفيد+عملة."""
    merged = _collapse_onix_groups(raw, detail=True)
    rows_out: list[dict] = []
    tot_dr = tot_cr = 0.0
    for row in merged:
        code = str(row.get("A_CODE") or "").strip()
        if not code:
            continue
        name = str(row.get("A_NAME") or "").strip() or code
        dtl_typ = str(row.get("AC_DTL_TYP") or "0").strip() or "0"
        dtl_code = str(row.get("AC_CODE_DTL") or "").strip()
        dtl_name = str(row.get("DTL_NAME") or "").strip() or dtl_code
        branch_code_row, branch_name = _book_branch(row, names)
        cy = str(row.get("A_CY") or "SAR").strip() or "SAR"
        open_net = _f(row.get("OPEN_NET"))
        mv_dr = _f(row.get("MV_DR"))
        mv_cr = _f(row.get("MV_CR"))
        close_net = round(open_net + mv_dr - mv_cr, 2)
        close_dr, close_cr = _split_dr_cr(close_net)
        if hide_zero and not close_dr and not close_cr:
            continue
        rows_out.append(
            {
                "account_code": code,
                "account_name": name,
                "dtl_typ": dtl_typ,
                "dtl_typ_label": _dtl_typ_label(dtl_typ),
                "dtl_code": dtl_code,
                "dtl_name": dtl_name,
                "branch_code": branch_code_row,
                "branch_name": branch_name,
                "book_branch_code": branch_code_row,
                "display_name": name,
                "currency": cy,
                "cheque_display": "",
                "debit": close_dr,
                "credit": close_cr,
                "debit_display": _amt_cell(close_dr),
                "credit_display": _amt_cell(close_cr),
            }
        )
        tot_dr = round(tot_dr + close_dr, 2)
        tot_cr = round(tot_cr + close_cr, 2)

    rows_out.sort(
        key=lambda r: (
            str(r.get("account_code") or ""),
            str(r.get("dtl_code") or ""),
            str(r.get("branch_code") or ""),
            str(r.get("currency") or ""),
        )
    )
    if len(rows_out) > _DETAIL_LIMIT:
        rows_out = rows_out[:_DETAIL_LIMIT]
    bal = _balance_fields(tot_dr, tot_cr)
    return {
        "mode": "analytic",
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "report_title": "كشف حساب إجمالي — أرصدة نهائية تحليلية",
        "group_by_label": "رقم الحساب · الحساب التحليلي · الفرع الدفتري · العملة",
        "currency": "SAR",
        "rows": rows_out,
        "totals": {
            "row_count": len(rows_out),
            "row_count_display": f"{len(rows_out):,}",
            "debit": tot_dr,
            "credit": tot_cr,
            "debit_display": _fmt_money(tot_dr),
            "credit_display": _fmt_money(tot_cr),
            "open_dr": 0.0,
            "open_cr": 0.0,
            "open_dr_display": "",
            "open_cr_display": "",
            "mv_dr": 0.0,
            "mv_cr": 0.0,
            "mv_dr_display": "",
            "mv_cr_display": "",
            **bal,
        },
        "filters": {
            "branch": brn,
            "posted_only": posted_only,
            "hide_zero": hide_zero,
            "mode": "analytic",
        },
    }


def _build_detail(
    raw: list[dict],
    *,
    names: dict[str, str],
    hide_zero: bool,
    d_from,
    d_to,
    brn: str,
    posted_only: bool,
) -> dict[str, Any]:
    merged = _collapse_onix_groups(raw, detail=True)
    rows_out: list[dict] = []
    tot_open_dr = tot_open_cr = tot_mv_dr = tot_mv_cr = tot_close_dr = tot_close_cr = 0.0

    for row in merged:
        code = str(row.get("A_CODE") or "").strip()
        if not code:
            continue
        name = str(row.get("A_NAME") or "").strip() or code
        dtl_typ = str(row.get("AC_DTL_TYP") or "0").strip() or "0"
        dtl_code = str(row.get("AC_CODE_DTL") or "").strip()
        dtl_name = str(row.get("DTL_NAME") or "").strip() or dtl_code
        branch_code_row, branch_name = _book_branch(row, names)
        cy = str(row.get("A_CY") or "SAR").strip() or "SAR"
        open_net = _f(row.get("OPEN_NET"))
        mv_dr = _f(row.get("MV_DR"))
        mv_cr = _f(row.get("MV_CR"))
        open_dr, open_cr = _split_dr_cr(open_net)
        close_net = round(open_net + mv_dr - mv_cr, 2)
        close_dr, close_cr = _split_dr_cr(close_net)
        if hide_zero and not (open_dr or open_cr or mv_dr or mv_cr or close_dr or close_cr):
            continue
        rows_out.append(
            {
                "account_code": code,
                "account_name": name,
                "dtl_typ": dtl_typ,
                "dtl_typ_label": _dtl_typ_label(dtl_typ),
                "dtl_code": dtl_code,
                "dtl_name": dtl_name,
                "branch_code": branch_code_row,
                "branch_name": branch_name,
                "book_branch_code": branch_code_row,
                "currency": cy,
                "open_dr": open_dr,
                "open_cr": open_cr,
                "mv_dr": mv_dr,
                "mv_cr": mv_cr,
                "close_dr": close_dr,
                "close_cr": close_cr,
                "open_dr_display": _amt_cell(open_dr),
                "open_cr_display": _amt_cell(open_cr),
                "mv_dr_display": _amt_cell(mv_dr),
                "mv_cr_display": _amt_cell(mv_cr),
                "close_dr_display": _amt_cell(close_dr),
                "close_cr_display": _amt_cell(close_cr),
                "debit": close_dr,
                "credit": close_cr,
                "debit_display": _amt_cell(close_dr),
                "credit_display": _amt_cell(close_cr),
                "display_name": name,
                "cheque_display": "",
            }
        )
        tot_open_dr = round(tot_open_dr + open_dr, 2)
        tot_open_cr = round(tot_open_cr + open_cr, 2)
        tot_mv_dr = round(tot_mv_dr + mv_dr, 2)
        tot_mv_cr = round(tot_mv_cr + mv_cr, 2)
        tot_close_dr = round(tot_close_dr + close_dr, 2)
        tot_close_cr = round(tot_close_cr + close_cr, 2)

    rows_out.sort(
        key=lambda r: (
            str(r.get("account_code") or ""),
            str(r.get("dtl_code") or ""),
            str(r.get("branch_code") or ""),
            str(r.get("currency") or ""),
        )
    )
    if len(rows_out) > _DETAIL_LIMIT:
        rows_out = rows_out[:_DETAIL_LIMIT]

    bal = _balance_fields(tot_close_dr, tot_close_cr)
    return {
        "mode": "detail",
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "report_title": "كشف حساب إجمالي — تفصيلي تحليلي (أرصدة مع الحركة)",
        "group_by_label": "رقم الحساب · الحساب التحليلي · الفرع الدفتري · العملة",
        "currency": "SAR",
        "rows": rows_out,
        "totals": {
            "row_count": len(rows_out),
            "row_count_display": f"{len(rows_out):,}",
            "debit": tot_close_dr,
            "credit": tot_close_cr,
            "debit_display": _fmt_money(tot_close_dr),
            "credit_display": _fmt_money(tot_close_cr),
            "open_dr": tot_open_dr,
            "open_cr": tot_open_cr,
            "open_dr_display": _fmt_money(tot_open_dr),
            "open_cr_display": _fmt_money(tot_open_cr),
            "mv_dr": tot_mv_dr,
            "mv_cr": tot_mv_cr,
            "mv_dr_display": _fmt_money(tot_mv_dr),
            "mv_cr_display": _fmt_money(tot_mv_cr),
            **bal,
        },
        "filters": {
            "branch": brn,
            "posted_only": posted_only,
            "hide_zero": hide_zero,
            "mode": "detail",
        },
    }


def _inventory_sanity(raw: list[dict]) -> dict[str, Any]:
    """سلامة مخزون البضاعة 11301001: صافي الدفتر مقابل عرض الفرع المستفيد.

    التحويلات الداخلية تُظهر مدينًا كبيرًا (DOC_BRN فارغ) ودائنًا كبيرًا
    (الفرع المستفيد) في عرض أونكس، بينما صافي الفرع الدفتري مدين فقط.
    """
    code = "11301001"
    by_book: dict[str, float] = {}
    by_benef: dict[str, float] = {}
    name = "مخزون بضائع مشتراة بغرض البيع"
    for row in raw or []:
        if str(row.get("A_CODE") or "").strip() != code:
            continue
        if str(row.get("A_NAME") or "").strip():
            name = str(row.get("A_NAME")).strip()
        close = round(
            _f(row.get("OPEN_NET")) + _f(row.get("MV_DR")) - _f(row.get("MV_CR")),
            2,
        )
        book = str(row.get("BRN_NO") or "").strip() or "—"
        benef = str(row.get("DOC_BRN") or "").strip()
        by_book[book] = round(_f(by_book.get(book)) + close, 2)
        by_benef[benef] = round(_f(by_benef.get(benef)) + close, 2)

    if not by_book and not by_benef:
        return {
            "account_code": code,
            "account_name": name,
            "present": False,
            "ok": True,
            "net": 0.0,
            "net_display": "0.00",
            "net_side": "مدين",
            "benef_debit": 0.0,
            "benef_credit": 0.0,
            "benef_debit_display": "0.00",
            "benef_credit_display": "0.00",
            "note": "لا قيود للمخزون ضمن الفترة/الفلتر.",
        }

    book_net = round(sum(by_book.values()), 2)
    book_dr = round(sum(v for v in by_book.values() if v > 0), 2)
    book_cr = round(sum(-v for v in by_book.values() if v < 0), 2)
    benef_dr = round(sum(v for v in by_benef.values() if v > 0), 2)
    benef_cr = round(sum(-v for v in by_benef.values() if v < 0), 2)
    benef_net = round(benef_dr - benef_cr, 2)
    ok = abs(benef_net - book_net) < 0.05 and book_cr < 0.05
    return {
        "account_code": code,
        "account_name": name,
        "present": True,
        "ok": ok,
        "net": book_net,
        "net_display": _fmt_money(abs(book_net)),
        "net_side": "مدين" if book_net >= 0 else "دائن",
        "book_debit": book_dr,
        "book_credit": book_cr,
        "book_debit_display": _fmt_money(book_dr),
        "book_credit_display": _fmt_money(book_cr),
        "benef_debit": benef_dr,
        "benef_credit": benef_cr,
        "benef_debit_display": _fmt_money(benef_dr),
        "benef_credit_display": _fmt_money(benef_cr),
        "note": (
            "الجدول يعرض الرصيد النهائي حسب الفرع الدفتري (مدين أو دائن). "
            "أرقام المستفيد الكبيرة كانت تحويلات داخلية وصافيها هو الرصيد الظاهر."
            if ok
            else "تحذير: صافي عرض المستفيد لا يطابق صافي الدفتر — راجع القيود."
        ),
    }


def _attach_inventory_sanity(result: dict[str, Any], raw: list[dict]) -> dict[str, Any]:
    result["inventory_check"] = _inventory_sanity(raw)
    return result


def build_trial_balance(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    posted_only: bool = False,
    hide_zero: bool = True,
    mode: str = "summary",
    use_cache: bool = False,
) -> dict[str, Any]:
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    brn = str(branch_code or "").strip()
    mode_n = _normalize_mode(mode)
    key = _cache_key(
        d_from,
        d_to,
        branch_code=brn,
        posted_only=posted_only,
        hide_zero=hide_zero,
        mode=mode_n,
    )
    if use_cache:
        cached = cache.get(key)
        if isinstance(cached, dict):
            return cached

    names = _branch_names()
    if mode_n == "summary":
        raw = _fetch_tb_summary_rows(
            d_from, d_to, branch_code=brn, posted_only=posted_only
        )
        result = _build_summary(
            raw,
            names=names,
            hide_zero=hide_zero,
            d_from=d_from,
            d_to=d_to,
            brn=brn,
            posted_only=posted_only,
        )
    else:
        raw = _fetch_tb_detail_rows(
            d_from, d_to, branch_code=brn, posted_only=posted_only
        )
        if mode_n == "detail":
            result = _build_detail(
                raw,
                names=names,
                hide_zero=hide_zero,
                d_from=d_from,
                d_to=d_to,
                brn=brn,
                posted_only=posted_only,
            )
        else:
            result = _build_analytic(
                raw,
                names=names,
                hide_zero=hide_zero,
                d_from=d_from,
                d_to=d_to,
                brn=brn,
                posted_only=posted_only,
            )
    result["branch_gaps"] = _branch_gap_summary(_gap_rows_from_raw(raw), names)
    _attach_inventory_sanity(result, raw)

    try:
        cache.set(key, result, _CACHE_TTL)
    except Exception:
        pass
    return result


def build_trial_balance_excel(report: dict[str, Any]) -> HttpResponse:
    rows = report.get("rows") or []
    totals = report.get("totals") or {}
    period = escape(str(report.get("period_label") or ""))
    title = escape(str(report.get("report_title") or "ميزان المراجعة"))
    mode = str(report.get("mode") or "summary")
    buf = io.StringIO()
    buf.write("\ufeff")
    buf.write(
        "<html xmlns:o=\"urn:schemas-microsoft-com:office:office\" "
        "xmlns:x=\"urn:schemas-microsoft-com:office:excel\" "
        "xmlns=\"http://www.w3.org/TR/REC-html40\">"
        "<head><meta charset=\"utf-8\">"
        "<!--[if gte mso 9]><xml><x:ExcelWorkbook><x:ExcelWorksheets>"
        "<x:ExcelWorksheet><x:Name>ميزان المراجعة</x:Name>"
        "<x:WorksheetOptions><x:DisplayRightToLeft/></x:WorksheetOptions>"
        "</x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
        "<style>"
        "table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:11px;}"
        "th,td{border:1px solid #94a3b8;padding:4px 7px;white-space:nowrap;}"
        "th{background:#1e3a5f;color:#fff;font-weight:700;}"
        "td.num{mso-number-format:'\\#\\,\\#\\#0\\.00';text-align:left;}"
        "td.link{color:#1d4f91;font-weight:700;}"
        "tr.tot td{background:#dbeafe;font-weight:800;}"
        "tr.bal td{background:#fef9c3;font-weight:800;}"
        "</style></head><body>"
    )
    buf.write(f"<h3>{title}</h3><p>الفترة: {period} · العملة: SAR</p><table>")
    if mode == "detail":
        buf.write(
            "<thead><tr>"
            "<th rowspan=\"2\">الرقم</th><th rowspan=\"2\">الاسم</th>"
            "<th rowspan=\"2\">النوع</th>"
            "<th rowspan=\"2\">رقم التحليلي</th><th rowspan=\"2\">اسم التحليلي</th>"
            "<th rowspan=\"2\">الفرع</th><th rowspan=\"2\">العملة</th>"
            "<th colspan=\"2\">افتتاحي</th>"
            "<th colspan=\"2\">حركة</th>"
            "<th colspan=\"2\">رصيد ختامي</th>"
            "</tr><tr>"
            "<th>مدين</th><th>دائن</th><th>مدين</th><th>دائن</th>"
            "<th>مدين</th><th>دائن</th>"
            "</tr></thead><tbody>"
        )
        for row in rows:
            buf.write(
                "<tr>"
                f"<td>{escape(str(row.get('account_code') or ''))}</td>"
                f"<td>{escape(str(row.get('account_name') or ''))}</td>"
                f"<td>{escape(str(row.get('dtl_typ_label') or row.get('dtl_typ') or ''))}</td>"
                f"<td class=\"link\">{escape(str(row.get('dtl_code') or ''))}</td>"
                f"<td>{escape(str(row.get('dtl_name') or ''))}</td>"
                f"<td>{escape(str(row.get('branch_name') or ''))}</td>"
                "<td>SAR</td>"
                f"<td class=\"num\">{escape(str(row.get('open_dr_display') or ''))}</td>"
                f"<td class=\"num\">{escape(str(row.get('open_cr_display') or ''))}</td>"
                f"<td class=\"num\">{escape(str(row.get('mv_dr_display') or ''))}</td>"
                f"<td class=\"num\">{escape(str(row.get('mv_cr_display') or ''))}</td>"
                f"<td class=\"num\">{escape(str(row.get('close_dr_display') or ''))}</td>"
                f"<td class=\"num\">{escape(str(row.get('close_cr_display') or ''))}</td>"
                "</tr>"
            )
        buf.write(
            "<tr class=\"tot\">"
            f"<td colspan=\"6\">إجمالي حسب العملة : SAR — عدد السجلات : "
            f"{escape(str(totals.get('row_count_display') or '0'))}</td><td>SAR</td>"
            f"<td class=\"num\">{escape(str(totals.get('open_dr_display') or ''))}</td>"
            f"<td class=\"num\">{escape(str(totals.get('open_cr_display') or ''))}</td>"
            f"<td class=\"num\">{escape(str(totals.get('mv_dr_display') or ''))}</td>"
            f"<td class=\"num\">{escape(str(totals.get('mv_cr_display') or ''))}</td>"
            f"<td class=\"num\">{escape(str(totals.get('debit_display') or ''))}</td>"
            f"<td class=\"num\">{escape(str(totals.get('credit_display') or ''))}</td>"
            "</tr>"
        )
    else:
        has_dtl = mode == "analytic"
        if has_dtl:
            buf.write(
                "<thead><tr>"
                "<th rowspan=\"2\">الرقم</th><th rowspan=\"2\">الاسم</th>"
                "<th rowspan=\"2\">النوع</th>"
                "<th rowspan=\"2\">رقم التحليلي</th><th rowspan=\"2\">اسم التحليلي</th>"
                "<th rowspan=\"2\">الفرع</th>"
                "<th rowspan=\"2\">العملة</th>"
                "<th colspan=\"2\">الأرصدة</th>"
                "</tr><tr><th>مدين</th><th>دائن</th></tr></thead><tbody>"
            )
            for row in rows:
                buf.write(
                    "<tr>"
                    f"<td>{escape(str(row.get('account_code') or ''))}</td>"
                    f"<td>{escape(str(row.get('account_name') or ''))}</td>"
                    f"<td>{escape(str(row.get('dtl_typ_label') or row.get('dtl_typ') or ''))}</td>"
                    f"<td class=\"link\">{escape(str(row.get('dtl_code') or ''))}</td>"
                    f"<td>{escape(str(row.get('dtl_name') or ''))}</td>"
                    f"<td>{escape(str(row.get('branch_name') or ''))}</td>"
                    "<td>SAR</td>"
                    f"<td class=\"num\">{escape(str(row.get('debit_display') or ''))}</td>"
                    f"<td class=\"num\">{escape(str(row.get('credit_display') or ''))}</td>"
                    "</tr>"
                )
            buf.write(
                "<tr class=\"tot\">"
                f"<td colspan=\"6\">إجمالي حسب العملة : SAR — عدد السجلات : "
                f"{escape(str(totals.get('row_count_display') or '0'))}</td>"
                "<td>SAR</td>"
                f"<td class=\"num\">{escape(str(totals.get('debit_display') or '0.00'))}</td>"
                f"<td class=\"num\">{escape(str(totals.get('credit_display') or '0.00'))}</td>"
                "</tr>"
            )
        else:
            buf.write(
                "<thead><tr>"
                "<th rowspan=\"2\">الرقم</th><th rowspan=\"2\">الاسم</th>"
                "<th rowspan=\"2\">الفرع</th>"
                "<th rowspan=\"2\">العملة</th>"
                "<th colspan=\"2\">الأرصدة</th>"
                "</tr><tr><th>مدين</th><th>دائن</th></tr></thead><tbody>"
            )
            for row in rows:
                buf.write(
                    "<tr>"
                    f"<td>{escape(str(row.get('account_code') or ''))}</td>"
                    f"<td>{escape(str(row.get('account_name') or ''))}</td>"
                    f"<td>{escape(str(row.get('branch_name') or ''))}</td>"
                    "<td>SAR</td>"
                    f"<td class=\"num\">{escape(str(row.get('debit_display') or ''))}</td>"
                    f"<td class=\"num\">{escape(str(row.get('credit_display') or ''))}</td>"
                    "</tr>"
                )
            buf.write(
                "<tr class=\"tot\">"
                f"<td colspan=\"3\">إجمالي حسب العملة : SAR — عدد السجلات : "
                f"{escape(str(totals.get('row_count_display') or '0'))}</td>"
                "<td>SAR</td>"
                f"<td class=\"num\">{escape(str(totals.get('debit_display') or '0.00'))}</td>"
                f"<td class=\"num\">{escape(str(totals.get('credit_display') or '0.00'))}</td>"
                "</tr>"
            )
    buf.write("</tbody></table></body></html>")
    period_safe = (
        str(report.get("period_label") or "")
        .replace(" ", "")
        .replace("→", "_")
        .replace(":", "-")
    )
    filename = f"trial_balance_{mode}_{period_safe or 'report'}.xls"
    resp = HttpResponse(
        buf.getvalue().encode("utf-8"),
        content_type="application/vnd.ms-excel; charset=utf-8",
    )
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


__all__ = [
    "build_trial_balance",
    "build_trial_balance_excel",
    "peek_trial_balance",
    "fetch_income_branches",
]
