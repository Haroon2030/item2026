"""كشف اختلاف عبوة نفس الوحدة بين الشراء (دخول) والتحويل الصادر (خروج).

الفكرة العملية:
- الصنف دخل شراءً بوحدة مثل «كرتون» وشد/عبوة 12
- وطلع تحويلاً صادراً بنفس اسم الوحدة «كرتون» لكن عبوة 6
- هذا خطأ ضبط وحدة/عبوة (وليس مقارنة كرتون مع باكت)
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
_CACHE_VER = "v7"


def _f(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _int_num(value: Any) -> int:
    """عرض أرقام صحيحة بدون عشرية (12 بدل 12.0)."""
    try:
        return int(round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _plain_ratio(value: Any) -> str:
    """نسبة بلا فاصلة محلية؛ بلا أصفار زائدة (0.5 أو 2)."""
    try:
        num = round(float(value or 0), 4)
    except (TypeError, ValueError):
        return "0"
    if abs(num - round(num)) < 1e-9:
        return str(int(round(num)))
    text = f"{num:.4f}".rstrip("0").rstrip(".")
    return text or "0"


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
    doc_nos: tuple[str, ...] = ()

    @property
    def base_qty(self) -> float:
        return round(self.qty * float(self.p_size or 0), 4)


def _norm_doc_no(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].replace("-", "", 1).isdigit():
        text = text[:-2]
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        try:
            return str(int(text))
        except ValueError:
            return text
    return text


def _doc_nos_display(nos: tuple[str, ...] | list[str], *, limit: int = 6) -> str:
    clean: list[str] = []
    seen: set[str] = set()
    for raw in nos or ():
        no = _norm_doc_no(raw)
        if not no or no in seen:
            continue
        seen.add(no)
        clean.append(no)
    if not clean:
        return "—"
    shown = clean[:limit]
    extra = len(clean) - len(shown)
    text = "، ".join(shown)
    return f"{text} +" if extra > 0 else text


def _merge_pack_rows(rows: list[dict[str, Any]], *, with_docs: bool) -> list[_PackAgg]:
    """دمج صفوف أوراكل حسب الصنف/المخزن/الوحدة/العبوة مع جمع أرقام المستندات."""
    buckets: dict[tuple[str, str, str, float], dict[str, Any]] = {}
    for row in rows or []:
        item_code = str(row.get("I_CODE") or "").strip()
        if not item_code:
            continue
        unit = str(row.get("UNIT") or "").strip() or "—"
        try:
            p_size = float(row.get("P_SIZE") or 0)
        except (TypeError, ValueError):
            p_size = 0.0
        wh_code = str(row.get("W_CODE") or "").strip()
        key = (item_code, wh_code, unit, round(p_size, 4))
        slot = buckets.get(key)
        if slot is None:
            slot = {
                "item_code": item_code,
                "item_name": str(row.get("I_NAME") or "").strip() or item_code,
                "wh_code": wh_code,
                "unit": unit,
                "p_size": p_size,
                "qty": 0.0,
                "docs": [],
                "seen_docs": set(),
            }
            buckets[key] = slot
        try:
            slot["qty"] += float(row.get("QTY_TOTAL") or 0)
        except (TypeError, ValueError):
            pass
        if with_docs:
            doc = _norm_doc_no(row.get("TR_NO"))
            if doc and doc not in slot["seen_docs"]:
                slot["seen_docs"].add(doc)
                slot["docs"].append(doc)
    out: list[_PackAgg] = []
    for slot in buckets.values():
        qty = float(slot["qty"] or 0)
        if abs(qty) < 1e-9:
            continue
        out.append(
            _PackAgg(
                item_code=slot["item_code"],
                item_name=slot["item_name"],
                wh_code=slot["wh_code"],
                unit=slot["unit"],
                p_size=float(slot["p_size"] or 0),
                qty=qty,
                doc_nos=tuple(slot["docs"]),
            )
        )
    return out


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
    return _merge_pack_rows(rows or [], with_docs=False)


def _fetch_outbound_transfer_packs(
    date_from: date,
    date_to_excl: date,
    *,
    min_pack_size: float,
    warehouse_codes: list[str],
) -> list[_PackAgg]:
    """تحويلات صادرة (خروج) من المخازن المحددة — TR_INOUT_TYPE=1 عبر F_W_CODE."""
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
        SELECT /*+ LEADING(m d) USE_NL(d) INDEX(d INDX_SER_WHTRNS_DTL) */
          TO_CHAR(d.I_CODE) AS I_CODE,
          NVL(NULLIF(TRIM(i.I_NAME), ''), TO_CHAR(d.I_CODE)) AS I_NAME,
          TO_CHAR(m.F_W_CODE) AS W_CODE,
          NVL(NULLIF(TRIM(TO_CHAR(d.ITM_UNT)), ''), '—') AS UNIT,
          ROUND(NVL(d.P_SIZE, 0), 4) AS P_SIZE,
          TO_CHAR(m.TR_NO) AS TR_NO,
          ROUND(SUM(NVL(d.I_QTY, 0)), 4) AS QTY_TOTAL
        FROM {schema}.IAS_WHTRNS_MST m
        JOIN {schema}.IAS_WHTRNS_DTL d
          ON d.TR_SER = m.TR_SER
        LEFT JOIN {schema}.IAS_ITM_MST i
          ON i.I_CODE = d.I_CODE
        WHERE m.TR_DATE >= :d_from
          AND m.TR_DATE < :d_to_excl
          AND m.TR_INOUT_TYPE = 1
          AND {_hung_ok('m')}
          AND NVL(d.P_SIZE, 0) >= :min_ps
          AND d.I_CODE IS NOT NULL
          AND TO_CHAR(m.F_W_CODE) IN ({', '.join(wh_keys)})
        GROUP BY
          TO_CHAR(d.I_CODE),
          NVL(NULLIF(TRIM(i.I_NAME), ''), TO_CHAR(d.I_CODE)),
          TO_CHAR(m.F_W_CODE),
          NVL(NULLIF(TRIM(TO_CHAR(d.ITM_UNT)), ''), '—'),
          ROUND(NVL(d.P_SIZE, 0), 4),
          TO_CHAR(m.TR_NO)
        HAVING ROUND(SUM(NVL(d.I_QTY, 0)), 4) <> 0
        """,
        params,
    )
    return _merge_pack_rows(rows or [], with_docs=True)

def build_unit_pack_mismatch_report(
    date_from,
    date_to,
    *,
    warehouse_codes: str,
    min_pack_size: float = 2,
    tolerance_pct: float = 0.1,
    limit: int = 100,
) -> dict[str, Any]:
    """دخل شراء بوحدة/عبوة، وطلع تحويل صادر بنفس اسم الوحدة وعبوة مختلفة."""
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

    date_to_excl = d_to + timedelta(days=1)
    cache_key = (
        f"packmismatch:{_CACHE_VER}:{d_from}:{d_to}:{','.join(wh_codes)}:"
        f"minps={min_ps:.4f}:tol={tol:.4f}:lim={int(limit)}"
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
    transfers = _fetch_outbound_transfer_packs(
        d_from,
        date_to_excl,
        min_pack_size=min_ps,
        warehouse_codes=wh_codes,
    )

    # نفس الصنف + المخزن + اسم الوحدة (كرتون مع كرتون فقط)
    pur_buckets: dict[tuple[str, str, str], list[_PackAgg]] = {}
    for r in purchases:
        key = (r.item_code, r.wh_code, _unit_key(r.unit))
        pur_buckets.setdefault(key, []).append(r)

    tr_buckets: dict[tuple[str, str, str], list[_PackAgg]] = {}
    for r in transfers:
        key = (r.item_code, r.wh_code, _unit_key(r.unit))
        tr_buckets.setdefault(key, []).append(r)

    mismatch_rows: list[dict[str, Any]] = []
    for key, pur_list in pur_buckets.items():
        tr_list = tr_buckets.get(key)
        if not tr_list:
            continue

        # عبوة الشراء الغالبة لنفس اسم الوحدة = المرجع (مثل كرتون شد 12)
        expected = _pick_dominant(pur_list)
        if not expected or expected.p_size <= 0:
            continue

        for tr in tr_list:
            if tr.p_size <= 0:
                continue
            # مثال الخطأ: شراء كرتون 12 مقابل تحويل كرتون 6
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
                    "purchase_unit": expected.unit,
                    "purchase_p_size": _int_num(expected.p_size),
                    "transfer_p_size": _int_num(tr.p_size),
                    "pack_diff": _int_num(abs(tr.p_size - expected.p_size)),
                    "ratio": _plain_ratio(ratio),
                    "diff_pct": _int_num(diff_pct),
                    "purchase_qty": _int_num(expected.qty),
                    "transfer_qty": _int_num(tr.qty),
                    "purchase_base_qty": _int_num(expected.base_qty),
                    "transfer_base_qty": _int_num(tr.base_qty),
                    "transfer_unit": tr.unit,
                    "transfer_tr_nos": _doc_nos_display(tr.doc_nos),
                    "match_mode": "same_unit_pack",
                    "error_kind": "P_SIZE",
                }
            )

    for r in mismatch_rows:
        buy_ps = float(r.get("purchase_p_size") or 0)
        tr_ps = float(r.get("transfer_p_size") or 0)
        r["impact_base_diff"] = _int_num(abs(tr_ps - buy_ps))

    mismatch_rows.sort(
        key=lambda r: (
            -abs(float(r.get("diff_pct") or 0)),
            -abs(float(r.get("impact_base_diff") or 0)),
            r.get("item_code") or "",
        )
    )
    all_rows = mismatch_rows[: max(1, int(limit or 100))]

    report = {
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "filters": {
            "warehouse_codes": warehouse_codes,
            "min_pack_size": min_ps,
            "tolerance_pct": tol,
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


def build_pack_mismatch_branch_detail(
    date_from,
    date_to,
    *,
    item_code: str,
    wh_code: str,
    unit: str,
    purchase_p_size: float,
    transfer_p_size: float,
) -> dict[str, Any]:
    """تفاصيل فرق الشد للتحويلات الصادرة حسب فرع الوجهة."""
    from .oracle_stock import _branch_names

    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")

    code = str(item_code or "").strip()
    src_wh = str(wh_code or "").strip()
    unit_name = str(unit or "").strip()
    unit_want = _unit_key(unit_name)
    buy_ps = float(purchase_p_size or 0)
    out_ps = float(transfer_p_size or 0)
    if not code or not src_wh:
        raise OracleStockError("يلزم كود الصنف ومخزن المصدر.")
    if buy_ps <= 0 or out_ps <= 0:
        raise OracleStockError("عبوة الشراء/الخروج غير صالحة.")

    schema = _schema()
    date_to_excl = d_to + timedelta(days=1)
    pack_diff = abs(out_ps - buy_ps)
    # لا نفلتر اسم الوحدة في SQL: كرتون ≠ كرتــون نصاً لكن نفس المفتاح بعد التطبيع
    params: dict[str, Any] = {
        "d_from": d_from,
        "d_to_excl": date_to_excl,
        "icode": code,
        "src_wh": src_wh,
        "out_ps": out_ps,
    }

    rows = _fetch_all(
        f"""
        SELECT /*+ LEADING(m d) USE_NL(d) INDEX(d INDX_SER_WHTRNS_DTL) */
          TO_CHAR(NVL(tw.CONN_BRN_NO, '')) AS BRN_NO,
          TO_CHAR(m.T_W_CODE) AS DST_WH,
          NVL(NULLIF(TRIM(tw.W_NAME), ''), TO_CHAR(m.T_W_CODE)) AS DST_WH_NAME,
          TO_CHAR(m.TR_NO) AS TR_NO,
          TO_CHAR(m.TR_DATE, 'YYYY-MM-DD') AS TR_DATE,
          NVL(NULLIF(TRIM(TO_CHAR(d.ITM_UNT)), ''), '—') AS UNIT,
          ROUND(NVL(d.P_SIZE, 0), 4) AS P_SIZE,
          ROUND(SUM(NVL(d.I_QTY, 0)), 4) AS QTY_TOTAL
        FROM {schema}.IAS_WHTRNS_MST m
        JOIN {schema}.IAS_WHTRNS_DTL d
          ON d.TR_SER = m.TR_SER
        LEFT JOIN {schema}.WAREHOUSE_DETAILS tw
          ON tw.W_CODE = m.T_W_CODE
        WHERE m.TR_DATE >= :d_from
          AND m.TR_DATE < :d_to_excl
          AND m.TR_INOUT_TYPE = 1
          AND {_hung_ok('m')}
          AND TO_CHAR(m.F_W_CODE) = :src_wh
          AND TO_CHAR(d.I_CODE) = :icode
          AND ABS(NVL(d.P_SIZE, 0) - :out_ps) < 0.0001
        GROUP BY
          TO_CHAR(NVL(tw.CONN_BRN_NO, '')),
          TO_CHAR(m.T_W_CODE),
          NVL(NULLIF(TRIM(tw.W_NAME), ''), TO_CHAR(m.T_W_CODE)),
          TO_CHAR(m.TR_NO),
          TO_CHAR(m.TR_DATE, 'YYYY-MM-DD'),
          NVL(NULLIF(TRIM(TO_CHAR(d.ITM_UNT)), ''), '—'),
          ROUND(NVL(d.P_SIZE, 0), 4)
        HAVING ROUND(SUM(NVL(d.I_QTY, 0)), 4) <> 0
        """,
        params,
    )

    item_name = code
    name_rows = _fetch_all(
        f"""
        SELECT NVL(NULLIF(TRIM(I_NAME), ''), TO_CHAR(I_CODE)) AS I_NAME
        FROM {schema}.IAS_ITM_MST
        WHERE TO_CHAR(I_CODE) = :icode
          AND ROWNUM = 1
        """,
        {"icode": code},
    )
    if name_rows:
        item_name = str(name_rows[0].get("I_NAME") or code).strip() or code

    branch_names = _branch_names()
    by_brn: dict[str, dict[str, Any]] = {}
    seen_unit_label = unit_name
    for row in rows or []:
        row_unit = str(row.get("UNIT") or "").strip() or "—"
        if unit_want and _unit_key(row_unit) != unit_want:
            continue
        if not seen_unit_label or seen_unit_label == unit_name:
            seen_unit_label = row_unit
        brn = str(row.get("BRN_NO") or "").strip() or "—"
        slot = by_brn.get(brn)
        if slot is None:
            slot = {
                "branch_code": brn,
                "branch_name": branch_names.get(brn) or brn,
                "qty": 0.0,
                "tr_nos": [],
                "seen": set(),
                "dst_whs": set(),
            }
            by_brn[brn] = slot
        try:
            slot["qty"] += float(row.get("QTY_TOTAL") or 0)
        except (TypeError, ValueError):
            pass
        tr_no = _norm_doc_no(row.get("TR_NO"))
        if tr_no and tr_no not in slot["seen"]:
            slot["seen"].add(tr_no)
            slot["tr_nos"].append(tr_no)
        dst = str(row.get("DST_WH") or "").strip()
        if dst:
            slot["dst_whs"].add(dst)

    branch_rows: list[dict[str, Any]] = []
    for slot in by_brn.values():
        qty = float(slot["qty"] or 0)
        branch_rows.append(
            {
                "branch_code": slot["branch_code"],
                "branch_name": slot["branch_name"],
                "transfer_qty": _int_num(qty),
                "pack_diff": _int_num(pack_diff),
                "base_diff": _int_num(qty * pack_diff),
                "transfer_p_size": _int_num(out_ps),
                "purchase_p_size": _int_num(buy_ps),
                "tr_count": len(slot["tr_nos"]),
                "transfer_tr_nos": _doc_nos_display(slot["tr_nos"], limit=12),
                "dst_wh_count": len(slot["dst_whs"]),
            }
        )

    branch_rows.sort(
        key=lambda r: (
            -abs(int(r.get("base_diff") or 0)),
            -abs(int(r.get("transfer_qty") or 0)),
            str(r.get("branch_name") or ""),
        )
    )

    return {
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "item_code": code,
        "item_name": item_name,
        "wh_code": src_wh,
        "unit": seen_unit_label or unit_name,
        "purchase_p_size": _int_num(buy_ps),
        "transfer_p_size": _int_num(out_ps),
        "pack_diff": _int_num(pack_diff),
        "rows": branch_rows,
        "kpis": {
            "branch_count": len(branch_rows),
            "transfer_qty": _int_num(sum(float(r.get("transfer_qty") or 0) for r in branch_rows)),
            "base_diff": _int_num(
                sum(float(r.get("base_diff") or 0) for r in branch_rows)
            ),
        },
    }

