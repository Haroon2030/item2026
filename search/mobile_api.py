"""واجهة JSON لتطبيق الموبايل — نفس حساب الموقع ونفس أرقام تحليل المبيعات."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import timedelta
from functools import wraps
from typing import Any, Callable

from django.contrib.auth import authenticate
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import MobileAuthToken
from .validators import ValidationError

logger = logging.getLogger(__name__)

_TOKEN_DAYS = 180
_MAX_TOKENS_PER_USER = 8


def _json_error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({'ok': False, 'error': message}, status=status)


def _read_json(request) -> dict[str, Any]:
    raw = (request.body or b'').decode('utf-8', errors='replace').strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _user_payload(user) -> dict[str, Any]:
    profile = getattr(user, 'profile', None)
    display_name = (
        ((profile.display_name if profile else '') or '').strip()
        or (user.first_name or '').strip()
        or (user.username or '').strip()
        or 'مستخدم'
    )
    role_name = ((profile.role_name if profile else '') or '').strip()
    if not role_name:
        role_name = 'مدير النظام' if user.is_staff else 'مستخدم'
    return {
        'username': user.username,
        'display_name': display_name,
        'role_name': role_name,
        'is_staff': bool(user.is_staff),
    }


def _bearer_raw(request) -> str:
    header = request.META.get('HTTP_AUTHORIZATION') or ''
    if header.lower().startswith('bearer '):
        return header[7:].strip()
    return ''


def _user_from_bearer(request):
    raw = _bearer_raw(request)
    if not raw:
        return None
    now = timezone.now()
    token = (
        MobileAuthToken.objects.select_related('user', 'user__profile')
        .filter(key_hash=_hash_token(raw), expires_at__gt=now)
        .first()
    )
    if token is None:
        return None
    user = token.user
    if not user.is_active:
        token.delete()
        return None
    token.last_used_at = now
    token.save(update_fields=['last_used_at'])
    return user


def _issue_token(user) -> str:
    now = timezone.now()
    MobileAuthToken.objects.filter(user=user, expires_at__lte=now).delete()
    extras = list(
        MobileAuthToken.objects.filter(user=user)
        .order_by('-created_at')
        .values_list('pk', flat=True)[_MAX_TOKENS_PER_USER - 1 :]
    )
    if extras:
        MobileAuthToken.objects.filter(pk__in=extras).delete()
    raw = secrets.token_urlsafe(32)
    MobileAuthToken.objects.create(
        user=user,
        key_hash=_hash_token(raw),
        expires_at=now + timedelta(days=_TOKEN_DAYS),
    )
    return raw


def mobile_auth(view: Callable):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        user = _user_from_bearer(request)
        if user is None:
            return _json_error('انتهت الجلسة. سجّل الدخول مجدداً.', 401)
        request.user = user
        return view(request, *args, **kwargs)

    return wrapper


def _branch_row(row: dict) -> dict[str, Any]:
    return {
        'code': str(row.get('branch_code') or ''),
        'name': str(row.get('branch_name') or row.get('branch_code') or '—'),
        'sales': float(row.get('sales_total') or 0),
        'sales_display': str(row.get('sales_total_display') or '0.00'),
        'invoices': int(row.get('invoice_count') or 0),
        'invoices_display': str(row.get('invoice_count_display') or '0'),
        'returns_display': str(row.get('return_total_display') or '0.00'),
        'avg_basket_display': str(row.get('avg_basket_display') or '0.00'),
        'share_pct': float(row.get('share_pct') or 0),
        'share_display': str(row.get('share_display') or '0%'),
        'no_sales': bool(row.get('no_sales')),
    }


def _rank_row(row: dict | None) -> dict[str, Any]:
    data = row or {}
    return {
        'title': str(data.get('title') or ''),
        'name': str(data.get('name') or '—'),
        'value_display': str(data.get('value_display') or '—'),
        'hint': str(data.get('hint') or ''),
    }


def _daily_payload(dash: dict, date_from, date_to) -> dict[str, Any]:
    kpis = dash.get('kpis') or {}
    pos = dash.get('pos') or {}
    wholesale = dash.get('wholesale') or {}
    ranks = dash.get('ranks') or {}
    pos_totals = pos.get('totals') or {}
    return {
        'date_from': date_from.isoformat(),
        'date_to': date_to.isoformat(),
        'period_label': str(dash.get('period_label') or ''),
        'scope_label': str(dash.get('scope_label') or ''),
        'from_cache': bool(dash.get('from_cache')),
        'kpis': {
            'pos_sales_display': str(kpis.get('pos_sales') or '0.00'),
            'pos_invoices_display': str(kpis.get('pos_invoices') or '0'),
            'pos_returns_display': str(kpis.get('pos_returns') or '0.00'),
            'pos_branches': int(pos_totals.get('branch_count') or 0),
            'wholesale_sales_display': str(kpis.get('wholesale_sales') or '0.00'),
            'wholesale_invoices_display': str(
                kpis.get('wholesale_invoices') or '0'
            ),
            'onix_sales_display': str(kpis.get('onix_sales') or '0.00'),
            'combined_sales_display': str(kpis.get('combined_sales') or '0.00'),
            'combined_invoices_display': str(
                kpis.get('combined_invoices') or '0'
            ),
        },
        'pos_branches': [
            _branch_row(row) for row in (pos.get('branches') or [])
        ],
        'wholesale_branches': [
            _branch_row(row) for row in (wholesale.get('branches') or [])
        ],
        'ranks': {
            'top_visit_branch': _rank_row(ranks.get('top_visit_branch')),
            'top_sales_branch': _rank_row(ranks.get('top_sales_branch')),
            'top_return_branch': _rank_row(ranks.get('top_return_branch')),
        },
    }


@csrf_exempt
@require_POST
@never_cache
def mobile_login(request):
    body = _read_json(request)
    username = str(body.get('username') or '').strip()
    password = str(body.get('password') or '')
    if not username or not password:
        return _json_error('أدخل اسم المستخدم وكلمة المرور.')
    user = authenticate(request=request, username=username, password=password)
    if user is None:
        return _json_error('بيانات الدخول غير صحيحة.', 401)
    token = _issue_token(user)
    return JsonResponse(
        {'ok': True, 'token': token, 'user': _user_payload(user)}
    )


@csrf_exempt
@require_POST
@never_cache
def mobile_logout(request):
    raw = _bearer_raw(request)
    if raw:
        MobileAuthToken.objects.filter(key_hash=_hash_token(raw)).delete()
    return JsonResponse({'ok': True})


@csrf_exempt
@require_GET
@never_cache
@mobile_auth
def mobile_me(request):
    return JsonResponse({'ok': True, 'user': _user_payload(request.user)})


@csrf_exempt
@require_GET
@never_cache
@mobile_auth
def mobile_filters(request):
    from .oracle_stock import (
        fetch_sales_group_options,
        fetch_warehouse_options,
        oracle_enabled,
        oracle_session,
    )

    branches: list[dict] = []
    groups: list[dict] = []
    if oracle_enabled():
        try:
            with oracle_session():
                warehouses = fetch_warehouse_options(active_only=True)
                groups_raw = fetch_sales_group_options()
            branch_map: dict[str, str] = {}
            for w in warehouses:
                brn = str(w.get('branch_code') or '').strip()
                if brn:
                    branch_map.setdefault(
                        brn, str(w.get('branch_name') or brn)
                    )
            branches = [
                {'code': code, 'name': name}
                for code, name in sorted(
                    branch_map.items(), key=lambda x: (x[1], x[0])
                )
            ]
            groups = [
                {
                    'code': str(g.get('code') or ''),
                    'name': str(g.get('name') or g.get('code') or ''),
                }
                for g in groups_raw
                if str(g.get('code') or '').strip()
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning('mobile filters failed: %s', exc)
    return JsonResponse({'ok': True, 'branches': branches, 'groups': groups})


def _sales_scope(request):
    from .views import _parse_sales_dates

    date_from, date_to = _parse_sales_dates(
        request.GET.get('date_from'),
        request.GET.get('date_to'),
    )
    branch = str(request.GET.get('branch') or '').strip()
    group = str(request.GET.get('group') or '').strip()
    return date_from, date_to, branch, group


@csrf_exempt
@require_GET
@never_cache
@mobile_auth
def mobile_sales_daily(request):
    try:
        date_from, date_to, branch, group = _sales_scope(request)
    except ValidationError as exc:
        return _json_error(str(exc))

    from .oracle_stock import oracle_enabled
    from .sales_dashboard import (
        build_sales_branches,
        build_sales_branches_from_cache,
    )

    dash = None
    try:
        if not oracle_enabled():
            raise RuntimeError('أوراكل غير مفعّل.')
        dash = build_sales_branches(
            date_from,
            date_to,
            branch_code=branch,
            group_code=group,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning('mobile daily sales failed: %s', exc)
        dash = build_sales_branches_from_cache(
            date_from,
            date_to,
            branch_code=branch,
            group_code=group,
        )
        if dash is None:
            return _json_error('تعذّر جلب المبيعات اليومية.', 503)
        dash['from_cache'] = True
    return JsonResponse(
        {'ok': True, 'daily': _daily_payload(dash, date_from, date_to)}
    )


@csrf_exempt
@require_GET
@never_cache
@mobile_auth
def mobile_sales_groups(request):
    try:
        date_from, date_to, branch, group = _sales_scope(request)
    except ValidationError as exc:
        return _json_error(str(exc))

    from .oracle_stock import oracle_enabled
    from .sales_dashboard import build_sales_groups

    empty = {
        'rows': [],
        'totals': {
            'invoice_count_display': '0',
            'qty_display': '0',
            'sales_total_display': '0.00',
            'group_count_display': '0',
        },
        'warning': '',
    }
    try:
        if not oracle_enabled():
            empty['warning'] = 'أوراكل غير مفعّل.'
            return JsonResponse({'ok': True, 'groups': empty})
        payload = build_sales_groups(
            date_from,
            date_to,
            branch_code=branch,
            group_code=group,
            reconcile=True,
        )
        return JsonResponse({'ok': True, 'groups': payload})
    except Exception as exc:  # noqa: BLE001
        logger.warning('mobile group sales failed: %s', exc)
        empty['warning'] = str(exc) or 'تعذّر جلب مبيعات المجموعات.'
        return JsonResponse({'ok': True, 'groups': empty})
