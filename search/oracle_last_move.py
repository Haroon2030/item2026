"""آخر حركة على الأصناف خلال فترة: آخر تاريخ شراء وآخر تاريخ بيع — SELECT فقط.

- الشراء : IAS_PI_BILL_DTL / IAS_PI_BILL_MST
- البيع  : IAS_BILL_DTL / IAS_BILL_MST (آجل ونقدي) + IAS_POS_BILL_DTL / IAS_POS_BILL_MST (نقاط البيع)
النتيجة مخزّنة مؤقتاً دقائق قليلة لأن الاستعلام على فترة شهر قد يستغرق ثوانٍ.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any

from django.core.cache import cache

from .oracle_stock import (
    _bill_mst_ok,
    _fetch_all,
    _hung_ok,
    _pos_owner,
    _run_parallel,
    _schema,
    oracle_enabled,
)

logger = logging.getLogger(__name__)

CACHE_SECONDS = 300
LOCK_SECONDS = 90


def _day(value: Any) -> str:
    if value is None:
        return ""
    try:
        return value.strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return str(value)[:10]


def _int_list(values) -> list[int]:
    out: list[int] = []
    for v in values or []:
        text = str(v or "").strip()
        if text.isdigit():
            out.append(int(text))
    return out


def _merge_max(target: dict[str, str], rows: list[dict]) -> None:
    for row in rows:
        code = str(row.get("I_CODE") or "").strip()
        if code.endswith(".0"):
            code = code[:-2]
        day = _day(row.get("LAST_DT"))
        if code and day and day > target.get(code, ""):
            target[code] = day


def fetch_movement_summary(
    date_from: date,
    date_to: date,
    warehouses: list[str] | None = None,
) -> dict[str, dict[str, str]]:
    """{item_code: {"purchase": 'YYYY-MM-DD'|'', "sale": 'YYYY-MM-DD'|''}} للأصناف
    التي لها شراء أو بيع ضمن [date_from, date_to] (شاملة)."""
    if not oracle_enabled():
        return {}
    whs = sorted(set(_int_list(warehouses)))
    key = f"lastmove:v1:{date_from.isoformat()}:{date_to.isoformat()}:{','.join(map(str, whs))}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    # طلبات متزامنة لنفس الفترة: واحد يحسب والباقي ينتظر نتيجته بدل أن تُثقل أوراكل
    lock_key = key + ":lock"
    if not cache.add(lock_key, 1, LOCK_SECONDS):
        waited = 0.0
        while waited < LOCK_SECONDS:
            time.sleep(0.5)
            waited += 0.5
            hit = cache.get(key)
            if hit is not None:
                return hit
            if cache.get(lock_key) is None:
                break
    try:
        return _compute_movement_summary(key, date_from, date_to, whs)
    finally:
        cache.delete(lock_key)


def _compute_movement_summary(
    key: str, date_from: date, date_to: date, whs: list[int]
) -> dict[str, dict[str, str]]:
    schema = _schema()
    pos = _pos_owner()
    params: dict[str, Any] = {
        "d_from": date_from,
        "d_to_excl": date_to + timedelta(days=1),
    }
    wh_list = ", ".join(str(w) for w in whs)
    wh_pur = f"AND NVL(d.W_CODE, m.W_CODE) IN ({wh_list})" if whs else ""
    wh_bill = f"AND d.W_CODE IN ({wh_list})" if whs else ""
    wh_pos = f"AND m.W_CODE IN ({wh_list})" if whs else ""

    purchase: dict[str, str] = {}
    sale: dict[str, str] = {}
    failed = False
    try:
        _merge_max(
            purchase,
            _fetch_all(
                f"""
                SELECT d.I_CODE AS I_CODE, MAX(m.BILL_DATE) AS LAST_DT
                FROM {schema}.IAS_PI_BILL_DTL d
                JOIN {schema}.IAS_PI_BILL_MST m
                  ON m.BILL_NO = d.BILL_NO
                 AND m.BILL_SER = d.BILL_SER
                 AND m.BILL_DOC_TYPE = d.BILL_DOC_TYPE
                WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
                  AND d.I_CODE IS NOT NULL
                  {wh_pur}
                GROUP BY d.I_CODE
                """,
                dict(params),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        failed = True
        logger.warning("Last purchase summary failed: %s", exc)
    try:
        _merge_max(
            sale,
            _fetch_all(
                f"""
                SELECT d.I_CODE AS I_CODE, MAX(b.BILL_DATE) AS LAST_DT
                FROM {schema}.IAS_BILL_DTL d
                JOIN {schema}.IAS_BILL_MST b ON b.BILL_SER = d.BILL_SER
                WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
                  AND b.BILL_DOC_TYPE IN (1, 4, 5, 8)
                  AND {_bill_mst_ok("b")}
                  AND d.I_CODE IS NOT NULL
                  {wh_bill}
                GROUP BY d.I_CODE
                """,
                dict(params),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        failed = True
        logger.warning("Last bill sale summary failed: %s", exc)
    try:
        _merge_max(
            sale,
            _fetch_all(
                f"""
                SELECT /*+ LEADING(m d) USE_NL(d)
                           INDEX(m POSBILLMST_BILLDATEUSRBRN)
                           INDEX(d IAS_POS_INDX_BILL_DTL) */
                       d.I_CODE AS I_CODE, MAX(m.BILL_DATE) AS LAST_DT
                FROM {pos}.IAS_POS_BILL_MST m
                JOIN {pos}.IAS_POS_BILL_DTL d ON d.BILL_NO = m.BILL_NO
                WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
                  AND {_hung_ok("m")}
                  AND d.I_CODE IS NOT NULL
                  {wh_pos}
                GROUP BY d.I_CODE
                """,
                dict(params),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        failed = True
        logger.warning("Last POS sale summary failed: %s", exc)

    out: dict[str, dict[str, str]] = {}
    for code in set(purchase) | set(sale):
        out[code] = {"purchase": purchase.get(code, ""), "sale": sale.get(code, "")}
    if not failed:
        cache.set(key, out, CACHE_SECONDS)
    return out


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _norm_code(value: Any) -> str:
    text = str(value or "").strip()
    return text[:-2] if text.endswith(".0") else text


def fetch_overstock_items(
    date_from: date,
    date_to: date,
    warehouses: list[str] | None = None,
    *,
    min_qty: float = 100.0,
    max_pct: float = 5.0,
) -> dict[str, Any]:
    """أصناف كميتها كبيرة ومبيعاتها ضمن الفترة لا تتجاوز max_pct% من الكمية.

    تُحسب فقط في المخازن المرتبطة بالمبيعات (التي سُجّل عليها بيع نقاط بيع/فواتير
    ضمن الفترة). النتيجة مجمّعة لكل صنف:
    {"warehouses": [codes], "items": {code: {"stock", "sold", "pct", "unit", "whs": [{"wh","qty","sold"}]}}}
    الكمية بوحدة المخزون (أصغر عبوة). SELECT فقط.
    """
    empty: dict[str, Any] = {"warehouses": [], "items": {}}
    if not oracle_enabled():
        return empty
    whs = sorted(set(_int_list(warehouses)))
    key = (
        f"overstock:v2:{date_from.isoformat()}:{date_to.isoformat()}:"
        f"{','.join(map(str, whs))}:{min_qty}:{max_pct}"
    )
    hit = cache.get(key)
    if hit is not None:
        return hit

    schema = _schema()
    pos = _pos_owner()
    params: dict[str, Any] = {
        "d_from": date_from,
        "d_to_excl": date_to + timedelta(days=1),
    }
    wh_in = ", ".join(str(w) for w in whs)
    wh_bill = f"AND d.W_CODE IN ({wh_in})" if whs else ""
    wh_pos = f"AND NVL(d.W_CODE, m.W_CODE) IN ({wh_in})" if whs else ""
    wh_stock = f"AND w.W_CODE IN ({wh_in})" if whs else ""
    pos_qty = "NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(d.P_SIZE, 1))"

    def _job_pos() -> list[dict]:
        return _fetch_all(
            f"""
            SELECT /*+ LEADING(m d) USE_NL(d)
                       INDEX(m POSBILLMST_BILLDATEUSRBRN)
                       INDEX(d IAS_POS_INDX_BILL_DTL) */
                   d.I_CODE AS I_CODE, NVL(d.W_CODE, m.W_CODE) AS W_CODE,
                   SUM({pos_qty}) AS QTY
            FROM {pos}.IAS_POS_BILL_MST m
            JOIN {pos}.IAS_POS_BILL_DTL d ON d.BILL_NO = m.BILL_NO
            WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
              AND {_hung_ok("m")}
              AND d.I_CODE IS NOT NULL
              {wh_pos}
            GROUP BY d.I_CODE, NVL(d.W_CODE, m.W_CODE)
            """,
            dict(params),
        )

    def _job_bill() -> list[dict]:
        sql = """
            SELECT d.I_CODE AS I_CODE, d.W_CODE AS W_CODE,
                   SUM({qty}) AS QTY
            FROM {schema}.IAS_BILL_DTL d
            JOIN {schema}.IAS_BILL_MST b ON b.BILL_SER = d.BILL_SER
            WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
              AND b.BILL_DOC_TYPE IN (1, 4, 5, 8)
              AND {ok}
              AND d.I_CODE IS NOT NULL
              {wh}
            GROUP BY d.I_CODE, d.W_CODE
            """
        try:
            return _fetch_all(
                sql.format(
                    qty="NVL(d.I_QTY, 0) * NVL(d.P_SIZE, 1)",
                    schema=schema, ok=_bill_mst_ok("b"), wh=wh_bill,
                ),
                dict(params),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Overstock bill sales (with P_SIZE) failed: %s", exc)
            return _fetch_all(
                sql.format(
                    qty="NVL(d.I_QTY, 0)",
                    schema=schema, ok=_bill_mst_ok("b"), wh=wh_bill,
                ),
                dict(params),
            )

    def _job_machines() -> list[dict]:
        try:
            return _fetch_all(
                f"""
                SELECT DISTINCT TO_CHAR(DEF_WCODE) AS W_CODE
                FROM {pos}.IAS_POS_MACHINE
                WHERE DEF_WCODE IS NOT NULL AND NVL(INACTIVE, 0) = 0
                """,
                {},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("POS machine default warehouses failed: %s", exc)
            return []

    def _job_stock() -> list[dict]:
        # كل الأرصدة الموجبة دفعة واحدة (بالتوازي مع المبيعات) ثم نُصفّيها
        # بالمخازن المرتبطة في بايثون — أسرع من انتظار نتيجة المبيعات أولاً.
        return _fetch_all(
            f"""
            SELECT TO_CHAR(s.I_CODE) AS I_CODE, TO_CHAR(s.W_CODE) AS W_CODE,
                   s.ITM_UNT AS UNIT, NVL(s.AVL_QTY, 0) AS QTY
            FROM (
                SELECT w.I_CODE, w.W_CODE, w.ITM_UNT, w.AVL_QTY,
                       ROW_NUMBER() OVER (
                           PARTITION BY w.I_CODE, w.W_CODE
                           ORDER BY NVL(w.P_SIZE, 1), w.ITM_UNT
                       ) AS RN
                FROM {schema}.IAS_ITM_WCODE w
                WHERE NVL(w.AVL_QTY, 0) > 0
                  {wh_stock}
            ) s
            WHERE s.RN = 1
            """,
            {},
        )

    pos_rows, bill_rows, machine_rows, stock_rows = _run_parallel(
        [_job_pos, _job_bill, _job_machines, _job_stock], max_workers=4
    )

    sold: dict[tuple[str, str], float] = {}
    for row in list(pos_rows) + list(bill_rows):
        code = _norm_code(row.get("I_CODE"))
        wh = _norm_code(row.get("W_CODE"))
        if code and wh:
            sold[(code, wh)] = sold.get((code, wh), 0.0) + _num(row.get("QTY"))

    # المخازن المرتبطة بالمبيعات = المخزن الافتراضي لأجهزة نقاط البيع الفعّالة
    # (IAS_POS_MACHINE.DEF_WCODE) + أي مخزن سُجّل عليه بيع ضمن الفترة
    linked_set = {wh for (_code, wh) in sold}
    for row in machine_rows:
        wh = _norm_code(row.get("W_CODE"))
        if wh.isdigit() and (not whs or int(wh) in whs):
            linked_set.add(wh)
    linked = sorted(w for w in linked_set if w.isdigit())
    if not linked:
        return empty
    linked_lookup = set(linked)

    items: dict[str, dict[str, Any]] = {}
    for row in stock_rows:
        code = _norm_code(row.get("I_CODE"))
        wh = _norm_code(row.get("W_CODE"))
        qty = _num(row.get("QTY"))
        if not code or wh not in linked_lookup or qty <= 0:
            continue
        rec = items.setdefault(
            code,
            {"stock": 0.0, "sold": 0.0, "unit": str(row.get("UNIT") or "").strip(), "whs": []},
        )
        sold_q = sold.get((code, wh), 0.0)
        rec["stock"] += qty
        rec["sold"] += sold_q
        rec["whs"].append({"wh": wh, "qty": qty, "sold": sold_q})

    out_items: dict[str, dict[str, Any]] = {}
    for code, rec in items.items():
        if rec["stock"] < min_qty:
            continue
        pct = rec["sold"] / rec["stock"] * 100.0
        if pct > max_pct:
            continue
        rec["pct"] = round(max(pct, 0.0), 2)
        rec["stock"] = round(rec["stock"], 2)
        rec["sold"] = round(max(rec["sold"], 0.0), 2)
        rec["whs"].sort(key=lambda r: -r["qty"])
        out_items[code] = rec

    result = {"warehouses": linked, "items": out_items}
    cache.set(key, result, CACHE_SECONDS)
    return result


def fetch_stock_by_warehouse(
    item_codes: list[str],
    warehouses: list[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """الكمية المتبقية بعد الترحيل لكل صنف في كل مخزن (>0 فقط)، لأصناف صفحة صغيرة.

    = IAS_ITM_WCODE.AVL_QTY − مبيعات نقاط البيع غير المرحّلة (لا تقل عن صفر)،
    بوحدة المخزون (أصغر عبوة). يعيد {item_code: [{"wh","qty","unit"}, ...]}.
    """
    from .oracle_stock import _PENDING_SALES_LOOKBACK_DAYS, _pos_owner as _pos

    codes = _int_list(item_codes)[:100]
    if not codes or not oracle_enabled():
        return {}
    whs = sorted(set(_int_list(warehouses)))
    schema = _schema()
    pos = _pos()
    in_items = ", ".join("'" + str(c) + "'" for c in codes)
    wh_w = f"AND w.W_CODE IN ({', '.join(str(w) for w in whs)})" if whs else ""
    wh_pend = (
        f"AND NVL(d.W_CODE, m.W_CODE) IN ({', '.join(str(w) for w in whs)})"
        if whs
        else ""
    )
    days = int(_PENDING_SALES_LOOKBACK_DAYS)
    qty_expr = "NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(d.P_SIZE, 1))"
    try:
        rows = _fetch_all(
            f"""
            SELECT TO_CHAR(s.I_CODE) AS I_CODE, TO_CHAR(s.W_CODE) AS W_CODE,
                   s.ITM_UNT AS UNIT,
                   GREATEST(0, NVL(s.AVL_QTY, 0) - GREATEST(0, NVL(pend.QTY, 0))) AS QTY
            FROM (
                SELECT w.I_CODE, w.W_CODE, w.ITM_UNT, w.AVL_QTY,
                       ROW_NUMBER() OVER (
                           PARTITION BY w.I_CODE, w.W_CODE
                           ORDER BY NVL(w.P_SIZE, 1), w.ITM_UNT
                       ) AS RN
                FROM {schema}.IAS_ITM_WCODE w
                WHERE w.I_CODE IN ({in_items})
                  {wh_w}
            ) s
            LEFT JOIN (
                SELECT I_CODE, W_CODE, ROUND(SUM(QTY), 4) AS QTY
                FROM (
                    SELECT TO_CHAR(d.I_CODE) AS I_CODE,
                           TO_CHAR(NVL(d.W_CODE, m.W_CODE)) AS W_CODE,
                           SUM({qty_expr}) AS QTY
                    FROM {pos}.IAS_POS_BILL_DTL d
                    JOIN {pos}.IAS_POS_BILL_MST m
                      ON m.BILL_NO = d.BILL_NO
                     AND m.BRN_NO = d.BRN_NO
                     AND NVL(m.BILL_SRL, 0) = NVL(d.BILL_SRL, 0)
                    WHERE d.I_CODE IN ({in_items})
                      AND NVL(m.POSTED, 0) = 0
                      AND NVL(m.HUNG, 0) = 0
                      AND m.BILL_DATE >= TRUNC(SYSDATE) - {days}
                      AND NVL(d.W_CODE, m.W_CODE) IS NOT NULL
                      {wh_pend}
                    GROUP BY TO_CHAR(d.I_CODE), TO_CHAR(NVL(d.W_CODE, m.W_CODE))
                    UNION ALL
                    SELECT TO_CHAR(d.I_CODE) AS I_CODE,
                           TO_CHAR(NVL(d.W_CODE, m.W_CODE)) AS W_CODE,
                           -SUM({qty_expr}) AS QTY
                    FROM {pos}.IAS_POS_RT_BILL_DTL d
                    JOIN {pos}.IAS_POS_RT_BILL_MST m
                      ON m.RT_BILL_NO = d.RT_BILL_NO
                     AND m.BRN_NO = d.BRN_NO
                    WHERE d.I_CODE IN ({in_items})
                      AND NVL(m.POSTED, 0) = 0
                      AND NVL(m.HUNG, 0) = 0
                      AND m.RT_BILL_DATE >= TRUNC(SYSDATE) - {days}
                      AND NVL(d.W_CODE, m.W_CODE) IS NOT NULL
                      {wh_pend}
                    GROUP BY TO_CHAR(d.I_CODE), TO_CHAR(NVL(d.W_CODE, m.W_CODE))
                )
                GROUP BY I_CODE, W_CODE
            ) pend
              ON pend.I_CODE = TO_CHAR(s.I_CODE)
             AND pend.W_CODE = TO_CHAR(s.W_CODE)
            WHERE s.RN = 1
            """,
            {},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Stock by warehouse failed: %s", exc)
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = str(row.get("I_CODE") or "").strip()
        if code.endswith(".0"):
            code = code[:-2]
        wh = str(row.get("W_CODE") or "").strip()
        if wh.endswith(".0"):
            wh = wh[:-2]
        try:
            qty = float(row.get("QTY") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if not code or not wh or qty <= 0:
            continue
        out.setdefault(code, []).append(
            {"wh": wh, "qty": qty, "unit": str(row.get("UNIT") or "").strip()}
        )
    for lst in out.values():
        lst.sort(key=lambda r: -r["qty"])
    return out


def fetch_last_purchases(
    item_codes: list[str],
    warehouses: list[str] | None = None,
    *,
    lookback_days: int = 2000,
) -> dict[str, dict[str, str]]:
    """{item_code: {"date": 'YYYY-MM-DD', "wh": رقم مخزن آخر شراء}} في المخازن المحددة
    (بلا حصره بفترة الشاشة)."""
    codes = _int_list(item_codes)[:100]
    if not codes or not oracle_enabled():
        return {}
    whs = sorted(set(_int_list(warehouses)))
    schema = _schema()
    in_items = ", ".join("'" + str(c) + "'" for c in codes)
    wh_pur = (
        f"AND NVL(d.W_CODE, m.W_CODE) IN ({', '.join(str(w) for w in whs)})"
        if whs
        else ""
    )
    since = date.today() - timedelta(days=max(1, int(lookback_days)))
    try:
        rows = _fetch_all(
            f"""
            SELECT I_CODE, LAST_DT, W_CODE
            FROM (
                SELECT d.I_CODE AS I_CODE,
                       m.BILL_DATE AS LAST_DT,
                       NVL(d.W_CODE, m.W_CODE) AS W_CODE,
                       ROW_NUMBER() OVER (
                           PARTITION BY d.I_CODE
                           ORDER BY m.BILL_DATE DESC NULLS LAST,
                                    m.BILL_NO DESC NULLS LAST
                       ) AS RN
                FROM {schema}.IAS_PI_BILL_DTL d
                JOIN {schema}.IAS_PI_BILL_MST m
                  ON m.BILL_NO = d.BILL_NO
                 AND m.BILL_SER = d.BILL_SER
                 AND m.BILL_DOC_TYPE = d.BILL_DOC_TYPE
                WHERE d.I_CODE IN ({in_items})
                  AND m.BILL_DATE >= :d_from
                  {wh_pur}
            )
            WHERE RN = 1
            """,
            {"d_from": since},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Last purchases failed: %s", exc)
        return {}
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        code = str(row.get("I_CODE") or "").strip()
        if code.endswith(".0"):
            code = code[:-2]
        day = _day(row.get("LAST_DT"))
        wh = row.get("W_CODE")
        wh_text = "" if wh is None else str(wh).strip()
        if wh_text.endswith(".0"):
            wh_text = wh_text[:-2]
        if code and day:
            out[code] = {"date": day, "wh": wh_text}
    return out
