from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('instances', '0006_merge_vpn_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='instance',
            name='vpn_country',
            field=models.CharField(blank=True, default='', max_length=2),
        ),
        migrations.AlterField(
            model_name='instance',
            name='vpn_type',
            field=models.CharField(
                choices=[('openvpn', 'OpenVPN'), ('tor', 'Tor (Whonix-like)'), ('wireguard', 'WireGuard')],
                default='openvpn',
                max_length=16,
            ),
        ),
    ]
