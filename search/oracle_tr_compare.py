"""مقارنة طلبات التحويل المخزني مع رصيد المخزن المطلوب والمخزن الرئيسي للفرع."""

from __future__ import annotations

from datetime import date
from typing import Any

from .oracle_pr_compare import (
    _dt_label,
    _fetch_stock_for_items,
    _fetch_unit_packs_map,
    _norm_code,
    _qty,
    _stock_cells_for_unit,
    _today_bounds,
)
from .oracle_stock import (
    _bind_brn,
    _branch_names,
    _fetch_all,
    _schema,
    oracle_enabled,
)


def _bind_num(value: Any):
    text = str(value or "").strip()
    if text.isdigit():
        try:
            return int(text)
        except ValueError:
            return text
    return text


def _inactive_ok(alias: str) -> str:
    return f"({alias}.INACTIVE IS NULL OR {alias}.INACTIVE = 0)"


def _wh_name_sql(alias: str) -> str:
    return f"NVL(NULLIF(TRIM({alias}.W_NAME), ''), TO_CHAR({alias}.W_CODE))"


def _wh_display(code: Any, name: Any = "") -> str:
    code = _norm_code(code)
    name = str(name or "").strip()
    if code and name and name != code:
        return f"{code} — {name}"
    return name or code or "—"


def fetch_main_warehouse_for_branch(branch_code: str) -> dict:
    """المخزن الرئيسي للفرع: غالباً W_CODE = رقم الفرع × 10 (سكاي 6 → 60)."""
    brn_raw = str(branch_code or "").strip()
    brn = _bind_brn(brn_raw)
    if brn in ("", None) or not oracle_enabled():
        return {"code": "", "name": ""}
    schema = _schema()
    rows = _fetch_all(
        f"""
        SELECT w.W_CODE, {_wh_name_sql("w")} AS W_NAME, NVL(w.MAIN_WCODE, 0) AS MAIN_WCODE
        FROM {schema}.WAREHOUSE_DETAILS w
        WHERE w.CONN_BRN_NO = :brn
          AND {_inactive_ok("w")}
          AND w.W_CODE IS NOT NULL
        """,
        {"brn": brn},
    )
    by_code: dict[int, dict] = {}
    for row in rows:
        try:
            code = int(row.get("W_CODE"))
        except (TypeError, ValueError):
            continue
        by_code[code] = {
            "code": _norm_code(code),
            "name": str(row.get("W_NAME") or "").strip() or _norm_code(code),
            "main_flag": int(row.get("MAIN_WCODE") or 0),
        }
    try:
        brn_n = int(brn)
    except (TypeError, ValueError):
        brn_n = None
    if brn_n:
        for cand in (brn_n * 10, brn_n * 100, brn_n * 1000, brn_n):
            hit = by_code.get(cand)
            if hit:
                return {"code": hit["code"], "name": hit["name"]}
    zeros = [row for row in by_code.values() if row["main_flag"] == 0]
    pool = zeros or list(by_code.values())
    if not pool:
        return {"code": "", "name": ""}
    pool.sort(key=lambda row: int(row["code"]) if str(row["code"]).isdigit() else 10**9)
    return {"code": pool[0]["code"], "name": pool[0]["name"]}


def _wh_cell(stock_by_wh: dict[str, dict], code: str) -> dict | None:
    want = _norm_code(code)
    if not want:
        return None
    if want in stock_by_wh:
        return stock_by_wh[want]
    for key, cell in stock_by_wh.items():
        if _norm_code(key) == want:
            return cell
    return None


def fetch_today_transfer_requests(
    *,
    branch_code: str,
    day: date | None = None,
    warehouse_code: str = "",
) -> list[dict]:
    """طلبات التحويل ليوم وفرع (اختياريًا حسب مخزن الطالب W_CODE)."""
    if not oracle_enabled():
        return []
    brn = _bind_brn(branch_code)
    if brn in ("", None):
        return []
    wh = str(warehouse_code or "").strip()
    schema = _schema()
    d_from, d_to_excl = _today_bounds(day)
    names = _branch_names()
    params: dict[str, Any] = {
        "d_from": d_from,
        "d_to_excl": d_to_excl,
        "brn": brn,
    }
    wh_sql = ""
    if wh:
        params["wh"] = _bind_num(wh)
        wh_sql = "AND m.W_CODE = :wh"

    rows = _fetch_all(
        f"""
        SELECT /*+ LEADING(m d) USE_NL(d) INDEX(d INDX_SER_OUTREQ_DTL) */
               m.OUT_REQ_TYPE AS TR_TYPE,
               m.OUT_REQ_NO AS TR_NO,
               m.OUT_REQ_SER AS TR_SER,
               NVL(m.AD_DATE, m.OUT_REQ_DATE) AS TR_WHEN,
               TO_CHAR(NVL(m.AD_DATE, m.OUT_REQ_DATE), 'YYYY-MM-DD HH24:MI') AS TR_WHEN_LABEL,
               m.BRN_NO AS BRANCH_CODE,
               m.W_CODE AS TO_W_CODE,
               {_wh_name_sql("tw")} AS TO_W_NAME,
               m.F_W_CODE AS FROM_W_CODE,
               {_wh_name_sql("fw")} AS FROM_W_NAME,
               m.AD_U_ID AS USER_CODE,
               NVL(u.U_A_NAME, NVL(u.U_E_NAME, TO_CHAR(m.AD_U_ID))) AS USER_NAME,
               m.APPROVED AS APPROVED,
               m.PROCESSED AS PROCESSED,
               COUNT(DISTINCT d.I_CODE) AS ITEM_COUNT,
               ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL
        FROM {schema}.IAS_OUT_REQUEST_MST m
        JOIN {schema}.IAS_OUT_REQUEST_DTL d
          ON d.OUT_REQ_SER = m.OUT_REQ_SER
        LEFT JOIN {schema}.WAREHOUSE_DETAILS tw ON tw.W_CODE = m.W_CODE
        LEFT JOIN {schema}.WAREHOUSE_DETAILS fw ON fw.W_CODE = m.F_W_CODE
        LEFT JOIN {schema}.USER_R u ON u.U_ID = m.AD_U_ID
        WHERE m.OUT_REQ_DATE >= :d_from
          AND m.OUT_REQ_DATE < :d_to_excl
          AND {_inactive_ok("m")}
          AND m.BRN_NO = :brn
          {wh_sql}
        GROUP BY m.OUT_REQ_TYPE, m.OUT_REQ_NO, m.OUT_REQ_SER,
                 NVL(m.AD_DATE, m.OUT_REQ_DATE),
                 m.BRN_NO, m.W_CODE, tw.W_NAME, tw.W_CODE,
                 m.F_W_CODE, fw.W_NAME, fw.W_CODE,
                 m.AD_U_ID, u.U_A_NAME, u.U_E_NAME,
                 m.APPROVED, m.PROCESSED
        ORDER BY NVL(m.AD_DATE, m.OUT_REQ_DATE) DESC, m.OUT_REQ_SER DESC
        """,
        params,
    )

    main_wh = fetch_main_warehouse_for_branch(str(brn))
    out: list[dict] = []
    for row in rows:
        branch = _norm_code(row.get("BRANCH_CODE"))
        processed = int(row.get("PROCESSED") or 0) == 1
        approved = int(row.get("APPROVED") or 0) == 1
        if processed:
            status = "مرحّل"
            status_kind = "posted"
        elif approved:
            status = "معتمد"
            status_kind = "approved"
        else:
            status = "قيد الطلب"
            status_kind = "open"
        out.append(
            {
                "tr_type": str(row.get("TR_TYPE") or "").strip(),
                "tr_no": str(row.get("TR_NO") or "").strip(),
                "tr_ser": str(row.get("TR_SER") or "").strip(),
                "date_label": str(row.get("TR_WHEN_LABEL") or "").strip()
                or _dt_label(row.get("TR_WHEN")),
                "branch_code": branch,
                "branch_name": names.get(branch) or names.get(str(row.get("BRANCH_CODE") or "").strip()) or branch,
                "to_wh_code": _norm_code(row.get("TO_W_CODE")),
                "to_wh_name": str(row.get("TO_W_NAME") or "").strip()
                or _norm_code(row.get("TO_W_CODE"))
                or "—",
                "from_wh_code": _norm_code(row.get("FROM_W_CODE")),
                "from_wh_name": str(row.get("FROM_W_NAME") or "").strip()
                or _norm_code(row.get("FROM_W_CODE"))
                or "—",
                "from_wh_label": _wh_display(
                    row.get("FROM_W_CODE"), row.get("FROM_W_NAME")
                ),
                "to_wh_label": _wh_display(row.get("TO_W_CODE"), row.get("TO_W_NAME")),
                "main_wh_code": main_wh.get("code") or "",
                "main_wh_name": main_wh.get("name") or "",
                "main_wh_label": _wh_display(
                    main_wh.get("code"), main_wh.get("name")
                ),
                "user_code": str(row.get("USER_CODE") or "").strip(),
                "user_name": str(row.get("USER_NAME") or "").strip(),
                "item_count": int(row.get("ITEM_COUNT") or 0),
                "qty_total": float(row.get("QTY_TOTAL") or 0),
                "qty_display": _qty(row.get("QTY_TOTAL") or 0),
                "processed": processed,
                "approved": approved,
                "status": status,
                "status_kind": status_kind,
            }
        )
    return out


def _fetch_request_header(tr_type: str, tr_no: str, tr_ser: str) -> dict | None:
    schema = _schema()
    names = _branch_names()
    rows = _fetch_all(
        f"""
        SELECT m.OUT_REQ_TYPE AS TR_TYPE,
               m.OUT_REQ_NO AS TR_NO,
               m.OUT_REQ_SER AS TR_SER,
               NVL(m.AD_DATE, m.OUT_REQ_DATE) AS TR_WHEN,
               TO_CHAR(NVL(m.AD_DATE, m.OUT_REQ_DATE), 'YYYY-MM-DD HH24:MI') AS TR_WHEN_LABEL,
               m.BRN_NO AS BRANCH_CODE,
               m.W_CODE AS TO_W_CODE,
               {_wh_name_sql("tw")} AS TO_W_NAME,
               m.F_W_CODE AS FROM_W_CODE,
               {_wh_name_sql("fw")} AS FROM_W_NAME,
               m.AD_U_ID AS USER_CODE,
               NVL(u.U_A_NAME, NVL(u.U_E_NAME, TO_CHAR(m.AD_U_ID))) AS USER_NAME,
               m.APPROVED AS APPROVED,
               m.PROCESSED AS PROCESSED
        FROM {schema}.IAS_OUT_REQUEST_MST m
        LEFT JOIN {schema}.WAREHOUSE_DETAILS tw ON tw.W_CODE = m.W_CODE
        LEFT JOIN {schema}.WAREHOUSE_DETAILS fw ON fw.W_CODE = m.F_W_CODE
        LEFT JOIN {schema}.USER_R u ON u.U_ID = m.AD_U_ID
        WHERE m.OUT_REQ_SER = :tr_ser
          AND m.OUT_REQ_TYPE = :tr_type
          AND m.OUT_REQ_NO = :tr_no
          AND {_inactive_ok("m")}
        FETCH FIRST 1 ROWS ONLY
        """,
        {
            "tr_ser": _bind_num(tr_ser),
            "tr_type": _bind_num(tr_type),
            "tr_no": _bind_num(tr_no),
        },
    )
    if not rows:
        return None
    row = rows[0]
    branch = _norm_code(row.get("BRANCH_CODE"))
    processed = int(row.get("PROCESSED") or 0) == 1
    approved = int(row.get("APPROVED") or 0) == 1
    to_wh_code = _norm_code(row.get("TO_W_CODE"))
    to_wh_name = str(row.get("TO_W_NAME") or "").strip() or to_wh_code or "—"
    from_wh_code = _norm_code(row.get("FROM_W_CODE"))
    from_wh_name = str(row.get("FROM_W_NAME") or "").strip() or from_wh_code or "—"
    main_wh = fetch_main_warehouse_for_branch(branch)
    main_wh_code = main_wh.get("code") or to_wh_code
    main_wh_name = main_wh.get("name") or to_wh_name
    return {
        "tr_type": str(row.get("TR_TYPE") or "").strip(),
        "tr_no": str(row.get("TR_NO") or "").strip(),
        "tr_ser": str(row.get("TR_SER") or "").strip(),
        "date_label": str(row.get("TR_WHEN_LABEL") or "").strip()
        or _dt_label(row.get("TR_WHEN")),
        "branch_code": branch,
        "branch_name": names.get(branch) or branch,
        "to_wh_code": to_wh_code,
        "to_wh_name": to_wh_name,
        "to_wh_label": _wh_display(to_wh_code, to_wh_name),
        "from_wh_code": from_wh_code,
        "from_wh_name": from_wh_name,
        "from_wh_label": _wh_display(from_wh_code, from_wh_name),
        "main_wh_code": main_wh_code,
        "main_wh_name": main_wh_name,
        "main_wh_label": _wh_display(main_wh_code, main_wh_name),
        "dest_differs": bool(
            main_wh_code and to_wh_code and main_wh_code != to_wh_code
        ),
        "user_code": str(row.get("USER_CODE") or "").strip(),
        "user_name": str(row.get("USER_NAME") or "").strip(),
        "processed": processed,
        "approved": approved,
        "status": "مرحّل" if processed else ("معتمد" if approved else "قيد الطلب"),
        "status_kind": "posted" if processed else ("approved" if approved else "open"),
    }


def _fetch_request_items(tr_ser: str) -> list[dict]:
    schema = _schema()
    rows = _fetch_all(
        f"""
        SELECT /*+ INDEX(d INDX_SER_OUTREQ_DTL) */
               d.I_CODE AS ITEM_CODE,
               MAX(NVL(NULLIF(TRIM(i.I_NAME), ''), d.I_CODE)) AS ITEM_NAME,
               NVL(NULLIF(TRIM(d.ITM_UNT), ''), '—') AS ITEM_UNIT,
               ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS REQ_QTY
        FROM {schema}.IAS_OUT_REQUEST_DTL d
        LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE d.OUT_REQ_SER = :tr_ser
        GROUP BY d.I_CODE, NVL(NULLIF(TRIM(d.ITM_UNT), ''), '—')
        HAVING ROUND(SUM(NVL(d.I_QTY, 0)), 2) <> 0
        ORDER BY MAX(NVL(NULLIF(TRIM(i.I_NAME), ''), d.I_CODE)),
                 NVL(NULLIF(TRIM(d.ITM_UNT), ''), '—')
        """,
        {"tr_ser": _bind_num(tr_ser)},
    )
    return [
        {
            "code": str(row.get("ITEM_CODE") or "").strip(),
            "name": str(row.get("ITEM_NAME") or "").strip()
            or str(row.get("ITEM_CODE") or "").strip(),
            "unit": str(row.get("ITEM_UNIT") or "").strip() or "—",
            "req_qty": float(row.get("REQ_QTY") or 0),
            "req_display": _qty(row.get("REQ_QTY") or 0),
        }
        for row in rows
        if str(row.get("ITEM_CODE") or "").strip()
    ]


def _cell_or_empty(cell: dict | None) -> dict:
    if not cell:
        return {
            "qty": 0.0,
            "qty_display": "—",
            "pending_qty": 0.0,
            "pending_display": "",
            "expected_qty": 0.0,
            "expected_display": "—",
            "has_qty": False,
        }
    return {
        **cell,
        "has_qty": float(cell.get("qty") or 0) > 0
        or float(cell.get("expected_qty") or 0) != 0,
    }


def build_transfer_request_compare(
    *,
    tr_type: str,
    tr_no: str,
    tr_ser: str,
) -> dict[str, Any] | None:
    """
    مقارنة أصناف طلب التحويل مع:
    - رصيد المخزن المطلوب (F_W_CODE) بعد خصم غير المرحّل
    - رصيد المخزن الرئيسي للفرع بعد الترحيل
    """
    if not oracle_enabled():
        return None
    tr_type = str(tr_type or "").strip()
    tr_no = str(tr_no or "").strip()
    tr_ser = str(tr_ser or "").strip()
    if not (tr_type and tr_no and tr_ser):
        return None

    header = _fetch_request_header(tr_type, tr_no, tr_ser)
    if not header:
        return None

    from_wh = header.get("from_wh_code") or ""
    main_wh = header.get("main_wh_code") or header.get("to_wh_code") or ""
    to_wh = header.get("to_wh_code") or ""
    wh_codes: list[str] = []
    for code in (from_wh, main_wh, to_wh):
        if code and code not in wh_codes:
            wh_codes.append(code)

    items = _fetch_request_items(tr_ser)
    item_codes = [row["code"] for row in items]
    stock_map = _fetch_stock_for_items(item_codes, warehouse_codes=wh_codes or None)
    packs_map = _fetch_unit_packs_map(item_codes)

    compare_items: list[dict] = []
    source_ok = 0
    source_short = 0
    for item in items:
        packs = packs_map.get(item["code"]) or {}
        for row in stock_map.get(item["code"]) or []:
            unit = str(row.get("unit") or "").strip()
            psz = row.get("p_size")
            if unit and psz and float(psz) > 0 and unit not in packs:
                packs[unit] = float(psz)
        stock_by_wh = _stock_cells_for_unit(
            stock_map.get(item["code"]) or [],
            req_unit=item.get("unit") or "—",
            packs=packs,
        )
        source = _cell_or_empty(_wh_cell(stock_by_wh, from_wh))
        dest = _cell_or_empty(_wh_cell(stock_by_wh, main_wh))
        req_qty = float(item["req_qty"] or 0)
        source_expected = float(source.get("expected_qty") or 0)
        can_cover = source_expected + 1e-9 >= req_qty
        if can_cover:
            source_ok += 1
        else:
            source_short += 1
        compare_items.append(
            {
                **item,
                "source": source,
                "dest": dest,
                "can_cover": can_cover,
                "gap_qty": round(req_qty - source_expected, 4),
                "gap_display": _qty(max(req_qty - source_expected, 0)),
            }
        )

    return {
        "header": header,
        "items": compare_items,
        "item_count": len(compare_items),
        "source_ok_count": source_ok,
        "source_short_count": source_short,
    }
