"""إدارة مستخدمي التطبيق: إضافة / تعديل / حذف."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from .forms import AppUserForm

User = get_user_model()


def _is_staff(user) -> bool:
    return bool(user.is_authenticated and user.is_staff)


@login_required
@user_passes_test(_is_staff)
def user_list(request):
    users = (
        User.objects.select_related('profile')
        .order_by('first_name', 'username')
    )
    rows = []
    for user in users:
        profile = getattr(user, 'profile', None)
        rows.append(
            {
                'id': user.pk,
                'name': (profile.display_name if profile else '')
                or user.first_name
                or user.username,
                'phone': (profile.phone if profile else '') or user.username,
                'is_self': user.pk == request.user.pk,
                'is_staff': user.is_staff,
            }
        )
    return render(
        request,
        'search/users.html',
        {
            'users': rows,
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
    users = User.objects.select_related('profile').order_by('first_name', 'username')
    rows = [
        {
            'id': u.pk,
            'name': getattr(getattr(u, 'profile', None), 'display_name', '')
            or u.first_name
            or u.username,
            'phone': getattr(getattr(u, 'profile', None), 'phone', '') or u.username,
            'is_self': u.pk == request.user.pk,
            'is_staff': u.is_staff,
        }
        for u in users
    ]
    return render(
        request,
        'search/users.html',
        {'users': rows, 'form': form, 'editing': None},
    )


@login_required
@user_passes_test(_is_staff)
@require_http_methods(['GET', 'POST'])
def user_edit(request, user_id: int):
    target = get_object_or_404(User.objects.select_related('profile'), pk=user_id)
    form = AppUserForm(request.POST or None, instance=target)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'تم تحديث المستخدم بنجاح.')
        return redirect('user_list')
    users = User.objects.select_related('profile').order_by('first_name', 'username')
    rows = [
        {
            'id': u.pk,
            'name': getattr(getattr(u, 'profile', None), 'display_name', '')
            or u.first_name
            or u.username,
            'phone': getattr(getattr(u, 'profile', None), 'phone', '') or u.username,
            'is_self': u.pk == request.user.pk,
            'is_staff': u.is_staff,
        }
        for u in users
    ]
    return render(
        request,
        'search/users.html',
        {'users': rows, 'form': form, 'editing': target},
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
