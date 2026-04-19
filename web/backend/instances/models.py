from django.conf import settings
from django.db import models


class Instance(models.Model):
    VPN_TYPE_CHOICES = [
        ("openvpn", "OpenVPN"),
        ("tor", "Tor (Whonix-like)"),
        ("wireguard", "WireGuard"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="instances",
    )
    name = models.CharField(max_length=64, unique=True)
    container_id = models.CharField(max_length=64, blank=True, default="")
    image = models.CharField(max_length=256, default="redroid/redroid:12.0.0_64only-latest")
    gpu_mode = models.CharField(max_length=16, default="guest")
    extra_props = models.TextField(blank=True, default="")
    device_profile = models.CharField(max_length=32, blank=True, default="")
    magisk_enabled = models.BooleanField(default=False)
    vpn_enabled = models.BooleanField(default=False)
    vpn_type = models.CharField(max_length=16, choices=VPN_TYPE_CHOICES, default="openvpn")
    vpn_provider = models.CharField(max_length=64, blank=True, default="custom")
    vpn_username = models.CharField(max_length=128, blank=True, default="")
    vpn_password = models.CharField(max_length=128, blank=True, default="")
    vpn_country = models.CharField(max_length=2, blank=True, default="")
    fake_lat = models.FloatField(null=True, blank=True, default=None)
    fake_lng = models.FloatField(null=True, blank=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def serial(self):
        return f"{self.name}:5555"
