"""نماذج إدارة المستخدمين."""

from __future__ import annotations

import re

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password

from .models import UserProfile

User = get_user_model()
_PHONE_RE = re.compile(r'^[0-9+\-\s]{7,20}$')


class AppUserForm(forms.Form):
    name = forms.CharField(
        label='الاسم',
        max_length=150,
        widget=forms.TextInput(
            attrs={
                'placeholder': 'اسم المستخدم',
                'autocomplete': 'name',
            }
        ),
    )
    phone = forms.CharField(
        label='الرقم',
        max_length=20,
        widget=forms.TextInput(
            attrs={
                'placeholder': '05xxxxxxxx',
                'autocomplete': 'tel',
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
            }
        ),
    )

    def __init__(self, *args, instance: User | None = None, **kwargs):
        self.instance = instance
        super().__init__(*args, **kwargs)
        if instance is None:
            self.fields['password'].required = True
            self.fields['password'].widget.attrs['placeholder'] = 'كلمة سر قوية'
        else:
            self.fields['password'].help_text = 'اتركها فارغة إن لم ترد تغييرها'
            self.fields['name'].initial = (instance.first_name or instance.username or '').strip()
            profile = getattr(instance, 'profile', None)
            self.fields['phone'].initial = (profile.phone if profile else instance.username) or ''

    def clean_name(self) -> str:
        name = (self.cleaned_data.get('name') or '').strip()
        if len(name) < 2:
            raise forms.ValidationError('الاسم قصير جداً.')
        return name

    def clean_phone(self) -> str:
        phone = re.sub(r'\s+', '', (self.cleaned_data.get('phone') or '').strip())
        if not _PHONE_RE.match(phone):
            raise forms.ValidationError('الرقم غير صالح.')
        qs = User.objects.filter(username=phone)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError('هذا الرقم مستخدم مسبقاً.')
        return phone

    def clean_password(self) -> str:
        password = self.cleaned_data.get('password') or ''
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

        if self.instance is None:
            user = User.objects.create_user(
                username=phone,
                password=password,
                first_name=name,
                is_active=True,
            )
        else:
            user = self.instance
            user.username = phone
            user.first_name = name
            user.is_active = True
            if password:
                user.set_password(password)
            user.save()

        UserProfile.objects.update_or_create(
            user=user,
            defaults={'phone': phone, 'display_name': name},
        )
        return user
