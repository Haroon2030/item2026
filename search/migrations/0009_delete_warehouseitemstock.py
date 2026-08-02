from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('search', '0008_warehouseitemstock'),
    ]

    operations = [
        migrations.DeleteModel(
            name='WarehouseItemStock',
        ),
    ]
