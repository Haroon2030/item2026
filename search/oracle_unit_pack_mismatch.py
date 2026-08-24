"""كشف اختلاف الوحدات/الأحجام (P_SIZE) بين المشتريات والتحويلات الواردة.

الفكرة:
- من مشتريات الفترة: نجمع (الصنف I_CODE + المخزن W_CODE + اسم الوحدة ITM_UNT) مع P_SIZE وكمية الوحدات.
- من تحويلات واردة الفترة (TR_INOUT_TYPE=2): نفس التجميع.
- إذا كانت P_SIZE مختلفة لنفس (الصنف + المخزن + اسم الوحدة) فهذا غالباً خطأ في ضبط الوحدات/العبوات أو اختيار الوحدة عند الإدخال.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, date
from typing import Any

from django.core.cache import cache

from .oracle_stock import (
    OracleStockError,
    _as_date,
    _hung_ok,
    _schema,
    _fetch_all,
    oracle_enabled,
)

_CACHE_TTL = 900
_CACHE_VER = "v1"


def _f(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _unit_key(unit: Any) -> str:
    text = str(unit or "").strip()
    for ch in ("\u200e", "\u200f", "\u0640", " ", "\u00a0", "\t"):
        text = text.replace(ch, "")
    return text.casefold()


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


@dataclass(frozen=True)
class _PackAgg:
    item_code: str
    item_name: str
    wh_code: str
    unit: str
    p_size: float
    qty: float

    @property
    def base_qty(self) -> float:
        return round(self.qty * float(self.p_size or 0), 4)


def _pick_dominant(rows: list[_PackAgg]) -> _PackAgg | None:
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: (-r.qty, -r.p_size))
    return rows[0]


def _fetch_purchase_packs(
    date_from: date,
    date_to_excl: date,
    *,
    min_pack_size: float,
    warehouse_codes: list[str],
) -> list[_PackAgg]:
    if not warehouse_codes:
        raise OracleStockError("يلزم تحديد مخازن لرفع الأداء.")

    schema = _schema()
    params: dict[str, Any] = {
        "d_from": date_from,
        "d_to_excl": date_to_excl,
        "min_ps": float(min_pack_size),
    }
    wh_keys: list[str] = []
    for i, wh in enumerate(warehouse_codes):
        key = f"w{i}"
        wh_keys.append(f":{key}")
        params[key] = wh

    rows = _fetch_all(
        f"""
        SELECT
          TO_CHAR(d.I_CODE) AS I_CODE,
          NVL(NULLIF(TRIM(i.I_NAME), ''), TO_CHAR(d.I_CODE)) AS I_NAME,
          TO_CHAR(NVL(d.W_CODE, m.W_CODE)) AS W_CODE,
          NVL(NULLIF(TRIM(TO_CHAR(d.ITM_UNT)), ''), '—') AS UNIT,
          ROUND(NVL(d.P_SIZE, 0), 4) AS P_SIZE,
          ROUND(SUM(NVL(d.I_QTY, 0)), 4) AS QTY_TOTAL
        FROM {schema}.IAS_PI_BILL_MST m
        JOIN {schema}.IAS_PI_BILL_DTL d
          ON d.BILL_SER = m.BILL_SER
        LEFT JOIN {schema}.IAS_ITM_MST i
          ON i.I_CODE = d.I_CODE
        WHERE m.BILL_DATE >= :d_from
          AND m.BILL_DATE < :d_to_excl
          AND {_hung_ok('m')}
          AND NVL(d.P_SIZE, 0) >= :min_ps
          AND d.I_CODE IS NOT NULL
          AND TO_CHAR(NVL(d.W_CODE, m.W_CODE)) IN ({', '.join(wh_keys)})
        GROUP BY
          TO_CHAR(d.I_CODE),
          NVL(NULLIF(TRIM(i.I_NAME), ''), TO_CHAR(d.I_CODE)),
          TO_CHAR(NVL(d.W_CODE, m.W_CODE)),
          NVL(NULLIF(TRIM(TO_CHAR(d.ITM_UNT)), ''), '—'),
          ROUND(NVL(d.P_SIZE, 0), 4)
        HAVING ROUND(SUM(NVL(d.I_QTY, 0)), 4) <> 0
        """,
        params,
    )
    out: list[_PackAgg] = []
    for row in rows or []:
        out.append(
            _PackAgg(
                item_code=str(row.get("I_CODE") or "").strip(),
                item_name=str(row.get("I_NAME") or "").strip()
                or str(row.get("I_CODE") or "").strip(),
                wh_code=str(row.get("W_CODE") or "").strip(),
                unit=str(row.get("UNIT") or "").strip() or "—",
                p_size=float(row.get("P_SIZE") or 0),
                qty=float(row.get("QTY_TOTAL") or 0),
            )
        )
    return out


def _fetch_inbound_transfer_packs(
    date_from: date,
    date_to_excl: date,
    *,
    min_pack_size: float,
    warehouse_codes: list[str],
) -> list[_PackAgg]:
    if not warehouse_codes:
        raise OracleStockError("يلزم تحديد مخازن لرفع الأداء.")

    schema = _schema()
    params: dict[str, Any] = {
        "d_from": date_from,
        "d_to_excl": date_to_excl,
        "min_ps": float(min_pack_size),
    }
    wh_keys: list[str] = []
    for i, wh in enumerate(warehouse_codes):
        key = f"w{i}"
        wh_keys.append(f":{key}")
        params[key] = wh

    rows = _fetch_all(
        f"""
        SELECT
          TO_CHAR(d.I_CODE) AS I_CODE,
          NVL(NULLIF(TRIM(i.I_NAME), ''), TO_CHAR(d.I_CODE)) AS I_NAME,
          TO_CHAR(d.W_CODE) AS W_CODE,
          NVL(NULLIF(TRIM(TO_CHAR(d.ITM_UNT)), ''), '—') AS UNIT,
          ROUND(NVL(d.P_SIZE, 0), 4) AS P_SIZE,
          ROUND(SUM(NVL(d.I_QTY, 0)), 4) AS QTY_TOTAL
        FROM {schema}.IAS_WHTRNS_MST m
        JOIN {schema}.IAS_WHTRNS_DTL d
          ON d.TR_SER = m.TR_SER
        LEFT JOIN {schema}.IAS_ITM_MST i
          ON i.I_CODE = d.I_CODE
        WHERE m.TR_DATE >= :d_from
          AND m.TR_DATE < :d_to_excl
          AND m.TR_INOUT_TYPE = 2
          AND {_hung_ok('m')}
          AND NVL(d.P_SIZE, 0) >= :min_ps
          AND d.I_CODE IS NOT NULL
          AND TO_CHAR(d.W_CODE) IN ({', '.join(wh_keys)})
        GROUP BY
          TO_CHAR(d.I_CODE),
          NVL(NULLIF(TRIM(i.I_NAME), ''), TO_CHAR(d.I_CODE)),
          TO_CHAR(d.W_CODE),
          NVL(NULLIF(TRIM(TO_CHAR(d.ITM_UNT)), ''), '—'),
          ROUND(NVL(d.P_SIZE, 0), 4)
        HAVING ROUND(SUM(NVL(d.I_QTY, 0)), 4) <> 0
        """,
        params,
    )
    out: list[_PackAgg] = []
    for row in rows or []:
        out.append(
            _PackAgg(
                item_code=str(row.get("I_CODE") or "").strip(),
                item_name=str(row.get("I_NAME") or "").strip()
                or str(row.get("I_CODE") or "").strip(),
                wh_code=str(row.get("W_CODE") or "").strip(),
                unit=str(row.get("UNIT") or "").strip() or "—",
                p_size=float(row.get("P_SIZE") or 0),
                qty=float(row.get("QTY_TOTAL") or 0),
            )
        )
    return out


def build_unit_pack_mismatch_report(
    date_from,
    date_to,
    *,
    warehouse_codes: str,
    min_pack_size: float = 2,
    tolerance_pct: float = 0.1,
    qty_tolerance_pct: float = 20.0,
    limit: int = 100,
) -> dict[str, Any]:
    """تقرير اختلاف P_SIZE للوحدة الكبيرة بين المشتريات والتحويلات الواردة."""
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")
    if (d_to - d_from).days > 90:
        raise OracleStockError("الفترة القصوى لهذا التقرير 90 يوم لتجنب بطء أوراكل.")

    wh_codes = _parse_wh_codes(warehouse_codes)
    if not wh_codes:
        raise OracleStockError("حدد مخازن (مثال: 3,901,902,401).")

    min_ps = float(min_pack_size or 2)
    if min_ps < 2:
        min_ps = 2.0
    tol = float(tolerance_pct or 0.1)
    qty_tol = float(qty_tolerance_pct or 20.0)

    date_to_excl = d_to + timedelta(days=1)
    cache_key = (
        f"packmismatch:{_CACHE_VER}:{d_from}:{d_to}:{','.join(wh_codes)}:"
        f"minps={min_ps:.4f}:tol={tol:.4f}:qtytol={qty_tol:.4f}:lim={int(limit)}"
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached

    purchases = _fetch_purchase_packs(
        d_from,
        date_to_excl,
        min_pack_size=min_ps,
        warehouse_codes=wh_codes,
    )
    transfers = _fetch_inbound_transfer_packs(
        d_from,
        date_to_excl,
        min_pack_size=min_ps,
        warehouse_codes=wh_codes,
    )

    # index by (item_code, wh_code, unit_key)
    pur_buckets: dict[tuple[str, str, str], list[_PackAgg]] = {}
    for r in purchases:
        key = (r.item_code, r.wh_code, _unit_key(r.unit))
        pur_buckets.setdefault(key, []).append(r)

    tr_buckets: dict[tuple[str, str, str], list[_PackAgg]] = {}
    for r in transfers:
        key = (r.item_code, r.wh_code, _unit_key(r.unit))
        tr_buckets.setdefault(key, []).append(r)

    # index by (item_code, wh_code) ignoring الوحدة — مفيد إذا كانت أسماء الوحدات لا تتطابق نصاً
    pur_by_item_wh: dict[tuple[str, str], list[_PackAgg]] = {}
    for r in purchases:
        key2 = (r.item_code, r.wh_code)
        pur_by_item_wh.setdefault(key2, []).append(r)

    tr_by_item_wh: dict[tuple[str, str], list[_PackAgg]] = {}
    for r in transfers:
        key2 = (r.item_code, r.wh_code)
        tr_by_item_wh.setdefault(key2, []).append(r)

    mismatch_rows: list[dict[str, Any]] = []
    for key, pur_list in pur_buckets.items():
        tr_list = tr_buckets.get(key)
        if not tr_list:
            continue

        expected = _pick_dominant(pur_list)  # P_SIZE المتوقع من المشتريات لنفس الوحدة
        if not expected or expected.p_size <= 0:
            continue

        for tr in tr_list:
            if tr.p_size <= 0:
                continue
            diff_pct = (tr.p_size - expected.p_size) / expected.p_size * 100.0
            if abs(diff_pct) < tol:
                continue
            ratio = tr.p_size / expected.p_size if expected.p_size else 0.0
            mismatch_rows.append(
                {
                    "item_code": expected.item_code,
                    "item_name": expected.item_name,
                    "wh_code": expected.wh_code,
                    "unit": expected.unit,
                    "purchase_p_size": round(expected.p_size, 4),
                    "transfer_p_size": round(tr.p_size, 4),
                    "ratio": round(ratio, 4),
                    "diff_pct": round(diff_pct, 3),
                    "purchase_qty": round(expected.qty, 4),
                    "transfer_qty": round(tr.qty, 4),
                    "purchase_base_qty": round(expected.base_qty, 4),
                    "transfer_base_qty": round(tr.base_qty, 4),
                    "transfer_unit": tr.unit,
                    "match_mode": "unit_name",
                }
            )

    # وضع ثانٍ: قارن أكبر P_SIZE في المشتريات مع أي P_SIZE في التحويل مختلف (حتى لو الوحدة النصية مختلفة)
    strict_seen: set[tuple[str, str, str, float, float]] = set()
    for r in mismatch_rows:
        strict_seen.add(
            (
                str(r.get("item_code") or ""),
                str(r.get("wh_code") or ""),
                str(r.get("unit") or ""),
                float(r.get("purchase_p_size") or 0),
                float(r.get("transfer_p_size") or 0),
            )
        )

    for key2, pur_list in pur_by_item_wh.items():
        tr_list = tr_by_item_wh.get(key2) or []
        if not tr_list:
            continue
        # المتوقع: أكبر P_SIZE ظهرت في مشتريات نفس المخزن/الصنف (عادة هي "الوحدة الكبيرة" فعلاً)
        pur_max = max(pur_list or [], key=lambda r: (r.p_size, r.qty))
        if not pur_max or pur_max.p_size <= 0:
            continue
        expected_p = float(pur_max.p_size)
        for tr in tr_list:
            if tr.p_size <= 0:
                continue
            diff_pct = (tr.p_size - expected_p) / expected_p * 100.0
            if abs(diff_pct) < tol:
                continue
            ratio = tr.p_size / expected_p if expected_p else 0.0
            dedup_key = (
                str(key2[0]),
                str(key2[1]),
                str(tr.unit),
                float(pur_max.p_size),
                float(tr.p_size),
            )
            if dedup_key in strict_seen:
                continue
            mismatch_rows.append(
                {
                    "item_code": pur_max.item_code,
                    "item_name": pur_max.item_name,
                    "wh_code": pur_max.wh_code,
                    "unit": tr.unit,
                    "purchase_p_size": round(pur_max.p_size, 4),
                    "transfer_p_size": round(tr.p_size, 4),
                    "ratio": round(ratio, 4),
                    "diff_pct": round(diff_pct, 3),
                    "purchase_qty": round(pur_max.qty, 4),
                    "transfer_qty": round(tr.qty, 4),
                    "purchase_base_qty": round(pur_max.base_qty, 4),
                    "transfer_base_qty": round(tr.base_qty, 4),
                    "transfer_unit": tr.unit,
                    "match_mode": "dominant_max_ps",
                }
            )
            strict_seen.add(dedup_key)

    # sort biggest transfer base first (أثر التحويل في التخزين)
    mismatch_rows.sort(
        key=lambda r: (-abs(float(r.get("transfer_base_qty") or 0)), r["item_code"])
    )
    # فحص فرق الكمية (Base Qty) لنفس P_SIZE المتوقع — هذا يلتقط حالة 288 vs 960.
    qty_mismatch_rows: list[dict[str, Any]] = []
    for key, pur_list in pur_buckets.items():
        tr_list = tr_buckets.get(key) or []
        if not tr_list:
            continue
        expected = _pick_dominant(pur_list)
        if not expected or expected.base_qty <= 0:
            continue
        expected_p = float(expected.p_size)
        tr_same = [tr for tr in tr_list if abs(float(tr.p_size or 0) - expected_p) < 0.0001]
        if not tr_same:
            continue
        tr_qty = sum(float(tr.qty or 0) for tr in tr_same)
        tr_base_qty = round(tr_qty * expected_p, 4)
        diff_pct_qty = (tr_base_qty - expected.base_qty) / expected.base_qty * 100.0
        if abs(diff_pct_qty) < qty_tol:
            continue
        ratio = tr_base_qty / expected.base_qty if expected.base_qty else 0.0
        qty_mismatch_rows.append(
            {
                "item_code": expected.item_code,
                "item_name": expected.item_name,
                "wh_code": expected.wh_code,
                "unit": expected.unit,
                "purchase_p_size": round(expected_p, 4),
                "transfer_p_size": round(expected_p, 4),
                "ratio": round(ratio, 4),
                "diff_pct": round(diff_pct_qty, 3),
                "purchase_qty": round(expected.qty, 4),
                "transfer_qty": round(tr_qty, 4),
                "purchase_base_qty": round(expected.base_qty, 4),
                "transfer_base_qty": round(tr_base_qty, 4),
                "error_kind": "QTY_BASE",
                "impact_base_diff": round(abs(tr_base_qty - expected.base_qty), 4),
            }
        )

    # Merge + sort
    all_rows = qty_mismatch_rows + mismatch_rows
    for r in all_rows:
        if "error_kind" not in r:
            r["error_kind"] = "P_SIZE"
        r["impact_base_diff"] = round(
            abs(float(r.get("transfer_base_qty") or 0) - float(r.get("purchase_base_qty") or 0)),
            4,
        )

    all_rows.sort(
        key=lambda r: (-abs(float(r.get("impact_base_diff") or 0)), r.get("item_code") or "")
    )
    all_rows = all_rows[: max(1, int(limit or 100))]

    report = {
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "filters": {
            "warehouse_codes": warehouse_codes,
            "min_pack_size": min_ps,
            "tolerance_pct": tol,
            "qty_tolerance_pct": qty_tol,
            "limit": int(limit or 100),
        },
        "kpis": {
            "mismatch_count": len(all_rows),
            "min_pack_size": min_ps,
        },
        "rows": all_rows,
    }
    try:
        cache.set(cache_key, report, _CACHE_TTL)
    except Exception:
        pass
    return report

