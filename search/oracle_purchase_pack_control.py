"""رقابة اختلاف عبوة الشراء: صنف اشتُري بوحدة وعبوة ثم أُدخل بعدها بنفس الوحدة وعبوة مختلفة.

مثال: «باكت» عبوة 12 في الفاتورة السابقة و«باكت» عبوة 16 في الفاتورة الجديدة لنفس الصنف.
المقارنة مع آخر شراء سابق لنفس الصنف ونفس عائلة الوحدة (كرتون/كرتون1/كرتون 2 = كرتون، أي فرع) خلال LOOKBACK_DAYS قبل بداية الفترة.

قراءة فقط (SELECT) من IAS_PI_BILL_MST / IAS_PI_BILL_DTL. الشاشة الرئيسية بنفس شكل رقابة
الشراء: (مستخدم × جهاز) بعدد البنود المخالفة؛ وبالنقر تُفتح تفاصيل البنود مع الفاتورة السابقة.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.cache import cache

from .oracle_purchase_control import (
    DETAIL_ROWS_LIMIT,
    MAX_DAYS,
    _int,
    _resolve_sessions,
    _warehouse_names,
)
from .oracle_sqlutil import num_bind
from .oracle_stock import (
    OracleStockError,
    _as_date,
    _branch_names,
    _fetch_all,
    _norm_brn_code,
    _schema,
    _user_names,
    oracle_enabled,
)

CACHE_SECONDS = 300
LOOKBACK_DAYS = 365
_PURCHASE_DOC_TYPES = "1, 4, 5, 8"
_TERM = "NVL(NULLIF(TRIM(AD_TRMNL_NM), ''), '-')"
# اسم الوحدة بلا أرقام ومسافات وتطويل: «كرتون1» و«كرتــون 2» و«كرتون» عائلة واحدة
_UNIT_KEY = "REGEXP_REPLACE(TO_CHAR(d.ITM_UNT), '[0-9[:space:]]|' || UNISTR('\\0640'), '')"


def _num(value: Any) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{f:g}"


def _flagged_cte(d_lookback, d_from, d_to_excl, branch, warehouse, group, user_id):
    """CTE: كل بند شراء في الفترة تختلف عبوته عن عبوة آخر شراء سابق لنفس الصنف والوحدة."""
    schema = _schema()
    params: dict[str, Any] = {"d_lb": d_lookback, "d_from": d_from, "d_to_excl": d_to_excl}
    outer: list[str] = []
    if branch:
        params["brn"] = int(branch) if branch.isdigit() else branch
        outer.append("AND BRN_NO = :brn")
    if warehouse:
        params["wh"] = warehouse
        outer.append(f"AND W_CODE = {num_bind(':wh')}")
    if group:
        params["gcode"] = group
        outer.append("AND G_CODE = :gcode")
    if user_id:
        params["f_user"] = int(user_id)
        outer.append("AND AD_U_ID = :f_user")
    sql = f"""
    WITH L AS (
      SELECT d.I_CODE, TO_CHAR(d.ITM_UNT) AS UNIT, d.P_SIZE,
             m.BILL_SER, m.BILL_NO, m.BILL_DATE, m.BRN_NO, m.W_CODE, m.V_NAME,
             m.AD_U_ID, m.AD_DATE, m.AD_TRMNL_NM, i.G_CODE, i.I_NAME,
             LAG(d.P_SIZE) OVER (PARTITION BY d.I_CODE, {_UNIT_KEY} ORDER BY m.BILL_DATE, m.BILL_SER, d.RCRD_NO) AS PREV_PS,
             LAG(TO_CHAR(d.ITM_UNT)) OVER (PARTITION BY d.I_CODE, {_UNIT_KEY} ORDER BY m.BILL_DATE, m.BILL_SER, d.RCRD_NO) AS PREV_UNIT,
             LAG(m.BILL_SER) OVER (PARTITION BY d.I_CODE, {_UNIT_KEY} ORDER BY m.BILL_DATE, m.BILL_SER, d.RCRD_NO) AS PREV_SER,
             LAG(m.BILL_NO) OVER (PARTITION BY d.I_CODE, {_UNIT_KEY} ORDER BY m.BILL_DATE, m.BILL_SER, d.RCRD_NO) AS PREV_NO,
             LAG(m.BILL_DATE) OVER (PARTITION BY d.I_CODE, {_UNIT_KEY} ORDER BY m.BILL_DATE, m.BILL_SER, d.RCRD_NO) AS PREV_DATE
      FROM {schema}.IAS_PI_BILL_MST m
      JOIN {schema}.IAS_PI_BILL_DTL d ON d.BILL_SER = m.BILL_SER
      LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
      WHERE m.BILL_DATE >= :d_lb AND m.BILL_DATE < :d_to_excl
        AND m.BILL_DOC_TYPE IN ({_PURCHASE_DOC_TYPES})
        AND d.I_CODE IS NOT NULL AND NVL(d.P_SIZE, 0) > 0
    ),
    F AS (
      SELECT * FROM L
      WHERE BILL_DATE >= :d_from AND PREV_PS IS NOT NULL
        AND PREV_PS <> P_SIZE AND PREV_SER <> BILL_SER
        {" ".join(outer)}
    )
    """
    return sql, params


def _fetch_summary(d_lb, d_from, d_to_excl, branch, warehouse, group, user_id):
    cte, params = _flagged_cte(d_lb, d_from, d_to_excl, branch, warehouse, group, user_id)
    return _fetch_all(
        f"""{cte}
        SELECT TO_CHAR(BRN_NO) AS BRANCH_CODE, TO_CHAR(AD_U_ID) AS USER_ID,
               {_TERM} AS TERMINAL,
               COUNT(*) AS LINES_N,
               COUNT(DISTINCT BILL_SER) AS BILLS_N,
               COUNT(DISTINCT I_CODE) AS ITEMS_N
        FROM F
        GROUP BY BRN_NO, AD_U_ID, {_TERM}
        """,
        params,
    )


def assemble_report(summary: list[dict], *, branch_names: dict, user_names: dict) -> dict[str, Any]:
    rows = []
    tot_lines = tot_bills = 0
    for r in summary:
        branch = _norm_brn_code(r.get("BRANCH_CODE"))
        user = str(r.get("USER_ID") or "").strip()
        lines, bills = _int(r.get("LINES_N")), _int(r.get("BILLS_N"))
        tot_lines += lines
        tot_bills += bills
        rows.append(
            {
                "branch_code": branch,
                "user_id": user,
                "branch": branch_names.get(branch) or branch or "-",
                "user": user_names.get(user) or user or "-",
                "terminal": str(r.get("TERMINAL") or "-"),
                "lines": lines,
                "bills": bills,
                "items": _int(r.get("ITEMS_N")),
            }
        )
    rows.sort(key=lambda r: (-r["lines"], r["user"]))
    return {
        "control_rows": rows,
        "totals": {"lines": f"{tot_lines:,}", "bills": f"{tot_bills:,}"},
    }


def _validate(date_from, date_to, user_id: str):
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    uid = str(user_id or "").strip()
    if uid and not uid.isdigit():
        raise OracleStockError("رقم المستخدم يجب أن يكون أرقاماً فقط.")
    d_from, d_to = _as_date(date_from), _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")
    if (d_to - d_from).days >= MAX_DAYS:
        raise OracleStockError(f"الفترة القصوى {MAX_DAYS} يوماً.")
    return d_from, d_to, uid


def build_pack_control(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    warehouse_code: str = "",
    group_code: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    d_from, d_to, uid = _validate(date_from, date_to, user_id)
    brn = _norm_brn_code(branch_code)
    wh = str(warehouse_code or "").strip()
    gcode = str(group_code or "").strip()
    key = f"purchases:packctl:v2:{d_from}:{d_to}:{brn}:{wh}:{gcode}:{uid}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    summary = _fetch_summary(
        d_from - timedelta(days=LOOKBACK_DAYS), d_from, d_to + timedelta(days=1),
        brn, wh, gcode, uid,
    )
    report = assemble_report(summary, branch_names=_branch_names(), user_names=_user_names())
    report["period_label"] = f"{d_from.isoformat()} → {d_to.isoformat()}"
    cache.set(key, report, CACHE_SECONDS)
    return report


# ——— التفاصيل ———


def _fetch_detail(d_lb, d_from, d_to_excl, branch, warehouse, group, *, user_id, terminal, limit):
    cte, params = _flagged_cte(d_lb, d_from, d_to_excl, branch, warehouse, group, "")
    if user_id:
        params["p_user"] = int(user_id)
        user_sql = "AND AD_U_ID = :p_user"
    else:
        user_sql = "AND AD_U_ID IS NULL"
    params["p_term"] = terminal
    where = f"{_TERM} = :p_term {user_sql}"
    total = _fetch_all(f"{cte} SELECT COUNT(*) AS N FROM F WHERE {where}", params)
    rows = _fetch_all(
        f"""{cte}
        SELECT * FROM (
          SELECT TO_CHAR(BRN_NO) AS BRANCH_CODE, TO_CHAR(W_CODE) AS W_CODE,
                 TO_CHAR(I_CODE) AS I_CODE,
                 NVL(NULLIF(TRIM(I_NAME), ''), TO_CHAR(I_CODE)) AS I_NAME,
                 UNIT, PREV_UNIT, P_SIZE, PREV_PS,
                 TO_CHAR(BILL_NO) AS BILL_NO, BILL_DATE, AD_DATE,
                 TO_CHAR(PREV_NO) AS PREV_NO, PREV_DATE,
                 NVL(NULLIF(TRIM(V_NAME), ''), '-') AS V_NAME,
                 TO_CHAR(AD_U_ID) AS USER_ID, {_TERM} AS TERMINAL
          FROM F WHERE {where}
          ORDER BY BILL_DATE DESC, BILL_SER DESC
        ) WHERE ROWNUM <= :lim
        """,
        {**params, "lim": limit},
    )
    return rows, _int((total[0] if total else {}).get("N"))


def assemble_details(
    rows: list[dict],
    total_n: int,
    *,
    branch_names: dict,
    user_names: dict,
    warehouse_names: dict,
    limit: int,
    sessions: list[dict] | None = None,
) -> dict[str, Any]:
    lines = []
    for idx, r in enumerate(rows):
        sess = (sessions[idx] if sessions and idx < len(sessions) else None) or {"state": "none"}
        branch = _norm_brn_code(r.get("BRANCH_CODE"))
        wh = str(r.get("W_CODE") or "").strip()
        uid = str(r.get("USER_ID") or "").strip()
        d, pd, added = r.get("BILL_DATE"), r.get("PREV_DATE"), r.get("AD_DATE")
        lines.append(
            {
                "item_code": str(r.get("I_CODE") or ""),
                "item": str(r.get("I_NAME") or "-"),
                "unit": str(r.get("UNIT") or "-"),
                "pack": _num(r.get("P_SIZE")),
                "prev_unit": str(r.get("PREV_UNIT") or "-"),
                "prev_pack": _num(r.get("PREV_PS")),
                "bill_no": str(r.get("BILL_NO") or ""),
                "date": d.strftime("%Y-%m-%d") if d else "",
                "prev_bill_no": str(r.get("PREV_NO") or ""),
                "prev_date": pd.strftime("%Y-%m-%d") if pd else "",
                "vendor": str(r.get("V_NAME") or "-"),
                "user": user_names.get(uid) or uid or "-",
                "terminal": str(r.get("TERMINAL") or "-"),
                "added_at": added.strftime("%Y-%m-%d %H:%M") if added else "-",
                "branch": branch_names.get(branch) or branch or "-",
                "warehouse": warehouse_names.get(wh) or wh or "-",
                "session_state": sess["state"],
                "session_user": sess.get("osuser", ""),
                "session_server": sess.get("server", ""),
            }
        )
    return {
        "lines": lines,
        "count": f"{total_n:,}",
        "truncated": total_n > len(lines),
        "limit": limit,
    }


def build_pack_control_details(
    date_from,
    date_to,
    *,
    user_id: str,
    terminal: str,
    branch_code: str = "",
    warehouse_code: str = "",
    group_code: str = "",
    limit: int = DETAIL_ROWS_LIMIT,
) -> dict[str, Any]:
    d_from, d_to, uid = _validate(date_from, date_to, user_id)
    lim = max(10, min(int(limit or DETAIL_ROWS_LIMIT), 2000))
    rows, total_n = _fetch_detail(
        d_from - timedelta(days=LOOKBACK_DAYS),
        d_from,
        d_to + timedelta(days=1),
        _norm_brn_code(branch_code),
        str(warehouse_code or "").strip(),
        str(group_code or "").strip(),
        user_id=uid,
        terminal=str(terminal or "-").strip()[:60] or "-",
        limit=lim,
    )
    user_names = _user_names()
    report = assemble_details(
        rows,
        total_n,
        branch_names=_branch_names(),
        user_names=user_names,
        warehouse_names=_warehouse_names(),
        limit=lim,
        sessions=_resolve_sessions(rows, uid),
    )
    report["user"] = user_names.get(uid) or uid or "-"
    return report
