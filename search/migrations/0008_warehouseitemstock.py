from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('search', '0007_delete_itemstockvalue'),
    ]

    operations = [
        migrations.CreateModel(
            name='WarehouseItemStock',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('warehouse', models.CharField(db_index=True, max_length=16, verbose_name='المخزن')),
                ('item_code', models.CharField(db_index=True, max_length=64, verbose_name='رقم الصنف')),
                ('name', models.CharField(blank=True, default='', max_length=255, verbose_name='اسم الصنف')),
                ('barcode', models.CharField(blank=True, default='', max_length=128, verbose_name='الباركود')),
                ('unit', models.CharField(blank=True, default='', max_length=64, verbose_name='الوحدة')),
                ('g_code', models.CharField(blank=True, db_index=True, default='', max_length=64, verbose_name='المجموعة الرئيسية')),
                ('sub_g_code', models.CharField(blank=True, db_index=True, default='', max_length=64, verbose_name='المجموعة الفرعية')),
                ('quantity', models.CharField(blank=True, default='', max_length=64, verbose_name='الكمية')),
                ('avg_cost', models.CharField(blank=True, default='', max_length=64, verbose_name='التكلفة')),
                ('source', models.CharField(blank=True, default='onyx_excel', max_length=64, verbose_name='المصدر')),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'رصيد مخزون مستورد',
                'verbose_name_plural': 'أرصدة مخزون مستوردة',
            },
        ),
        migrations.AddConstraint(
            model_name='warehouseitemstock',
            constraint=models.UniqueConstraint(
                fields=('warehouse', 'item_code'),
                name='uniq_wh_item_stock_import',
            ),
        ),
        migrations.AddIndex(
            model_name='warehouseitemstock',
            index=models.Index(fields=['warehouse', 'g_code'], name='idx_whstock_wh_group'),
        ),
    ]
