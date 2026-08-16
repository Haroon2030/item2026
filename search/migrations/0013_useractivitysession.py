from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('search', '0012_delete_mobileauthtoken'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='UserActivitySession',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('user_name', models.CharField(max_length=150, verbose_name='الاسم')),
                (
                    'user_phone',
                    models.CharField(
                        blank=True, default='', max_length=20, verbose_name='الرقم'
                    ),
                ),
                ('login_at', models.DateTimeField(db_index=True, verbose_name='وقت الدخول')),
                (
                    'logout_at',
                    models.DateTimeField(
                        blank=True, db_index=True, null=True, verbose_name='وقت الخروج'
                    ),
                ),
                (
                    'ip_address',
                    models.CharField(
                        blank=True, default='', max_length=45, verbose_name='عنوان IP'
                    ),
                ),
                (
                    'user_agent',
                    models.CharField(
                        blank=True, default='', max_length=300, verbose_name='المتصفح'
                    ),
                ),
                (
                    'session_key',
                    models.CharField(
                        blank=True,
                        db_index=True,
                        default='',
                        max_length=40,
                        verbose_name='مفتاح الجلسة',
                    ),
                ),
                (
                    'source',
                    models.CharField(default='web', max_length=16, verbose_name='المصدر'),
                ),
                (
                    'user',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='activity_sessions',
                        to=settings.AUTH_USER_MODEL,
                        verbose_name='المستخدم',
                    ),
                ),
            ],
            options={
                'verbose_name': 'جلسة مستخدم',
                'verbose_name_plural': 'جلسات المستخدمين',
                'ordering': ['-login_at'],
            },
        ),
        migrations.AddIndex(
            model_name='useractivitysession',
            index=models.Index(
                fields=['user', 'login_at'], name='search_uact_user_login_idx'
            ),
        ),
    ]
