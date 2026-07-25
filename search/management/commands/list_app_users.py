from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'عرض حسابات الدخول (الاسم والرقم) للتشخيص.'

    def handle(self, *args, **options):
        users = (
            get_user_model()
            .objects.select_related('profile')
            .order_by('username')
        )
        if not users:
            self.stdout.write(self.style.WARNING('لا يوجد مستخدمون.'))
            return

        self.stdout.write('اسم الدخول | الاسم | الرقم | مشرف | مفعّل')
        self.stdout.write('-' * 60)
        for user in users:
            profile = getattr(user, 'profile', None)
            self.stdout.write(
                ' | '.join(
                    [
                        user.username,
                        user.first_name or '—',
                        (profile.phone if profile else '') or '—',
                        'نعم' if user.is_staff else 'لا',
                        'نعم' if user.is_active else 'لا',
                    ]
                )
            )
