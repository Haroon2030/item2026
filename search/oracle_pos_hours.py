"""فواتير نقاط البيع بالساعة — عدد الفواتير وقيمتها في فترة تاريخ وساعات محددة.

قراءة فقط (SELECT) من رأس فاتورة نقاط البيع IAS_POS_BILL_MST:
- BILL_DATE تاريخ بلا وقت (كل الفواتير 00:00)، والوقت في BILL_TIME نصاً بصيغة HH24:MI:SS.
- القيمة = BILL_AMT + VAT_AMT (شاملة الضريبة) بنفس شروط تحليل المبيعات: غير معلّقة ومبلغ منطقي،
  فيطابق عدد الفواتير ما يظهر في تحليل المبيعات للفترة نفسها (قبل المرتجع).
- نطاق ساعات يعبر منتصف الليل (مثل 22:00 → 02:00) يُحسب ضمن تاريخ الفاتورة نفسه.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from django.core.cache import cache

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _bill_amt_ok,
    _branch_names,
    _date_params,
    _fetch_all,
    _hung_ok,
    _norm_brn_code,
    _pos_owner,
    oracle_enabled,
)

logger = logging.getLogger(__name__)

_CACHE_TTL = 120
_CACHE_VER = "v3"
MAX_DAYS = 92
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: Any, default: str) -> str:
    """«8:5» أو «08:05» أو «08:05:00» → «08:05»؛ قيمة غير صالحة ترفع ValueError."""
    text = str(value or "").strip()
    if not text:
        return default
    parts = text.split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        raise ValueError("وقت غير صالح — استخدم الصيغة ساعة:دقيقة مثل 08:30.")
    hhmm = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    if not _TIME_RE.match(hhmm):
        raise ValueError("وقت غير صالح — الساعة من 00 إلى 23 والدقائق من 00 إلى 59.")
    return hhmm


def window_hours(time_from: str, time_to: str) -> list[int]:
    """الساعات التي يمسّها النطاق بالترتيب (يلتفّ بعد منتصف الليل)."""
    start, end = int(time_from[:2]), int(time_to[:2])
    if time_from <= time_to:
        return list(range(start, end + 1))
    return list(range(start, 24)) + list(range(0, end + 1))


def _time_clause(time_from: str, time_to: str) -> str:
    """شرط الوقت على BILL_TIME (نص HH24:MI:SS يُقارَن أبجدياً بشكل صحيح)؛ نهاية الدقيقة مشمولة."""
    if time_from == "00:00" and time_to == "23:59":
        return ""
    if time_from <= time_to:
        return "AND m.BILL_TIME >= :t_from AND m.BILL_TIME <= :t_to"
    return "AND (m.BILL_TIME >= :t_from OR m.BILL_TIME <= :t_to)"


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _count(value: int) -> str:
    return f"{value:,}"


def _pct(part: float, whole: float) -> float:
    return round(part * 100.0 / whole, 1) if whole else 0.0


def _row(label: str, invoices: int, value: float, total_invoices: int, top_invoices: int) -> dict[str, Any]:
    share = _pct(invoices, total_invoices)
    return {
        "label": label,
        "invoices": invoices,
        "invoices_display": _count(invoices),
        "value": round(value, 2),
        "value_display": _money(value),
        "avg_display": _money(value / invoices) if invoices else "—",
        "share": share,
        "share_display": f"{share:.1f}%",
        # عرض شريط النسبة نسبةً لأعلى صف حتى يظهر الفرق بين الساعات بوضوح
        "bar_width": f"{(invoices * 100.0 / top_invoices) if top_invoices else 0:.1f}",
    }


def _per_day(invoices: int, days: int) -> str:
    """متوسط فواتير اليوم بمنزلة عشرية واحدة (12.5) ودون كسور حين يكون عدداً صحيحاً."""
    avg = invoices / days if days else 0.0
    return f"{avg:,.0f}" if float(avg).is_integer() else f"{avg:,.1f}"


def hour_label(hour: int) -> str:
    return f"{hour:02d}:00 – {hour:02d}:59"


def build_pos_hours_report(
    date_from,
    date_to,
    *,
    time_from: str = "00:00",
    time_to: str = "23:59",
    branch_code: str = "",
) -> dict[str, Any]:
    """عدد فواتير نقاط البيع وقيمتها في الفترة والساعات المحددة: بالساعة وبالفرع وباليوم."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    d_from, d_to = _as_date(date_from), _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد تاريخ النهاية.")
    if (d_to - d_from).days + 1 > MAX_DAYS:
        raise OracleStockError(f"الفترة أطول من {MAX_DAYS} يوماً — اختر فترة أقصر.")
    time_from = parse_hhmm(time_from, "00:00")
    time_to = parse_hhmm(time_to, "23:59")
    brn = _norm_brn_code(branch_code)

    key = f"pos_hours:{_CACHE_VER}:{d_from}:{d_to}:{time_from}:{time_to}:{brn}"
    try:
        cached = cache.get(key)
    except Exception:  # noqa: BLE001
        cached = None
    if cached is not None:
        return cached

    pos = _pos_owner()
    params: dict[str, Any] = _date_params(d_from, d_to)
    time_sql = _time_clause(time_from, time_to)
    if time_sql:
        params["t_from"] = f"{time_from}:00"
        params["t_to"] = f"{time_to}:59"
    brn_sql = ""
    if brn:
        params["brn"] = int(brn) if brn.isdigit() else brn
        brn_sql = "AND m.BRN_NO = :brn"

    rows = _fetch_all(
        f"""
        SELECT
            TRUNC(m.BILL_DATE) AS D,
            m.BRN_NO AS BRN,
            SUBSTR(m.BILL_TIME, 1, 2) AS HH,
            COUNT(DISTINCT m.BILL_NO) AS INVOICES,
            ROUND(SUM(NVL(m.BILL_AMT, 0) + NVL(m.VAT_AMT, 0)), 2) AS VALUE_TOTAL
        FROM {pos}.IAS_POS_BILL_MST m
        WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
          AND {_hung_ok("m")}
          AND {_bill_amt_ok("m", "BILL_AMT")}
          {time_sql}
          {brn_sql}
        GROUP BY TRUNC(m.BILL_DATE), m.BRN_NO, SUBSTR(m.BILL_TIME, 1, 2)
        """,
        params,
    )

    names = _branch_names()
    by_hour: dict[int, list] = {}
    by_branch: dict[str, list] = {}
    by_day: dict[date, list] = {}
    hour_branch: dict[tuple[int, str], int] = {}
    day_branch: dict[tuple[date, str], int] = {}
    total_invoices = 0
    total_value = 0.0
    for r in rows:
        invoices = int(r.get("INVOICES") or 0)
        value = _f(r.get("VALUE_TOTAL"))
        try:
            hour = int(str(r.get("HH") or "").strip())
        except ValueError:
            continue  # BILL_TIME فارغ أو تالف — لا يقع في أي ساعة
        code = _norm_brn_code(r.get("BRN"))
        day = _as_date(r.get("D"))
        for bucket, k in ((by_hour, hour), (by_branch, code), (by_day, day)):
            acc = bucket.setdefault(k, [0, 0.0])
            acc[0] += invoices
            acc[1] += value
        hour_branch[(hour, code)] = hour_branch.get((hour, code), 0) + invoices
        day_branch[(day, code)] = day_branch.get((day, code), 0) + invoices
        total_invoices += invoices
        total_value += value

    # كل ساعات النطاق تظهر حتى بلا فواتير (صفر) حتى تُرى الساعات الهادئة
    hours = window_hours(time_from, time_to)
    top_hour = max((by_hour.get(h, [0])[0] for h in hours), default=0)
    hour_rows = [
        dict(_row(hour_label(h), *by_hour.get(h, [0, 0.0]), total_invoices, top_hour), hour=h)
        for h in hours
    ]
    busiest = max(hour_rows, key=lambda x: x["invoices"], default=None)

    # الأيام المقسوم عليها: أيام الفترة التي مضت فعلاً (فترة تنتهي في المستقبل لا تُقسَم على أيام لم تأتِ)
    days = (d_to - d_from).days + 1
    days_elapsed = max(1, (min(d_to, date.today()) - d_from).days + 1)

    top_branch = max((v[0] for v in by_branch.values()), default=0)
    branch_rows = sorted(
        (
            dict(_row(names.get(code, code or "—"), inv, val, total_invoices, top_branch), code=code)
            for code, (inv, val) in by_branch.items()
        ),
        key=lambda x: (-x["invoices"], x["code"]),
    )

    # عدد فواتير كل فرع في كل ساعة (أعمدة بأسماء الفروع، بترتيب الأكثر فواتير)
    branch_cols = [{"code": b["code"], "name": b["label"]} for b in branch_rows]
    for h in hour_rows:
        h["by_branch"] = [
            _count(hour_branch[(h["hour"], col["code"])]) if hour_branch.get((h["hour"], col["code"])) else "—"
            for col in branch_cols
        ]
    branch_totals = [b["invoices_display"] for b in branch_rows]
    # متوسط اليوم لكل فرع = إجمالي الفترة ÷ عدد الأيام
    for b in branch_rows:
        b["per_day_invoices_display"] = _per_day(b["invoices"], days_elapsed)
        b["per_day_value_display"] = _money(b["value"] / days_elapsed)
    branch_per_day = [b["per_day_invoices_display"] for b in branch_rows]

    day_rows = []
    if d_from != d_to:
        top_day = max((v[0] for v in by_day.values()), default=0)
        day_rows = [
            dict(
                _row(d.isoformat(), *by_day[d], total_invoices, top_day),
                date=d.isoformat(),
                by_branch=[
                    _count(day_branch[(d, col["code"])]) if day_branch.get((d, col["code"])) else "—"
                    for col in branch_cols
                ],
            )
            for d in sorted(by_day)
        ]
    report = {
        "from": d_from.isoformat(),
        "to": d_to.isoformat(),
        "days": days,
        "time_from": time_from,
        "time_to": time_to,
        "crosses_midnight": time_from > time_to,
        "branch": brn,
        "branch_name": names.get(brn, brn) if brn else "",
        "invoices": total_invoices,
        "invoices_display": _count(total_invoices),
        "value": round(total_value, 2),
        "value_display": _money(total_value),
        "avg_display": _money(total_value / total_invoices) if total_invoices else "—",
        "per_day_display": _count(round(total_invoices / days_elapsed)) if days > 1 else "",
        "days_elapsed": days_elapsed,
        "per_day_invoices_display": _per_day(total_invoices, days_elapsed),
        "per_day_value_display": _money(total_value / days_elapsed),
        "busiest_label": busiest["label"] if busiest and busiest["invoices"] else "—",
        "busiest_invoices_display": busiest["invoices_display"] if busiest and busiest["invoices"] else "",
        "hours": hour_rows,
        "branches": branch_rows,
        "branch_cols": branch_cols,
        "branch_totals": branch_totals,
        "branch_per_day": branch_per_day,
        "days_rows": day_rows,
    }
    try:
        cache.set(key, report, _CACHE_TTL)
    except Exception:  # noqa: BLE001
        pass
    return report
