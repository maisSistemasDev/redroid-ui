from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("instances", "0008_instance_device_profile"),
    ]

    operations = [
        migrations.AddField(
            model_name="instance",
            name="magisk_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
