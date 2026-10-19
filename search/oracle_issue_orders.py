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
from urllib.parse import urlencode

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
_CACHE_VER = "v3"
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

    base_drill = {
        "date_from": d_from.isoformat(), "date_to": d_to.isoformat(), "status": status,
        "branch": str(branch_code or ""), "account": str(account_code or ""), "group": str(group_code or ""),
    }
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
        # عرض الشريط كنص بنقطة عشرية (القالب يعرض الأرقام بفاصلة فيُبطل CSS)
        row["bar_width"] = f"{max(0.0, min(100.0, row['share_pct'])):.1f}"
        labels = []
        for d in dims:
            code = _code(r.get(_DIM_SQL[d][1]))
            row[f"{d}_code"] = code
            row[f"{d}_name"] = label(d, code)
            labels.append({"code": code, "name": row[f"{d}_name"]})
        row["labels"] = labels
        # فلاتر تفصيل اليوم: فلاتر التقرير مع قيم أبعاد هذا الصف
        q = dict(base_drill)
        for d in dims:
            q[d] = row[f"{d}_code"]
        row["drill"] = "" if q.get("group") == "(بلا)" else urlencode(q)
        row["drill_title"] = " ← ".join(l["name"] for l in labels)
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
        "drill_total": urlencode(base_drill),
    }
    cache.set(cache_key, report, _CACHE_TTL)
    return report


_WEEKDAYS = ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد")


def build_issue_orders_daily(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    account_code: str = "",
    group_code: str = "",
    status: str = "",
) -> dict[str, Any]:
    """أوامر الصرف حسب الأيام لنفس فلاتر صف في التقرير (تفصيل عند الضغط على عدد الأوامر)."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    d_from, d_to = _as_date(date_from), _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد تاريخ النهاية.")
    if (d_to - d_from).days > MAX_DAYS:
        raise OracleStockError(f"الفترة أطول من {MAX_DAYS} يوماً.")
    if status not in STATUS_CHOICES:
        status = ""
    key = f"issue_orders:daily:{_CACHE_VER}:{d_from}:{d_to}:{branch_code}:{account_code}:{group_code}:{status}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    sch = _schema()
    where, params = _where(d_from, d_to, branch_code, account_code, group_code, status)
    rows_raw = _fetch_all(
        f"""
        SELECT TRUNC(m.OUT_DATE) AS D, {_METRICS}
        {_FROM.format(sch=sch)}
        WHERE {where}
        GROUP BY TRUNC(m.OUT_DATE)
        ORDER BY TRUNC(m.OUT_DATE)
        """,
        params,
    )
    total_value = round(sum(_f(r.get("VALUE_AMT")) for r in rows_raw), 2)
    total_cost = round(sum(_f(r.get("COST_AMT")) for r in rows_raw), 2)
    total_qty = sum(float(r.get("QTY") or 0) for r in rows_raw)
    # للأمر تاريخ واحد، فمجموع أوامر الأيام = عدد الأوامر الفعلي
    total_orders = sum(int(r.get("ORDERS") or 0) for r in rows_raw)

    rows: list[dict[str, Any]] = []
    for r in rows_raw:
        day = r["D"].date() if hasattr(r["D"], "date") else r["D"]
        value = _f(r.get("VALUE_AMT"))
        orders = int(r.get("ORDERS") or 0)
        share = _pct(value, total_value)
        rows.append({
            "date": day.isoformat(),
            "weekday": _WEEKDAYS[day.weekday()],
            "orders": orders,
            "orders_display": f"{orders:,}",
            "qty_display": _qty(r.get("QTY")),
            "value": value,
            "value_display": _money(value),
            "cost_display": _money(r.get("COST_AMT")),
            "avg_order_display": _money(value / orders if orders else 0),
            "share_display": f"{share:.1f}%",
            "bar_width": f"{max(0.0, min(100.0, share)):.1f}",
        })
    names_b, names_g = _branch_names(), _group_name_lookup()
    names_a = {a["code"]: a["name"] for a in fetch_issue_account_options()}
    scope = [
        x for x in (
            names_b.get(str(branch_code), str(branch_code)) if branch_code else "",
            names_a.get(str(account_code), str(account_code)) if account_code else "",
            names_g.get(str(group_code), str(group_code)) if group_code else "",
        ) if x
    ]
    peak = max(rows, key=lambda x: x["value"]) if rows else None
    report = {
        "from": d_from.isoformat(),
        "to": d_to.isoformat(),
        "scope_label": " · ".join(scope) if scope else "كل الفروع والحسابات والمجموعات",
        "status_label": STATUS_CHOICES[status],
        "rows": rows,
        "days": len(rows),
        "orders_display": f"{total_orders:,}",
        "qty_display": _qty(total_qty),
        "value_display": _money(total_value),
        "cost_display": _money(total_cost),
        "avg_order_display": _money(total_value / total_orders if total_orders else 0),
        "peak": peak,
    }
    cache.set(key, report, _CACHE_TTL)
    return report


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
