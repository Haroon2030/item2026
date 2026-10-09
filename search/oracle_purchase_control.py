"""رقابة فواتير الشراء: من أدخل الفاتورة، من أي جهاز، ونقد أم آجل.

قراءة فقط (SELECT) من IAS_PI_BILL_MST. نوع المستند BILL_DOC_TYPE:
1 (وأحياناً 5) = شراء نقدي له رقم صندوق، و4 / 8 = شراء آجل — كما في بقية النظام.
اسم الجهاز AD_TRMNL_NM هو اسم الحاسوب الذي أُدخلت منه الفاتورة.

الشاشة الرئيسية: جدول (مستخدم × جهاز) بأعداد النقد والآجل؛ وبالنقر على العدد
تُفتح شاشة تفاصيل فواتير ذلك المستخدم على ذلك الجهاز من ذلك النوع.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.core.cache import cache

from .oracle_sqlutil import num_bind
from .oracle_stock import (
    OracleStockError,
    _as_date,
    _branch_names,
    _django_lookup_get,
    _django_lookup_set,
    _fetch_all,
    _norm_brn_code,
    _schema,
    _user_names,
    oracle_enabled,
)

logger = logging.getLogger(__name__)

MAX_DAYS = 31
CACHE_SECONDS = 300
DETAIL_ROWS_LIMIT = 1000

CASH, CREDIT = "cash", "credit"
KIND_LABEL = {CASH: "نقد", CREDIT: "آجل"}
_DOC_KIND = {1: CASH, 5: CASH, 4: CREDIT, 8: CREDIT}
_KIND_DOC_TYPES = {CASH: (1, 5), CREDIT: (4, 8)}


def _money(value: Any) -> str:
    return f"{float(value or 0):,.2f}"


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _warehouse_names() -> dict[str, str]:
    hit, cached = _django_lookup_get("warehouse_names:v1")
    if hit:
        return cached
    try:
        rows = _fetch_all(
            f"SELECT TO_CHAR(W_CODE) AS W_CODE, W_NAME FROM {_schema()}.WAREHOUSE_DETAILS "
            "WHERE W_CODE IS NOT NULL"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Warehouse names unavailable: %s", exc)
        return {}  # لا يُخزَّن الفشل (كان يبقى {} في الكاش أسبوعاً)
    names = {
        str(r.get("W_CODE") or "").strip(): str(r.get("W_NAME") or "").strip()
        for r in rows
        if r.get("W_CODE") is not None
    }
    return _django_lookup_set("warehouse_names:v1", names)


def _filters(branch: str, warehouse: str, group: str, params: dict) -> str:
    parts: list[str] = []
    if branch:
        params["brn"] = int(branch) if branch.isdigit() else branch
        parts.append("AND m.BRN_NO = :brn")
    if warehouse:
        params["wh"] = warehouse
        parts.append(f"AND m.W_CODE = {num_bind(':wh')}")
    if group:
        params["gcode"] = group
        parts.append(
            f"""AND EXISTS (
              SELECT 1
              FROM {_schema()}.IAS_PI_BILL_DTL d
              JOIN {_schema()}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
              WHERE d.BILL_SER = m.BILL_SER
                AND i.G_CODE = :gcode)"""
        )
    return "\n".join(parts)


def _fetch_summary(d_from, d_to_excl, branch, warehouse, group, user_id=""):
    schema = _schema()
    params: dict[str, Any] = {"d_from": d_from, "d_to_excl": d_to_excl}
    flt = _filters(branch, warehouse, group, params)
    if user_id:
        params["f_user"] = int(user_id)
        flt += "\n          AND m.AD_U_ID = :f_user"
    return _fetch_all(
        f"""
        SELECT TO_CHAR(m.BRN_NO) AS BRANCH_CODE, TO_CHAR(m.AD_U_ID) AS USER_ID,
               NVL(NULLIF(TRIM(m.AD_TRMNL_NM), ''), '-') AS TERMINAL,
               m.BILL_DOC_TYPE AS KIND_CODE,
               COUNT(*) AS N,
               SUM(CASE WHEN NVL(m.BILL_POST, 0) = 1 THEN 1 ELSE 0 END) AS POSTED_N,
               ROUND(SUM(NVL(m.BILL_AMT, 0) + NVL(m.VAT_AMT, 0)), 2) AS AMT
        FROM {schema}.IAS_PI_BILL_MST m
        WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
          AND m.BILL_DOC_TYPE IN (1, 4, 5, 8) {flt}
        GROUP BY m.BRN_NO, m.AD_U_ID, NVL(NULLIF(TRIM(m.AD_TRMNL_NM), ''), '-'),
                 m.BILL_DOC_TYPE
        """,
        params,
    )


def assemble_report(
    summary: list[dict],
    *,
    branch_names: dict[str, str],
    user_names: dict[str, str],
) -> dict[str, Any]:
    """جدول رقابة (مستخدم × جهاز) + إجماليات. دالة نقية قابلة للاختبار."""
    totals = {CASH: [0, 0.0], CREDIT: [0, 0.0]}
    control: dict[tuple, dict] = {}
    posted_all = 0

    def bname(code: Any) -> str:
        code = _norm_brn_code(code)
        return branch_names.get(code) or code or "-"

    def uname(code: Any) -> str:
        code = str(code or "").strip()
        return user_names.get(code) or code or "-"

    for r in summary:
        kind = _DOC_KIND.get(_int(r.get("KIND_CODE")))
        if not kind:
            continue
        n, amt = _int(r.get("N")), float(r.get("AMT") or 0)
        posted = _int(r.get("POSTED_N"))
        totals[kind][0] += n
        totals[kind][1] += amt
        posted_all += posted
        user = str(r.get("USER_ID") or "").strip()
        terminal = str(r.get("TERMINAL") or "-")
        key = (_norm_brn_code(r.get("BRANCH_CODE")), user, terminal)
        row = control.setdefault(
            key,
            {
                "branch_code": key[0],
                "user_id": user,
                "branch": bname(key[0]),
                "user": uname(user),
                "terminal": terminal,
                CASH: 0, CREDIT: 0, "total": 0, "posted": 0, "amount": 0.0,
            },
        )
        row[kind] += n
        row["total"] += n
        row["posted"] += posted
        row["amount"] += amt

    rows = sorted(control.values(), key=lambda r: (-r["total"], r["user"]))
    for r in rows:
        r["amount_display"] = _money(r["amount"])
        r["unposted"] = r["total"] - r["posted"]

    count_all = sum(t[0] for t in totals.values())
    return {
        "control_rows": rows,
        "totals": {
            "cash": f"{totals[CASH][0]:,}",
            "credit": f"{totals[CREDIT][0]:,}",
            "total": f"{count_all:,}",
            "unposted": f"{count_all - posted_all:,}",
            "amount": _money(sum(t[1] for t in totals.values())),
        },
    }


def build_purchase_control(
    date_from,
    date_to,
    *,
    branch_code: str = "",
    warehouse_code: str = "",
    group_code: str = "",
    user_id: str = "",
) -> dict[str, Any]:
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
    brn = _norm_brn_code(branch_code)
    wh = str(warehouse_code or "").strip()
    gcode = str(group_code or "").strip()

    key = f"purchases:control:v3:{d_from}:{d_to}:{brn}:{wh}:{gcode}:{uid}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    summary = _fetch_summary(d_from, d_to + timedelta(days=1), brn, wh, gcode, uid)
    report = assemble_report(
        summary,
        branch_names=_branch_names(),
        user_names=_user_names(),
    )
    report["period_label"] = f"{d_from.isoformat()} → {d_to.isoformat()}"
    cache.set(key, report, CACHE_SECONDS)
    return report


# ——— شاشة التفاصيل ———


def _fetch_detail(
    d_from, d_to_excl, branch, warehouse, group, *, user_id, terminal, doc_types, limit
):
    schema = _schema()
    params: dict[str, Any] = {"d_from": d_from, "d_to_excl": d_to_excl}
    flt = _filters(branch, warehouse, group, params)
    doc_binds = []
    for i, code in enumerate(doc_types):
        params[f"dt{i}"] = code
        doc_binds.append(f":dt{i}")
    if user_id:
        params["p_user"] = int(user_id)
        user_sql = "AND m.AD_U_ID = :p_user"
    else:
        user_sql = "AND m.AD_U_ID IS NULL"
    params["p_term"] = terminal
    where = f"""m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
          AND m.BILL_DOC_TYPE IN ({", ".join(doc_binds)})
          AND NVL(NULLIF(TRIM(m.AD_TRMNL_NM), ''), '-') = :p_term
          {user_sql} {flt}"""
    totals = _fetch_all(
        f"""
        SELECT COUNT(*) AS N,
               ROUND(SUM(NVL(m.BILL_AMT, 0) + NVL(m.VAT_AMT, 0)), 2) AS AMT
        FROM {schema}.IAS_PI_BILL_MST m
        WHERE {where}
        """,
        params,
    )
    rows = _fetch_all(
        f"""
        SELECT * FROM (
          SELECT TO_CHAR(m.BRN_NO) AS BRANCH_CODE, TO_CHAR(m.W_CODE) AS W_CODE,
                 TO_CHAR(m.BILL_NO) AS BILL_NO, m.BILL_DATE, m.AD_DATE,
                 NVL(NULLIF(TRIM(m.V_NAME), ''), '-') AS V_NAME,
                 TO_CHAR(m.AD_U_ID) AS USER_ID,
                 NVL(NULLIF(TRIM(m.AD_TRMNL_NM), ''), '-') AS TERMINAL,
                 NVL(m.BILL_POST, 0) AS POSTED,
                 ROUND(NVL(m.BILL_AMT, 0) + NVL(m.VAT_AMT, 0), 2) AS AMT
          FROM {schema}.IAS_PI_BILL_MST m
          WHERE {where}
          ORDER BY m.BILL_DATE DESC, m.BILL_SER DESC
        ) WHERE ROWNUM <= :lim
        """,
        {**params, "lim": limit},
    )
    return rows, (totals[0] if totals else {})


# ——— جلسة Windows الريموت وقت إضافة الفاتورة (قراءة لحظية فقط، لا تخزين) ———
#
# سجل دخول أونكس IAS_USR_LGN_HSTRY يحفظ لكل دخول رقم جلسة Oracle (SESSION_SID/SESSION_AUDSID)
# واسم جهاز العميل، وV$SESSION يعرض مستخدم Windows (OSUSER) والخادم (MACHINE) للجلسات الحيّة فقط.
# فنربط الفاتورة بآخر دخول لمستخدمها على جهازها قبل وقت إضافتها، ثم نقرأ الجلسة إن كانت حيّة.

SESSION_LOOKBACK_DAYS = 7
SESSION_MAX_IDS = 300


def _norm_term(value: Any) -> str:
    return str(value or "").replace("\x00", "").strip().rstrip("=").strip().upper()


def _fetch_login_events(user_id: str, t_from, t_to) -> list[dict]:
    return _fetch_all(
        f"""
        SELECT LGN_OUT_DATE AS T, LGN_TYP AS TYP, TRMNL_NM AS TERM,
               SESSION_SID AS SID, SESSION_AUDSID AS AUD
        FROM {_schema()}.IAS_USR_LGN_HSTRY
        WHERE U_ID = :p_user AND LGN_OUT_DATE >= :t0 AND LGN_OUT_DATE <= :t1
        ORDER BY LGN_OUT_DATE
        """,
        {"p_user": int(user_id), "t0": t_from, "t1": t_to},
    )


def _fetch_live_sessions(audsids: list[int]) -> dict[tuple, dict]:
    """جلسات Oracle الحيّة الآن لمعرّفات محددة: (SID, AUDSID) ← مستخدم Windows والخادم."""
    ids = sorted({int(a) for a in audsids if a is not None})[:SESSION_MAX_IDS]
    if not ids:
        return {}
    params = {f"a{i}": v for i, v in enumerate(ids)}
    rows = _fetch_all(
        f"""
        SELECT SID, AUDSID, OSUSER, MACHINE, LOGON_TIME
        FROM V$SESSION
        WHERE TYPE = 'USER' AND AUDSID IN ({", ".join(":" + k for k in params)})
        """,
        params,
    )
    return {(r["SID"], r["AUDSID"]): r for r in rows}


def match_session(added_at, terminal: Any, logouts: dict, logins: list[dict], live: dict) -> dict:
    """جلسة المستخدم وقت إضافة الفاتورة.

    state: live = الجلسة حيّة ومعروف مستخدم Windows · ended = وُجد دخول لكن الجلسة انتهت ·
    none = لا دخول معروف لهذا الجهاز قبل الوقت."""
    if not added_at:
        return {"state": "none"}
    term = _norm_term(terminal)
    pick = None
    for e in logins:  # مرتبة زمنياً
        t = e.get("T")
        if not t or t > added_at or _norm_term(e.get("TERM")) != term:
            continue
        key = (e.get("SID"), e.get("AUD"))
        if any(t <= x <= added_at for x in logouts.get(key, ())):
            continue  # أُغلقت هذه الجلسة قبل الفاتورة
        pick = e
    if pick is None:
        return {"state": "none"}
    lv = live.get((pick.get("SID"), pick.get("AUD")))
    started = lv.get("LOGON_TIME") if lv else None
    if not lv or (started and started > added_at):
        return {"state": "ended"}
    machine = str(lv.get("MACHINE") or "").replace("\x00", "").strip()
    return {
        "state": "live",
        "osuser": str(lv.get("OSUSER") or "").strip(),
        "server": machine.rsplit("\\", 1)[-1] or machine,
    }


def match_sessions_for_rows(rows: list[dict], events: list[dict], live: dict) -> list[dict]:
    logins = [e for e in events if _int(e.get("TYP")) == 1]
    logouts: dict[tuple, list] = {}
    for e in events:
        if _int(e.get("TYP")) == 0 and e.get("T"):
            logouts.setdefault((e.get("SID"), e.get("AUD")), []).append(e["T"])
    return [
        match_session(r.get("AD_DATE"), r.get("TERMINAL"), logouts, logins, live)
        for r in rows
    ]


def _resolve_sessions(rows: list[dict], user_id: str) -> list[dict]:
    """ربط كل صف بجلسته. أي فشل في قراءة الجلسات لا يُسقط الشاشة (يعود «غير معروف»)."""
    blank = [{"state": "none"} for _ in rows]
    times = [r["AD_DATE"] for r in rows if r.get("AD_DATE")]
    if not user_id or not times:
        return blank
    try:
        events = _fetch_login_events(
            user_id, min(times) - timedelta(days=SESSION_LOOKBACK_DAYS), max(times)
        )
        live = _fetch_live_sessions([e.get("AUD") for e in events if _int(e.get("TYP")) == 1])
        return match_sessions_for_rows(rows, events, live)
    except Exception as exc:  # noqa: BLE001
        logger.warning("session resolve failed: %s", exc)
        return blank


def assemble_details(
    rows: list[dict],
    totals: dict,
    *,
    kind: str,
    branch_names: dict[str, str],
    user_names: dict[str, str],
    warehouse_names: dict[str, str],
    limit: int,
    sessions: list[dict] | None = None,
) -> dict[str, Any]:
    invoices = []
    for idx, r in enumerate(rows):
        sess = (sessions[idx] if sessions and idx < len(sessions) else None) or {"state": "none"}
        d = r.get("BILL_DATE")
        branch = _norm_brn_code(r.get("BRANCH_CODE"))
        wh = str(r.get("W_CODE") or "").strip()
        uid = str(r.get("USER_ID") or "").strip()
        added = r.get("AD_DATE")
        invoices.append(
            {
                "added_at": added.strftime("%Y-%m-%d %H:%M") if added else "-",
                "user": user_names.get(uid) or uid or "-",
                "terminal": str(r.get("TERMINAL") or "-"),
                "date": d.strftime("%Y-%m-%d") if d else "",
                "bill_no": str(r.get("BILL_NO") or ""),
                "vendor": str(r.get("V_NAME") or "-"),
                "branch": branch_names.get(branch) or branch or "-",
                "warehouse": warehouse_names.get(wh) or wh or "-",
                "amount_display": _money(r.get("AMT")),
                "posted": bool(_int(r.get("POSTED"))),
                "session_state": sess["state"],
                "session_user": sess.get("osuser", ""),
                "session_server": sess.get("server", ""),
            }
        )
    total_n = _int(totals.get("N"))
    return {
        "kind": kind,
        "kind_label": KIND_LABEL[kind],
        "invoices": invoices,
        "count": f"{total_n:,}",
        "amount": _money(totals.get("AMT")),
        "truncated": total_n > len(invoices),
        "limit": limit,
    }


def build_purchase_control_details(
    date_from,
    date_to,
    *,
    kind: str,
    user_id: str,
    terminal: str,
    branch_code: str = "",
    warehouse_code: str = "",
    group_code: str = "",
    limit: int = DETAIL_ROWS_LIMIT,
) -> dict[str, Any]:
    """فواتير مستخدم واحد على جهاز واحد من نوع واحد (نقد أو آجل) ضمن نفس فلاتر الشاشة."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    doc_types = _KIND_DOC_TYPES.get(kind)
    if not doc_types:
        raise OracleStockError("نوع الفاتورة غير صحيح.")
    uid = str(user_id or "").strip()
    if uid and not uid.isdigit():
        raise OracleStockError("رقم المستخدم غير صحيح.")
    d_from, d_to = _as_date(date_from), _as_date(date_to)
    if d_from > d_to or (d_to - d_from).days >= MAX_DAYS:
        raise OracleStockError(f"الفترة غير صحيحة (الحد الأقصى {MAX_DAYS} يوماً).")
    lim = max(10, min(int(limit or DETAIL_ROWS_LIMIT), 2000))

    rows, totals = _fetch_detail(
        d_from,
        d_to + timedelta(days=1),
        _norm_brn_code(branch_code),
        str(warehouse_code or "").strip(),
        str(group_code or "").strip(),
        user_id=uid,
        terminal=str(terminal or "-").strip()[:60] or "-",
        doc_types=doc_types,
        limit=lim,
    )
    user_names = _user_names()
    sessions = _resolve_sessions(rows, uid)
    report = assemble_details(
        rows,
        totals,
        kind=kind,
        branch_names=_branch_names(),
        sessions=sessions,
        user_names=user_names,
        warehouse_names=_warehouse_names(),
        limit=lim,
    )
    report["user"] = user_names.get(uid) or uid or "-"
    return report
