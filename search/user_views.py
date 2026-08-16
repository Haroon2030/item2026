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
from .models import UserActivitySession

User = get_user_model()


def _is_staff(user) -> bool:
    return bool(user.is_authenticated and user.is_staff)


def _user_rows(request):
    users = list(User.objects.select_related('profile').all())

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
    for row in UserActivitySession.objects.filter(logout_at__isnull=True).order_by('-login_at'):
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
