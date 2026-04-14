from rest_framework import serializers

from .models import Instance


class InstanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Instance
        fields = [
            "id", "user", "name", "container_id",
            "image", "gpu_mode", "extra_props",
            "vpn_enabled", "vpn_type", "vpn_provider", "created_at",
        ]
        read_only_fields = ["id", "user", "container_id", "created_at"]


class CreateInstanceSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=64)
    image = serializers.CharField(max_length=256, default="redroid/redroid:12.0.0_64only-latest")
    gpu_mode = serializers.CharField(max_length=16, default="guest")
    extra_props = serializers.CharField(required=False, default="", allow_blank=True)
    vpn_enabled = serializers.BooleanField(required=False, default=False)
    vpn_type = serializers.ChoiceField(choices=["openvpn", "wireguard"], required=False, default="openvpn")
    vpn_provider = serializers.CharField(required=False, default="custom", allow_blank=True, max_length=64)
    vpn_config = serializers.CharField(required=False, default="", allow_blank=True)
    vpn_ca_cert = serializers.CharField(required=False, default="", allow_blank=True)
    vpn_username = serializers.CharField(required=False, default="", allow_blank=True, max_length=128)
    vpn_password = serializers.CharField(required=False, default="", allow_blank=True, max_length=128)


class UpdateVpnSerializer(serializers.Serializer):
    vpn_enabled = serializers.BooleanField(required=False, default=True)
    vpn_type = serializers.ChoiceField(choices=["openvpn", "wireguard"], required=False, default="openvpn")
    vpn_provider = serializers.CharField(required=False, default="custom", allow_blank=True, max_length=64)
    vpn_config = serializers.CharField(required=False, default="", allow_blank=True)
    vpn_ca_cert = serializers.CharField(required=False, default="", allow_blank=True)
    vpn_username = serializers.CharField(required=False, default="", allow_blank=True, max_length=128)
    vpn_password = serializers.CharField(required=False, default="", allow_blank=True, max_length=128)
