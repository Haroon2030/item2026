"""تقرير الموظفين النشطين الذين لهم حركة أو قيد في شهر محدد.

قراءة فقط (SELECT) من S_EMP مع هيكل الموظفين S_HRCHY ومراكز التكلفة COST_CENTERS.
- نشط: S_EMP.INACTIVE غير مفعّل.
- يُستثنى كل من يقع تحت فرع الهيكل «سلة المحذوفات» (وأبنائه).
- آخر راتب أساسي: آخر قيد دائن على حساب السلف (SALARY_ACCOUNT) بتحليلي الموظف يذكر بيانه الراتب
  (استحقاق راتب / لكم راتب / اثبات رواتب…) دون الخصومات والإضافي والفروق وسداد السلف،
  حتى نهاية الشهر المختار؛ المبلغ = الدائن، والتاريخ = تاريخ القيد.
- طريقة صرف الراتب (تحويل/نقد): من آخر قيد راتب للموظف على حساب السلف خلال الشهر المختار يمكن تصنيفه:
  بيانه فيه تحويل/بنك/حوالة → تحويل؛ أو سند صرف (DOC_TYPE = 3) مدين أو بيانه فيه نقد/كاش → نقد؛
  وإلا «غير محدد» (قيد استحقاق لم يُصرف بعد أو بيان لا يدل على الطريقة).
- حركة/قيد في الشهر: قيد على حسابه التحليلي في IAS_POST_DTL (AC_DTL_TYP = 7، AC_CODE_DTL = رقم الموظف)،
  أو حركة موظف HRS_EMP_MOVMNT، أو توقيع حضور HRS_MCHN_DATA_ATTNDNC.
"""

from __future__ import annotations

import re
from datetime import date
from html import escape
from typing import Any

from django.core.cache import cache
from django.http import HttpResponse

from .oracle_stock import (
    OracleStockError,
    _branch_names,
    _fetch_all,
    _norm_brn_code,
    _schema,
    oracle_enabled,
)

CACHE_SECONDS = 600
TRASH_HRCHY_NAME = "سلة المحذوفات"
SALARY_ACCOUNT = "11601001"  # سلف: قيود الموظف التحليلية (AC_DTL_TYP = 7)
# بيان قيد الراتب: «استحقاق راتب» · «لكم راتب شهر» · «اثبات رواتب» · «استحقاق الرواتب النقدية»…
SALARY_DESC_INCLUDE = "راتب|رواتب"
# قيود دائنة تذكر الراتب لكنها ليست راتباً أساسياً: خصم، إضافي، فروقات، سداد سلف، تسوية/إقفال/تصفية
SALARY_DESC_EXCLUDE = "خصم|اضافي|إضافي|فرق|سداد|تسوي|اقفال|إقفال|افقال|تصفية|عجز"
# طريقة الصرف من بيان قيد الراتب
PAY_TRANSFER_RE = "تحويل|بنك|حوال|نافذه"
PAY_CASH_RE = "نقد|كاش"
PAY_FILTERS = {"transfer": "T", "cash": "C", "unknown": ""}
_PAY_LABEL = {"T": "تحويل", "C": "نقد", "": "غير محدد"}
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def parse_month(value: Any, *, default: date | None = None) -> tuple[date, date]:
    """'YYYY-MM' → (أول الشهر، أول الشهر التالي)."""
    text = str(value or "").strip()
    m = _MONTH_RE.match(text)
    if not m:
        if default is None:
            raise OracleStockError("الشهر غير صحيح (الصيغة YYYY-MM).")
        first = default.replace(day=1)
    else:
        year, month = int(m.group(1)), int(m.group(2))
        if not (2000 <= year <= 2100 and 1 <= month <= 12):
            raise OracleStockError("الشهر غير صحيح.")
        first = date(year, month, 1)
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return first, nxt


def previous_month(today: date | None = None) -> str:
    today = today or date.today()
    y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return f"{y:04d}-{m:02d}"


def _fetch_rows(d_from: date, d_to_excl: date, branch: str) -> list[dict]:
    s = _schema()
    params: dict[str, Any] = {
        "d_from": d_from,
        "d_to": d_to_excl,
        "trash": TRASH_HRCHY_NAME,
        "sal_acc": SALARY_ACCOUNT,
        "sal_inc": SALARY_DESC_INCLUDE,
        "sal_exc": SALARY_DESC_EXCLUDE,
        "pay_tr": PAY_TRANSFER_RE,
        "pay_cash": PAY_CASH_RE,
    }
    branch_sql = ""
    if branch:
        params["brn"] = int(branch) if branch.isdigit() else branch
        branch_sql = "AND e.BRN_NO = :brn"
    return _fetch_all(
        f"""
        SELECT TO_CHAR(e.EMP_NO) AS EMP_NO,
               TRIM(e.EMP_L_NM) AS EMP_NAME,
               TO_CHAR(e.BRN_NO) AS BRANCH_CODE,
               TO_CHAR(e.CC_CODE) AS CC_CODE,
               TRIM(cc.CC_A_NAME) AS CC_NAME,
               TRIM(h.HRCHY_L_NM) AS HRCHY_NAME,
               TRIM(hp.HRCHY_L_NM) AS PARENT_NAME,
               e.STRT_WRK_DATE AS HIRE_DATE,
               sal.CR_AMT AS SALARY_AMT,
               sal.DOC_DATE AS SALARY_DATE,
               pay.METH AS PAY_METHOD
        FROM {s}.S_EMP e
        LEFT JOIN (
          SELECT AC_CODE_DTL, CR_AMT, DOC_DATE FROM (
            SELECT p.AC_CODE_DTL, p.CR_AMT, p.DOC_DATE,
                   ROW_NUMBER() OVER (
                     PARTITION BY p.AC_CODE_DTL
                     ORDER BY p.DOC_DATE DESC, p.DOC_NO DESC, p.DOC_SER DESC) AS RN
            FROM {s}.IAS_POST_DTL p
            WHERE p.A_CODE = :sal_acc AND p.AC_DTL_TYP = 7
              AND NVL(p.CR_AMT, 0) > 0
              AND REGEXP_LIKE(p.DOC_DESC, :sal_inc)
              AND NOT REGEXP_LIKE(p.DOC_DESC, :sal_exc)
              AND p.DOC_DATE < :d_to)
          WHERE RN = 1
        ) sal ON sal.AC_CODE_DTL = TO_CHAR(e.EMP_NO)
        LEFT JOIN (
          SELECT AC_CODE_DTL, METH FROM (
            SELECT p.AC_CODE_DTL,
                   CASE
                     WHEN REGEXP_LIKE(p.DOC_DESC, :pay_tr) THEN 'T'
                     WHEN REGEXP_LIKE(p.DOC_DESC, :pay_cash)
                       OR (p.DOC_TYPE = 3 AND NVL(p.DR_AMT, 0) > 0) THEN 'C'
                   END AS METH,
                   ROW_NUMBER() OVER (
                     PARTITION BY p.AC_CODE_DTL
                     ORDER BY p.DOC_DATE DESC, p.DOC_NO DESC, p.DOC_SER DESC) AS RN
            FROM {s}.IAS_POST_DTL p
            WHERE p.A_CODE = :sal_acc AND p.AC_DTL_TYP = 7
              AND p.DOC_DATE >= :d_from AND p.DOC_DATE < :d_to
              AND REGEXP_LIKE(p.DOC_DESC, :sal_inc)
              AND NOT REGEXP_LIKE(p.DOC_DESC, :sal_exc)
              AND (REGEXP_LIKE(p.DOC_DESC, :pay_tr) OR REGEXP_LIKE(p.DOC_DESC, :pay_cash)
                   OR (p.DOC_TYPE = 3 AND NVL(p.DR_AMT, 0) > 0)))
          WHERE RN = 1
        ) pay ON pay.AC_CODE_DTL = TO_CHAR(e.EMP_NO)
        LEFT JOIN {s}.S_HRCHY h ON h.HRCHY_NO = e.HRCHY_NO
        LEFT JOIN {s}.S_HRCHY hp ON hp.HRCHY_NO = h.HRCHY_PARNT
        LEFT JOIN {s}.COST_CENTERS cc ON cc.CC_CODE = TO_CHAR(e.CC_CODE)
        WHERE NVL(e.INACTIVE, 0) = 0
          AND NOT EXISTS (
            SELECT 1 FROM (
              SELECT HRCHY_NO FROM {s}.S_HRCHY
              START WITH TRIM(HRCHY_L_NM) = :trash
              CONNECT BY PRIOR HRCHY_NO = HRCHY_PARNT
            ) t WHERE t.HRCHY_NO = e.HRCHY_NO)
          AND (
            TO_CHAR(e.EMP_NO) IN (
              SELECT TO_CHAR(p.AC_CODE_DTL) FROM {s}.IAS_POST_DTL p
              WHERE p.AC_DTL_TYP = 7 AND p.DOC_DATE >= :d_from AND p.DOC_DATE < :d_to)
            OR e.EMP_NO IN (
              SELECT m.EMP_NO FROM {s}.HRS_EMP_MOVMNT m
              WHERE m.DOC_DATE >= :d_from AND m.DOC_DATE < :d_to)
            OR e.EMP_NO IN (
              SELECT a.EMP_NO FROM {s}.HRS_MCHN_DATA_ATTNDNC a
              WHERE a.SGN_DATE >= :d_from AND a.SGN_DATE < :d_to)
          )
          {branch_sql}
        ORDER BY e.BRN_NO, e.CC_CODE, h.HRCHY_L_NM, e.EMP_L_NM
        """,
        params,
    )


def assemble_rows(rows: list[dict], *, branch_names: dict[str, str]) -> list[dict]:
    out = []
    for r in rows:
        branch = _norm_brn_code(r.get("BRANCH_CODE"))
        cc_code = str(r.get("CC_CODE") or "").strip()
        cc_name = str(r.get("CC_NAME") or "").strip()
        hr = str(r.get("HRCHY_NAME") or "").strip()
        parent = str(r.get("PARENT_NAME") or "").strip()
        hire = r.get("HIRE_DATE")
        sal_amt, sal_date = r.get("SALARY_AMT"), r.get("SALARY_DATE")
        pay = str(r.get("PAY_METHOD") or "").strip()
        pay = pay if pay in ("T", "C") else ""
        out.append(
            {
                "emp_no": str(r.get("EMP_NO") or ""),
                "name": " ".join(str(r.get("EMP_NAME") or "").split()) or "-",
                "branch": branch_names.get(branch) or branch or "-",
                "cc_code": cc_code or "-",
                "cc_name": cc_name or "-",
                "parent": parent if parent not in ("", "0") else "-",
                "structure": hr or "-",
                "hire_date": hire.strftime("%Y-%m-%d") if hire else "-",
                "salary": f"{float(sal_amt):,.2f}" if sal_amt else "-",
                "salary_date": sal_date.strftime("%Y-%m-%d") if sal_date else "-",
                "pay_key": pay,
                "pay_method": _PAY_LABEL[pay],
            }
        )
    return out


def build_employees_report(
    month: Any, *, branch_code: str = "", pay: str = ""
) -> dict[str, Any]:
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    d_from, d_to = parse_month(month)
    brn = _norm_brn_code(branch_code)
    key = f"employees:month:v5:{d_from}:{brn}"
    rows = cache.get(key)
    if rows is None:
        rows = assemble_rows(_fetch_rows(d_from, d_to, brn), branch_names=_branch_names())
        cache.set(key, rows, CACHE_SECONDS)
    total = len(rows)
    pay_counts = {
        "transfer": sum(1 for r in rows if r["pay_key"] == "T"),
        "cash": sum(1 for r in rows if r["pay_key"] == "C"),
    }
    if pay in PAY_FILTERS:
        rows = [r for r in rows if r["pay_key"] == PAY_FILTERS[pay]]
    return {
        "month": d_from.strftime("%Y-%m"),
        "employees": rows,
        "count": f"{len(rows):,}",
        "total": f"{total:,}",
        "pay": pay if pay in PAY_FILTERS else "",
        "pay_counts": pay_counts,
    }


_EXCEL_COLUMNS = (
    ("emp_no", "رقم الموظف"),
    ("name", "الاسم"),
    ("branch", "الفرع"),
    ("cc_code", "كود المركز"),
    ("cc_name", "المركز"),
    ("parent", "الهيكل الرئيسي"),
    ("structure", "الهيكل"),
    ("hire_date", "تاريخ التوظيف"),
    ("salary", "آخر راتب أساسي"),
    ("salary_date", "تاريخ آخر راتب"),
    ("pay_method", "طريقة الصرف"),
)


def build_employees_excel(report: dict[str, Any], *, branch_label: str = "") -> HttpResponse:
    """تصدير الجدول إلى Excel (RTL، ترويسة ملوّنة، نصوص كاملة بلا تحويل أرقام)."""
    rows = report.get("employees") or []
    head = "".join(f"<th>{escape(label)}</th>" for _, label in _EXCEL_COLUMNS)
    body = []
    for i, r in enumerate(rows, 1):
        cells = "".join(
            f'<td class="txt">{escape(str(r.get(key) or ""))}</td>' for key, _ in _EXCEL_COLUMNS
        )
        body.append(f'<tr><td class="txt">{i}</td>{cells}</tr>')
    title = f"بيانات الموظفين — {report.get('month', '')}"
    if branch_label:
        title += f" — {branch_label}"
    html = (
        "﻿<html xmlns:o=\"urn:schemas-microsoft-com:office:office\" "
        "xmlns:x=\"urn:schemas-microsoft-com:office:excel\" "
        "xmlns=\"http://www.w3.org/TR/REC-html40\">"
        "<head><meta charset=\"utf-8\">"
        "<!--[if gte mso 9]><xml><x:ExcelWorkbook><x:ExcelWorksheets>"
        "<x:ExcelWorksheet><x:Name>الموظفون</x:Name>"
        "<x:WorksheetOptions><x:DisplayRightToLeft/><x:FreezePanes/>"
        "<x:SplitHorizontal>2</x:SplitHorizontal><x:TopRowBottomPane>2</x:TopRowBottomPane>"
        "</x:WorksheetOptions></x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
        "<style>"
        "table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:12px;}"
        "th,td{border:1px solid #94a3b8;padding:4px 8px;text-align:right;}"
        "th{background:#d9e2f3;color:#1a2b33;font-weight:700;}"
        "td.txt{mso-number-format:'\@';}"
        "</style></head><body dir=\"rtl\">"
        f"<p><b>{escape(title)}</b> — العدد {escape(str(report.get('count', len(rows))))}</p>"
        f"<table><thead><tr><th>#</th>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
        "</body></html>"
    )
    resp = HttpResponse(
        html.encode("utf-8"), content_type="application/vnd.ms-excel; charset=utf-8"
    )
    resp["Content-Disposition"] = f'attachment; filename="employees-{report.get("month", "")}.xls"'
    return resp
