"""إدارة مستخدمي التطبيق: إضافة / تعديل / حذف."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from .forms import AppUserForm

User = get_user_model()


def _is_staff(user) -> bool:
    return bool(user.is_authenticated and user.is_staff)


def _user_rows(request):
    users = User.objects.select_related('profile').order_by('first_name', 'username')
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
