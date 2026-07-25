import os
import re

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'إنشاء أو تحديث مستخدم تسجيل الدخول من متغيرات البيئة.'

    def handle(self, *args, **options):
        username = os.environ.get('APP_LOGIN_USERNAME', '').strip()
        password = os.environ.get('APP_LOGIN_PASSWORD', '')

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
        user, created = user_model.objects.get_or_create(
            username=username,
            defaults={
                'is_active': True,
                'is_staff': True,
                'is_superuser': True,
                'first_name': username,
            },
        )
        user.is_active = True
        user.is_staff = True
        user.is_superuser = True
        if not user.first_name:
            user.first_name = username
        user.set_password(password)
        user.save()

        from search.models import UserProfile

        if re.fullmatch(r'[0-9+\-]{7,20}', username):
            phone = username
        else:
            phone = f'05{user.pk:08d}'

        UserProfile.objects.update_or_create(
            user=user,
            defaults={
                'display_name': user.first_name or username,
                'phone': phone[:20],
            },
        )

        action = 'تم إنشاء' if created else 'تم تحديث'
        self.stdout.write(self.style.SUCCESS(f'{action} مستخدم الدخول: {username}'))
