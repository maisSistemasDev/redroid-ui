from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("instances", "0007_vpn_tor_country"),
    ]

    operations = [
        migrations.AddField(
            model_name="instance",
            name="device_profile",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
