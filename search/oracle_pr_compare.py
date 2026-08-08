"""مقارنة طلبات الشراء مع أرصدة المخازن — قراءة فقط من أوراكل."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .oracle_stock import (
    _branch_names,
    _fetch_all,
    _fmt_inv_qty,
    _schema,
    expected_stock_qty,
    fetch_pending_sales_qty_map,
    fetch_warehouse_options,
    oracle_enabled,
)


def _qty(value: Any) -> str:
    return _fmt_inv_qty(float(value or 0))


def _unit_key(unit: str) -> str:
    text = str(unit or "").strip()
    for ch in ("\u200e", "\u200f", "\u0640", " ", "\u00a0", "\t"):
        text = text.replace(ch, "")
    return text.casefold()


def _pack_for(packs: dict[str, float], unit: str) -> float | None:
    unit = str(unit or "").strip()
    if not unit or not packs:
        return None
    if unit in packs:
        return float(packs[unit])
    key = _unit_key(unit)
    for name, psz in packs.items():
        if _unit_key(name) == key:
            return float(psz)
    return None


def _convert_qty(
    qty: float,
    from_unit: str,
    to_unit: str,
    packs: dict[str, float],
    *,
    from_pack: float | None = None,
    to_pack: float | None = None,
) -> float | None:
    """حوّل كمية بين وحدتين عبر P_SIZE (كمية الأساس = qty × pack)."""
    qty = float(qty or 0)
    if _unit_key(from_unit) == _unit_key(to_unit):
        return round(qty, 4)
    pf = from_pack if from_pack and from_pack > 0 else _pack_for(packs, from_unit)
    pt = to_pack if to_pack and to_pack > 0 else _pack_for(packs, to_unit)
    if not pf or not pt or pf <= 0 or pt <= 0:
        return None
    return round(qty * pf / pt, 4)


def _fetch_unit_packs_map(item_codes: list[str]) -> dict[str, dict[str, float]]:
    """{item_code: {unit_name: P_SIZE}} من IAS_ITM_DTL."""
    codes = [str(c).strip() for c in item_codes if str(c).strip()]
    if not codes:
        return {}
    schema = _schema()
    params: dict[str, Any] = {}
    keys = []
    for i, code in enumerate(codes):
        key = f"c{i}"
        keys.append(f":{key}")
        params[key] = code
    try:
        rows = _fetch_all(
            f"""
            SELECT TO_CHAR(I_CODE) AS ITEM_CODE,
                   ITM_UNT AS ITEM_UNIT,
                   P_SIZE
            FROM {schema}.IAS_ITM_DTL
            WHERE TO_CHAR(I_CODE) IN ({', '.join(keys)})
              AND NVL(P_SIZE, 0) > 0
            """,
            params,
        )
    except Exception:
        return {code: {} for code in codes}

    out: dict[str, dict[str, float]] = {code: {} for code in codes}
    for row in rows:
        code = str(row.get("ITEM_CODE") or "").strip()
        unit = str(row.get("ITEM_UNIT") or "").strip()
        try:
            psz = float(row.get("P_SIZE") or 0)
        except (TypeError, ValueError):
            psz = 0.0
        if not code or not unit or psz <= 0:
            continue
        out.setdefault(code, {})[unit] = psz
    return out


def _today_bounds(day: date | None = None) -> tuple[date, date]:
    d = day or date.today()
    return d, d + timedelta(days=1)


def _dt_label(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    return text


def _norm_code(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].replace("-", "", 1).isdigit():
        text = text[:-2]
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        try:
            return str(int(text))
        except ValueError:
            return text
    return text


def fetch_warehouses_for_branch(branch_code: str = "") -> list[dict]:
    """مخازن الفرع المحدد فقط — بدون فرع لا تُرجع شيئاً."""
    raw = str(branch_code or "").strip()
    brn = _norm_code(raw)
    if not (raw or brn) or not oracle_enabled():
        return []

    schema = _schema()
    names = _branch_names()
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(w.W_CODE) AS W_CODE,
               w.W_NAME,
               w.W_E_NAME,
               TO_CHAR(w.CONN_BRN_NO) AS BRANCH_CODE
        FROM {schema}.WAREHOUSE_DETAILS w
        WHERE w.W_CODE IS NOT NULL
          AND NVL(w.INACTIVE, 0) = 0
          AND (
            TO_CHAR(w.CONN_BRN_NO) = :branch
            OR LTRIM(REGEXP_REPLACE(TO_CHAR(w.CONN_BRN_NO), '[^0-9]', ''), '0') = :branch_norm
            OR LTRIM(REGEXP_REPLACE(TO_CHAR(w.CONN_BRN_NO), '[^0-9]', ''), '0') =
               LTRIM(REGEXP_REPLACE(:branch, '[^0-9]', ''), '0')
          )
        ORDER BY w.W_NAME, w.W_CODE
        """,
        {"branch": raw or brn, "branch_norm": brn or raw},
    )
    # احتياطي إن كان ترميز الفرع بأصفار بادئة أو بصيغة رقمية مختلفة
    if not rows:
        all_rows = fetch_warehouse_options(active_only=True)
        return [
            row
            for row in all_rows
            if _norm_code(row.get("branch_code")) == brn
            or str(row.get("branch_code") or "").strip() == raw
        ]

    out: list[dict] = []
    for row in rows:
        code = str(row.get("W_CODE") or "").strip()
        if not code:
            continue
        branch = str(row.get("BRANCH_CODE") or "").strip()
        name = str(row.get("W_NAME") or row.get("W_E_NAME") or "").strip() or code
        out.append(
            {
                "code": code,
                "name": name,
                "branch_code": branch,
                "branch_name": names.get(branch)
                or names.get(brn)
                or names.get(_norm_code(branch))
                or branch
                or "—",
            }
        )
    return out


def fetch_today_purchase_requests(
    *,
    branch_code: str,
    day: date | None = None,
    warehouse_code: str = "",
) -> list[dict]:
    """طلبات شراء اليوم لفرع محدد من P_REQUEST (اختياريًا حسب مخزن المقصد)."""
    if not oracle_enabled():
        return []
    brn = str(branch_code or "").strip()
    if not brn:
        return []
    brn_norm = _norm_code(brn)
    wh = str(warehouse_code or "").strip()

    schema = _schema()
    d_from, d_to_excl = _today_bounds(day)
    names = _branch_names()
    wh_filter = ""
    params: dict[str, Any] = {
        "d_from": d_from,
        "d_to_excl": d_to_excl,
        "branch": brn,
        "branch_norm": brn_norm or brn,
    }
    if wh:
        wh_filter = "AND TO_CHAR(p.W_CODE) = :wh"
        params["wh"] = wh
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(p.PR_TYPE) AS PR_TYPE,
               TO_CHAR(p.PR_NO) AS PR_NO,
               TO_CHAR(p.PR_SER) AS PR_SER,
               NVL(p.AD_DATE, p.PR_DATE) AS PR_WHEN,
               TO_CHAR(NVL(p.AD_DATE, p.PR_DATE), 'YYYY-MM-DD HH24:MI') AS PR_WHEN_LABEL,
               TO_CHAR(p.BRN_NO) AS BRANCH_CODE,
               TO_CHAR(p.W_CODE) AS WAREHOUSE_CODE,
               TO_CHAR(p.V_CODE) AS VENDOR_CODE,
               MAX(
                 NVL(NULLIF(TRIM(vd.V_A_NAME), ''), TO_CHAR(p.V_CODE))
               ) AS VENDOR_NAME,
               TO_CHAR(p.AD_U_ID) AS USER_CODE,
               MAX(NVL(u.U_A_NAME, NVL(u.U_E_NAME, TO_CHAR(p.AD_U_ID)))) AS USER_NAME,
               COUNT(DISTINCT TO_CHAR(d.I_CODE)) AS ITEM_COUNT,
               ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL
        FROM {schema}.P_REQUEST p
        JOIN {schema}.P_REQUEST_DETAIL d
          ON d.PR_TYPE = p.PR_TYPE
         AND d.PR_NO = p.PR_NO
         AND d.PR_SER = p.PR_SER
        LEFT JOIN {schema}.USER_R u ON u.U_ID = p.AD_U_ID
        LEFT JOIN {schema}.V_DETAILS vd
          ON TO_CHAR(vd.V_CODE) = TO_CHAR(p.V_CODE)
        WHERE p.PR_DATE >= :d_from
          AND p.PR_DATE < :d_to_excl
          AND NVL(p.INACTIVE, 0) = 0
          AND (
            TO_CHAR(p.BRN_NO) = :branch
            OR LTRIM(REGEXP_REPLACE(TO_CHAR(p.BRN_NO), '[^0-9]', ''), '0') = :branch_norm
            OR LTRIM(REGEXP_REPLACE(TO_CHAR(p.BRN_NO), '[^0-9]', ''), '0') =
               LTRIM(REGEXP_REPLACE(:branch, '[^0-9]', ''), '0')
          )
          {wh_filter}
        GROUP BY TO_CHAR(p.PR_TYPE),
                 TO_CHAR(p.PR_NO),
                 TO_CHAR(p.PR_SER),
                 NVL(p.AD_DATE, p.PR_DATE),
                 TO_CHAR(p.BRN_NO),
                 TO_CHAR(p.W_CODE),
                 TO_CHAR(p.V_CODE),
                 TO_CHAR(p.AD_U_ID)
        ORDER BY NVL(p.AD_DATE, p.PR_DATE) DESC, TO_CHAR(p.PR_SER) DESC
        """,
        params,
    )

    out: list[dict] = []
    for row in rows:
        branch = str(row.get("BRANCH_CODE") or "").strip()
        date_label = str(row.get("PR_WHEN_LABEL") or "").strip() or _dt_label(
            row.get("PR_WHEN")
        )
        out.append(
            {
                "pr_type": str(row.get("PR_TYPE") or "").strip(),
                "pr_no": str(row.get("PR_NO") or "").strip(),
                "pr_ser": str(row.get("PR_SER") or "").strip(),
                "date_label": date_label,
                "branch_code": branch,
                "branch_name": names.get(branch) or branch,
                "warehouse_code": str(row.get("WAREHOUSE_CODE") or "").strip(),
                "vendor_code": str(row.get("VENDOR_CODE") or "").strip(),
                "vendor_name": str(row.get("VENDOR_NAME") or "").strip()
                or str(row.get("VENDOR_CODE") or "").strip()
                or "—",
                "user_code": str(row.get("USER_CODE") or "").strip(),
                "user_name": str(row.get("USER_NAME") or "").strip(),
                "item_count": int(row.get("ITEM_COUNT") or 0),
                "qty_total": float(row.get("QTY_TOTAL") or 0),
                "qty_display": _qty(row.get("QTY_TOTAL") or 0),
            }
        )
    return out


def _fetch_request_header(pr_type: str, pr_no: str, pr_ser: str) -> dict | None:
    schema = _schema()
    names = _branch_names()
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(p.PR_TYPE) AS PR_TYPE,
               TO_CHAR(p.PR_NO) AS PR_NO,
               TO_CHAR(p.PR_SER) AS PR_SER,
               NVL(p.AD_DATE, p.PR_DATE) AS PR_WHEN,
               TO_CHAR(NVL(p.AD_DATE, p.PR_DATE), 'YYYY-MM-DD HH24:MI') AS PR_WHEN_LABEL,
               TO_CHAR(p.BRN_NO) AS BRANCH_CODE,
               TO_CHAR(p.V_CODE) AS VENDOR_CODE,
               NVL(NULLIF(TRIM(vd.V_A_NAME), ''), TO_CHAR(p.V_CODE)) AS VENDOR_NAME,
               TO_CHAR(p.AD_U_ID) AS USER_CODE,
               NVL(u.U_A_NAME, NVL(u.U_E_NAME, TO_CHAR(p.AD_U_ID))) AS USER_NAME
        FROM {schema}.P_REQUEST p
        LEFT JOIN {schema}.USER_R u ON u.U_ID = p.AD_U_ID
        LEFT JOIN {schema}.V_DETAILS vd
          ON TO_CHAR(vd.V_CODE) = TO_CHAR(p.V_CODE)
        WHERE TO_CHAR(p.PR_TYPE) = :pr_type
          AND TO_CHAR(p.PR_NO) = :pr_no
          AND TO_CHAR(p.PR_SER) = :pr_ser
          AND NVL(p.INACTIVE, 0) = 0
        FETCH FIRST 1 ROWS ONLY
        """,
        {"pr_type": pr_type, "pr_no": pr_no, "pr_ser": pr_ser},
    )
    if not rows:
        return None
    row = rows[0]
    branch = str(row.get("BRANCH_CODE") or "").strip()
    date_label = str(row.get("PR_WHEN_LABEL") or "").strip() or _dt_label(
        row.get("PR_WHEN")
    )
    return {
        "pr_type": str(row.get("PR_TYPE") or "").strip(),
        "pr_no": str(row.get("PR_NO") or "").strip(),
        "pr_ser": str(row.get("PR_SER") or "").strip(),
        "date_label": date_label,
        "branch_code": branch,
        "branch_name": names.get(branch) or branch,
        "vendor_code": str(row.get("VENDOR_CODE") or "").strip(),
        "vendor_name": str(row.get("VENDOR_NAME") or "").strip()
        or str(row.get("VENDOR_CODE") or "").strip()
        or "—",
        "user_code": str(row.get("USER_CODE") or "").strip(),
        "user_name": str(row.get("USER_NAME") or "").strip(),
    }


def _fetch_request_items(pr_type: str, pr_no: str, pr_ser: str) -> list[dict]:
    schema = _schema()
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(d.I_CODE) AS ITEM_CODE,
               MAX(NVL(NULLIF(TRIM(i.I_NAME), ''), TO_CHAR(d.I_CODE))) AS ITEM_NAME,
               NVL(NULLIF(TRIM(TO_CHAR(d.ITM_UNT)), ''), '—') AS ITEM_UNIT,
               ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS REQ_QTY
        FROM {schema}.P_REQUEST_DETAIL d
        LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE TO_CHAR(d.PR_TYPE) = :pr_type
          AND TO_CHAR(d.PR_NO) = :pr_no
          AND TO_CHAR(d.PR_SER) = :pr_ser
        GROUP BY TO_CHAR(d.I_CODE), NVL(NULLIF(TRIM(TO_CHAR(d.ITM_UNT)), ''), '—')
        HAVING ROUND(SUM(NVL(d.I_QTY, 0)), 2) <> 0
        ORDER BY MAX(NVL(NULLIF(TRIM(i.I_NAME), ''), TO_CHAR(d.I_CODE))),
                 NVL(NULLIF(TRIM(TO_CHAR(d.ITM_UNT)), ''), '—')
        """,
        {"pr_type": pr_type, "pr_no": pr_no, "pr_ser": pr_ser},
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


def _fetch_stock_for_items(
    item_codes: list[str],
    *,
    warehouse_codes: list[str] | None = None,
    branch_code: str = "",
) -> dict[str, list[dict]]:
    """
    أرصدة موجبة لكل صنف×مخزن×وحدة تخزين (IAS_ITM_WCODE).
    لا يخلط وحدات مختلفة في رقم واحد.
    """
    codes = [str(c).strip() for c in item_codes if str(c).strip()]
    if not codes:
        return {}

    schema = _schema()
    params: dict[str, Any] = {}
    code_keys = []
    for i, code in enumerate(codes):
        key = f"c{i}"
        code_keys.append(f":{key}")
        params[key] = code

    scope_filters: list[str] = []
    wh_list = [str(w).strip() for w in (warehouse_codes or []) if str(w).strip()]
    brn = _norm_code(branch_code)
    if not wh_list and brn:
        wh_list = [
            str(row.get("code") or "").strip()
            for row in fetch_warehouses_for_branch(brn)
            if str(row.get("code") or "").strip()
        ]
        # إن لم تُربط مخازن بالفرع، لا تُرجع أرصدة من كل الشركة
        if not wh_list:
            return {code: [] for code in codes}
    if wh_list:
        wh_keys = []
        for i, wh in enumerate(wh_list):
            key = f"w{i}"
            wh_keys.append(f":{key}")
            params[key] = wh
        scope_filters.append(f"TO_CHAR(w.W_CODE) IN ({', '.join(wh_keys)})")

    scope_sql = f"AND {' AND '.join(scope_filters)}" if scope_filters else ""

    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(w.I_CODE) AS ITEM_CODE,
               TO_CHAR(w.W_CODE) AS WAREHOUSE_CODE,
               MAX(NVL(wh.W_NAME, TO_CHAR(w.W_CODE))) AS WAREHOUSE_NAME,
               TO_CHAR(MAX(wh.CONN_BRN_NO)) AS BRANCH_CODE,
               NVL(NULLIF(TRIM(TO_CHAR(w.ITM_UNT)), ''), '—') AS ITEM_UNIT,
               ROUND(MAX(NVL(w.P_SIZE, 0)), 4) AS P_SIZE,
               ROUND(SUM(NVL(w.AVL_QTY, 0)), 2) AS QTY
        FROM {schema}.IAS_ITM_WCODE w
        LEFT JOIN {schema}.WAREHOUSE_DETAILS wh
          ON TO_CHAR(wh.W_CODE) = TO_CHAR(w.W_CODE)
        WHERE TO_CHAR(w.I_CODE) IN ({', '.join(code_keys)})
          AND NVL(w.AVL_QTY, 0) > 0
          {scope_sql}
        GROUP BY TO_CHAR(w.I_CODE),
                 TO_CHAR(w.W_CODE),
                 NVL(NULLIF(TRIM(TO_CHAR(w.ITM_UNT)), ''), '—')
        HAVING ROUND(SUM(NVL(w.AVL_QTY, 0)), 2) > 0
        ORDER BY TO_CHAR(w.I_CODE), SUM(NVL(w.AVL_QTY, 0)) DESC
        """,
        params,
    )

    names = _branch_names()
    by_item: dict[str, list[dict]] = {code: [] for code in codes}
    for row in rows:
        code = str(row.get("ITEM_CODE") or "").strip()
        wh = str(row.get("WAREHOUSE_CODE") or "").strip()
        if not code or not wh:
            continue
        try:
            p_size = float(row.get("P_SIZE") or 0)
        except (TypeError, ValueError):
            p_size = 0.0
        qty = float(row.get("QTY") or 0)
        branch = str(row.get("BRANCH_CODE") or "").strip()
        by_item.setdefault(code, []).append(
            {
                "warehouse_code": wh,
                "warehouse_name": str(row.get("WAREHOUSE_NAME") or "").strip() or wh,
                "branch_code": branch,
                "branch_name": names.get(branch) or branch or "—",
                "unit": str(row.get("ITEM_UNIT") or "").strip() or "—",
                "p_size": p_size if p_size > 0 else None,
                "qty": qty,
                "qty_display": _qty(qty),
            }
        )

    # المبيعات غير المرحلة بوحدة المخزون الأساسية — تُحوَّل لاحقاً لوحدة الطلب
    pending_map = fetch_pending_sales_qty_map(codes, warehouse_codes=wh_list or None)
    for code, stock_rows in by_item.items():
        pending_by_wh = pending_map.get(code) or {}
        for row in stock_rows:
            wh = row["warehouse_code"]
            row["pending_base"] = float(pending_by_wh.get(wh) or 0)
    return by_item


def _stock_cells_for_unit(
    stock_rows: list[dict],
    *,
    req_unit: str,
    packs: dict[str, float],
) -> dict[str, dict]:
    """
    أرصدة المخازن بوحدة الطلب: نفس الوحدة إن وُجدت، وإلا تحويل عبر P_SIZE.
    المعلق (P_QTY) يُحوَّل من وحدة الأساس إلى وحدة الطلب.
    """
    req_unit = str(req_unit or "").strip() or "—"
    req_key = _unit_key(req_unit)
    req_pack = _pack_for(packs, req_unit)

    # أفضل مصدر لكل مخزن: تطابق الوحدة أولاً، وإلا أصغر عبوة قابلة للتحويل
    best_by_wh: dict[str, dict] = {}
    for row in stock_rows:
        wh = row["warehouse_code"]
        src_unit = str(row.get("unit") or "").strip() or "—"
        src_pack = row.get("p_size") or _pack_for(packs, src_unit)
        exact = _unit_key(src_unit) == req_key
        if exact:
            qty = float(row["qty"] or 0)
        else:
            converted = _convert_qty(
                float(row["qty"] or 0),
                src_unit,
                req_unit,
                packs,
                from_pack=float(src_pack) if src_pack else None,
                to_pack=float(req_pack) if req_pack else None,
            )
            if converted is None:
                continue
            qty = converted

        pending_base = float(row.get("pending_base") or 0)
        # pending بوحدة الأساس ≈ أصغر P_SIZE؛ حوّلها لوحدة الطلب
        base_pack = None
        if packs:
            base_pack = min(packs.values()) if packs else None
        pending_qty = 0.0
        if pending_base > 0:
            if req_pack and req_pack > 0:
                # الأساس غالباً pack=1 أو أصغر عبوة
                if base_pack and base_pack > 0:
                    pending_qty = round(pending_base * base_pack / req_pack, 4)
                else:
                    pending_qty = round(pending_base / req_pack, 4)
            elif exact and src_pack and src_pack > 0:
                pending_qty = round(pending_base / float(src_pack), 4)
            elif exact:
                pending_qty = pending_base

        expected = expected_stock_qty(qty, pending_qty)
        candidate = {
            "warehouse_code": wh,
            "warehouse_name": row["warehouse_name"],
            "branch_code": row.get("branch_code") or "",
            "branch_name": row.get("branch_name") or "—",
            "unit": req_unit,
            "qty": qty,
            "qty_display": _qty(qty),
            "pending_qty": pending_qty,
            "pending_display": _qty(pending_qty) if pending_qty else "",
            "expected_qty": expected,
            "expected_display": _qty(expected),
            "expected_low": expected < float(qty or 0),
            "expected_neg": False,
            "_exact": exact,
            "_src_pack": float(src_pack) if src_pack else 0.0,
        }
        prev = best_by_wh.get(wh)
        if prev is None:
            best_by_wh[wh] = candidate
            continue
        # فضّل التطابق المباشر، ثم الكمية الأكبر بعد التحويل
        if candidate["_exact"] and not prev["_exact"]:
            best_by_wh[wh] = candidate
        elif candidate["_exact"] == prev["_exact"] and candidate["qty"] > prev["qty"]:
            best_by_wh[wh] = candidate

    out: dict[str, dict] = {}
    for wh, cell in best_by_wh.items():
        cell.pop("_exact", None)
        cell.pop("_src_pack", None)
        out[wh] = cell
    return out


def build_purchase_request_compare(
    *,
    pr_type: str,
    pr_no: str,
    pr_ser: str,
    warehouse_codes: list[str] | None = None,
    branch_code: str = "",
) -> dict[str, Any] | None:
    """
    مقارنة أصناف طلب شراء مع المخازن ذات الرصيد فقط.
    الكمية تُعرض بنفس وحدة الطلب (حبة/كرتون…).
    يقيّد بالمخزن المختار أو بمخازن الفرع المحدد.
    """
    if not oracle_enabled():
        return None
    pr_type = str(pr_type or "").strip()
    pr_no = str(pr_no or "").strip()
    pr_ser = str(pr_ser or "").strip()
    if not (pr_type and pr_no and pr_ser):
        return None

    header = _fetch_request_header(pr_type, pr_no, pr_ser)
    if not header:
        return None

    items = _fetch_request_items(pr_type, pr_no, pr_ser)
    scope_branch = _norm_code(branch_code) or _norm_code(header.get("branch_code"))
    item_codes = [row["code"] for row in items]
    stock_map = _fetch_stock_for_items(
        item_codes,
        warehouse_codes=warehouse_codes,
        branch_code="" if warehouse_codes else scope_branch,
    )
    packs_map = _fetch_unit_packs_map(item_codes)

    compare_items: list[dict] = []
    warehouse_map: dict[str, dict] = {}
    for item in items:
        packs = packs_map.get(item["code"]) or {}
        # أكمل P_SIZE من صفوف المخزون إن نقص تعريف الوحدة في التفاصيل
        for row in stock_map.get(item["code"]) or []:
            u = str(row.get("unit") or "").strip()
            psz = row.get("p_size")
            if u and psz and float(psz) > 0 and u not in packs:
                packs[u] = float(psz)
        stock_by_wh = _stock_cells_for_unit(
            stock_map.get(item["code"]) or [],
            req_unit=item.get("unit") or "—",
            packs=packs,
        )
        stock_rows = list(stock_by_wh.values())
        for wh_code, cell in stock_by_wh.items():
            if wh_code not in warehouse_map:
                warehouse_map[wh_code] = {
                    "code": wh_code,
                    "name": cell["warehouse_name"],
                    "branch_name": cell["branch_name"],
                }
        expected_total = round(
            sum(float(r.get("expected_qty", r["qty"]) or 0) for r in stock_rows), 2
        )
        compare_items.append(
            {
                **item,
                "stock_count": len(stock_rows),
                "has_stock": bool(stock_rows),
                "stock_total": round(sum(float(r["qty"]) for r in stock_rows), 2),
                "stock_total_display": _qty(
                    sum(float(r["qty"]) for r in stock_rows)
                ),
                "expected_total": expected_total,
                "expected_total_display": _qty(expected_total),
                "stock_by_wh": stock_by_wh,
            }
        )

    warehouses = sorted(
        warehouse_map.values(),
        key=lambda row: (row["branch_name"], row["name"], row["code"]),
    )
    for item in compare_items:
        item["cells"] = [
            item["stock_by_wh"].get(wh["code"]) for wh in warehouses
        ]

    return {
        "header": header,
        "items": compare_items,
        "warehouses": warehouses,
        "item_count": len(compare_items),
        "warehouse_count": len(warehouses),
        "with_stock_count": sum(1 for row in compare_items if row["has_stock"]),
        "without_stock_count": sum(1 for row in compare_items if not row["has_stock"]),
    }
