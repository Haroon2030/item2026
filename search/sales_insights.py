"""
قياس الأداء — قراءة حقيقية مع فلترة ومقارنة فترتين (قراءة فقط من أوراكل).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def _prior_period(date_from: date, date_to: date) -> tuple[date, date]:
    span = (date_to - date_from).days + 1
    prior_to = date_from - timedelta(days=1)
    prior_from = prior_to - timedelta(days=span - 1)
    return prior_from, prior_to


def _pct_change(current: float, previous: float) -> float | None:
    if previous == 0:
        return None if current == 0 else 100.0
    return round((current - previous) / abs(previous) * 100.0, 1)


def _fmt_money(value: float) -> str:
    return f'{float(value or 0):,.2f}'


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return '—'
    sign = '+' if value > 0 else ''
    return f'{sign}{value:.1f}%'


def _fmt_int(value: int) -> str:
    return f'{int(value or 0):,}'


def _sum_pair(
    rows: list[dict],
    key_a: str,
    key_b: str,
) -> tuple[float, float, float | None]:
    total_a = round(sum(float(r.get(key_a) or 0) for r in rows), 2)
    total_b = round(sum(float(r.get(key_b) or 0) for r in rows), 2)
    return total_a, total_b, _pct_change(total_a, total_b)


def _branch_table_totals(rows: list[dict]) -> dict[str, Any]:
    sales_a, sales_b, sales_delta = _sum_pair(rows, 'sales_a', 'sales_b')
    inv_a = sum(int(r.get('inv_a') or 0) for r in rows)
    inv_b = sum(int(r.get('inv_b') or 0) for r in rows)
    ret_a = round(sum(float(r.get('ret_a') or 0) for r in rows), 2)
    rate = round((ret_a / sales_a) * 100.0, 1) if sales_a else 0.0
    return {
        'sales_a_display': _fmt_money(sales_a),
        'sales_b_display': _fmt_money(sales_b),
        'sales_delta': sales_delta,
        'sales_delta_display': _fmt_pct(sales_delta),
        'inv_a_display': _fmt_int(inv_a),
        'inv_b_display': _fmt_int(inv_b),
        'ret_a_display': _fmt_money(ret_a),
        'return_rate_a_display': f'{rate:.1f}%',
    }


def _group_table_totals(rows: list[dict]) -> dict[str, Any]:
    sales_a, sales_b, sales_delta = _sum_pair(rows, 'sales_a', 'sales_b')
    inv_a = sum(int(r.get('inv_a') or 0) for r in rows)
    inv_b = sum(int(r.get('inv_b') or 0) for r in rows)
    ret_a = round(sum(float(r.get('ret_a') or 0) for r in rows), 2)
    rate = round((ret_a / sales_a) * 100.0, 1) if sales_a else 0.0
    avg_a = round(sales_a / inv_a, 2) if inv_a else 0.0
    return {
        'sales_a_display': _fmt_money(sales_a),
        'sales_b_display': _fmt_money(sales_b),
        'sales_delta': sales_delta,
        'sales_delta_display': _fmt_pct(sales_delta),
        'inv_a_display': _fmt_int(inv_a),
        'inv_b_display': _fmt_int(inv_b),
        'avg_a_display': _fmt_money(avg_a),
        'ret_a_display': _fmt_money(ret_a),
        'return_rate_a_display': f'{rate:.1f}%',
    }


def _daily_table_totals(rows: list[dict]) -> dict[str, Any]:
    sales = round(sum(float(r.get('sales') or 0) for r in rows), 2)
    inv = sum(int(r.get('inv') or 0) for r in rows)
    ret_c = sum(int(r.get('ret_count') or 0) for r in rows)
    gross = round(sum(float(r.get('gross') or 0) for r in rows), 2)
    avg = round(sales / inv, 2) if inv else 0.0
    return {
        'sales_display': _fmt_money(sales),
        'inv_display': _fmt_int(inv),
        'ret_count_display': _fmt_int(ret_c),
        'avg_display': _fmt_money(avg),
        'gross_display': _fmt_money(gross),
    }


def _items_sales_totals(rows: list[dict]) -> dict[str, Any]:
    sales_a, sales_b, sales_delta = _sum_pair(rows, 'sales_a', 'sales_b')
    return {
        'sales_a_display': _fmt_money(sales_a),
        'sales_b_display': _fmt_money(sales_b),
        'sales_delta': sales_delta,
        'sales_delta_display': _fmt_pct(sales_delta),
    }


def _items_return_totals(rows: list[dict]) -> dict[str, Any]:
    ret_a, ret_b, ret_delta = _sum_pair(rows, 'ret_a', 'ret_b')
    return {
        'ret_a_display': _fmt_money(ret_a),
        'ret_b_display': _fmt_money(ret_b),
        'ret_delta': ret_delta,
        'ret_delta_display': _fmt_pct(ret_delta),
    }


def _sum_sales(rows: list[dict]) -> tuple[float, int]:
    sales = sum(float(r.get('sales_total') or 0) for r in rows)
    inv = sum(int(r.get('invoice_count') or 0) for r in rows)
    return sales, inv


def _sum_returns(rows: list[dict]) -> tuple[float, int]:
    total = sum(float(r.get('return_total') or r.get('sales_total') or 0) for r in rows)
    count = sum(int(r.get('return_count') or r.get('invoice_count') or 0) for r in rows)
    return total, count


def _health_score(
    sales_delta: float | None,
    return_rate: float,
    top_share: float,
    invoice_delta: float | None,
) -> dict[str, Any]:
    score = 55.0

    if sales_delta is None:
        pass
    elif sales_delta >= 8:
        score += 22
    elif sales_delta >= 2:
        score += 14
    elif sales_delta >= 0:
        score += 6
    elif sales_delta >= -8:
        score -= 8
    elif sales_delta >= -15:
        score -= 16
    else:
        score -= 24

    if return_rate <= 1.5:
        score += 14
    elif return_rate <= 3:
        score += 6
    elif return_rate <= 5:
        score -= 6
    elif return_rate <= 8:
        score -= 14
    else:
        score -= 22

    if top_share >= 55:
        score -= 8
    elif top_share >= 40:
        score -= 3

    if invoice_delta is not None:
        if invoice_delta >= 5:
            score += 6
        elif invoice_delta <= -10:
            score -= 8

    score = int(max(0, min(100, round(score))))
    if score >= 75:
        level, label, tone = 'good', 'أداء قوي', 'good'
    elif score >= 55:
        level, label, tone = 'ok', 'أداء مقبول', 'ok'
    elif score >= 35:
        level, label, tone = 'warn', 'يحتاج انتباهاً', 'warn'
    else:
        level, label, tone = 'bad', 'أداء ضعيف', 'bad'
    return {
        'score': score,
        'level': level,
        'label': label,
        'tone': tone,
    }


def _load_sales_rows(
    date_from,
    date_to,
    system: str,
    branch_code: str = '',
    group_code: str = '',
) -> list[dict]:
    """صفوف للمقارنة: فروع، أو فروع ضمن مجموعة، أو صف واحد لفرع محدد."""
    from .oracle_stock import fetch_branch_sales_totals, fetch_group_sales_totals

    brn = str(branch_code or '').strip()
    gcode = str(group_code or '').strip()

    if gcode:
        rows = fetch_group_sales_totals(
            date_from,
            date_to,
            system=system,
            branch_code=brn,
            group_code=gcode,
            by_branch=True,
        )
        if brn:
            rows = [r for r in rows if str(r.get('branch_code') or '') == brn]
        return rows

    rows = fetch_branch_sales_totals(date_from, date_to, system=system)
    if brn:
        rows = [r for r in rows if str(r.get('branch_code') or '') == brn]
    return rows


def _load_period_bundle(
    date_from,
    date_to,
    system: str,
    branch_code: str = '',
    group_code: str = '',
) -> dict[str, Any]:
    from .oracle_stock import fetch_branch_return_totals, fetch_top_returned_items

    brn = str(branch_code or '').strip()
    gcode = str(group_code or '').strip()
    sales_rows = _load_sales_rows(date_from, date_to, system, brn, gcode)
    return_rows = fetch_branch_return_totals(
        date_from,
        date_to,
        system=system,
        branch_code=brn,
        group_code=gcode,
        limit=40,
    )
    top_items = fetch_top_returned_items(
        date_from,
        date_to,
        system=system,
        branch_code=brn,
        group_code=gcode,
        limit=8,
    )
    sales, inv = _sum_sales(sales_rows)
    ret_amt, ret_cnt = _sum_returns(return_rows)
    return {
        'sales_rows': sales_rows,
        'return_rows': return_rows,
        'top_items': top_items,
        'sales': sales,
        'invoices': inv,
        'returns': ret_amt,
        'return_count': ret_cnt,
        'avg_basket': round(sales / inv, 2) if inv else 0.0,
        'return_rate': round((ret_amt / sales) * 100.0, 2) if sales else 0.0,
    }


def _build_compare_table(
    cur_rows: list[dict],
    prior_rows: list[dict],
    cur_returns: list[dict],
    prior_returns: list[dict],
) -> list[dict]:
    """جدول مقارنة صف بصف (فرع) بين فترتين."""
    prior_map = {str(r.get('branch_code') or ''): r for r in prior_rows}
    cur_ret_map = {str(r.get('branch_code') or ''): r for r in cur_returns}
    prior_ret_map = {str(r.get('branch_code') or ''): r for r in prior_returns}
    codes = set(prior_map) | {
        str(r.get('branch_code') or '') for r in cur_rows if r.get('branch_code')
    }
    out: list[dict] = []
    for code in codes:
        if not code:
            continue
        cur = next(
            (r for r in cur_rows if str(r.get('branch_code') or '') == code),
            None,
        ) or {}
        prev = prior_map.get(code) or {}
        cur_sales = float(cur.get('sales_total') or 0)
        prev_sales = float(prev.get('sales_total') or 0)
        cur_inv = int(cur.get('invoice_count') or 0)
        prev_inv = int(prev.get('invoice_count') or 0)
        cur_ret = float(
            (cur_ret_map.get(code) or {}).get('return_total')
            or (cur_ret_map.get(code) or {}).get('sales_total')
            or 0
        )
        prev_ret = float(
            (prior_ret_map.get(code) or {}).get('return_total')
            or (prior_ret_map.get(code) or {}).get('sales_total')
            or 0
        )
        sales_delta = _pct_change(cur_sales, prev_sales)
        name = str(
            cur.get('branch_name')
            or prev.get('branch_name')
            or (cur_ret_map.get(code) or {}).get('branch_name')
            or code
        )
        cur_rate = round((cur_ret / cur_sales) * 100.0, 2) if cur_sales else 0.0
        out.append(
            {
                'branch_code': code,
                'branch_name': name,
                'sales_a': cur_sales,
                'sales_a_display': _fmt_money(cur_sales),
                'sales_b': prev_sales,
                'sales_b_display': _fmt_money(prev_sales),
                'sales_delta': sales_delta,
                'sales_delta_display': _fmt_pct(sales_delta),
                'inv_a': cur_inv,
                'inv_a_display': _fmt_int(cur_inv),
                'inv_b': prev_inv,
                'inv_b_display': _fmt_int(prev_inv),
                'inv_delta_display': _fmt_pct(_pct_change(float(cur_inv), float(prev_inv))),
                'ret_a': cur_ret,
                'ret_a_display': _fmt_money(cur_ret),
                'ret_b': prev_ret,
                'ret_b_display': _fmt_money(prev_ret),
                'ret_delta_display': _fmt_pct(_pct_change(cur_ret, prev_ret)),
                'return_rate_a': cur_rate,
                'return_rate_a_display': f'{cur_rate:.1f}%',
            }
        )
    out.sort(
        key=lambda r: (
            r['sales_delta'] is None,
            r['sales_delta'] if r['sales_delta'] is not None else 0,
            -r['sales_a'],
        )
    )
    return out


def _build_group_compare_table(
    cur_rows: list[dict],
    prior_rows: list[dict],
    cur_returns: list[dict],
    prior_returns: list[dict],
) -> list[dict]:
    """جدول مقارنة المجموعات بين فترتين."""
    prior_map = {str(r.get('group_code') or ''): r for r in prior_rows}
    cur_ret_map = {str(r.get('group_code') or ''): r for r in cur_returns}
    codes = set(prior_map) | {
        str(r.get('group_code') or '') for r in cur_rows if r.get('group_code')
    } | set(cur_ret_map)
    out: list[dict] = []
    for code in codes:
        if not code:
            continue
        cur = next(
            (r for r in cur_rows if str(r.get('group_code') or '') == code),
            None,
        ) or {}
        prev = prior_map.get(code) or {}
        cur_sales = float(cur.get('sales_total') or 0)
        prev_sales = float(prev.get('sales_total') or 0)
        cur_inv = int(cur.get('invoice_count') or 0)
        prev_inv = int(prev.get('invoice_count') or 0)
        cur_ret = float(
            (cur_ret_map.get(code) or {}).get('return_total')
            or (cur_ret_map.get(code) or {}).get('sales_total')
            or 0
        )
        sales_delta = _pct_change(cur_sales, prev_sales)
        name = str(
            cur.get('group_name')
            or prev.get('group_name')
            or (cur_ret_map.get(code) or {}).get('group_name')
            or code
        )
        cur_rate = round((cur_ret / cur_sales) * 100.0, 2) if cur_sales else 0.0
        avg_a = round(cur_sales / cur_inv, 2) if cur_inv else 0.0
        out.append(
            {
                'group_code': code,
                'group_name': name,
                'sales_a': cur_sales,
                'sales_a_display': _fmt_money(cur_sales),
                'sales_b': prev_sales,
                'sales_b_display': _fmt_money(prev_sales),
                'sales_delta': sales_delta,
                'sales_delta_display': _fmt_pct(sales_delta),
                'inv_a': cur_inv,
                'inv_a_display': _fmt_int(cur_inv),
                'inv_b': prev_inv,
                'inv_b_display': _fmt_int(prev_inv),
                'inv_delta_display': _fmt_pct(_pct_change(float(cur_inv), float(prev_inv))),
                'ret_a': cur_ret,
                'ret_a_display': _fmt_money(cur_ret),
                'return_rate_a': cur_rate,
                'return_rate_a_display': f'{cur_rate:.1f}%',
                'avg_a_display': _fmt_money(avg_a),
            }
        )
    out.sort(
        key=lambda r: (
            r['sales_delta'] is None,
            r['sales_delta'] if r['sales_delta'] is not None else 0,
            -r['sales_a'],
        )
    )
    return out


def _load_group_period_rows(
    date_from,
    date_to,
    system: str,
    branch_code: str = '',
    group_code: str = '',
) -> tuple[list[dict], list[dict]]:
    from .oracle_stock import fetch_group_return_totals, fetch_group_sales_totals

    brn = str(branch_code or '').strip()
    gcode = str(group_code or '').strip()
    sales_rows = fetch_group_sales_totals(
        date_from,
        date_to,
        system=system,
        branch_code=brn,
        group_code=gcode,
        by_branch=False,
    )
    return_rows = fetch_group_return_totals(
        date_from,
        date_to,
        system=system,
        branch_code=brn,
        group_code=gcode,
        limit=80,
    )
    return sales_rows, return_rows


def build_performance_insights(
    date_from: date,
    date_to: date,
    system: str = 'pos',
    branch_code: str = '',
    group_code: str = '',
    compare_from: date | None = None,
    compare_to: date | None = None,
) -> dict[str, Any]:
    """يبني قراءة قرار قابلة للفلترة والمقارنة."""
    brn = str(branch_code or '').strip()
    gcode = str(group_code or '').strip()

    if compare_from and compare_to:
        prior_from, prior_to = compare_from, compare_to
        compare_mode = 'custom'
    else:
        prior_from, prior_to = _prior_period(date_from, date_to)
        compare_mode = 'auto'

    cur = _load_period_bundle(date_from, date_to, system, brn, gcode)
    prior = _load_period_bundle(prior_from, prior_to, system, brn, gcode)

    cur_sales, cur_inv = cur['sales'], cur['invoices']
    prior_sales, prior_inv = prior['sales'], prior['invoices']
    cur_ret_amt = cur['returns']
    prior_ret_amt = prior['returns']

    sales_delta = _pct_change(cur_sales, prior_sales)
    inv_delta = _pct_change(float(cur_inv), float(prior_inv))
    ret_delta = _pct_change(cur_ret_amt, prior_ret_amt)
    return_rate = cur['return_rate']
    prior_return_rate = prior['return_rate']
    avg_basket = cur['avg_basket']
    basket_delta = _pct_change(avg_basket, prior['avg_basket'])

    ranked_sales = sorted(
        cur['sales_rows'],
        key=lambda r: float(r.get('sales_total') or 0),
        reverse=True,
    )
    top_share = 0.0
    if ranked_sales and cur_sales and not brn:
        top_share = round(
            float(ranked_sales[0].get('sales_total') or 0) / cur_sales * 100.0, 1
        )
    elif brn and cur_sales:
        top_share = 100.0

    health = _health_score(sales_delta, return_rate, top_share, inv_delta)
    compare_table = _build_compare_table(
        cur['sales_rows'],
        prior['sales_rows'],
        cur['return_rows'],
        prior['return_rows'],
    )

    cur_groups, cur_group_rets = _load_group_period_rows(
        date_from, date_to, system, brn, gcode
    )
    prior_groups, prior_group_rets = _load_group_period_rows(
        prior_from, prior_to, system, brn, gcode
    )
    group_compare_table = _build_group_compare_table(
        cur_groups,
        prior_groups,
        cur_group_rets,
        prior_group_rets,
    )

    # يومي للفترة أ (بدون فلتر مجموعة — يعتمد رؤوس الفواتير)
    daily_table: list[dict] = []
    daily_note = ''
    if gcode:
        daily_note = 'الجدول اليومي لا يدعم فلتر المجموعة حالياً — أزل المجموعة لعرض الأيام.'
    else:
        from .oracle_stock import fetch_daily_sales_totals

        daily_rows = fetch_daily_sales_totals(
            date_from, date_to, system=system, branch_code=brn
        )
        for row in daily_rows:
            sales = float(row.get('sales_total') or 0)
            inv = int(row.get('invoice_count') or 0)
            ret_c = int(row.get('return_count') or 0)
            # تقدير المرتجع من صافي التعديل غير متاح هنا كقيمة منفصلة دائماً
            # نعرض عدد المرتجعات والمتاح من الصف
            daily_table.append(
                {
                    'day': str(row.get('day') or ''),
                    'day_display': str(row.get('day_display') or row.get('day') or ''),
                    'sales': sales,
                    'inv': inv,
                    'ret_count': ret_c,
                    'gross': float(row.get('gross_total') or 0),
                    'sales_display': _fmt_money(sales),
                    'inv_display': _fmt_int(inv),
                    'ret_count_display': _fmt_int(ret_c),
                    'avg_display': _fmt_money(float(row.get('avg_basket') or 0)),
                    'gross_display': _fmt_money(float(row.get('gross_total') or 0)),
                }
            )
        daily_table.sort(key=lambda r: r['day'])

    # أصناف: مبيعات ومرتجعات مع مقارنة أ/ب
    from .oracle_stock import fetch_top_returned_items, fetch_top_sales_items

    cur_sale_items = fetch_top_sales_items(
        date_from, date_to, system=system, branch_code=brn, group_code=gcode, limit=12
    )
    prior_sale_items = fetch_top_sales_items(
        prior_from, prior_to, system=system, branch_code=brn, group_code=gcode, limit=30
    )
    prior_sale_map = {
        str(r.get('item_code') or ''): r for r in prior_sale_items if r.get('item_code')
    }
    sales_items_table: list[dict] = []
    for row in cur_sale_items:
        code = str(row.get('item_code') or '')
        cur_amt = float(row.get('sales_total') or 0)
        prev = prior_sale_map.get(code) or {}
        prev_amt = float(prev.get('sales_total') or 0)
        delta = _pct_change(cur_amt, prev_amt)
        sales_items_table.append(
            {
                'item_code': code,
                'item_name': str(row.get('item_name') or code),
                'sales_a': cur_amt,
                'sales_b': prev_amt,
                'sales_a_display': _fmt_money(cur_amt),
                'sales_b_display': _fmt_money(prev_amt),
                'sales_delta': delta,
                'sales_delta_display': _fmt_pct(delta),
                'qty_a_display': _fmt_money(float(row.get('qty_total') or 0)),
                'inv_a_display': _fmt_int(int(row.get('invoice_count') or 0)),
            }
        )

    cur_ret_items = fetch_top_returned_items(
        date_from, date_to, system=system, branch_code=brn, group_code=gcode, limit=12
    )
    prior_ret_items = fetch_top_returned_items(
        prior_from, prior_to, system=system, branch_code=brn, group_code=gcode, limit=30
    )
    prior_ret_map = {
        str(r.get('item_code') or ''): r for r in prior_ret_items if r.get('item_code')
    }
    return_items_table: list[dict] = []
    for row in cur_ret_items:
        code = str(row.get('item_code') or '')
        cur_amt = float(row.get('return_total') or row.get('sales_total') or 0)
        prev = prior_ret_map.get(code) or {}
        prev_amt = float(prev.get('return_total') or prev.get('sales_total') or 0)
        delta = _pct_change(cur_amt, prev_amt)
        return_items_table.append(
            {
                'item_code': code,
                'item_name': str(row.get('item_name') or code),
                'ret_a': cur_amt,
                'ret_b': prev_amt,
                'ret_a_display': _fmt_money(cur_amt),
                'ret_b_display': _fmt_money(prev_amt),
                'ret_delta': delta,
                'ret_delta_display': _fmt_pct(delta),
                'qty_a_display': _fmt_money(float(row.get('qty_total') or 0)),
            }
        )

    movers_down = [
        {
            'branch_code': r['branch_code'],
            'branch_name': r['branch_name'],
            'sales_total': r['sales_a'],
            'sales_total_display': r['sales_a_display'],
            'delta_pct': r['sales_delta'],
            'delta_display': r['sales_delta_display'],
        }
        for r in compare_table
        if r['sales_delta'] is not None and r['sales_delta'] <= -12
    ][:8]
    movers_up = [
        {
            'branch_code': r['branch_code'],
            'branch_name': r['branch_name'],
            'sales_total': r['sales_a'],
            'sales_total_display': r['sales_a_display'],
            'delta_pct': r['sales_delta'],
            'delta_display': r['sales_delta_display'],
        }
        for r in compare_table
        if r['sales_delta'] is not None and r['sales_delta'] >= 12 and r['sales_a'] > 0
    ]
    movers_up.sort(key=lambda x: -(x['delta_pct'] or 0))
    movers_up = movers_up[:8]

    high_return_branches: list[dict] = []
    for row in cur['return_rows']:
        code = str(row.get('branch_code') or '')
        ret_amt = float(row.get('return_total') or row.get('sales_total') or 0)
        cur_br = next(
            (b for b in cur['sales_rows'] if str(b.get('branch_code') or '') == code),
            None,
        )
        sales_amt = float((cur_br or {}).get('sales_total') or 0)
        rate = round((ret_amt / sales_amt) * 100.0, 2) if sales_amt else 0.0
        if rate < 3 and ret_amt < (cur_sales * 0.015 if cur_sales else 0):
            continue
        high_return_branches.append(
            {
                'branch_code': code,
                'branch_name': str(row.get('branch_name') or code),
                'return_total': ret_amt,
                'return_total_display': _fmt_money(ret_amt),
                'return_rate': rate,
                'return_rate_display': f'{rate:.1f}%',
            }
        )
    high_return_branches.sort(key=lambda x: (-x['return_rate'], -x['return_total']))

    scope_bits = []
    if brn:
        scope_bits.append(
            next(
                (
                    str(r.get('branch_name') or brn)
                    for r in (cur['sales_rows'] or compare_table)
                    if str(r.get('branch_code') or '') == brn
                ),
                brn,
            )
        )
    if gcode:
        scope_bits.append(f'مجموعة {gcode}')
    scope_label = ' · '.join(scope_bits) if scope_bits else 'كل الفروع'

    alerts: list[dict] = []
    if sales_delta is not None and sales_delta <= -10:
        alerts.append(
            {
                'severity': 'high',
                'title': 'هبوط في المبيعات ضمن الفلتر',
                'detail': f'انخفاض {_fmt_pct(sales_delta)} مقابل فترة المقارنة.',
                'action': 'راجع صفوف المقارنة السالبة وحدد سبب الهبوط.',
            }
        )
    if return_rate >= 5:
        alerts.append(
            {
                'severity': 'high',
                'title': 'نسبة مرتجع مرتفعة',
                'detail': f'المرتجع {return_rate:.1f}% من مبيعات النطاق المحدد.',
                'action': 'فلتر أصناف المرتجع لنفس الفرع/المجموعة.',
            }
        )
    elif return_rate >= 3:
        alerts.append(
            {
                'severity': 'medium',
                'title': 'نسبة مرتجع تحتاج مراقبة',
                'detail': f'المرتجع عند {return_rate:.1f}%.',
                'action': 'تابع أعلى أصناف الإرجاع في هذا النطاق.',
            }
        )
    for br in movers_down[:3]:
        alerts.append(
            {
                'severity': 'high' if (br['delta_pct'] or 0) <= -20 else 'medium',
                'title': f'تراجع: {br["branch_name"]}',
                'detail': f'انخفاض {br["delta_display"]} بين الفترتين.',
                'action': f'قارن يومياً حركة {br["branch_name"]}.',
            }
        )
    for br in high_return_branches[:2]:
        alerts.append(
            {
                'severity': 'medium',
                'title': f'مرتجع: {br["branch_name"]}',
                'detail': f'{br["return_rate_display"]} · {br["return_total_display"]}',
                'action': f'افحص أصناف مرتجع {br["branch_name"]}.',
            }
        )
    for item in cur['top_items'][:2]:
        name = str(item.get('item_name') or item.get('item_code') or '')
        amt = float(item.get('return_total') or item.get('sales_total') or 0)
        if amt <= 0:
            continue
        alerts.append(
            {
                'severity': 'medium',
                'title': f'صنف مرتجع: {name}',
                'detail': f'قيمة {_fmt_money(amt)} ضمن الفلتر الحالي.',
                'action': 'راجع الجودة والتوريد لهذا الصنف.',
            }
        )
    if top_share >= 50 and ranked_sales and not brn:
        alerts.append(
            {
                'severity': 'low',
                'title': 'تركّز على فرع واحد',
                'detail': f'{ranked_sales[0].get("branch_name")} = {top_share:.0f}% من النطاق.',
                'action': 'وسّع الدعم لبقية الفروع.',
            }
        )
    sev_rank = {'high': 0, 'medium': 1, 'low': 2}
    alerts.sort(key=lambda a: sev_rank.get(a['severity'], 9))
    alerts = alerts[:8]

    actions = [
        {
            'text': a['action'],
            'from_alert': a['title'],
            'severity': a['severity'],
        }
        for a in alerts[:5]
    ]
    if movers_up:
        best = movers_up[0]
        actions.append(
            {
                'text': (
                    f'عزّز {best["branch_name"]} (نمو {best["delta_display"]}) '
                    'وانقل التجربة للفروع المتراجعة.'
                ),
                'from_alert': 'فرصة نمو',
                'severity': 'low',
            }
        )
    if not actions:
        actions.append(
            {
                'text': 'لا إشارات حرجة ضمن هذا الفلتر — جرّب توسيع/تضييق النطاق للمقارنة.',
                'from_alert': 'وضع مستقر',
                'severity': 'low',
            }
        )
    actions = actions[:6]

    best_branch = ranked_sales[0] if ranked_sales else None
    worst_drop = movers_down[0] if movers_down else None
    top_ret_branch = high_return_branches[0] if high_return_branches else (
        cur['return_rows'][0] if cur['return_rows'] else None
    )

    return {
        'system': system,
        'scope_label': scope_label,
        'branch_code': brn,
        'group_code': gcode,
        'compare_mode': compare_mode,
        'date_from': date_from.isoformat(),
        'date_to': date_to.isoformat(),
        'prior_from': prior_from.isoformat(),
        'prior_to': prior_to.isoformat(),
        'period_a_label': f'{date_from.isoformat()} → {date_to.isoformat()}',
        'period_b_label': f'{prior_from.isoformat()} → {prior_to.isoformat()}',
        'health': health,
        'kpis': {
            'sales': _fmt_money(cur_sales),
            'sales_num': cur_sales,
            'sales_b': _fmt_money(prior_sales),
            'sales_delta': sales_delta,
            'sales_delta_display': _fmt_pct(sales_delta),
            'invoices': _fmt_int(cur_inv),
            'invoices_b': _fmt_int(prior_inv),
            'invoices_delta': inv_delta,
            'invoices_delta_display': _fmt_pct(inv_delta),
            'returns': _fmt_money(cur_ret_amt),
            'returns_b': _fmt_money(prior_ret_amt),
            'returns_delta': ret_delta,
            'returns_delta_display': _fmt_pct(ret_delta),
            'return_rate': return_rate,
            'return_rate_display': f'{return_rate:.1f}%',
            'prior_return_rate_display': f'{prior_return_rate:.1f}%',
            'avg_basket': _fmt_money(avg_basket),
            'avg_basket_b': _fmt_money(prior['avg_basket']),
            'avg_basket_delta_display': _fmt_pct(basket_delta),
            'prior_sales': _fmt_money(prior_sales),
            'prior_invoices': _fmt_int(prior_inv),
            'branch_count': len(cur['sales_rows']) or (1 if brn else 0),
            'top_share': top_share,
            'top_share_display': f'{top_share:.0f}%',
        },
        'compare_summary': [
            {
                'metric': 'المبيعات',
                'a': _fmt_money(cur_sales),
                'b': _fmt_money(prior_sales),
                'delta': _fmt_pct(sales_delta),
                'delta_num': sales_delta,
            },
            {
                'metric': 'الفواتير',
                'a': _fmt_int(cur_inv),
                'b': _fmt_int(prior_inv),
                'delta': _fmt_pct(inv_delta),
                'delta_num': inv_delta,
            },
            {
                'metric': 'المرتجع',
                'a': _fmt_money(cur_ret_amt),
                'b': _fmt_money(prior_ret_amt),
                'delta': _fmt_pct(ret_delta),
                'delta_num': ret_delta,
            },
            {
                'metric': 'نسبة المرتجع',
                'a': f'{return_rate:.1f}%',
                'b': f'{prior_return_rate:.1f}%',
                'delta': _fmt_pct(_pct_change(return_rate, prior_return_rate)),
                'delta_num': _pct_change(return_rate, prior_return_rate),
            },
            {
                'metric': 'متوسط السلة',
                'a': _fmt_money(avg_basket),
                'b': _fmt_money(prior['avg_basket']),
                'delta': _fmt_pct(basket_delta),
                'delta_num': basket_delta,
            },
        ],
        'compare_table': compare_table[:25],
        'compare_totals': _branch_table_totals(compare_table[:25]),
        'group_compare_table': group_compare_table[:40],
        'group_totals': _group_table_totals(group_compare_table[:40]),
        'daily_table': daily_table[:62],
        'daily_totals': _daily_table_totals(daily_table[:62]),
        'daily_note': daily_note,
        'sales_items_table': sales_items_table[:12],
        'sales_items_totals': _items_sales_totals(sales_items_table[:12]),
        'return_items_table': return_items_table[:12],
        'return_items_totals': _items_return_totals(return_items_table[:12]),
        'highlights': {
            'best_branch': {
                'name': str((best_branch or {}).get('branch_name') or '—'),
                'value': _fmt_money(float((best_branch or {}).get('sales_total') or 0)),
            },
            'worst_drop': {
                'name': str((worst_drop or {}).get('branch_name') or '—'),
                'value': (worst_drop or {}).get('delta_display') or '—',
            },
            'top_return_branch': {
                'name': str((top_ret_branch or {}).get('branch_name') or '—'),
                'value': _fmt_money(
                    float(
                        (top_ret_branch or {}).get('return_total')
                        or (top_ret_branch or {}).get('sales_total')
                        or 0
                    )
                ),
            },
        },
        'movers_down': movers_down[:5],
        'movers_up': movers_up[:5],
        'high_return_branches': high_return_branches[:5],
        'alerts': alerts,
        'actions': actions,
    }
