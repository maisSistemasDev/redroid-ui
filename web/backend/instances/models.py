from django.conf import settings
from django.db import models


class Instance(models.Model):
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
    vpn_enabled = models.BooleanField(default=False)
    vpn_type = models.CharField(max_length=16, default="openvpn")
    vpn_provider = models.CharField(max_length=64, default="custom")
    vpn_username = models.CharField(max_length=128, blank=True, default="")
    vpn_password = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def serial(self):
        if self.vpn_enabled:
            return f"{self.name}-vpn:5555"
        return f"{self.name}:5555"
