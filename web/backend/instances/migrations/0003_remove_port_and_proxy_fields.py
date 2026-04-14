from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("instances", "0002_instance_proxy_fields"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="instance",
            name="host_port",
        ),
        migrations.RemoveField(
            model_name="instance",
            name="proxy_type",
        ),
        migrations.RemoveField(
            model_name="instance",
            name="proxy_host",
        ),
        migrations.RemoveField(
            model_name="instance",
            name="proxy_port",
        ),
        migrations.RemoveField(
            model_name="instance",
            name="proxy_username",
        ),
        migrations.RemoveField(
            model_name="instance",
            name="proxy_password",
        ),
    ]
