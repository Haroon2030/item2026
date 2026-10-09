"""رقابة التحويلات المخزنية وأوامر الصرف المخزني — نفس منطق رقابة فواتير الشراء.

قراءة فقط (SELECT). كل شاشة جدول (مستخدم × جهاز) بأعداد كل نوع، وبالنقر على العدد
تُفتح تفاصيل مستندات ذلك المستخدم على ذلك الجهاز من ذلك النوع (مع جلسة Windows اللحظية).

- التحويلات: IAS_WHTRNS_MST — صادر TR_INOUT_TYPE=1 · وارد 2.
- أوامر الصرف المخزني: IAS_OUTGOING_MST (OUT_TYPE=1).
المنفِّذ AD_U_ID والجهاز AD_TRMNL_NM والترحيل من أعمدة المستند نفسه.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.cache import cache

from .oracle_purchase_control import (
    DETAIL_ROWS_LIMIT,
    MAX_DAYS,
    _int,
    _money,
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

# الأعمدة المشتركة بين الشاشتين تُعرَّف هنا؛ كل شاشة تضيف جدولها وأنواعها فقط.
SPECS: dict[str, dict[str, Any]] = {
    "transfers": {
        "title": "رقابة التحويلات",
        "noun": "تحويل",
        "kinds": (("out", "صادر"), ("in", "وارد")),
        "kind_expr": "CASE m.TR_INOUT_TYPE WHEN 1 THEN 'out' WHEN 2 THEN 'in' END",
        "mst": "IAS_WHTRNS_MST",
        "dtl": "IAS_WHTRNS_DTL",
        "dtl_join": "d.TR_SER = m.TR_SER",
        "base": "m.TR_INOUT_TYPE IN (1, 2)",
        "date": "m.TR_DATE",
        "order": "m.TR_SER",
        "no": "m.TR_NO",
        # TR_POST و TR_AMT فارغان في التحويلات: الحالة = استلام الصادر، والمبلغ = الكمية × تكلفة البنود
        "post": "CASE WHEN m.TR_INOUT_TYPE = 1 THEN m.PROCESSED ELSE 1 END",
        "amt": (
            "(SELECT SUM(NVL(d.I_QTY, 0) * NVL(d.STK_COST, 0)) "
            "FROM {schema}.IAS_WHTRNS_DTL d WHERE d.TR_SER = m.TR_SER)"
        ),
        "done_label": "مستلم",
        "undone_label": "غير مستلم",
        "wh": "m.W_CODE",
        "party": "TO_CHAR(m.F_W_CODE) || '>' || TO_CHAR(m.T_W_CODE)",
        "desc": "m.TR_DESC",
        "party_label": "من ← إلى",
    },
    "issues": {
        "title": "رقابة أوامر الصرف المخزني",
        "noun": "أمر صرف",
        "kinds": (("issue", "أمر صرف"),),
        "kind_expr": "'issue'",
        "mst": "IAS_OUTGOING_MST",
        "dtl": "IAS_OUTGOING_DTL",
        "dtl_join": "d.OUT_SER = m.OUT_SER AND d.OUT_TYPE = m.OUT_TYPE",
        "base": "m.OUT_TYPE = 1",
        "date": "m.OUT_DATE",
        "order": "m.OUT_SER",
        "no": "m.OUT_NO",
        "post": "m.OUT_POST",
        "done_label": "مرحّل",
        "undone_label": "غير مرحّل",
        "amt": "NVL(m.OUT_AMT, 0) + NVL(m.VAT_AMT, 0)",
        "wh": "m.W_CODE",
        "party": "TO_CHAR(m.A_CODE)",
        "desc": "m.A_DESC",
        "party_label": "الحساب",
    },
}

_TERM = "NVL(NULLIF(TRIM(m.AD_TRMNL_NM), ''), '-')"


def get_spec(key: str) -> dict[str, Any]:
    spec = SPECS.get(key)
    if not spec:
        raise OracleStockError("نوع الرقابة غير صحيح.")
    if "{schema}" in spec["amt"]:
        spec = {**spec, "amt": spec["amt"].replace("{schema}", _schema())}
    return spec


def _filters(spec, branch: str, warehouse: str, group: str, params: dict) -> str:
    parts: list[str] = []
    if branch:
        params["brn"] = int(branch) if branch.isdigit() else branch
        parts.append("AND m.BRN_NO = :brn")
    if warehouse:
        params["wh"] = warehouse
        parts.append(f"AND {spec['wh']} = {num_bind(':wh')}")
    if group:
        params["gcode"] = group
        parts.append(
            f"""AND EXISTS (
              SELECT 1
              FROM {_schema()}.{spec['dtl']} d
              JOIN {_schema()}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
              WHERE {spec['dtl_join']}
                AND i.G_CODE = :gcode)"""
        )
    return "\n".join(parts)


def _fetch_summary(spec, d_from, d_to_excl, branch, warehouse, group, user_id=""):
    params: dict[str, Any] = {"d_from": d_from, "d_to_excl": d_to_excl}
    flt = _filters(spec, branch, warehouse, group, params)
    if user_id:
        params["f_user"] = int(user_id)
        flt += "\n          AND m.AD_U_ID = :f_user"
    kind = spec["kind_expr"]
    return _fetch_all(
        f"""
        SELECT TO_CHAR(m.BRN_NO) AS BRANCH_CODE, TO_CHAR(m.AD_U_ID) AS USER_ID,
               {_TERM} AS TERMINAL,
               {kind} AS KIND_CODE,
               COUNT(*) AS N,
               SUM(CASE WHEN NVL({spec['post']}, 0) = 1 THEN 1 ELSE 0 END) AS POSTED_N,
               ROUND(SUM({spec['amt']}), 2) AS AMT
        FROM {_schema()}.{spec['mst']} m
        WHERE {spec['date']} >= :d_from AND {spec['date']} < :d_to_excl
          AND {spec['base']} {flt}
        GROUP BY m.BRN_NO, m.AD_U_ID, {_TERM}, {kind}
        """,
        params,
    )


def assemble_report(
    spec: dict, summary: list[dict], *, branch_names: dict, user_names: dict
) -> dict[str, Any]:
    """جدول رقابة (مستخدم × جهاز) + إجماليات. دالة نقية قابلة للاختبار."""
    keys = [k for k, _ in spec["kinds"]]
    totals = {k: 0 for k in keys}
    amount_all = 0.0
    posted_all = 0
    control: dict[tuple, dict] = {}

    def bname(code: Any) -> str:
        code = _norm_brn_code(code)
        return branch_names.get(code) or code or "-"

    for r in summary:
        kind = str(r.get("KIND_CODE") or "")
        if kind not in totals:
            continue
        n, amt, posted = _int(r.get("N")), float(r.get("AMT") or 0), _int(r.get("POSTED_N"))
        totals[kind] += n
        amount_all += amt
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
                "user": user_names.get(user) or user or "-",
                "terminal": terminal,
                "counts": {k: 0 for k in keys},
                "total": 0,
                "posted": 0,
                "amount": 0.0,
            },
        )
        row["counts"][kind] += n
        row["total"] += n
        row["posted"] += posted
        row["amount"] += amt

    rows = sorted(control.values(), key=lambda r: (-r["total"], r["user"]))
    for r in rows:
        r["amount_display"] = _money(r["amount"])
        r["unposted"] = r["total"] - r["posted"]
        r["cells"] = [
            {"kind": k, "label": lbl, "n": r["counts"][k]} for k, lbl in spec["kinds"]
        ]

    count_all = sum(totals.values())
    return {
        "kinds": [{"key": k, "label": lbl} for k, lbl in spec["kinds"]],
        "done_label": spec["done_label"],
        "undone_label": spec["undone_label"],
        "control_rows": rows,
        "totals": {
            "cells": [f"{totals[k]:,}" for k in keys],
            "total": f"{count_all:,}",
            "unposted": f"{count_all - posted_all:,}",
            "amount": _money(amount_all),
        },
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


def build_control(
    spec_key: str,
    date_from,
    date_to,
    *,
    branch_code: str = "",
    warehouse_code: str = "",
    group_code: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    spec = get_spec(spec_key)
    d_from, d_to, uid = _validate(date_from, date_to, user_id)
    brn = _norm_brn_code(branch_code)
    wh = str(warehouse_code or "").strip()
    gcode = str(group_code or "").strip()

    key = f"doc_control:{spec_key}:v2:{d_from}:{d_to}:{brn}:{wh}:{gcode}:{uid}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    summary = _fetch_summary(spec, d_from, d_to + timedelta(days=1), brn, wh, gcode, uid)
    report = assemble_report(
        spec, summary, branch_names=_branch_names(), user_names=_user_names()
    )
    report["period_label"] = f"{d_from.isoformat()} → {d_to.isoformat()}"
    cache.set(key, report, CACHE_SECONDS)
    return report


# ——— التفاصيل ———


def _fetch_detail(spec, d_from, d_to_excl, branch, warehouse, group, *, user_id, terminal, kind, limit):
    params: dict[str, Any] = {"d_from": d_from, "d_to_excl": d_to_excl, "p_kind": kind}
    flt = _filters(spec, branch, warehouse, group, params)
    if user_id:
        params["p_user"] = int(user_id)
        user_sql = "AND m.AD_U_ID = :p_user"
    else:
        user_sql = "AND m.AD_U_ID IS NULL"
    params["p_term"] = terminal
    schema = _schema()
    where = f"""{spec['date']} >= :d_from AND {spec['date']} < :d_to_excl
          AND {spec['base']}
          AND {spec['kind_expr']} = :p_kind
          AND {_TERM} = :p_term
          {user_sql} {flt}"""
    totals = _fetch_all(
        f"""
        SELECT COUNT(*) AS N, ROUND(SUM({spec['amt']}), 2) AS AMT
        FROM {schema}.{spec['mst']} m
        WHERE {where}
        """,
        params,
    )
    rows = _fetch_all(
        f"""
        SELECT * FROM (
          SELECT TO_CHAR(m.BRN_NO) AS BRANCH_CODE, TO_CHAR({spec['wh']}) AS W_CODE,
                 TO_CHAR({spec['no']}) AS DOC_NO, {spec['date']} AS DOC_DATE, m.AD_DATE,
                 {spec['party']} AS PARTY,
                 NVL(NULLIF(TRIM({spec['desc']}), ''), '-') AS DESCR,
                 TO_CHAR(m.AD_U_ID) AS USER_ID,
                 {_TERM} AS TERMINAL,
                 NVL({spec['post']}, 0) AS POSTED,
                 ROUND({spec['amt']}, 2) AS AMT
          FROM {schema}.{spec['mst']} m
          WHERE {where}
          ORDER BY {spec['date']} DESC, {spec['order']} DESC
        ) WHERE ROWNUM <= :lim
        """,
        {**params, "lim": limit},
    )
    return rows, (totals[0] if totals else {})


def _party_label(spec_key: str, party: Any, wh_names: dict[str, str]) -> str:
    text = str(party or "").strip()
    if spec_key == "transfers" and ">" in text:
        src, dst = (p.strip() for p in text.split(">", 1))
        return f"{wh_names.get(src) or src or '-'} ← {wh_names.get(dst) or dst or '-'}"
    return text or "-"


def assemble_details(
    spec_key: str,
    rows: list[dict],
    totals: dict,
    *,
    kind: str,
    branch_names: dict,
    user_names: dict,
    warehouse_names: dict,
    limit: int,
    sessions: list[dict] | None = None,
) -> dict[str, Any]:
    spec = get_spec(spec_key)
    docs = []
    for idx, r in enumerate(rows):
        sess = (sessions[idx] if sessions and idx < len(sessions) else None) or {"state": "none"}
        d, added = r.get("DOC_DATE"), r.get("AD_DATE")
        branch = _norm_brn_code(r.get("BRANCH_CODE"))
        wh = str(r.get("W_CODE") or "").strip()
        uid = str(r.get("USER_ID") or "").strip()
        docs.append(
            {
                "added_at": added.strftime("%Y-%m-%d %H:%M") if added else "-",
                "user": user_names.get(uid) or uid or "-",
                "terminal": str(r.get("TERMINAL") or "-"),
                "date": d.strftime("%Y-%m-%d") if d else "",
                "doc_no": str(r.get("DOC_NO") or ""),
                "party": _party_label(spec_key, r.get("PARTY"), warehouse_names),
                "descr": str(r.get("DESCR") or "-"),
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
    labels = dict(spec["kinds"])
    return {
        "kind": kind,
        "kind_label": labels[kind],
        "party_label": spec["party_label"],
        "done_label": spec["done_label"],
        "undone_label": spec["undone_label"],
        "docs": docs,
        "count": f"{total_n:,}",
        "amount": _money(totals.get("AMT")),
        "truncated": total_n > len(docs),
        "limit": limit,
    }


def build_control_details(
    spec_key: str,
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
    spec = get_spec(spec_key)
    if kind not in dict(spec["kinds"]):
        raise OracleStockError("نوع المستند غير صحيح.")
    d_from, d_to, uid = _validate(date_from, date_to, user_id)
    lim = max(10, min(int(limit or DETAIL_ROWS_LIMIT), 2000))

    rows, totals = _fetch_detail(
        spec,
        d_from,
        d_to + timedelta(days=1),
        _norm_brn_code(branch_code),
        str(warehouse_code or "").strip(),
        str(group_code or "").strip(),
        user_id=uid,
        terminal=str(terminal or "-").strip()[:60] or "-",
        kind=kind,
        limit=lim,
    )
    # _resolve_sessions يقرأ AD_DATE و TERMINAL فقط من كل صف
    sessions = _resolve_sessions(rows, uid)
    user_names = _user_names()
    report = assemble_details(
        spec_key,
        rows,
        totals,
        kind=kind,
        branch_names=_branch_names(),
        user_names=user_names,
        warehouse_names=_warehouse_names(),
        limit=lim,
        sessions=sessions,
    )
    report["user"] = user_names.get(uid) or uid or "-"
    return report
