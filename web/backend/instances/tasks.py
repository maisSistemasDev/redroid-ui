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
from .services import AdbService, DockerService, TouchService, VpnService

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
        vpn_enabled = bool(payload.get("vpn_enabled", False))
        vpn_type = payload.get("vpn_type", "openvpn")
        vpn_provider = payload.get("vpn_provider", "custom")
        vpn_config = payload.get("vpn_config", "")
        vpn_username = payload.get("vpn_username", "")
        vpn_password = payload.get("vpn_password", "")

        # --- VPN sidecar setup (before creating the redroid container) ---
        if vpn_enabled:
            if vpn_provider == "custom" and vpn_type == "openvpn" and not vpn_config.strip():
                raise RuntimeError("Conteúdo do arquivo .ovpn é obrigatório quando VPN custom/OpenVPN está ativa.")
            if vpn_config.strip():
                VpnService.store_config(name, vpn_config, vpn_username, vpn_password)
            VpnService.start_sidecar(name, vpn_type=vpn_type, provider=vpn_provider, username=vpn_username, password=vpn_password)
            if not VpnService.wait_tunnel(name, timeout=30):
                logger.warning("create_instance_task: VPN tunnel not ready for %s, continuing anyway", name)

        cid = DockerService.create_container(
            name=name,
            image=image,
            gpu_mode=gpu_mode,
            extra_props=extra_props,
            user_id=user.id,
            vpn_enabled=vpn_enabled,
        )

        instance = Instance.objects.create(
            user=user,
            name=name,
            container_id=cid,
            image=image,
            gpu_mode=gpu_mode,
            extra_props=extra_props,
            vpn_enabled=vpn_enabled,
            vpn_type=vpn_type,
            vpn_provider=vpn_provider,
            vpn_username=vpn_username,
            vpn_password=vpn_password,
        )
        TouchService.setup_virtual_touch(name)
        AdbService.connect(instance.serial)
        if AdbService.wait_boot(instance.serial, 30):
            AdbService.dismiss_overlays(instance.serial)
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

        AdbService.connect(instance.serial)
        container_hint = (instance.container_id or instance.name or "").strip()
        AdbService.install_apk(instance.serial, str(path), container_hint=container_hint)
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

