import os
import re

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from search.debug_auth import auth_log, fingerprint


class Command(BaseCommand):
    help = 'إنشاء/مزامنة حساب الدخول الأساسي من متغيرات البيئة.'

    def handle(self, *args, **options):
        username = os.environ.get('APP_LOGIN_USERNAME', '').strip()
        password = os.environ.get('APP_LOGIN_PASSWORD', '')

        if not username or not password:
            # region agent log
            auth_log(
                'A,B',
                'ensure_app_user.py:handle',
                'bootstrap_env_missing',
                {
                    'hasUsername': bool(username),
                    'hasPassword': bool(password),
                },
            )
            # endregion
            self.stdout.write(
                self.style.WARNING(
                    'APP_LOGIN_USERNAME/APP_LOGIN_PASSWORD غير مضبوطين؛ لم يُنشأ مستخدم.'
                )
            )
            return

        if len(password) < 6:
            self.stderr.write(
                self.style.ERROR('APP_LOGIN_PASSWORD يجب ألا تقل عن 6 أحرف.')
            )
            return

        user_model = get_user_model()
        user = user_model.objects.filter(username=username).first()
        existed = user is not None
        password_matched_before = bool(user and user.check_password(password))

        if user is None:
            user = user_model.objects.create_user(
                username=username,
                password=password,
                first_name=username,
                is_active=True,
                is_staff=True,
                is_superuser=True,
            )
            self.stdout.write(self.style.SUCCESS(f'تم إنشاء حساب الدخول: {username}'))
        else:
            # متغيرات البيئة هي المرجع لهذا الحساب فقط،
            # حتى يمكن استعادة الدخول بتغيير APP_LOGIN_PASSWORD.
            # الحسابات المُنشأة من شاشة المستخدمين لا تتأثر (أسماؤها أرقام مختلفة).
            changed = []
            if not user.check_password(password):
                user.set_password(password)
                changed.append('كلمة السر')
            if not user.is_active:
                user.is_active = True
                changed.append('التفعيل')
            if not user.is_staff:
                user.is_staff = True
                changed.append('صلاحية الإشراف')
            if not user.is_superuser:
                user.is_superuser = True
                changed.append('صلاحية المدير')
            if not user.first_name:
                user.first_name = username
                changed.append('الاسم')

            if changed:
                user.save()
                self.stdout.write(
                    self.style.SUCCESS(
                        f'تمت مزامنة حساب الدخول {username} ({"، ".join(changed)}).'
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(f'حساب الدخول {username} مطابق للبيئة.')
                )

        from search.models import UserProfile

        if re.fullmatch(r'[0-9+\-]{7,20}', username):
            phone = username
        else:
            existing = getattr(user, 'profile', None)
            phone = existing.phone if existing and existing.phone else f'05{user.pk:08d}'

        profile, created_profile = UserProfile.objects.get_or_create(
            user=user,
            defaults={
                'display_name': user.first_name or username,
                'phone': phone[:20],
            },
        )
        if not created_profile and not profile.phone:
            profile.phone = phone[:20]
            profile.display_name = profile.display_name or user.first_name or username
            profile.save(update_fields=['phone', 'display_name', 'updated_at'])

        # region agent log
        auth_log(
            'A,B,C',
            'ensure_app_user.py:handle',
            'bootstrap_sync_complete',
            {
                'usernameFingerprint': fingerprint(username),
                'usernameLength': len(username),
                'passwordLength': len(password),
                'userExisted': existed,
                'passwordMatchedBefore': password_matched_before,
                'passwordMatchesAfter': user.check_password(password),
                'usersCount': user_model.objects.count(),
                'dbEngine': user_model.objects.db,
            },
        )
        # endregion
