from __future__ import annotations

import logging
import re
from pathlib import Path

from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model

from .models import Instance
from .serializers import InstanceSerializer
from .services import AdbService, DockerService, GpsService, MagiskService, TouchService, VpnService, apply_device_profile, resolve_serial

logger = logging.getLogger(__name__)


def _sanitize_name(name: str) -> str:
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$", name):
        raise ValueError("Nome inválido: use letras, números, ponto, hífen ou underscore.")
    return name


def _publish_job(user_id: int, payload: dict) -> None:
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    async_to_sync(channel_layer.group_send)(
        f"jobs.user.{user_id}",
        {"type": "job.message", "payload": payload},
    )


def _instance_payload(instance: Instance) -> dict:
    data = InstanceSerializer(instance).data
    try:
        cs = DockerService.container_status(instance.name)
        data["running"] = cs["running"]
        data["status"] = cs["status"]
    except Exception:
        data["running"] = False
        data["status"] = "unknown"
    try:
        data["connection_status"] = AdbService.connection_status(resolve_serial(instance))
    except Exception:
        data["connection_status"] = "unknown"
    if instance.vpn_enabled:
        try:
            data["vpn_connection_status"] = VpnService.connection_status(
                instance.name,
                vpn_type=instance.vpn_type,
            )
        except Exception:
            data["vpn_connection_status"] = "unknown"
    else:
        data["vpn_connection_status"] = "disabled"
    return data


@shared_task(bind=True, name="instances.create_instance")
def create_instance_task(self, user_id: int, payload: dict) -> dict:
    User = get_user_model()
    job_id = self.request.id
    _publish_job(user_id, {"job_id": job_id, "kind": "create_instance", "status": "started"})
    try:
        user = User.objects.get(id=user_id)
        if user.instances.count() >= user.max_instances:
            raise RuntimeError(f"Limite de {user.max_instances} instâncias atingido.")

        name = _sanitize_name(payload.get("name", ""))
        image = payload.get("image", "redroid/redroid:12.0.0_64only-latest")
        gpu_mode = payload.get("gpu_mode", "guest")
        extra_props = payload.get("extra_props", "")
        device_profile = payload.get("device_profile", "")
        vpn_enabled = bool(payload.get("vpn_enabled", False))
        vpn_type = payload.get("vpn_type", "openvpn")
        vpn_provider = payload.get("vpn_provider", "custom")
        vpn_config = payload.get("vpn_config", "")
        vpn_ca_cert = payload.get("vpn_ca_cert", "")
        vpn_username = payload.get("vpn_username", "")
        vpn_password = payload.get("vpn_password", "")
        vpn_country = payload.get("vpn_country", "")
        magisk_enabled = bool(payload.get("magisk_enabled", False))
        fake_lat = payload.get("fake_lat")
        fake_lng = payload.get("fake_lng")

        # --- VPN sidecar setup (before creating the Android container) ---
        if vpn_enabled:
            if vpn_type == "openvpn" and vpn_provider == "custom" and not vpn_config.strip():
                raise RuntimeError("Conteúdo do arquivo .ovpn é obrigatório quando VPN custom/OpenVPN está ativa.")
            if vpn_type == "openvpn" and vpn_config.strip():
                VpnService.store_config(name, vpn_config, vpn_username, vpn_password, ca_cert=vpn_ca_cert)
            VpnService.start_sidecar(
                name,
                vpn_type=vpn_type,
                provider=vpn_provider,
                username=vpn_username,
                password=vpn_password,
                country=vpn_country,
            )
            # Tor takes longer to bootstrap than OpenVPN
            tunnel_timeout = 120 if vpn_type == "tor" else 30
            if not VpnService.wait_tunnel(name, timeout=tunnel_timeout, vpn_type=vpn_type):
                logger.warning("create_instance_task: VPN tunnel not ready for %s, continuing anyway", name)

        effective_image = image
        if device_profile:
            effective_image = apply_device_profile(image, device_profile)
            if effective_image != image:
                logger.info("create_instance_task: using profiled image %s for %s", effective_image, name)

        if magisk_enabled:
            try:
                effective_image = MagiskService.apply_magisk(effective_image)
                logger.info("create_instance_task: using magisk image %s for %s", effective_image, name)
            except Exception as e:
                logger.warning("create_instance_task: Magisk image creation failed: %s", e)
                magisk_enabled = False

        cid = DockerService.create_container(
            name=name,
            image=effective_image,
            gpu_mode=gpu_mode,
            extra_props=extra_props,
            user_id=user.id,
        )

        # Gateway mode: set the sidecar as the Android container's gateway
        # so internet traffic flows through the VPN/Tor tunnel.
        if vpn_enabled:
            VpnService.setup_gateway(name)

        instance = Instance.objects.create(
            user=user,
            name=name,
            container_id=cid,
            image=image,
            gpu_mode=gpu_mode,
            extra_props=extra_props,
            device_profile=device_profile,
            magisk_enabled=magisk_enabled,
            vpn_enabled=vpn_enabled,
            vpn_type=vpn_type,
            vpn_provider=vpn_provider,
            vpn_username=vpn_username,
            vpn_password=vpn_password,
            vpn_country=vpn_country,
            fake_lat=fake_lat,
            fake_lng=fake_lng,
        )
        TouchService.setup_virtual_touch(name)
        TouchService.setup_virtual_keyboard(name)
        serial = resolve_serial(instance)
        try:
            AdbService.connect(serial)
            if AdbService.wait_boot(serial, 30):
                AdbService.dismiss_overlays(serial)
                if fake_lat is not None and fake_lng is not None:
                    GpsService.set_location(serial, fake_lat, fake_lng)
        except Exception as e:
            logger.warning(
                "create_instance_task: instance %s created but ADB is not ready yet (%s)",
                name,
                e,
            )
        instance_data = _instance_payload(instance)
        _publish_job(
            user_id,
            {
                "job_id": job_id,
                "kind": "create_instance",
                "status": "success",
                "instance": instance_data,
            },
        )
        return {"ok": True, "instance_id": instance.id}
    except Exception as exc:
        logger.exception("create_instance_task failed user=%s", user_id)
        _publish_job(
            user_id,
            {"job_id": job_id, "kind": "create_instance", "status": "error", "detail": str(exc)},
        )
        raise


@shared_task(bind=True, name="instances.install_apk")
def install_apk_task(self, user_id: int, instance_id: int, apk_path: str, filename: str) -> dict:
    job_id = self.request.id
    _publish_job(
        user_id,
        {
            "job_id": job_id,
            "kind": "install_apk",
            "status": "started",
            "instance_id": instance_id,
            "filename": filename,
        },
    )
    path = Path(apk_path)
    try:
        instance = Instance.objects.get(id=instance_id, user_id=user_id)
        if not path.exists():
            raise RuntimeError("Arquivo APK temporário não encontrado para processamento.")

        serial = resolve_serial(instance)
        AdbService.connect(serial)
        container_hint = (instance.container_id or instance.name or "").strip()
        AdbService.install_apk(serial, str(path), container_hint=container_hint)
        _publish_job(
            user_id,
            {
                "job_id": job_id,
                "kind": "install_apk",
                "status": "success",
                "instance_id": instance_id,
                "filename": filename,
            },
        )
        return {"ok": True}
    except Exception as exc:
        logger.exception("install_apk_task failed user=%s instance=%s", user_id, instance_id)
        _publish_job(
            user_id,
            {
                "job_id": job_id,
                "kind": "install_apk",
                "status": "error",
                "instance_id": instance_id,
                "filename": filename,
                "detail": str(exc),
            },
        )
        raise
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

