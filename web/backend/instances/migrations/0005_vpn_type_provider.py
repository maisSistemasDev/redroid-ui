from django.db import migrations, models


def add_column_if_missing(apps, schema_editor):
    """Add vpn_type and vpn_provider only if they don't already exist."""
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'instances_instance' AND column_name = 'vpn_type'"
        )
        if not cursor.fetchone():
            cursor.execute(
                "ALTER TABLE instances_instance ADD COLUMN vpn_type varchar(16) NOT NULL DEFAULT 'openvpn'"
            )
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'instances_instance' AND column_name = 'vpn_provider'"
        )
        if not cursor.fetchone():
            cursor.execute(
                "ALTER TABLE instances_instance ADD COLUMN vpn_provider varchar(64) NOT NULL DEFAULT 'custom'"
            )


class Migration(migrations.Migration):

    dependencies = [
        ("instances", "0004_vpn_fields"),
    ]

    operations = [
        migrations.RunPython(add_column_if_missing, migrations.RunPython.noop),
    ]
