from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("instances", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="instance",
            name="proxy_type",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        migrations.AddField(
            model_name="instance",
            name="proxy_host",
            field=models.CharField(blank=True, default="", max_length=256),
        ),
        migrations.AddField(
            model_name="instance",
            name="proxy_port",
            field=models.IntegerField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="instance",
            name="proxy_username",
            field=models.CharField(blank=True, default="", max_length=256),
        ),
        migrations.AddField(
            model_name="instance",
            name="proxy_password",
            field=models.CharField(blank=True, default="", max_length=256),
        ),
    ]
