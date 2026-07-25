import os
import re

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'إنشاء مستخدم الدخول من متغيرات البيئة (بدون إعادة تعيين كلمة السر في كل تشغيل).'

    def handle(self, *args, **options):
        username = os.environ.get('APP_LOGIN_USERNAME', '').strip()
        password = os.environ.get('APP_LOGIN_PASSWORD', '')
        force_password = os.environ.get('APP_LOGIN_FORCE_PASSWORD', '').strip() in {
            '1',
            'true',
            'yes',
            'on',
        }

        if not username or not password:
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
        created = False

        if user is None:
            user = user_model.objects.create_user(
                username=username,
                password=password,
                first_name=username,
                is_active=True,
                is_staff=True,
                is_superuser=True,
            )
            created = True
            self.stdout.write(self.style.SUCCESS(f'تم إنشاء مستخدم الدخول: {username}'))
        else:
            # لا نعيد كلمة السر في كل Redeploy حتى لا تُلغى تعديلات شاشة المستخدمين
            changed = []
            if not user.is_active:
                user.is_active = True
                changed.append('is_active')
            if not user.is_staff:
                user.is_staff = True
                changed.append('is_staff')
            if not user.is_superuser:
                user.is_superuser = True
                changed.append('is_superuser')
            if force_password:
                user.set_password(password)
                changed.append('password')
            if changed:
                user.save()
                self.stdout.write(
                    self.style.SUCCESS(
                        f'تم تحديث مستخدم الدخول ({", ".join(changed)}): {username}'
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f'مستخدم الدخول موجود مسبقاً: {username} — لم تُغيَّر كلمة السر.'
                    )
                )

        from search.models import UserProfile

        if re.fullmatch(r'[0-9+\-]{7,20}', username):
            phone = username
        else:
            existing = getattr(user, 'profile', None)
            phone = (existing.phone if existing and existing.phone else f'05{user.pk:08d}')

        profile, profile_created = UserProfile.objects.get_or_create(
            user=user,
            defaults={
                'display_name': user.first_name or username,
                'phone': phone[:20],
            },
        )
        if not profile_created and not profile.phone:
            profile.phone = phone[:20]
            profile.display_name = profile.display_name or user.first_name or username
            profile.save(update_fields=['phone', 'display_name', 'updated_at'])
