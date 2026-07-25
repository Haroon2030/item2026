"""نماذج إدارة المستخدمين."""

from __future__ import annotations

import os
import re

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db.models import Q

from .debug_auth import auth_log, fingerprint
from .models import UserProfile

User = get_user_model()
_PHONE_RE = re.compile(r'^[0-9+\-\s]{7,20}$')


def _bootstrap_username() -> str:
    return (os.environ.get('APP_LOGIN_USERNAME') or '').strip()


class AppUserForm(forms.Form):
    name = forms.CharField(
        label='الاسم',
        max_length=150,
        widget=forms.TextInput(
            attrs={
                'placeholder': 'الاسم للدخول أيضاً',
                'autocomplete': 'off',
            }
        ),
    )
    phone = forms.CharField(
        label='الرقم',
        max_length=20,
        widget=forms.TextInput(
            attrs={
                'placeholder': '05xxxxxxxx',
                'autocomplete': 'off',
                'inputmode': 'tel',
                'dir': 'ltr',
            }
        ),
    )
    password = forms.CharField(
        label='كلمة السر',
        required=False,
        widget=forms.PasswordInput(
            attrs={
                'placeholder': '••••••••',
                'autocomplete': 'new-password',
                'dir': 'ltr',
            },
            render_value=False,
        ),
    )

    def __init__(self, *args, instance: User | None = None, **kwargs):
        self.instance = instance
        super().__init__(*args, **kwargs)
        if instance is None:
            self.fields['password'].required = True
            self.fields['password'].widget.attrs['placeholder'] = 'كلمة سر (6 أحرف على الأقل)'
        else:
            self.fields['password'].help_text = 'اتركها فارغة إن لم ترد تغييرها'
            profile = getattr(instance, 'profile', None)
            self.fields['name'].initial = (
                (profile.display_name if profile else '')
                or instance.first_name
                or instance.username
                or ''
            ).strip()
            self.fields['phone'].initial = (
                (profile.phone if profile else '') or ''
            ).strip()

    def clean_name(self) -> str:
        name = (self.cleaned_data.get('name') or '').strip()
        if not name:
            raise forms.ValidationError('الاسم مطلوب.')
        # الاسم يُستخدم للدخول — امنع التكرار مع أسماء أو أرقام مستخدمين آخرين
        qs = User.objects.filter(
            Q(first_name=name)
            | Q(profile__display_name=name)
            | Q(username=name)
            | Q(profile__phone=name)
        ).distinct()
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError('هذا الاسم مستخدم مسبقاً.')
        return name

    def clean_phone(self) -> str:
        phone = re.sub(r'\s+', '', (self.cleaned_data.get('phone') or '').strip())
        if not _PHONE_RE.match(phone):
            raise forms.ValidationError('الرقم غير صالح.')
        qs = UserProfile.objects.filter(phone=phone)
        if self.instance is not None:
            qs = qs.exclude(user_id=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError('هذا الرقم مستخدم مسبقاً.')
        # لا تتعارض مع username لحساب آخر (غير حساب الإقلاع)
        bootstrap = _bootstrap_username()
        other = User.objects.filter(username=phone)
        if self.instance is not None:
            other = other.exclude(pk=self.instance.pk)
        if bootstrap:
            other = other.exclude(username=bootstrap)
        if other.exists():
            raise forms.ValidationError('هذا الرقم مستخدم كمُعرّف دخول مسبقاً.')
        return phone

    def clean_password(self) -> str:
        password = (self.cleaned_data.get('password') or '').strip()
        if not password:
            if self.instance is None:
                raise forms.ValidationError('كلمة السر مطلوبة.')
            return ''
        if len(password) < 6:
            raise forms.ValidationError('كلمة السر يجب ألا تقل عن 6 أحرف.')
        validate_password(password)
        return password

    def save(self) -> User:
        name = self.cleaned_data['name']
        phone = self.cleaned_data['phone']
        password = self.cleaned_data.get('password') or ''
        bootstrap = _bootstrap_username()
        preserved_username = False

        if self.instance is None:
            user = User.objects.create_user(
                username=phone,
                password=password,
                first_name=name,
                is_active=True,
            )
            action = 'created'
        else:
            user = self.instance
            # حساب الإقلاع (admin) يحتفظ باسم الدخول حتى لا ينكسر APP_LOGIN_*
            if bootstrap and user.username == bootstrap:
                preserved_username = True
            else:
                user.username = phone
            user.first_name = name
            user.is_active = True
            if password:
                user.set_password(password)
            user.save()
            action = 'updated'

        profile, _ = UserProfile.objects.update_or_create(
            user=user,
            defaults={'phone': phone, 'display_name': name},
        )

        # region agent log
        auth_log(
            'EDIT',
            'search/forms.py:AppUserForm.save',
            'user_saved',
            {
                'action': action,
                'runId': 'post-fix',
                'preservedBootstrapUsername': preserved_username,
                'passwordChanged': bool(password),
                'nameLength': len(name),
                'phoneLength': len(phone),
                'usernameFingerprint': fingerprint(user.username),
                'phoneFingerprint': fingerprint(profile.phone),
                'nameFingerprint': fingerprint(name),
            },
        )
        # endregion
        return user
