"""إدارة مستخدمي التطبيق: إضافة / تعديل / حذف."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .forms import AppUserForm
from .models import UserActivitySession, UserNavPermission
from .nav_permissions import (
    SECTION_KEYS,
    NAV_SECTIONS,
    is_executive_role,
    is_section_manager_role,
    parse_permission_post,
    permission_form_catalog,
    preset_sections_for_role,
)

User = get_user_model()


def _is_staff(user) -> bool:
    return bool(user.is_authenticated and user.is_staff)


def _user_rows(request):
    users = list(User.objects.select_related('profile', 'nav_permission').all())

    def _sort_key(user):
        profile = getattr(user, 'profile', None)
        name = (
            (profile.display_name if profile else '')
            or user.first_name
            or user.username
            or ''
        ).strip()
        # أنت أولاً · ثم المدراء · ثم حسب الاسم
        return (
            0 if user.pk == request.user.pk else 1,
            0 if user.is_staff else 1,
            name.casefold(),
            user.username or '',
        )

    users.sort(key=_sort_key)
    rows = []
    for user in users:
        profile = getattr(user, 'profile', None)
        role_name = ((profile.role_name if profile else '') or '').strip()
        if not role_name:
            role_name = 'مدير النظام' if user.is_staff else 'مستخدم'
        is_exec = is_executive_role(user)
        is_mgr = is_section_manager_role(user)
        section_count = len(SECTION_KEYS) if (user.is_staff or is_exec) else 0
        if not user.is_staff and not is_exec:
            try:
                perm = user.nav_permission
            except UserNavPermission.DoesNotExist:
                perm = None
            if perm is None:
                preset = preset_sections_for_role(role_name)
                section_count = len(preset) if preset is not None else len(SECTION_KEYS)
            else:
                section_count = len(
                    [k for k in (perm.sections or []) if k in SECTION_KEYS]
                )
        rows.append(
            {
                'id': user.pk,
                'name': (profile.display_name if profile else '')
                or user.first_name
                or user.username,
                'phone': (profile.phone if profile else '') or user.username,
                'role_name': role_name,
                'is_self': user.pk == request.user.pk,
                'is_staff': user.is_staff,
                'is_executive': is_exec,
                'is_section_manager': is_mgr,
                'section_count': section_count,
            }
        )
    return rows


@login_required
@user_passes_test(_is_staff)
def user_list(request):
    return render(
        request,
        'search/users.html',
        {
            'users': _user_rows(request),
            'form': AppUserForm(),
            'editing': None,
        },
    )


@login_required
@user_passes_test(_is_staff)
@require_http_methods(['GET', 'POST'])
def user_create(request):
    if request.method == 'GET':
        return redirect('user_list')
    form = AppUserForm(request.POST)
    if form.is_valid():
        form.save()
        messages.success(request, 'تمت إضافة المستخدم بنجاح.')
        return redirect('user_list')
    return render(
        request,
        'search/users.html',
        {'users': _user_rows(request), 'form': form, 'editing': None},
    )


@login_required
@user_passes_test(_is_staff)
@require_http_methods(['GET', 'POST'])
def user_edit(request, user_id: int):
    target = get_object_or_404(User.objects.select_related('profile'), pk=user_id)
    form = AppUserForm(request.POST or None, instance=target)
    if request.method == 'POST' and form.is_valid():
        password_changed = bool(form.cleaned_data.get('password'))
        user = form.save()
        # إن غيّرت كلمة سر حسابك الحالي أبقِ الجلسة فعّالة
        if password_changed and user.pk == request.user.pk:
            update_session_auth_hash(request, user)
        messages.success(
            request,
            'تم حفظ التعديل. يمكن الدخول بالاسم أو الرقم مع كلمة السر.',
        )
        return redirect('user_list')
    return render(
        request,
        'search/users.html',
        {'users': _user_rows(request), 'form': form, 'editing': target},
    )


@login_required
@user_passes_test(_is_staff)
@require_POST
def user_delete(request, user_id: int):
    target = get_object_or_404(User, pk=user_id)
    if target.pk == request.user.pk:
        messages.error(request, 'لا يمكن حذف حسابك الحالي.')
        return redirect('user_list')
    if target.is_superuser and not request.user.is_superuser:
        messages.error(request, 'لا تملك صلاحية حذف هذا المستخدم.')
        return redirect('user_list')
    name = target.first_name or target.username
    target.delete()
    messages.success(request, f'تم حذف المستخدم «{name}».')
    return redirect('user_list')


def _parse_activity_date(raw: str, fallback):
    try:
        return datetime.strptime((raw or '').strip(), '%Y-%m-%d').date()
    except ValueError:
        return fallback


def _format_when(value) -> str:
    if not value:
        return ''
    return timezone.localtime(value).strftime('%Y-%m-%d %H:%M')


def _format_duration(delta) -> str:
    total = int(delta.total_seconds())
    if total < 0:
        total = 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f'{days} يوم')
    if hours:
        parts.append(f'{hours} ساعة')
    if minutes and days == 0:
        parts.append(f'{minutes} دقيقة')
    if not parts:
        return 'أقل من دقيقة'
    return ' و '.join(parts)


@login_required
@user_passes_test(_is_staff)
def user_activity(request):
    today = timezone.localdate()
    date_from = _parse_activity_date(request.GET.get('date_from', ''), today - timedelta(days=29))
    date_to = _parse_activity_date(request.GET.get('date_to', ''), today)
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    q = (request.GET.get('q') or '').strip()[:80]

    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(date_from, datetime.min.time()), tz)
    end_excl = timezone.make_aware(
        datetime.combine(date_to + timedelta(days=1), datetime.min.time()),
        tz,
    )

    sessions = (
        UserActivitySession.objects.select_related('user', 'user__profile')
        .filter(login_at__gte=start, login_at__lt=end_excl)
    )
    if q:
        sessions = sessions.filter(
            Q(user_name__icontains=q)
            | Q(user_phone__icontains=q)
            | Q(user__username__icontains=q)
            | Q(user__first_name__icontains=q)
        )
    sessions = list(sessions.order_by('-login_at')[:500])

    latest_open_ids = set()
    seen_users = set()
    # آخر جلسة مفتوحة لكل مستخدم — نقيّدها بالجلسات خلال يوم العمل الأخير بدل مسح الجدول كله
    open_since = timezone.now() - timedelta(days=2)
    for row in UserActivitySession.objects.filter(
        logout_at__isnull=True, login_at__gte=open_since
    ).only('id', 'user_id', 'login_at').order_by('-login_at')[:2000]:
        key = row.user_id if row.user_id is not None else f'anon-{row.pk}'
        if key in seen_users:
            continue
        seen_users.add(key)
        latest_open_ids.add(row.pk)

    rows = []
    users_seen = set()
    now = timezone.now()
    for row in sessions:
        users_seen.add(row.user_id or row.user_name)
        if row.logout_at:
            status = 'out'
            status_label = 'خرج'
            logout_display = _format_when(row.logout_at)
            duration = _format_duration(row.logout_at - row.login_at)
        elif row.pk in latest_open_ids:
            status = 'online'
            status_label = 'لا يزال داخل'
            logout_display = ''
            duration = _format_duration(now - row.login_at)
        else:
            status = 'missed'
            status_label = 'لم يُسجَّل خروج'
            logout_display = ''
            duration = ''
        rows.append(
            {
                'name': row.user_name,
                'phone': row.user_phone,
                'login_display': _format_when(row.login_at),
                'logout_display': logout_display,
                'duration': duration,
                'ip': row.ip_address,
                'status': status,
                'status_label': status_label,
            }
        )

    online_now = len(latest_open_ids)

    return render(
        request,
        'search/user_activity.html',
        {
            'rows': rows,
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'q': q,
            'session_count': len(rows),
            'user_count': len(users_seen),
            'online_now': online_now,
        },
    )


@login_required
@user_passes_test(_is_staff)
@require_http_methods(['GET', 'POST'])
def user_permissions(request, user_id: int):
    """منح أقسام رئيسية وحجب شاشات فرعية لمستخدم."""
    target = get_object_or_404(User.objects.select_related('profile', 'nav_permission'), pk=user_id)
    profile = getattr(target, 'profile', None)
    display_name = (
        (profile.display_name if profile else '')
        or target.first_name
        or target.username
    )

    if target.is_staff:
        messages.info(request, 'مدير النظام يملك كل الأقسام تلقائياً.')
        return redirect('user_list')

    if is_executive_role(target):
        role_label = ((profile.role_name if profile else '') or '').strip() or 'تنفيذي'
        messages.info(
            request,
            f'دور «{role_label}» يملك كل الشاشات تلقائياً عدا إدارة المستخدمين.',
        )
        return redirect('user_list')

    try:
        perm = target.nav_permission
    except UserNavPermission.DoesNotExist:
        perm = None

    if request.method == 'POST':
        sections, blocked = parse_permission_post(request.POST)
        if perm is None:
            perm = UserNavPermission(user=target)
        perm.sections = sections
        perm.blocked_screens = blocked
        perm.save()
        if hasattr(target, '_nav_permission_cache'):
            delattr(target, '_nav_permission_cache')
        messages.success(
            request,
            f'تم حفظ صلاحيات «{display_name}»: {len(sections)} قسم · حجب {len(blocked)} شاشة.',
        )
        return redirect('user_permissions', user_id=target.pk)

    selected = list(perm.sections or []) if perm else list(SECTION_KEYS)
    blocked = list(perm.blocked_screens or []) if perm else []
    catalog = permission_form_catalog(
        selected_sections=selected,
        blocked_screens=blocked,
    )
    return render(
        request,
        'search/user_permissions.html',
        {
            'target': target,
            'target_name': display_name,
            'target_phone': (profile.phone if profile else '') or target.username,
            'catalog': catalog,
            'section_count': len(NAV_SECTIONS),
            'has_saved': perm is not None,
        },
    )


# ——— شاشة الربط (أوراكل + المخازن) ———
_CONN_TEXT_FIELDS = (
    'oracle_host', 'oracle_service_name', 'oracle_user', 'oracle_schema',
    'default_warehouse', 'compare_warehouses', 'warehouses_text',
)


def _conn_form_values(row):
    values = {f: (getattr(row, f, '') if row else '') for f in _CONN_TEXT_FIELDS}
    values['oracle_port'] = (row.oracle_port if row else 1521) or 1521
    values['has_password'] = bool(row and row.oracle_password_enc)
    return values


def _conn_cfg_from_post(post, row):
    """يبني قاموس اتصال أوراكل من نموذج الشاشة؛ كلمة السر الفارغة = المحفوظة."""
    from . import runtime_config

    pwd = post.get('oracle_password') or ''
    if not pwd and row is not None:
        pwd = runtime_config.decrypt_secret(row.oracle_password_enc)
    try:
        port = int((post.get('oracle_port') or '1521').strip() or 1521)
    except ValueError:
        port = 1521
    return {
        'HOST': (post.get('oracle_host') or '').strip(),
        'PORT': port,
        'SERVICE_NAME': (post.get('oracle_service_name') or '').strip(),
        'USER': (post.get('oracle_user') or '').strip(),
        'PASSWORD': pwd,
        'SCHEMA': (post.get('oracle_schema') or '').strip(),
    }


@login_required
@user_passes_test(_is_staff)
@require_http_methods(['GET', 'POST'])
def connection_settings(request):
    import re

    from django.core.cache import cache

    from . import runtime_config
    from .models import ConnectionSetting

    row = runtime_config.get_row()
    if request.method == 'POST':
        errors = []
        cfg = _conn_cfg_from_post(request.POST, row)
        if not (cfg['HOST'] and cfg['USER'] and cfg['SERVICE_NAME'] and cfg['SCHEMA']):
            errors.append('أكمل خادم أوراكل والمستخدم وService Name والمخطط.')
        if not cfg['PASSWORD']:
            errors.append('كلمة سر أوراكل مطلوبة.')
        if cfg['SCHEMA'] and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_$#]*', cfg['SCHEMA']):
            errors.append('اسم المخطط (Schema) غير صالح.')
        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            def _identity(o):
                return (o.oracle_host, o.oracle_port, o.oracle_service_name, o.oracle_schema)

            before = _identity(row) if row else None
            obj = row or ConnectionSetting(pk=1)
            for f in _CONN_TEXT_FIELDS:
                setattr(obj, f, (request.POST.get(f) or '').strip())
            obj.oracle_port = cfg['PORT']
            new_pwd = request.POST.get('oracle_password') or ''
            if new_pwd:
                obj.oracle_password_enc = runtime_config.encrypt_secret(new_pwd)
            obj.save()
            runtime_config.refresh(force=True)
            if before != _identity(obj):
                # بيانات الكاش تخص العميل السابق (شهور المجموعات، مقارنات…)
                try:
                    cache.clear()
                except Exception:  # noqa: BLE001
                    pass
                messages.success(
                    request,
                    'تم الحفظ وتطبيق الربط فوراً. إن كان العميل مختلفاً فأعد مزامنة الباركودات.',
                )
            else:
                messages.success(request, 'تم الحفظ وتطبيق الربط فوراً.')
            return redirect('connection_settings')

    values = _conn_form_values(row)
    if request.method == 'POST':  # أعد عرض ما أُدخل عند وجود خطأ
        values.update({f: (request.POST.get(f) or '').strip() for f in _CONN_TEXT_FIELDS})
    return render(
        request,
        'search/connection_settings.html',
        {'v': values, 'saved': row is not None, 'updated_at': row.updated_at if row else None},
    )


@login_required
@user_passes_test(_is_staff)
@require_POST
def connection_test(request):
    from django.http import JsonResponse

    from . import runtime_config

    row = runtime_config.get_row()
    return JsonResponse(runtime_config.test_oracle(_conn_cfg_from_post(request.POST, row)))
