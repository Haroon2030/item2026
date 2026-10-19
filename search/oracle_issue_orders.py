"""تقرير أوامر الصرف المخزني — تحليل حسب الحساب والمجموعة والفرع.

قراءة فقط (SELECT):
- رأس الأمر: IAS_OUTGOING_MST (OUT_TYPE = 1)، الحساب A_CODE واسمه من ACCOUNT.
- البنود: IAS_OUTGOING_DTL، ومجموعة الصنف من IAS_ITM_MST.G_CODE.
قيمة الأمر = الكمية × سعر البند (تطابق OUT_AMT في رأس الأمر)، والتكلفة = الكمية × STK_COST.
"""

from __future__ import annotations

import logging
from datetime import date
from html import escape
from typing import Any

from django.core.cache import cache
from django.http import HttpResponse

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _bind_gcode,
    _branch_names,
    _date_params,
    _fetch_all,
    _group_name_lookup,
    _hung_ok,
    _schema,
    oracle_enabled,
)

logger = logging.getLogger(__name__)

_CACHE_TTL = 300
_LOOKUP_TTL = 1800
_CACHE_VER = "v2"
MAX_DAYS = 400

# طريقة التجميع: مفتاح → (عنوان، الأبعاد بالترتيب)
GROUP_BY_CHOICES: dict[str, tuple[str, tuple[str, ...]]] = {
    "account": ("حسب الحساب", ("account",)),
    "group": ("حسب المجموعة", ("group",)),
    "branch": ("حسب الفرع", ("branch",)),
    "account_group": ("الحساب ← المجموعة", ("account", "group")),
    "account_branch": ("الحساب ← الفرع", ("account", "branch")),
    "branch_account": ("الفرع ← الحساب", ("branch", "account")),
}
DEFAULT_GROUP_BY = "account"

STATUS_CHOICES: dict[str, str] = {
    "": "كل الأوامر",
    "posted": "مرحّلة فقط",
    "unposted": "غير مرحّلة فقط",
}

_DIM_LABELS = {"account": "الحساب", "group": "المجموعة", "branch": "الفرع"}


def _f(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _money(value: Any) -> str:
    return f"{_f(value):,.2f}"


def _qty(value: Any) -> str:
    qty = float(value or 0)
    if abs(qty - round(qty)) < 1e-9:
        return f"{int(round(qty)):,}"
    return f"{qty:,.2f}"


def _pct(part: float, total: float) -> float:
    return round(part / total * 100, 1) if total else 0.0


def _code(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].replace("-", "").isdigit():
        text = text[:-2]
    return text


def fetch_issue_account_options() -> list[dict]:
    """حسابات لها أوامر صرف مخزني (للفلتر) — كود واسم."""
    key = f"issue_orders:accounts:{_CACHE_VER}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    if not oracle_enabled():
        return []
    sch = _schema()
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(m.A_CODE) AS A_CODE, MAX(a.A_NAME) AS A_NAME
        FROM {sch}.IAS_OUTGOING_MST m
        LEFT JOIN {sch}.ACCOUNT a ON a.A_CODE = m.A_CODE
        WHERE m.OUT_TYPE = 1 AND m.A_CODE IS NOT NULL
        GROUP BY TO_CHAR(m.A_CODE)
        ORDER BY TO_CHAR(m.A_CODE)
        """
    )
    out = []
    for r in rows:
        code = _code(r.get("A_CODE"))
        if code:
            out.append({"code": code, "name": str(r.get("A_NAME") or "").strip() or code})
    cache.set(key, out, _LOOKUP_TTL)
    return out


def _where(
    date_from, date_to, branch_code: str, account_code: str, group_code: str, status: str
) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = _date_params(date_from, date_to)
    parts = [
        "m.OUT_TYPE = 1",
        "m.OUT_DATE >= :d_from",
        "m.OUT_DATE < :d_to_excl",
        _hung_ok("m"),
    ]
    brn = str(branch_code or "").strip()
    if brn:
        params["brn"] = int(brn) if brn.isdigit() else brn
        parts.append("m.BRN_NO = :brn")
    acc = str(account_code or "").strip()
    if acc:
        params["acc"] = acc
        parts.append("TO_CHAR(m.A_CODE) = :acc")
    gcode = str(group_code or "").strip()
    if gcode:
        params["gcode"] = _bind_gcode(gcode)
        parts.append("i.G_CODE = :gcode")
    if status == "posted":
        parts.append("m.OUT_POST = 1")
    elif status == "unposted":
        parts.append("NVL(m.OUT_POST, 0) = 0")
    return " AND ".join(parts), params


_FROM = """
        FROM {sch}.IAS_OUTGOING_MST m
        JOIN {sch}.IAS_OUTGOING_DTL d
          ON d.OUT_SER = m.OUT_SER AND d.OUT_TYPE = m.OUT_TYPE
        LEFT JOIN {sch}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        LEFT JOIN {sch}.ACCOUNT a ON a.A_CODE = m.A_CODE
"""

_DIM_SQL = {
    "account": ("TO_CHAR(m.A_CODE)", "ACC"),
    "group": ("NVL(TO_CHAR(i.G_CODE), '(بلا)')", "GRP"),
    "branch": ("TO_CHAR(m.BRN_NO)", "BRN"),
}

_METRICS = """
               COUNT(DISTINCT m.OUT_SER) AS ORDERS,
               ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY,
               ROUND(SUM(NVL(d.I_QTY, 0) * NVL(d.I_PRICE, 0)), 2) AS VALUE_AMT,
               ROUND(SUM(NVL(d.I_QTY, 0) * NVL(d.STK_COST, 0)), 2) AS COST_AMT,
               MAX(m.OUT_DATE) AS LAST_DATE
"""


def build_issue_orders_report(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    account_code: str = "",
    group_code: str = "",
    status: str = "",
    group_by: str = DEFAULT_GROUP_BY,
) -> dict[str, Any]:
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    d_from, d_to = _as_date(date_from), _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد تاريخ النهاية.")
    if (d_to - d_from).days > MAX_DAYS:
        raise OracleStockError(f"الفترة أطول من {MAX_DAYS} يوماً.")
    if group_by not in GROUP_BY_CHOICES:
        group_by = DEFAULT_GROUP_BY
    if status not in STATUS_CHOICES:
        status = ""

    cache_key = (
        f"issue_orders:report:{_CACHE_VER}:{d_from}:{d_to}:{branch_code}:{account_code}:"
        f"{group_code}:{status}:{group_by}"
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    sch = _schema()
    where, params = _where(d_from, d_to, branch_code, account_code, group_code, status)
    from_sql = _FROM.format(sch=sch)
    dims = GROUP_BY_CHOICES[group_by][1]
    group_by_label = GROUP_BY_CHOICES[group_by][0]
    # كل الفروع: يُضاف الفرع بُعداً أخيراً فيظهر كل فرع في صف مستقل
    auto_branch = not str(branch_code or "").strip() and "branch" not in dims
    if auto_branch:
        dims = dims + ("branch",)
        group_by_label += " ← الفرع"
    select_dims = ", ".join(f"{_DIM_SQL[d][0]} AS {_DIM_SQL[d][1]}" for d in dims)
    group_sql = ", ".join(_DIM_SQL[d][0] for d in dims)

    rows_raw = _fetch_all(
        f"""
        SELECT {select_dims}, {_METRICS}
        {from_sql}
        WHERE {where}
        GROUP BY {group_sql}
        """,
        params,
    )
    total_raw = _fetch_all(
        f"""
        SELECT {_METRICS},
               COUNT(DISTINCT m.A_CODE) AS ACCOUNTS,
               COUNT(DISTINCT m.BRN_NO) AS BRANCHES,
               COUNT(DISTINCT i.G_CODE) AS GROUPS
        {from_sql}
        WHERE {where}
        """,
        params,
    )[0]

    branch_names = _branch_names()
    group_names = _group_name_lookup()
    account_names = {a["code"]: a["name"] for a in fetch_issue_account_options()}

    def label(dim: str, code: str) -> str:
        if dim == "account":
            return account_names.get(code, code)
        if dim == "group":
            return group_names.get(code, code) if code != "(بلا)" else "بلا مجموعة"
        return branch_names.get(code, code)

    total_value = _f(total_raw.get("VALUE_AMT"))
    total_cost = _f(total_raw.get("COST_AMT"))
    total_qty = float(total_raw.get("QTY") or 0)
    orders_total = int(total_raw.get("ORDERS") or 0)

    rows: list[dict[str, Any]] = []
    for r in rows_raw:
        value = _f(r.get("VALUE_AMT"))
        cost = _f(r.get("COST_AMT"))
        qty = float(r.get("QTY") or 0)
        orders = int(r.get("ORDERS") or 0)
        row: dict[str, Any] = {
            "orders": orders,
            "orders_display": f"{orders:,}",
            "qty": qty,
            "qty_display": _qty(qty),
            "value": value,
            "value_display": _money(value),
            "cost": cost,
            "cost_display": _money(cost),
            "avg_order": round(value / orders, 2) if orders else 0.0,
            "avg_order_display": _money(value / orders if orders else 0),
            "share_pct": _pct(value, total_value),
            "last_date": r["LAST_DATE"].date().isoformat() if r.get("LAST_DATE") else "",
        }
        row["share_display"] = f"{row['share_pct']:.1f}%"
        labels = []
        for d in dims:
            code = _code(r.get(_DIM_SQL[d][1]))
            row[f"{d}_code"] = code
            row[f"{d}_name"] = label(d, code)
            labels.append({"code": code, "name": row[f"{d}_name"]})
        row["labels"] = labels
        rows.append(row)
    if len(dims) > 1:
        # الصفوف مجمّعة بالبُعد الأول (الأعلى قيمة أولاً) وداخل كل مجموعة الأعلى قيمة أولاً
        first = dims[0]
        first_totals: dict[str, float] = {}
        for row in rows:
            first_totals[row[f"{first}_code"]] = first_totals.get(row[f"{first}_code"], 0.0) + row["value"]
        rows.sort(
            key=lambda x: (
                -first_totals[x[f"{first}_code"]],
                x[f"{first}_name"],
                -x["value"],
            )
        )
        previous = None
        for row in rows:
            row["block_start"] = row[f"{first}_code"] != previous
            previous = row[f"{first}_code"]
    else:
        rows.sort(key=lambda x: (-x["value"], x[f"{dims[0]}_name"]))
    for i, row in enumerate(rows, 1):
        row["rank"] = i

    scope = []
    if branch_code:
        scope.append(branch_names.get(str(branch_code), str(branch_code)))
    if account_code:
        scope.append(account_names.get(str(account_code), str(account_code)))
    if group_code:
        scope.append(group_names.get(str(group_code), str(group_code)))

    report = {
        "from": d_from.isoformat(),
        "to": d_to.isoformat(),
        "group_by": group_by,
        "group_by_label": group_by_label,
        "auto_branch": auto_branch,
        "dims": [{"key": d, "label": _DIM_LABELS[d]} for d in dims],
        "status_label": STATUS_CHOICES[status],
        "scope_label": " · ".join(scope) if scope else "كل الفروع والحسابات والمجموعات",
        "rows": rows,
        "row_count": len(rows),
        "orders": orders_total,
        "orders_display": f"{orders_total:,}",
        "qty_display": _qty(total_qty),
        "value": total_value,
        "value_display": _money(total_value),
        "cost_display": _money(total_cost),
        "avg_order_display": _money(total_value / orders_total if orders_total else 0),
        "accounts": int(total_raw.get("ACCOUNTS") or 0),
        "branches": int(total_raw.get("BRANCHES") or 0),
        "groups": int(total_raw.get("GROUPS") or 0),
        "top": rows[0] if rows else None,
    }
    cache.set(cache_key, report, _CACHE_TTL)
    return report


WASTE_ACCOUNT_CODE = "41101006"  # حساب التوالف في أوامر الصرف


def _purchases_total(d_from, d_to, branch_code: str, group_code: str) -> float:
    """صافي المشتريات بعد المرتجعات شامل الضريبة، بنفس منطق شاشة المشتريات.

    بلا مجموعة: من رأس الفاتورة (المبلغ + الضريبة). مع مجموعة: من بنود أصناف المجموعة
    (الكمية × السعر − الخصم) مع الضريبة بنسبة VAT_PER، لأن رأس الفاتورة يضم مجموعات أخرى.
    """
    sch = _schema()
    params: dict[str, Any] = _date_params(d_from, d_to)
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    pi_brn = pr_brn = ""
    if brn:
        params["branch"] = brn
        pi_brn = "AND TO_CHAR(m.BRN_NO) = :branch"
        pr_brn = "AND TO_CHAR(r.BRN_NO) = :branch"
    if gcode:
        params["gcode"] = _bind_gcode(gcode)
        line = "(NVL(d.I_QTY, 0) * NVL(d.I_PRICE, 0) - NVL(d.DIS_AMT, 0)) * (1 + NVL(d.VAT_PER, 0) / 100)"
        pi_sql = f"""
            SELECT SUM({line}) AS AMT
            FROM {sch}.IAS_PI_BILL_MST m
            JOIN {sch}.IAS_PI_BILL_DTL d
              ON d.BILL_NO = m.BILL_NO AND d.BILL_SER = m.BILL_SER AND d.BILL_DOC_TYPE = m.BILL_DOC_TYPE
            JOIN {sch}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
            WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
              AND NVL(m.HUNG, 0) = 0 AND i.G_CODE = :gcode {pi_brn}"""
        pr_sql = f"""
            SELECT SUM({line}) AS AMT
            FROM {sch}.IAS_PR_BILL_MST r
            JOIN {sch}.IAS_PR_BILL_DTL d
              ON d.RT_BILL_NO = r.RT_BILL_NO AND d.RT_BILL_SER = r.RT_BILL_SER
             AND d.RT_BILL_DOC_TYPE = r.RT_BILL_DOC_TYPE
            JOIN {sch}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
            WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
              AND NVL(r.HUNG, 0) = 0 AND i.G_CODE = :gcode {pr_brn}"""
    else:
        pi_sql = f"""
            SELECT SUM(NVL(m.BILL_AMT, 0) + NVL(m.VAT_AMT, 0)) AS AMT
            FROM {sch}.IAS_PI_BILL_MST m
            WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
              AND NVL(m.HUNG, 0) = 0 {pi_brn}"""
        pr_sql = f"""
            SELECT SUM(NVL(r.BILL_AMT, 0) + NVL(r.VAT_AMT, 0)) AS AMT
            FROM {sch}.IAS_PR_BILL_MST r
            WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
              AND NVL(r.HUNG, 0) = 0 {pr_brn}"""
    purchases = float((_fetch_all(pi_sql, params) or [{}])[0].get("AMT") or 0)
    returns = float((_fetch_all(pr_sql, params) or [{}])[0].get("AMT") or 0)
    return round(purchases - returns, 2)


def _sales_total(d_from, d_to, branch_code: str, group_code: str) -> dict[str, float]:
    """صافي المبيعات بعد المرتجعات شامل الضريبة (نقاط البيع + الجملة) كما في شاشة المبيعات."""
    from .sales_dashboard import build_sales_branches, build_sales_branches_for_group

    if str(group_code or "").strip():
        payload = build_sales_branches_for_group(
            d_from, d_to, branch_code=str(branch_code or ""), group_code=str(group_code)
        )
    else:
        payload = build_sales_branches(d_from, d_to, branch_code=str(branch_code or ""))
    pos = _f(payload["pos"]["totals"].get("sales_total"))
    wholesale = _f(payload["wholesale"]["totals"].get("sales_total"))
    return {"pos": pos, "wholesale": wholesale, "total": round(pos + wholesale, 2)}


def build_waste_ratio(
    date_from, date_to, *, branch_code: str = "", group_code: str = ""
) -> dict[str, Any]:
    """نسبة التوالف (أوامر الصرف على حساب التوالف) من المشتريات ومن المبيعات لنفس الفترة والفلتر."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    d_from, d_to = _as_date(date_from), _as_date(date_to)
    key = f"issue_orders:waste:{_CACHE_VER}:{d_from}:{d_to}:{branch_code}:{group_code}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    waste_report = build_issue_orders_report(
        d_from, d_to, branch_code=branch_code, account_code=WASTE_ACCOUNT_CODE,
        group_code=group_code, group_by="account",
    )
    waste = _f(waste_report["value"])
    purchases = _purchases_total(d_from, d_to, branch_code, group_code)
    sales = _sales_total(d_from, d_to, branch_code, group_code)
    data = {
        "waste": waste,
        "waste_display": _money(waste),
        "waste_orders": waste_report["orders"],
        "purchases": purchases,
        "purchases_display": _money(purchases),
        "sales": sales["total"],
        "sales_display": _money(sales["total"]),
        "pos_display": _money(sales["pos"]),
        "wholesale_display": _money(sales["wholesale"]),
        "pct_purchases": round(waste / purchases * 100, 2) if purchases else None,
        "pct_sales": round(waste / sales["total"] * 100, 2) if sales["total"] else None,
    }
    for k in ("pct_purchases", "pct_sales"):
        data[k + "_display"] = f"{data[k]:.2f}%" if data[k] is not None else "—"
    cache.set(key, data, _CACHE_TTL)
    return data


def build_issue_orders_excel(report: dict[str, Any]) -> HttpResponse:
    """ملف Excel (HTML بامتداد xls) بكل صفوف التقرير."""
    dims = report.get("dims") or []
    head_cells = ["#"] + [d["label"] for d in dims] + [
        "عدد الأوامر", "الكمية", "القيمة", "التكلفة", "متوسط الأمر", "النسبة", "آخر أمر",
    ]
    head = "".join(f"<th>{escape(h)}</th>" for h in head_cells)
    body = []
    for row in report.get("rows") or []:
        cells = [f"<td>{row['rank']}</td>"]
        for label in row.get("labels") or []:
            cells.append(f'<td class="txt">{escape(str(label.get("name") or ""))}</td>')
        for key in ("orders_display", "qty_display", "value_display", "cost_display", "avg_order_display"):
            cells.append(f'<td class="num">{escape(str(row.get(key) or ""))}</td>')
        cells.append(f'<td class="txt">{escape(row["share_display"])}</td>')
        cells.append(f'<td class="txt">{escape(row["last_date"])}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    foot = (
        '<tr class="foot"><td></td><td>الإجمالي</td>'
        + "<td></td>" * (len(dims) - 1)
        + f'<td class="num">{escape(report["orders_display"])}</td>'
        f'<td class="num">{escape(report["qty_display"])}</td>'
        f'<td class="num">{escape(report["value_display"])}</td>'
        f'<td class="num">{escape(report["cost_display"])}</td>'
        f'<td class="num">{escape(report["avg_order_display"])}</td>'
        '<td class="txt">100.0%</td><td></td></tr>'
    )
    title = f"أوامر الصرف — {report['group_by_label']} — {report['from']} ← {report['to']}"
    html = (
        '<html xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:x="urn:schemas-microsoft-com:office:excel" '
        'xmlns="http://www.w3.org/TR/REC-html40"><head><meta charset="utf-8">'
        "<style>table{border-collapse:collapse;direction:rtl}th,td{border:1px solid #999;padding:4px 8px}"
        "th{background:#4a7fb5;color:#fff}.num{mso-number-format:'#,##0.00';text-align:left}"
        ".txt{mso-number-format:'@'}.foot td{font-weight:bold;background:#eef3f9}</style></head><body>"
        f"<table><caption>{escape(title)} — {escape(report['scope_label'])}</caption>"
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody><tfoot>{foot}</tfoot></table>"
        "</body></html>"
    )
    response = HttpResponse(html, content_type="application/vnd.ms-excel; charset=utf-8")
    stamp = date.today().strftime("%Y%m%d")
    response["Content-Disposition"] = f'attachment; filename="issue_orders_{report["group_by"]}_{stamp}.xls"'
    return response
