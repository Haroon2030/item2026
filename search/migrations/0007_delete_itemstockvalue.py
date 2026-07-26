from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('search', '0006_itemstockvalue'),
    ]

    operations = [
        migrations.DeleteModel(
            name='ItemStockValue',
        ),
    ]
