"""أصناف مسعّرة بنسبة ربح أقل من حد معيّن (افتراضي 15%).

كما في شاشة أسعار أونكس (مثال 06200 / مخزن 60 → 73.91%):
  متوسط التكلفة = I_CWTAVG من IAS_ITM_WCODE لمخزن صف السعر
                  (وحدة الحد الأدنى) — حتى لو الكمية = 0
                  وإن عُدم يُستخدم متوسط بطاقة الصنف.
  صافي السعر = I_PRICE / (1 + VAT/100)
  نسبة الربح = (صافي السعر − المتوسط × P_SIZE) / (المتوسط × P_SIZE) × 100
  ربح أونكس = نفس الصيغة على PRIMARY_COST × P_SIZE (شاشة التسعير)

وحدة العرض: صف واحد لكل صنف/مخزن/مستوى — الوحدة الرئيسية (MAIN_UNIT)
  أو وحدة البيع (SALE_UNIT) أو وحدة المخزون، ثم أصغر P_SIZE.
يُشترط رصيد كمية > 0 على الوحدة الرئيسية في نفس المخزن،
وحركة مخزون حقيقية (شراء / تحويل / تسوية كمية).
يُستثنى الصنف الذي رصيده من افتتاحي فقط بلا تلك الحركات.
"""

from __future__ import annotations

from typing import Any

from django.core.cache import cache

from .oracle_stock import (
    OracleStockError,
    _bind_gcode,
    _fetch_all,
    _schema,
    oracle_enabled,
)

_CACHE_TTL = 600
_MOVED_CODES_TTL = 7200
_CACHE_VER = "v25"
_DEFAULT_VAT_PCT = 15.0
_PAGE_SIZE = 200
_EXCEL_LIMIT = 100000
_SINGLE_WH_LIMIT = 500000
_SINGLE_WH_CAPS = (200, 500, 2000, 5000, 10000, 50000, 100000, 500000)


def _item_codes_with_stock_movement() -> frozenset[str]:
    """أكواد أصناف لها حركة مخزون: شراء أو تحويل أو تسوية كمية.

    لا يشمل افتتاح المخزون ولا مبيعات POS — لاستثناء «افتتاحي فقط».
    """
    ck = f"purch:stock_moved_codes:{_CACHE_VER}"
    hit = cache.get(ck)
    if hit is not None:
        return frozenset(hit)

    schema = _schema()
    rows = _fetch_all(
        f"""
        SELECT I_CODE FROM (
          SELECT DISTINCT I_CODE FROM {schema}.IAS_PI_BILL_DTL
          UNION
          SELECT DISTINCT I_CODE FROM {schema}.IAS_WHTRNS_DTL
          UNION
          SELECT DISTINCT d.I_CODE
            FROM {schema}.STK_ADJUSTMENT_DET d
            JOIN {schema}.STK_ADJUSTMENT m ON m.DOC_SER = d.DOC_SER
           WHERE m.ADJUST_TYPE = 1
        )
        """,
        {},
    )
    codes = [
        str(r.get("I_CODE") or "").strip()
        for r in rows
        if str(r.get("I_CODE") or "").strip()
    ]
    cache.set(ck, codes, _MOVED_CODES_TTL)
    return frozenset(codes)


def _keep_moved_stock_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """يستبعد أصناف بلا حركة مخزون (افتتاحي فقط ونحوها)."""
    if not rows:
        return rows
    moved = _item_codes_with_stock_movement()
    return [r for r in rows if str(r.get("item_code") or "").strip() in moved]


def _f(value: Any, nd: int = 2) -> float:
    try:
        return round(float(value or 0), nd)
    except (TypeError, ValueError):
        return 0.0


def _parse_wh_codes(raw: str) -> list[str]:
    text = str(raw or "").replace("،", ",").replace("-", ",")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _resolved_profit_bounds(
    min_profit_pct: float | None,
    max_profit_pct: float,
    *,
    include_negative: bool,
) -> tuple[float, float]:
    """يحوّل «من/إلى» إلى حدود SQL — من شامل، إلى غير شامل (<)."""
    max_p = float(max_profit_pct)
    if min_profit_pct is not None:
        min_p = float(min_profit_pct)
    elif include_negative:
        min_p = -999999.0
    else:
        min_p = -999999.0
    return min_p, max_p


def _filter_key(
    *,
    min_prft: float,
    max_prft: float,
    lev: int,
    wh_list: list[str],
    q: str,
    include_negative: bool,
    branch_code: str = "",
    group_code: str = "",
) -> str:
    return (
        f"purch:low_margin:{_CACHE_VER}:{min_prft}:{max_prft}:{lev}:{branch_code}:"
        f"{group_code}:{','.join(wh_list)}:{q}:{int(include_negative)}"
    )


def _build_sql_parts(
    schema: str,
    *,
    wh_list: list[str],
    q: str,
    include_negative: bool,
    params: dict[str, Any],
    branch_code: str = "",
    group_code: str = "",
) -> tuple[str, str, str, str, str, str]:
    wh_sql = ""
    if wh_list:
        keys: list[str] = []
        for i, wh in enumerate(wh_list):
            key = f"w{i}"
            keys.append(f":{key}")
            try:
                params[key] = int(wh)
            except ValueError:
                params[key] = wh
        wh_sql = f"AND p.W_CODE IN ({', '.join(keys)})"
    elif str(branch_code or "").strip():
        brn = str(branch_code).strip()
        try:
            params["brn"] = int(brn)
        except ValueError:
            params["brn"] = brn
        wh_sql = f"""
            AND EXISTS (
              SELECT 1
              FROM {schema}.WAREHOUSE_DETAILS wd
              WHERE wd.W_CODE = p.W_CODE
                AND wd.CONN_BRN_NO = :brn
            )
        """

    item_sql = ""
    gcode = str(group_code or "").strip()
    if gcode:
        params["gcode"] = _bind_gcode(gcode)
        item_sql += "AND m.G_CODE = :gcode\n"
    if q:
        params["iq"] = f"%{q}%"
        params["iq_exact"] = q
        item_sql += """
            AND (
              p.I_CODE = :iq_exact
              OR UPPER(m.I_NAME) LIKE UPPER(:iq)
            )
        """

    pos_only_sql = "AND x.PRFT_PCT > 0" if not include_negative else ""

    avg_sql = """
        CASE
          WHEN NVL(w.I_CWTAVG, 0) > 0 THEN w.I_CWTAVG
          ELSE m.I_CWTAVG
        END
    """
    vat_sql = """
        CASE
          WHEN NVL(m.VAT_PER, 0) > 0 THEN m.VAT_PER
          WHEN NVL(m.VAT_TYPE, 0) = 1 THEN :dflt_vat
          ELSE 0
        END
    """
    net_price_sql = f"(p.I_PRICE / (1 + ({vat_sql}) / 100))"
    return wh_sql, item_sql, pos_only_sql, avg_sql, vat_sql, net_price_sql


def _rows_from_oracle(rows: list[dict], lev: int, *, limit_pct: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    lim_pct = _f(limit_pct, 2)
    for r in rows:
        prft = _f(r.get("PRFT_PCT"), 2)
        price = _f(r.get("I_PRICE"), 4)
        net_price = _f(r.get("NET_PRICE"), 4)
        avg_cost = _f(r.get("AVG_COST"), 4)
        unit_cost = _f(r.get("UNIT_COST"), 4)
        min_price = _f(r.get("MIN_ITM_PRICE"), 4)
        min_lim_prft = r.get("MIN_LIMIT_PRFT_PCT")
        calc_lim_price = _f(r.get("LIMIT_PRICE"), 4)
        if min_price > 0 and min_lim_prft is not None:
            limit_price = min_price
            limit_profit_pct = _f(min_lim_prft, 2)
            limit_source = "min"
        else:
            limit_price = calc_lim_price
            limit_profit_pct = lim_pct
            limit_source = "calc"
        onix_prft = _f(r.get("ONIX_PRFT_PCT"), 2)
        primary_unit = _f(r.get("PRIMARY_UNIT_COST"), 4)
        primary_avg = _f(r.get("PRIMARY_COST"), 4)
        out.append(
            {
                "item_code": str(r.get("I_CODE") or "").strip(),
                "item_name": str(r.get("I_NAME") or "").strip(),
                "unit": str(r.get("ITM_UNT") or "").strip(),
                "lev_no": int(r.get("LEV_NO") or lev),
                "lev_name": str(r.get("LEV_A_NAME") or "").strip() or "—",
                "wh_code": str(r.get("W_CODE") or "").strip(),
                "g_code": str(r.get("G_CODE") or "").strip(),
                "g_name": str(r.get("G_NAME") or "").strip() or "—",
                "profit_pct": prft,
                "profit_pct_display": f"{prft:g}",
                "onyx_profit_pct": onix_prft,
                "onyx_profit_pct_display": f"{onix_prft:g}",
                "primary_cost": primary_avg,
                "primary_cost_display": f"{primary_avg:g}" if primary_avg > 0 else "—",
                "primary_unit_cost": primary_unit,
                "primary_unit_cost_display": f"{primary_unit:g}" if primary_unit > 0 else "—",
                "limit_profit_pct": limit_profit_pct,
                "limit_profit_pct_display": f"{limit_profit_pct:g}",
                "limit_price": limit_price,
                "limit_price_display": f"{limit_price:g}" if limit_price > 0 else "—",
                "limit_source": limit_source,
                "currency": str(r.get("A_CY") or "SAR").strip() or "SAR",
                "price": price,
                "price_display": f"{price:g}",
                "net_price": net_price,
                "net_price_display": f"{net_price:g}",
                "avg_cost": avg_cost,
                "avg_cost_display": f"{avg_cost:g}",
                "unit_cost": unit_cost,
                "unit_cost_display": f"{unit_cost:g}",
                "vat_pct": _f(r.get("VAT_PCT"), 2),
                "p_size": _f(r.get("P_SIZE"), 4),
            }
        )
    return out


def _inner_select_sql(
    schema: str,
    *,
    wh_sql: str,
    item_sql: str,
    avg_sql: str,
    vat_sql: str,
    net_price_sql: str,
) -> str:
    """سعر الوحدة الرئيسية/البيع فقط — صف واحد لكل صنف."""
    return f"""
        SELECT /*+ LEADING(p) USE_NL(m d pu w lv g) INDEX(p INV_PRC_LEV_NO_INDX) */
          p.I_CODE,
          m.I_NAME,
          p.ITM_UNT,
          p.LEV_NO,
          lv.LEV_A_NAME,
          p.W_CODE,
          m.G_CODE,
          NVL(NULLIF(TRIM(g.G_A_NAME), ''), NVL(TO_CHAR(m.G_CODE), '—')) AS G_NAME,
          ROUND(
            ({net_price_sql} - ({avg_sql}) * NVL(p.P_SIZE, 1))
            / NULLIF(({avg_sql}) * NVL(p.P_SIZE, 1), 0) * 100
          , 2) AS PRFT_PCT,
          ROUND(
            ({net_price_sql} - (
              CASE
                WHEN NVL(w.PRIMARY_COST, 0) > 0 THEN w.PRIMARY_COST
                ELSE ({avg_sql})
              END
            ) * NVL(p.P_SIZE, 1))
            / NULLIF((
              CASE
                WHEN NVL(w.PRIMARY_COST, 0) > 0 THEN w.PRIMARY_COST
                ELSE ({avg_sql})
              END
            ) * NVL(p.P_SIZE, 1), 0) * 100
          , 2) AS ONIX_PRFT_PCT,
          ROUND(p.I_PRICE, 4) AS I_PRICE,
          ROUND({net_price_sql}, 4) AS NET_PRICE,
          ROUND(({avg_sql}), 4) AS AVG_COST,
          ROUND(({avg_sql}) * NVL(p.P_SIZE, 1), 4) AS UNIT_COST,
          ROUND(
            CASE
              WHEN NVL(w.PRIMARY_COST, 0) > 0 THEN w.PRIMARY_COST
              ELSE ({avg_sql})
            END
          , 4) AS PRIMARY_COST,
          ROUND(
            CASE
              WHEN NVL(w.PRIMARY_COST, 0) > 0 THEN w.PRIMARY_COST
              ELSE ({avg_sql})
            END * NVL(p.P_SIZE, 1)
          , 4) AS PRIMARY_UNIT_COST,
          ROUND(NVL(p.P_SIZE, 1), 4) AS P_SIZE,
          ROUND(({vat_sql}), 2) AS VAT_PCT,
          CAST('SAR' AS VARCHAR2(10)) AS A_CY,
          p.MIN_ITM_PRICE,
          CASE
            WHEN NVL(p.MIN_ITM_PRICE, 0) > 0
             AND NVL(({avg_sql}) * NVL(p.P_SIZE, 1), 0) > 0
            THEN ROUND(
              (p.MIN_ITM_PRICE / (1 + ({vat_sql}) / 100) - ({avg_sql}) * NVL(p.P_SIZE, 1))
              / NULLIF(({avg_sql}) * NVL(p.P_SIZE, 1), 0) * 100
            , 2)
          END AS MIN_LIMIT_PRFT_PCT
        FROM {schema}.IAS_ITEM_PRICE p
        JOIN {schema}.IAS_ITM_MST m
          ON m.I_CODE = p.I_CODE
        JOIN {schema}.IAS_ITM_DTL d
          ON d.I_CODE = p.I_CODE
         AND NVL(d.MAIN_UNIT, 0) = 1
        JOIN (
          SELECT I_CODE, ITM_UNT
          FROM (
            SELECT
              ud.I_CODE,
              ud.ITM_UNT,
              ROW_NUMBER() OVER (
                PARTITION BY ud.I_CODE
                ORDER BY
                  CASE
                    WHEN NVL(ud.MAIN_UNIT, 0) = 1 THEN 0
                    WHEN NVL(ud.SALE_UNIT, 0) = 1 THEN 1
                    WHEN NVL(ud.STOCK_UNIT, 0) = 1 THEN 2
                    ELSE 3
                  END,
                  NVL(ud.P_SIZE, 1) ASC,
                  ud.ITM_UNT
              ) AS RN
            FROM {schema}.IAS_ITM_DTL ud
            WHERE NVL(ud.P_SIZE, 0) > 0
          )
          WHERE RN = 1
        ) pu
          ON pu.I_CODE = p.I_CODE
         AND pu.ITM_UNT = p.ITM_UNT
        JOIN {schema}.IAS_ITM_WCODE w
          ON w.I_CODE = p.I_CODE
         AND w.W_CODE = p.W_CODE
         AND w.ITM_UNT = d.ITM_UNT
         AND w.AVL_QTY > 0
        LEFT JOIN {schema}.IAS_PRICING_LEVELS lv
          ON lv.LEV_NO = p.LEV_NO
        LEFT JOIN {schema}.GROUP_DETAILS g
          ON g.G_CODE = m.G_CODE
        WHERE p.LEV_NO = :lev
          AND p.I_PRICE > 0
          AND NVL(p.P_SIZE, 1) > 0
          AND NVL(({avg_sql}), 0) > 0
          AND (m.INACTIVE IS NULL OR m.INACTIVE = 0)
          {wh_sql}
          {item_sql}
    """


def count_low_margin_priced_items(
    *,
    min_profit_pct: float | None = None,
    max_profit_pct: float = 15.0,
    lev_no: int = 1,
    warehouse_codes: str | list[str] | None = None,
    item_q: str = "",
    include_negative: bool = True,
    branch_code: str = "",
    group_code: str = "",
) -> int:
    """عدد الصفوف المطابقة ضمن نطاق نسبة الربح."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")

    min_prft, max_prft = _resolved_profit_bounds(
        min_profit_pct, max_profit_pct, include_negative=include_negative
    )
    lev = int(lev_no or 1)
    if isinstance(warehouse_codes, str):
        wh_list = _parse_wh_codes(warehouse_codes)
    else:
        wh_list = [str(w).strip() for w in (warehouse_codes or []) if str(w).strip()]
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    q = str(item_q or "").strip()

    ck = (
        _filter_key(
            min_prft=min_prft,
            max_prft=max_prft,
            lev=lev,
            wh_list=wh_list,
            q=q,
            include_negative=include_negative,
            branch_code=brn,
            group_code=gcode,
        )
        + ":count"
    )
    cached = cache.get(ck)
    if cached is not None:
        return int(cached)

    # العدد بعد استثناء الافتتاحي فقط — نفس مسار الجلب المصفّى
    total = len(
        _load_capped_rows(
            min_prft=min_prft,
            max_prft=max_prft,
            lev=lev,
            wh_list=wh_list,
            q=q,
            include_negative=include_negative,
            cap=None,
            branch_code=brn,
            group_code=gcode,
        )
    )
    cache.set(ck, total, _CACHE_TTL)
    return total


def _load_capped_rows(
    *,
    min_prft: float,
    max_prft: float,
    lev: int,
    wh_list: list[str],
    q: str,
    include_negative: bool,
    cap: int | None,
    branch_code: str = "",
    group_code: str = "",
) -> list[dict[str, Any]]:
    """يجلب الصفوف المطابقة بعد استثناء الافتتاحي فقط — بدون حد عند cap=None."""
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    base_key = _filter_key(
        min_prft=min_prft,
        max_prft=max_prft,
        lev=lev,
        wh_list=wh_list,
        q=q,
        include_negative=include_negative,
        branch_code=brn,
        group_code=gcode,
    )
    all_key = base_key + ":bulk=all"
    hit_all = cache.get(all_key)
    if isinstance(hit_all, list):
        return hit_all if cap is None else hit_all[:cap]

    schema = _schema()
    params: dict[str, Any] = {
        "min_prft": min_prft,
        "max_prft": max_prft,
        "lev": lev,
        "dflt_vat": _DEFAULT_VAT_PCT,
    }
    wh_sql, item_sql, pos_only_sql, avg_sql, vat_sql, net_price_sql = _build_sql_parts(
        schema,
        wh_list=wh_list,
        q=q,
        include_negative=include_negative,
        params=params,
        branch_code=brn,
        group_code=gcode,
    )
    inner = _inner_select_sql(
        schema,
        wh_sql=wh_sql,
        item_sql=item_sql,
        avg_sql=avg_sql,
        vat_sql=vat_sql,
        net_price_sql=net_price_sql,
    )

    # جلب كامل ثم تصفية الحركة في بايثون — ثم تطبيق cap (لا ROWNUM قبل التصفية)
    rows = _fetch_all(
        f"""
        SELECT
          x.I_CODE,
          x.I_NAME,
          x.ITM_UNT,
          x.LEV_NO,
          x.LEV_A_NAME,
          x.W_CODE,
          x.G_CODE,
          x.G_NAME,
          x.PRFT_PCT,
          x.ONIX_PRFT_PCT,
          x.I_PRICE,
          x.NET_PRICE,
          x.AVG_COST,
          x.UNIT_COST,
          x.PRIMARY_COST,
          x.PRIMARY_UNIT_COST,
          x.P_SIZE,
          x.VAT_PCT,
          x.A_CY,
          x.MIN_ITM_PRICE,
          x.MIN_LIMIT_PRFT_PCT,
          ROUND(
            x.UNIT_COST * (1 + :max_prft / 100) * (1 + x.VAT_PCT / 100)
          , 4) AS LIMIT_PRICE
        FROM (
          {inner}
        ) x
        WHERE x.PRFT_PCT >= :min_prft
          AND x.PRFT_PCT < :max_prft
          {pos_only_sql}
        ORDER BY
          CASE WHEN x.PRFT_PCT < 0 THEN 1 ELSE 0 END,
          x.PRFT_PCT DESC,
          x.I_CODE,
          x.W_CODE
        """,
        params,
    )
    out = _keep_moved_stock_rows(_rows_from_oracle(rows, lev, limit_pct=max_prft))
    cache.set(all_key, out, _CACHE_TTL)
    if cap is not None:
        capped = out[:cap]
        cache.set(base_key + f":bulk={cap}", capped, _CACHE_TTL)
        return capped
    return out


def fetch_low_margin_priced_items(
    *,
    min_profit_pct: float | None = None,
    max_profit_pct: float = 15.0,
    lev_no: int = 1,
    warehouse_codes: str | list[str] | None = None,
    item_q: str = "",
    limit: int = 20,
    offset: int = 0,
    include_negative: bool = True,
    with_total: bool = True,
    branch_code: str = "",
    group_code: str = "",
) -> dict[str, Any]:
    """يرجع صفحة من الأسعار ضمن نطاق نسبة الربح (من ≤ ربح < إلى)."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")

    try:
        max_prft = float(max_profit_pct)
    except (TypeError, ValueError) as exc:
        raise OracleStockError("حد نسبة الربح «إلى» غير صالح.") from exc

    min_prft, max_prft = _resolved_profit_bounds(
        min_profit_pct, max_prft, include_negative=include_negative
    )
    if min_prft >= max_prft:
        raise OracleStockError("«من» يجب أن يكون أقل من «إلى».")

    try:
        lev = int(lev_no or 1)
    except (TypeError, ValueError):
        lev = 1

    try:
        lim = max(1, min(int(limit or _PAGE_SIZE), _EXCEL_LIMIT))
    except (TypeError, ValueError):
        lim = _PAGE_SIZE

    try:
        off = max(0, int(offset or 0))
    except (TypeError, ValueError):
        off = 0

    if isinstance(warehouse_codes, str):
        wh_list = _parse_wh_codes(warehouse_codes)
    else:
        wh_list = [str(w).strip() for w in (warehouse_codes or []) if str(w).strip()]

    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    q = str(item_q or "").strip()
    single_wh = len(wh_list) == 1
    if not wh_list and not brn:
        raise OracleStockError(
            "اختر مخزناً (أو فرعاً) قبل العرض — مسح كل الشركة يستهلك أوراكل."
        )

    def _pack(
        all_rows: list[dict[str, Any]],
        *,
        cap_used: int | None,
        total_exact: int | None = None,
    ) -> dict[str, Any]:
        page = all_rows[off : off + lim]
        loaded_total = len(all_rows)
        total_matching = total_exact if total_exact is not None else loaded_total
        truncated = bool(cap_used is not None and loaded_total >= cap_used)
        if total_exact is not None:
            truncated = total_exact > loaded_total
            has_more = off + len(page) < total_exact
            scroll_max = total_exact
        else:
            has_more = off + len(page) < loaded_total or truncated
            scroll_max = min(max(loaded_total, off + lim + _PAGE_SIZE), _SINGLE_WH_LIMIT)
        return {
            "rows": page,
            "kpis": {
                "row_count": len(page),
                "total_matching": total_matching,
                "min_profit_pct": min_profit_pct,
                "max_profit_pct": max_prft,
                "lev_no": lev,
                "limit": lim,
                "offset": off,
                "truncated": truncated,
                "has_more": has_more,
                "single_warehouse": single_wh,
                "full_scroll": True,
            },
            "meta": {
                "warehouse_codes": wh_list,
                "branch_code": brn,
                "group_code": gcode,
                "item_q": q,
                "limit": lim,
                "offset": off,
                "include_negative": include_negative,
                "page_size": _PAGE_SIZE,
                "scroll_max": scroll_max,
            },
        }

    def _maybe_count() -> int | None:
        if not with_total:
            return None
        try:
            return count_low_margin_priced_items(
                min_profit_pct=min_profit_pct,
                max_profit_pct=max_prft,
                lev_no=lev,
                warehouse_codes=wh_list,
                item_q=q,
                include_negative=include_negative,
                branch_code=brn,
                group_code=gcode,
            )
        except Exception:  # noqa: BLE001
            return None

    total_exact = _maybe_count()
    need_end = min(max(off + lim, _PAGE_SIZE), _SINGLE_WH_LIMIT)
    if total_exact is not None:
        need_end = min(max(need_end, off + lim), total_exact, _SINGLE_WH_LIMIT)
    if lim >= _EXCEL_LIMIT:
        need_end = min(_EXCEL_LIMIT, total_exact or _EXCEL_LIMIT)

    bulk_prefix = (
        _filter_key(
            min_prft=min_prft,
            max_prft=max_prft,
            lev=lev,
            wh_list=wh_list,
            q=q,
            include_negative=include_negative,
            branch_code=brn,
            group_code=gcode,
        )
        + ":bulk="
    )

    for try_cap in reversed(_SINGLE_WH_CAPS):
        if try_cap < need_end:
            continue
        hit = cache.get(bulk_prefix + str(try_cap))
        if isinstance(hit, list) and len(hit) >= need_end:
            return _pack(hit, cap_used=try_cap, total_exact=total_exact)

    hit = cache.get(bulk_prefix + "all")
    if isinstance(hit, list) and len(hit) >= need_end:
        return _pack(hit, cap_used=None, total_exact=total_exact)

    if total_exact is not None and total_exact <= need_end:
        cap: int | None = None
    else:
        cap = next((c for c in _SINGLE_WH_CAPS if c >= need_end), _SINGLE_WH_LIMIT)

    all_rows = _load_capped_rows(
        min_prft=min_prft,
        max_prft=max_prft,
        lev=lev,
        wh_list=wh_list,
        q=q,
        include_negative=include_negative,
        cap=cap,
        branch_code=brn,
        group_code=gcode,
    )
    return _pack(all_rows, cap_used=cap, total_exact=total_exact)


def _xls_num(value: Any, *, css: str = "num", digits: int = 4) -> str:
    """خلية Excel رقمية خام — بدون رموز أو فواصل."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return f'<td class="{css}"></td>'
    return f'<td class="{css}">{n:.{digits}f}</td>'


def _xls_text(value: Any, *, css: str = "txt") -> str:
    """خلية Excel كنص — يحافظ على الأصفار البادئة في أكواد الأصناف."""
    from django.utils.html import escape

    return f'<td class="{css}">{escape(str(value or ""))}</td>'


def build_low_margin_excel(
    *,
    min_profit_pct: float | None = None,
    max_profit_pct: float = 15.0,
    lev_no: int = 1,
    warehouse_codes: str | list[str] | None = None,
    item_q: str = "",
    include_negative: bool = True,
    wh_name_map: dict[str, str] | None = None,
    branch_code: str = "",
    group_code: str = "",
):
    """تصدير الصفوف ضمن نطاق الربح إلى Excel (حتى _EXCEL_LIMIT)."""
    import io

    from django.http import HttpResponse

    report = fetch_low_margin_priced_items(
        min_profit_pct=min_profit_pct,
        max_profit_pct=max_profit_pct,
        lev_no=lev_no,
        warehouse_codes=warehouse_codes,
        item_q=item_q,
        limit=_EXCEL_LIMIT,
        offset=0,
        include_negative=include_negative,
        with_total=True,
        branch_code=branch_code,
        group_code=group_code,
    )
    names = wh_name_map or {}
    kpis = report.get("kpis") or {}
    min_p = kpis.get("min_profit_pct")
    range_label = (
        f"من {float(min_p):g}% إلى {max_profit_pct:g}%"
        if min_p is not None
        else f"أقل من {max_profit_pct:g}%"
    )
    buf = io.StringIO()
    buf.write("\ufeff")
    buf.write(
        "<html xmlns:o=\"urn:schemas-microsoft-com:office:office\" "
        "xmlns:x=\"urn:schemas-microsoft-com:office:excel\" "
        "xmlns=\"http://www.w3.org/TR/REC-html40\">"
        "<head><meta charset=\"utf-8\">"
        "<!--[if gte mso 9]><xml><x:ExcelWorkbook><x:ExcelWorksheets>"
        "<x:ExcelWorksheet><x:Name>أسعار منخفضة</x:Name>"
        "<x:WorksheetOptions><x:DisplayRightToLeft/></x:WorksheetOptions>"
        "</x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
        "<style>"
        "table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:11px;}"
        "th,td{border:1px solid #94a3b8;padding:4px 7px;white-space:nowrap;vertical-align:middle;}"
        "th{background:#d9e2f3;color:#1e293b;font-weight:700;}"
        "td.txt{mso-number-format:'\\@';}"
        "td.num{mso-number-format:'0\\.0000';text-align:left;}"
        "td.pct{mso-number-format:'0\\.00';text-align:left;}"
        "td.int{mso-number-format:'0';text-align:center;}"
        "tr.even td{background:#f8fafc;}"
        "caption{font-family:Tahoma,Arial;font-size:13px;font-weight:700;margin:8px 0;text-align:right;}"
        ".sub{font-size:10px;color:#475569;font-weight:400;}"
        "</style></head><body dir=\"rtl\">"
    )
    buf.write(
        f"<table><caption>أسعار بنسبة ربح {range_label}"
        f"<br><span class=\"sub\">"
        f"المستوى {int(lev_no or 1)} · "
        f"{int(kpis.get('total_matching') or len(report.get('rows') or []))} صنف"
        f"</span></caption>"
        "<thead><tr>"
        "<th>#</th><th>الرقم</th><th>اسم الصنف</th><th>الوحدة</th>"
        "<th>المخزن</th><th>اسم المخزن</th><th>المجموعة</th><th>كود المجموعة</th>"
        "<th>متوسط التكلفة</th><th>السعر شامل الضريبة</th>"
        "<th>نسبة الربح</th><th>ربح أونكس</th><th>حد الربح %</th><th>حد السعر</th>"
        "<th>العملة</th><th>المستوى</th><th>اسم المستوى</th>"
        "</tr></thead><tbody>"
    )
    for i, r in enumerate(report.get("rows") or [], start=1):
        wh = str(r.get("wh_code") or "")
        even = ' class="even"' if i % 2 == 0 else ""
        buf.write(f"<tr{even}>")
        buf.write(f'<td class="int">{i}</td>')
        buf.write(_xls_text(r.get("item_code"), css="txt"))
        buf.write(_xls_text(r.get("item_name"), css="txt"))
        buf.write(_xls_text(r.get("unit"), css="txt"))
        buf.write(_xls_text(wh, css="txt"))
        buf.write(_xls_text(names.get(wh) or "", css="txt"))
        buf.write(_xls_text(r.get("g_name"), css="txt"))
        buf.write(_xls_text(r.get("g_code"), css="txt"))
        buf.write(_xls_num(r.get("avg_cost"), css="num", digits=4))
        buf.write(_xls_num(r.get("price"), css="num", digits=4))
        buf.write(_xls_num(r.get("profit_pct"), css="pct", digits=2))
        buf.write(_xls_num(r.get("onyx_profit_pct"), css="pct", digits=2))
        buf.write(_xls_num(r.get("limit_profit_pct"), css="pct", digits=2))
        buf.write(_xls_num(r.get("limit_price"), css="num", digits=4))
        buf.write(_xls_text(r.get("currency"), css="txt"))
        buf.write(f'<td class="int">{int(r.get("lev_no") or lev_no or 1)}</td>')
        buf.write(_xls_text(r.get("lev_name"), css="txt"))
        buf.write("</tr>")
    buf.write("</tbody></table></body></html>")
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.ms-excel; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="low-margin-prices.xls"'
    return resp
