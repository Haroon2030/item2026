"""أصناف متشابهة بالاسم ومختلفة بالباركود — من فهرس ItemBarcode المحلي."""
from __future__ import annotations

import io
import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any

from django.http import HttpResponse
from django.utils.html import escape

from search.api_client import _normalize_text, list_groups

_CACHE_VER = "v1"
_DISPLAY_LIMIT = 5_000
_EXCEL_LIMIT = 50_000
_MIN_RATIO_DEFAULT = 0.88
_MIN_NAME_LEN = 4


def _name_key(name: str) -> str:
    text = _normalize_text(name)
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold()


def _block_key(name_key: str) -> str:
    compact = name_key.replace(" ", "")
    if len(compact) < _MIN_NAME_LEN:
        return compact
    return f"{compact[:14]}:{len(compact) // 5}"


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def _load_items(
    *,
    group_code: str = "",
    item_q: str = "",
) -> list[dict[str, Any]]:
    from search.models import ItemBarcode, ItemGroup

    qs = ItemBarcode.objects.exclude(item_code="").exclude(barcode="")
    gcode = str(group_code or "").strip()
    if gcode:
        qs = qs.filter(g_code=gcode)

    q = str(item_q or "").strip()
    if q:
        from django.db.models import Q

        qs = qs.filter(
            Q(item_code__icontains=q)
            | Q(barcode__icontains=q)
            | Q(name__icontains=q)
        )

    by_code: dict[str, dict[str, Any]] = {}
    for row in qs.iterator(chunk_size=2_000):
        ic = str(row.item_code or "").strip()
        bc = str(row.barcode or "").strip()
        if not ic or not bc:
            continue
        name = str(row.name or "").strip()
        unit = str(row.unit or "").strip()
        g = str(row.g_code or "").strip()
        slot = by_code.get(ic)
        if slot is None:
            by_code[ic] = {
                "item_code": ic,
                "item_name": name,
                "name_key": _name_key(name),
                "barcodes": {bc},
                "units": {unit} if unit else set(),
                "g_code": g,
            }
        else:
            slot["barcodes"].add(bc)
            if unit:
                slot["units"].add(unit)
            if len(name) > len(slot["item_name"]):
                slot["item_name"] = name
                slot["name_key"] = _name_key(name)
            if g and not slot["g_code"]:
                slot["g_code"] = g

    g_names: dict[str, str] = {
        str(g.g_code): str(g.g_name or g.g_code)
        for g in ItemGroup.objects.all().only("g_code", "g_name")
    }
    items = list(by_code.values())
    for it in items:
        gc = str(it.get("g_code") or "").strip()
        it["g_name"] = g_names.get(gc, gc or "—")
        it["barcode_list"] = sorted(it.pop("barcodes"))
        units = it.pop("units")
        it["unit"] = " · ".join(sorted(units)) if units else "—"
    return items


def _barcodes_differ(group: list[dict[str, Any]]) -> bool:
    all_bc: set[str] = set()
    for it in group:
        all_bc.update(it.get("barcode_list") or [])
    if len(all_bc) <= 1:
        return False
    if len(group) < 2:
        return False
    for i, a in enumerate(group):
        set_a = set(a.get("barcode_list") or [])
        for b in group[i + 1 :]:
            set_b = set(b.get("barcode_list") or [])
            if set_a != set_b:
                return True
    return len({it["item_code"] for it in group}) >= 2 and len(all_bc) > 1


def _build_exact_groups(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for it in items:
        key = str(it.get("name_key") or "").strip()
        if len(key.replace(" ", "")) < _MIN_NAME_LEN:
            continue
        buckets[key].append(it)
    groups: list[list[dict[str, Any]]] = []
    for key, bucket in buckets.items():
        if len({it["item_code"] for it in bucket}) < 2:
            continue
        if not _barcodes_differ(bucket):
            continue
        groups.append(bucket)
    return groups


def _build_similar_groups(
    items: list[dict[str, Any]],
    *,
    min_ratio: float,
) -> list[list[dict[str, Any]]]:
    blocks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for it in items:
        key = str(it.get("name_key") or "").strip()
        if len(key.replace(" ", "")) < _MIN_NAME_LEN:
            continue
        blocks[_block_key(key)].append(it)

    uf = _UnionFind()
    pair_ratio: dict[tuple[str, str], float] = {}

    for bucket in blocks.values():
        if len(bucket) < 2:
            continue
        codes = [it["item_code"] for it in bucket]
        for i, a in enumerate(bucket):
            for b in bucket[i + 1 :]:
                if a["item_code"] == b["item_code"]:
                    continue
                ratio = _similarity(a["name_key"], b["name_key"])
                if ratio < min_ratio:
                    continue
                if a["name_key"] == b["name_key"]:
                    continue
                uf.union(a["item_code"], b["item_code"])
                pair = tuple(sorted((a["item_code"], b["item_code"])))
                pair_ratio[pair] = max(pair_ratio.get(pair, 0.0), ratio)

    by_root: dict[str, list[dict[str, Any]]] = defaultdict(list)
    item_map = {it["item_code"]: it for it in items}
    for ic in item_map:
        by_root[uf.find(ic)].append(item_map[ic])

    groups: list[list[dict[str, Any]]] = []
    for bucket in by_root.values():
        if len({it["item_code"] for it in bucket}) < 2:
            continue
        if not _barcodes_differ(bucket):
            continue
        codes = sorted({it["item_code"] for it in bucket})
        min_pair = 1.0
        for i, ca in enumerate(codes):
            for cb in codes[i + 1 :]:
                min_pair = min(min_pair, pair_ratio.get((ca, cb), min_ratio))
        for it in bucket:
            it["_group_min_ratio"] = min_pair if min_pair < 1.0 else min_ratio
        groups.append(bucket)
    return groups


def _merge_groups(
    exact: list[list[dict[str, Any]]],
    similar: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    seen_sets: set[frozenset[str]] = set()
    merged: list[tuple[str, list[dict[str, Any]]]] = []

    for bucket in exact:
        codes = frozenset(it["item_code"] for it in bucket)
        if codes in seen_sets:
            continue
        seen_sets.add(codes)
        merged.append(("exact", bucket))

    for bucket in similar:
        codes = frozenset(it["item_code"] for it in bucket)
        if codes in seen_sets:
            continue
        seen_sets.add(codes)
        merged.append(("similar", bucket))

    merged.sort(key=lambda g: (-len(g[1]), g[1][0].get("name_key") or ""))
    return [{"match_type": mt, "items": bucket} for mt, bucket in merged]


def _flatten_groups(
    groups: list[dict[str, Any]],
    *,
    limit: int,
    whole_groups_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total_items = 0
    groups_included = 0
    for gid, grp in enumerate(groups, start=1):
        bucket = grp["items"]
        match_type = grp["match_type"]
        total_items += len(bucket)
        bucket_rows: list[dict[str, Any]] = []
        label = max(
            (it.get("item_name") or "" for it in bucket),
            key=len,
            default="",
        )
        name_key = bucket[0].get("name_key") or ""
        for idx, it in enumerate(sorted(bucket, key=lambda x: x["item_code"])):
            barcodes = it.get("barcode_list") or []
            bucket_rows.append(
                {
                    "group_id": gid,
                    "group_label": label,
                    "name_key": name_key,
                    "match_type": match_type,
                    "match_type_label": "مطابقة تامة" if match_type == "exact" else "اسم مشابه",
                    "group_size": len(bucket),
                    "row_in_group": idx + 1,
                    "is_first_in_group": idx == 0,
                    "item_code": it["item_code"],
                    "item_name": it.get("item_name") or "—",
                    "unit": it.get("unit") or "—",
                    "g_code": it.get("g_code") or "",
                    "g_name": it.get("g_name") or "—",
                    "barcodes_display": " · ".join(barcodes),
                    "barcodes_excel": ",".join(barcodes),
                    "primary_barcode": barcodes[0] if barcodes else "",
                    "barcode_count": len(barcodes),
                    "similarity_pct": round(
                        float(it.get("_group_min_ratio") or 1.0) * 100, 1
                    )
                    if match_type == "similar"
                    else 100.0,
                }
            )

        if whole_groups_only:
            if rows and len(rows) + len(bucket_rows) > limit:
                break
            rows.extend(bucket_rows)
            groups_included += 1
            continue

        if len(rows) >= limit:
            continue
        added = 0
        for br in bucket_rows:
            if len(rows) >= limit:
                break
            rows.append(br)
            added += 1
        if added:
            groups_included += 1

    truncated = total_items > len(rows) or len(groups) > groups_included
    kpis = {
        "total_groups": len(groups),
        "total_items": total_items,
        "shown": len(rows),
        "groups_shown": groups_included,
        "truncated": truncated,
        "has_more": truncated,
    }
    return rows, kpis


def fetch_name_barcode_conflicts(
    *,
    group_code: str = "",
    item_q: str = "",
    mode: str = "all",
    min_ratio: float = _MIN_RATIO_DEFAULT,
    limit: int = _DISPLAY_LIMIT,
    whole_groups_only: bool = False,
) -> dict[str, Any]:
    items = _load_items(group_code=group_code, item_q=item_q)
    if not items:
        return {
            "kpis": {
                "total_groups": 0,
                "total_items": 0,
                "shown": 0,
                "truncated": False,
                "has_more": False,
                "index_count": 0,
            },
            "rows": [],
            "meta": {"mode": mode, "min_ratio": min_ratio},
        }

    mode_norm = str(mode or "all").strip().lower()
    exact_groups: list[list[dict[str, Any]]] = []
    similar_groups: list[list[dict[str, Any]]] = []

    if mode_norm in ("all", "exact"):
        exact_groups = _build_exact_groups(items)
    if mode_norm in ("all", "similar"):
        similar_groups = _build_similar_groups(items, min_ratio=min_ratio)

    merged = _merge_groups(exact_groups, similar_groups)
    rows, kpis = _flatten_groups(
        merged,
        limit=max(1, int(limit or _DISPLAY_LIMIT)),
        whole_groups_only=whole_groups_only,
    )
    kpis["index_count"] = len(items)
    return {
        "kpis": kpis,
        "rows": rows,
        "meta": {"mode": mode_norm, "min_ratio": min_ratio, "group_code": group_code},
    }


def _xls_clean(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"—", "-", "–"}:
        return ""
    return text


def _xls_text(value: Any, *, css: str = "txt") -> str:
    return f'<td class="{css}">{escape(_xls_clean(value))}</td>'


def build_name_barcode_excel(
    *,
    group_code: str = "",
    item_q: str = "",
    mode: str = "all",
    min_ratio: float = _MIN_RATIO_DEFAULT,
) -> HttpResponse:
    report = fetch_name_barcode_conflicts(
        group_code=group_code,
        item_q=item_q,
        mode=mode,
        min_ratio=min_ratio,
        limit=_EXCEL_LIMIT,
        whole_groups_only=True,
    )
    kpis = report.get("kpis") or {}
    rows = report.get("rows") or []
    buf = io.StringIO()
    buf.write("\ufeff")
    buf.write(
        '<html xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:x="urn:schemas-microsoft-com:office:excel" '
        'xmlns="http://www.w3.org/TR/REC-html40">'
        '<head><meta charset="utf-8">'
        "<!--[if gte mso 9]><xml><x:ExcelWorkbook><x:ExcelWorksheets>"
        "<x:ExcelWorksheet><x:Name>اسم وباركود</x:Name>"
        "<x:WorksheetOptions><x:DisplayRightToLeft/></x:WorksheetOptions>"
        "</x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
        "<style>"
        "table{border-collapse:collapse;font-family:Tahoma,Arial;font-size:11px;width:100%;}"
        "th,td{border:1px solid #64748b;padding:2px 5px;white-space:nowrap;vertical-align:middle;mso-number-format:'\\@';}"
        "th{background:#d9e2f3;color:#1e293b;font-weight:700;text-align:center;}"
        "th.h-grp{background:#ede9fe;color:#5b21b6;}"
        "th.h-type{background:#ffedd5;color:#9a3412;}"
        "th.h-code{background:#e0e7ff;color:#3730a3;}"
        "th.h-name{background:#f1f5f9;color:#0f172a;}"
        "th.h-bc{background:#ede9fe;color:#5b21b6;}"
        "th.h-unit{background:#ecfdf5;color:#065f46;}"
        "th.h-gname{background:#e0f2fe;color:#0c4a6e;}"
        "td.txt{text-align:right;}"
        "td.code{text-align:center;font-weight:700;color:#4338ca;}"
        "td.bc{text-align:center;font-weight:700;color:#7c3aed;background:#f3e8ff;}"
        "td.type-exact{text-align:center;background:#dcfce7;color:#14532d;font-weight:700;}"
        "td.type-similar{text-align:center;background:#ffedd5;color:#9a3412;font-weight:700;}"
        "td.grp{text-align:center;font-weight:700;background:#f5f3ff;color:#5b21b6;}"
        "td.name{font-weight:600;color:#1e293b;}"
        "td.unit{text-align:center;color:#334155;}"
        "td.gname{color:#334155;}"
        "td.idx{text-align:center;mso-number-format:'0';}"
        "tr.grp-a td{background:#ffffff;}"
        "tr.grp-b td{background:#f8fafc;}"
        "tr.grp-b td.bc{background:#e9d5ff;}"
        "caption{font-family:Tahoma,Arial;font-size:13px;font-weight:700;margin:6px 0;text-align:right;}"
        ".sub{font-size:10px;color:#475569;font-weight:400;}"
        "</style></head><body dir=\"rtl\">"
    )
    shown_groups = int(kpis.get("groups_shown") or 0)
    total_groups = int(kpis.get("total_groups") or 0)
    buf.write(
        f"<table><caption>أصناف متشابهة بالاسم · باركود مختلف"
        f"<br><span class=\"sub\">"
        f"{shown_groups} / {total_groups} مجموعة · "
        f"{len(rows)} / {int(kpis.get('total_items') or 0)} صنف"
        f"{' · مقطوع عند الحد' if kpis.get('truncated') else ''}"
        f"</span></caption>"
        "<thead><tr>"
        "<th>#</th>"
        "<th class=\"h-code\">الرقم</th>"
        "<th class=\"h-name\">اسم الصنف</th>"
        "<th class=\"h-bc\">الباركود</th>"
        "<th class=\"h-grp\">مج.</th>"
        "<th class=\"h-type\">نوع</th>"
        "<th class=\"h-unit\">الوحدة</th>"
        "<th class=\"h-gname\">المجموعة</th>"
        "</tr></thead><tbody>"
    )
    prev_gid = None
    stripe = 0
    for i, r in enumerate(rows, start=1):
        gid = r.get("group_id")
        if gid != prev_gid:
            stripe = 1 - stripe
            prev_gid = gid
        row_cls = "grp-a" if stripe else "grp-b"
        match_type = str(r.get("match_type") or "")
        type_cls = "type-exact" if match_type == "exact" else "type-similar"
        g_label = _xls_clean(r.get("g_name"))
        g_code = _xls_clean(r.get("g_code"))
        if g_code and g_label:
            g_cell = f"{g_code} — {g_label}"
        elif g_code:
            g_cell = g_code
        else:
            g_cell = g_label
        buf.write(f'<tr class="{row_cls}">')
        buf.write(f'<td class="idx">{i}</td>')
        buf.write(_xls_text(r.get("item_code"), css="code"))
        buf.write(_xls_text(r.get("item_name"), css="name"))
        buf.write(
            _xls_text(r.get("primary_barcode") or r.get("barcodes_excel") or r.get("barcodes_display"), css="bc")
        )
        buf.write(_xls_text(r.get("group_id"), css="grp"))
        buf.write(
            f'<td class="{type_cls}">{escape(_xls_clean(r.get("match_type_label")))}</td>'
        )
        buf.write(_xls_text(r.get("unit"), css="unit"))
        buf.write(_xls_text(g_cell, css="gname"))
        buf.write("</tr>")
    buf.write("</tbody></table></body></html>")
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.ms-excel; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="name-barcode-conflicts.xls"'
    return resp


def group_options() -> list[dict[str, str]]:
    return [{"code": g["g_code"], "name": g["g_name"]} for g in list_groups()]
