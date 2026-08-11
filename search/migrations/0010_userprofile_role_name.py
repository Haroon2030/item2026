# Generated manually for UserProfile.role_name

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('search', '0009_delete_warehouseitemstock'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='role_name',
            field=models.CharField(
                blank=True,
                default='',
                max_length=100,
                verbose_name='اسم الدور',
            ),
        ),
    ]
