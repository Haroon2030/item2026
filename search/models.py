from django.db import models


class ItemBarcode(models.Model):
    """ربط الباركود برقم الصنف من GetAllItems."""

    barcode = models.CharField('الباركود', max_length=64, db_index=True)
    item_code = models.CharField('رقم الصنف', max_length=64, db_index=True)
    name = models.CharField('اسم الصنف', max_length=255, blank=True, default='')
    unit = models.CharField('الوحدة', max_length=64, blank=True, default='')
    pack_size = models.CharField('حجم العبوة', max_length=32, blank=True, default='')
    g_code = models.CharField('رمز المجموعة', max_length=64, blank=True, default='', db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'باركود صنف'
        verbose_name_plural = 'باركودات الأصناف'
        constraints = [
            models.UniqueConstraint(
                fields=['barcode', 'item_code', 'unit'],
                name='uniq_barcode_item_unit',
            ),
        ]

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
