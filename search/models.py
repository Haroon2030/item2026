from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    """بيانات إضافية لمستخدم التطبيق (الاسم المعروض والرقم)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile',
    )
    display_name = models.CharField('الاسم', max_length=150)
    phone = models.CharField('الرقم', max_length=20, unique=True, db_index=True)
    role_name = models.CharField('اسم الدور', max_length=100, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'ملف مستخدم'
        verbose_name_plural = 'ملفات المستخدمين'

    def __str__(self) -> str:
        return f'{self.display_name} ({self.phone})'


class UserActivitySession(models.Model):
    """جلسة دخول: وقت الدخول ووقت الخروج لكل مستخدم."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='activity_sessions',
        verbose_name='المستخدم',
    )
    user_name = models.CharField('الاسم', max_length=150)
    user_phone = models.CharField('الرقم', max_length=20, blank=True, default='')
    login_at = models.DateTimeField('وقت الدخول', db_index=True)
    logout_at = models.DateTimeField('وقت الخروج', null=True, blank=True, db_index=True)
    ip_address = models.CharField('عنوان IP', max_length=45, blank=True, default='')
    user_agent = models.CharField('المتصفح', max_length=300, blank=True, default='')
    session_key = models.CharField('مفتاح الجلسة', max_length=40, blank=True, default='', db_index=True)
    source = models.CharField('المصدر', max_length=16, default='web')

    class Meta:
        verbose_name = 'جلسة مستخدم'
        verbose_name_plural = 'جلسات المستخدمين'
        ordering = ['-login_at']
        indexes = [
            models.Index(fields=['user', 'login_at'], name='search_uact_user_login_idx'),
        ]

    def __str__(self) -> str:
        return f'{self.user_name} @ {self.login_at}'


class UserNavPermission(models.Model):
    """صلاحيات أقسام الشريط: أقسام ممنوحة + شاشات فرعية محجوبة."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='nav_permission',
        verbose_name='المستخدم',
    )
    sections = models.JSONField('الأقسام الممنوحة', default=list, blank=True)
    blocked_screens = models.JSONField('الشاشات المحجوبة', default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'صلاحية أقسام'
        verbose_name_plural = 'صلاحيات الأقسام'

    def __str__(self) -> str:
        return f'صلاحيات {self.user_id}'


class ItemBarcode(models.Model):
    """ربط الباركود برقم الصنف من GetAllItems."""

    barcode = models.CharField('الباركود', max_length=128, db_index=True)
    item_code = models.CharField('رقم الصنف', max_length=64, db_index=True)
    name = models.CharField('اسم الصنف', max_length=255, blank=True, default='')
    unit = models.CharField('الوحدة', max_length=64, blank=True, default='')
    pack_size = models.CharField('حجم العبوة', max_length=32, blank=True, default='')
    g_code = models.CharField('رمز المجموعة', max_length=64, blank=True, default='', db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'باركود صنف'
        verbose_name_plural = 'باركودات الأصناف'
        # بدون قيد فريد: نحتفظ بصفوف المصدر كما هي حتى عند تكرار شكل الوحدة.

    def __str__(self) -> str:
        return f'{self.barcode} → {self.item_code}'


class ItemGroup(models.Model):
    """مجموعات الأصناف من GetAllGroupDet (G_CODE)."""

    g_code = models.CharField('رمز المجموعة', max_length=64, unique=True)
    g_name = models.CharField('اسم المجموعة', max_length=255, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'مجموعة صنف'
        verbose_name_plural = 'مجموعات الأصناف'

    def __str__(self) -> str:
        return f'{self.g_code} — {self.g_name}'
