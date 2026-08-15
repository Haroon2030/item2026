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


class MobileAuthToken(models.Model):
    """رمز دخول تطبيق الموبايل (يُخزَّن هاشه فقط)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='mobile_tokens',
    )
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        verbose_name = 'رمز موبايل'
        verbose_name_plural = 'رموز الموبايل'

    def __str__(self) -> str:
        return f'{self.user_id} · {self.key_hash[:8]}'


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
