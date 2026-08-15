from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('search', '0011_mobileauthtoken'),
    ]

    operations = [
        migrations.DeleteModel(
            name='MobileAuthToken',
        ),
    ]
