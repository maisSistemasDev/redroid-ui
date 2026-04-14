from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("instances", "0004_vpn_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="instance",
            name="vpn_type",
            field=models.CharField(default="openvpn", max_length=16),
        ),
        migrations.AddField(
            model_name="instance",
            name="vpn_provider",
            field=models.CharField(default="custom", max_length=64),
        ),
    ]
