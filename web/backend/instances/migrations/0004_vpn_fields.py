from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("instances", "0003_remove_port_and_proxy_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="instance",
            name="vpn_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="instance",
            name="vpn_username",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="instance",
            name="vpn_password",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
    ]
