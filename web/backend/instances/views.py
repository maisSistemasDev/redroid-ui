from __future__ import annotations

import re
import uuid
import logging
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import AccessToken
from celery.result import AsyncResult

from .models import Instance
from .serializers import CreateInstanceSerializer, InstanceSerializer, UpdateVpnSerializer
from .services import (
    AdbService,
    DockerService,
    TouchService,
    VpnService,
    cleanup_procs,
)
from .tasks import create_instance_task, install_apk_task

logger = logging.getLogger(__name__)


def _sanitize_name(name: str) -> str:
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$", name):
        raise ValueError("Nome inválido: use letras, números, ponto, hífen ou underscore.")
    return name


class InstanceViewSet(viewsets.ModelViewSet):
    serializer_class = InstanceSerializer

    def get_queryset(self):
        return Instance.objects.filter(user=self.request.user)

    def _enrich(self, instance: Instance) -> dict:
        data = InstanceSerializer(instance).data
        try:
            cs = DockerService.container_status(instance.name)
            data["running"] = cs["running"]
            data["status"] = cs["status"]
        except Exception:
            data["running"] = False
            data["status"] = "unknown"
        if instance.vpn_enabled:
            data["vpn_active"] = VpnService.sidecar_running(instance.name)
        else:
            data["vpn_active"] = False
        return data

    def list(self, request, *args, **kwargs):
        qs = self.get_queryset()
        return Response([self._enrich(i) for i in qs])

    def create(self, request, *args, **kwargs):
        ser = CreateInstanceSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data

        try:
            name = _sanitize_name(d["name"])
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        if user.instances.count() >= user.max_instances:
            return Response(
                {"detail": f"Limite de {user.max_instances} instâncias atingido."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = {
            "name": name,
            "image": d["image"],
            "gpu_mode": d["gpu_mode"],
            "extra_props": d.get("extra_props", ""),
            "vpn_enabled": d.get("vpn_enabled", False),
            "vpn_config": d.get("vpn_config", ""),
            "vpn_username": d.get("vpn_username", ""),
            "vpn_password": d.get("vpn_password", ""),
        }
        try:
            task = create_instance_task.delay(user.id, payload)
        except Exception:
            logger.exception("create_instance: failed to enqueue task user=%s payload=%s", user.id, payload)
            return Response(
                {"detail": "Fila indisponível no momento. Tente novamente."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        logger.info("create_instance: enqueued user=%s job_id=%s name=%s", user.id, task.id, name)
        return Response(
            {"status": "queued", "job_id": task.id, "kind": "create_instance"},
            status=status.HTTP_202_ACCEPTED,
        )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        warnings: list[str] = []
        try:
            cleanup_procs(instance.serial)
        except Exception:
            pass
        try:
            DockerService.remove_instance_storage(instance.name)
        except Exception as e:
            # Log but do not block deletion — the DB record must be removed
            # so the user can recreate the instance with the same name.
            logger.warning("destroy: storage cleanup failed for %s: %s", instance.name, e)
            warnings.append(str(e))
        if instance.vpn_enabled:
            try:
                VpnService.remove_sidecar(instance.name)
            except Exception:
                pass
            VpnService.remove_config(instance.name)

        instance.delete()
        # Best-effort cleanup for stale folders from previous deployments.
        DockerService.cleanup_orphan_instance_data()
        resp: dict = {"status": "deleted"}
        if warnings:
            resp["warnings"] = warnings
        return Response(resp, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        instance = self.get_object()
        try:
            if instance.vpn_enabled:
                VpnService.start_sidecar(instance.name)
                VpnService.wait_tunnel(instance.name, timeout=20)
            DockerService.start(instance.name)
            TouchService.setup_virtual_touch(instance.name)
            TouchService.setup_virtual_keyboard(instance.name)
            AdbService.connect(instance.serial)
            if AdbService.wait_boot(instance.serial, 15):
                AdbService.dismiss_overlays(instance.serial)
        except RuntimeError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self._enrich(instance))

    @action(detail=True, methods=["post"])
    def stop(self, request, pk=None):
        instance = self.get_object()
        try:
            cleanup_procs(instance.serial)
            DockerService.stop(instance.name)
            if instance.vpn_enabled:
                VpnService.stop_sidecar(instance.name)
        except RuntimeError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self._enrich(instance))

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser])
    def install(self, request, pk=None):
        instance = self.get_object()
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Arquivo APK não enviado."}, status=status.HTTP_400_BAD_REQUEST)

        suffix = Path(f.name or "app.apk").suffix
        if suffix.lower() != ".apk":
            suffix = ".apk"
        jobs_dir = Path(settings.DATA_ROOT) / "_jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        tmp = jobs_dir / f"redroid-ui-{uuid.uuid4().hex}{suffix}"
        try:
            size = 0
            with tmp.open("wb") as out:
                for chunk in f.chunks():
                    size += len(chunk)
                    out.write(chunk)
            logger.info(
                "install_apk: request instance=%s file=%s size=%s",
                instance.name,
                f.name,
                size,
            )
            logger.info("install_apk: tmp written instance=%s path=%s", instance.name, tmp)
            try:
                task = install_apk_task.delay(request.user.id, instance.id, str(tmp), f.name or "app.apk")
            except Exception:
                logger.exception("install_apk: failed to enqueue task instance=%s", instance.name)
                tmp.unlink(missing_ok=True)
                return Response(
                    {"detail": "Fila indisponível no momento. Tente novamente."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            logger.info(
                "install_apk: enqueued user=%s instance=%s job_id=%s",
                request.user.id,
                instance.name,
                task.id,
            )
            return Response(
                {
                    "status": "queued",
                    "job_id": task.id,
                    "kind": "install_apk",
                    "instance_id": instance.id,
                    "filename": f.name,
                },
                status=status.HTTP_202_ACCEPTED,
            )
        except Exception:
            logger.exception("install_apk: unexpected error instance=%s", instance.name)
            return Response({"detail": "Erro interno ao instalar APK."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=["post"], url_path="cleanup_storage")
    def cleanup_storage(self, request):
        removed = DockerService.cleanup_orphan_instance_data()
        return Response({"status": "ok", "removed_folders": removed, "removed_count": len(removed)})

    @action(detail=True, methods=["get"], url_path="viewer_url")
    def viewer_url(self, request, pk=None):
        instance = self.get_object()
        token = AccessToken()
        token["instance_id"] = instance.id
        token["user_id"] = request.user.id
        token.set_exp(lifetime=timedelta(minutes=30))
        return Response({
            "instance_id": instance.id,
            "token": str(token),
        })

    @action(detail=True, methods=["put"], url_path="vpn")
    def update_vpn(self, request, pk=None):
        instance = self.get_object()
        ser = UpdateVpnSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data

        vpn_enabled = d.get("vpn_enabled", True)
        vpn_config = d.get("vpn_config", "")
        vpn_username = d.get("vpn_username", "")
        vpn_password = d.get("vpn_password", "")

        if vpn_enabled and vpn_config.strip():
            VpnService.store_config(instance.name, vpn_config, vpn_username, vpn_password)

        instance.vpn_enabled = vpn_enabled
        instance.vpn_username = vpn_username
        instance.vpn_password = vpn_password
        instance.save(update_fields=["vpn_enabled", "vpn_username", "vpn_password"])

        if not vpn_enabled:
            try:
                VpnService.stop_sidecar(instance.name)
                VpnService.remove_sidecar(instance.name)
            except Exception:
                pass
            VpnService.remove_config(instance.name)

        return Response(self._enrich(instance))

    @action(detail=True, methods=["post"], url_path="test_tap")
    def test_tap(self, request, pk=None):
        """Test if tap injection is working. Debug endpoint."""
        from .services import cmd_send_root
        instance = self.get_object()
        try:
            # Try to tap center of screen (360, 640 in 720x1280 coordinates)
            result = cmd_send_root(instance.serial, "input tap 360 640", timeout=5)
            return Response({
                "status": "ok" if result else "failed",
                "message": "Tap command executed" if result else "Tap command failed - may still work",
                "instance": instance.name,
                "serial": instance.serial,
            })
        except Exception as e:
            logger.exception("test_tap failed for %s", instance.name)
            return Response({
                "status": "error",
                "message": str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class JobStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, job_id: str):
        result = AsyncResult(job_id)
        state = result.state
        mapped = {
            "PENDING": "queued",
            "RECEIVED": "running",
            "STARTED": "running",
            "RETRY": "running",
            "SUCCESS": "success",
            "FAILURE": "error",
            "REVOKED": "error",
        }.get(state, "running")

        payload: dict[str, str] = {"job_id": job_id, "status": mapped, "state": state}
        if mapped == "success":
            payload["detail"] = "Tarefa concluída."
        elif mapped == "error":
            payload["detail"] = str(result.result)
        return Response(payload)
