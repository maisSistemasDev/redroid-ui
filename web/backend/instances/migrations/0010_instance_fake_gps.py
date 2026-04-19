from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("instances", "0009_instance_magisk_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="instance",
            name="fake_lat",
            field=models.FloatField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="instance",
            name="fake_lng",
            field=models.FloatField(blank=True, default=None, null=True),
        ),
    ]
