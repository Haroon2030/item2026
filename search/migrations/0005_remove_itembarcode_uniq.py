from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('search', '0004_expand_barcode_length'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='itembarcode',
            name='uniq_barcode_item_unit',
        ),
    ]
