from django.db import migrations, models


def add_columns_if_missing(apps, schema_editor):
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'instances_instance' AND column_name = 'vpn_type'"
        )
        if not cursor.fetchone():
            cursor.execute(
                "ALTER TABLE instances_instance "
                "ADD COLUMN vpn_type varchar(16) NOT NULL DEFAULT 'openvpn'"
            )
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'instances_instance' AND column_name = 'vpn_provider'"
        )
        if not cursor.fetchone():
            cursor.execute(
                "ALTER TABLE instances_instance "
                "ADD COLUMN vpn_provider varchar(64) NOT NULL DEFAULT 'custom'"
            )


class Migration(migrations.Migration):

    dependencies = [
        ("instances", "0004_vpn_fields"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(add_columns_if_missing, migrations.RunPython.noop),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="instance",
                    name="vpn_type",
                    field=models.CharField(
                        choices=[("openvpn", "OpenVPN"), ("wireguard", "WireGuard")],
                        default="openvpn",
                        max_length=16,
                    ),
                ),
                migrations.AddField(
                    model_name="instance",
                    name="vpn_provider",
                    field=models.CharField(blank=True, default="custom", max_length=64),
                ),
            ],
        ),
    ]
