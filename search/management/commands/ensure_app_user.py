import os

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

        if len(password) < 12:
            self.stderr.write(
                self.style.ERROR('APP_LOGIN_PASSWORD يجب ألا تقل عن 12 حرفاً.')
            )
            return

        user_model = get_user_model()
        user, created = user_model.objects.get_or_create(
            username=username,
            defaults={'is_active': True},
        )
        user.is_active = True
        user.set_password(password)
        user.save(update_fields=['password', 'is_active'])

        action = 'تم إنشاء' if created else 'تم تحديث'
        self.stdout.write(self.style.SUCCESS(f'{action} مستخدم الدخول: {username}'))
