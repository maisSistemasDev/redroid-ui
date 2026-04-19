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

from django.http import StreamingHttpResponse

from .models import Instance
from .serializers import CreateInstanceSerializer, InstanceSerializer, UpdateGpsSerializer, UpdateVpnSerializer
from .services import (
    AdbService,
    AdbShellService,
    BackupService,
    ClipboardService,
    DockerService,
    FileService,
    GpsService,
    TouchService,
    VpnService,
    cleanup_procs,
    get_device_profiles,
    resolve_serial,
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
        try:
            data["connection_status"] = AdbService.connection_status(resolve_serial(instance))
        except Exception:
            data["connection_status"] = "unknown"
        if instance.vpn_enabled:
            data["vpn_active"] = VpnService.sidecar_running(instance.name)
            data["vpn_connection_status"] = VpnService.connection_status(
                instance.name,
                vpn_type=instance.vpn_type,
            )
        else:
            data["vpn_active"] = False
            data["vpn_connection_status"] = "disabled"
        return data

    @action(detail=False, methods=["get"], url_path="device-profiles")
    def device_profiles(self, request):
        return Response(get_device_profiles())

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
            "device_profile": d.get("device_profile", ""),
            "magisk_enabled": d.get("magisk_enabled", False),
            "vpn_enabled": d.get("vpn_enabled", False),
            "vpn_type": d.get("vpn_type", "openvpn"),
            "vpn_provider": d.get("vpn_provider", "custom"),
            "vpn_config": d.get("vpn_config", ""),
            "vpn_ca_cert": d.get("vpn_ca_cert", ""),
            "vpn_username": d.get("vpn_username", ""),
            "vpn_password": d.get("vpn_password", ""),
            "vpn_country": d.get("vpn_country", ""),
            "fake_lat": d.get("fake_lat"),
            "fake_lng": d.get("fake_lng"),
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
            cleanup_procs(resolve_serial(instance))
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
                VpnService.start_sidecar(
                    instance.name,
                    vpn_type=instance.vpn_type,
                    provider=instance.vpn_provider,
                    username=instance.vpn_username,
                    password=instance.vpn_password,
                    country=getattr(instance, "vpn_country", ""),
                )
                VpnService.wait_tunnel(instance.name, timeout=60, vpn_type=instance.vpn_type)
            DockerService.start(instance.name)
            if instance.vpn_enabled:
                VpnService.setup_gateway(instance.name)
            TouchService.setup_virtual_touch(instance.name)
            TouchService.setup_virtual_keyboard(instance.name)
            serial = resolve_serial(instance)
            try:
                AdbService.connect(serial)
                if AdbService.wait_boot(serial, 15):
                    AdbService.dismiss_overlays(serial)
                    if instance.fake_lat is not None and instance.fake_lng is not None:
                        GpsService.set_location(serial, instance.fake_lat, instance.fake_lng)
            except Exception as e:
                logger.warning(
                    "start: instance %s started but ADB is not ready yet (%s)",
                    instance.name,
                    e,
                )
        except RuntimeError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self._enrich(instance))

    @action(detail=True, methods=["post"])
    def stop(self, request, pk=None):
        instance = self.get_object()
        try:
            cleanup_procs(resolve_serial(instance))
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
        tmp = jobs_dir / f"virtuale-{uuid.uuid4().hex}{suffix}"
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

    # ── GPS fake ──
    @action(detail=True, methods=["put"], url_path="gps")
    def update_gps(self, request, pk=None):
        instance = self.get_object()
        ser = UpdateGpsSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        lat = d.get("fake_lat")
        lng = d.get("fake_lng")

        instance.fake_lat = lat
        instance.fake_lng = lng
        instance.save(update_fields=["fake_lat", "fake_lng"])

        if lat is not None and lng is not None:
            serial = resolve_serial(instance)
            try:
                AdbService.connect(serial, retries=2, interval=2)
                GpsService.set_location(serial, lat, lng)
            except Exception as e:
                logger.warning("update_gps: failed to set location for %s: %s", instance.name, e)
        else:
            serial = resolve_serial(instance)
            try:
                GpsService.clear_location(serial)
            except Exception:
                pass

        return Response(self._enrich(instance))

    # ── Clipboard ──
    @action(detail=True, methods=["get"], url_path="clipboard")
    def get_clipboard(self, request, pk=None):
        instance = self.get_object()
        serial = resolve_serial(instance)
        text = ClipboardService.get_clipboard(serial)
        return Response({"text": text})

    @action(detail=True, methods=["post"], url_path="clipboard")
    def set_clipboard(self, request, pk=None):
        instance = self.get_object()
        text = request.data.get("text", "")
        serial = resolve_serial(instance)
        ok = ClipboardService.set_clipboard(serial, text)
        return Response({"status": "ok" if ok else "failed"})

    # ── File management ──
    @action(detail=True, methods=["get"], url_path="files")
    def list_files(self, request, pk=None):
        instance = self.get_object()
        path = request.query_params.get("path", "/sdcard/")
        serial = resolve_serial(instance)
        entries = FileService.list_files(serial, path)
        return Response({"path": path, "entries": entries})

    @action(detail=True, methods=["post"], url_path="files/upload", parser_classes=[MultiPartParser])
    def upload_file(self, request, pk=None):
        instance = self.get_object()
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Nenhum arquivo enviado."}, status=status.HTTP_400_BAD_REQUEST)
        remote_path = request.data.get("remote_path", "/sdcard/Download/")
        jobs_dir = Path(settings.DATA_ROOT) / "_jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        tmp = jobs_dir / f"upload-{uuid.uuid4().hex}-{f.name}"
        try:
            with tmp.open("wb") as out:
                for chunk in f.chunks():
                    out.write(chunk)
            serial = resolve_serial(instance)
            ok = FileService.push_file(serial, str(tmp), remote_path)
            return Response({"status": "ok" if ok else "failed", "filename": f.name})
        finally:
            tmp.unlink(missing_ok=True)

    @action(detail=True, methods=["get"], url_path="files/download")
    def download_file(self, request, pk=None):
        instance = self.get_object()
        remote_path = request.query_params.get("path", "")
        if not remote_path:
            return Response({"detail": "Parâmetro 'path' obrigatório."}, status=status.HTTP_400_BAD_REQUEST)
        serial = resolve_serial(instance)
        jobs_dir = Path(settings.DATA_ROOT) / "_jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        tmp = jobs_dir / f"download-{uuid.uuid4().hex}"
        try:
            ok = FileService.pull_file(serial, remote_path, str(tmp))
            if not ok or not tmp.exists():
                return Response({"detail": "Falha ao baixar arquivo."}, status=status.HTTP_404_NOT_FOUND)
            filename = remote_path.rsplit("/", 1)[-1] or "file"
            resp = StreamingHttpResponse(
                open(tmp, "rb"),
                content_type="application/octet-stream",
            )
            resp["Content-Disposition"] = f'attachment; filename="{filename}"'
            return resp
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    # ── Backup / Restore ──
    @action(detail=True, methods=["get"], url_path="backup")
    def backup(self, request, pk=None):
        instance = self.get_object()
        proc = BackupService.export_instance_stream(instance.name)
        if not proc or not proc.stdout:
            return Response({"detail": "Falha ao gerar backup."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        def stream():
            try:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                proc.terminate()
                proc.wait()

        resp = StreamingHttpResponse(stream(), content_type="application/gzip")
        resp["Content-Disposition"] = f'attachment; filename="{instance.name}-backup.tar.gz"'
        return resp

    @action(detail=True, methods=["post"], url_path="restore", parser_classes=[MultiPartParser])
    def restore(self, request, pk=None):
        instance = self.get_object()
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Arquivo de backup não enviado."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            is_running = DockerService.container_status(instance.name).get("running", False)
        except Exception:
            is_running = False
        if is_running:
            return Response({"detail": "Pare a instância antes de restaurar."}, status=status.HTTP_409_CONFLICT)

        jobs_dir = Path(settings.DATA_ROOT) / "_jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        tmp = jobs_dir / f"restore-{uuid.uuid4().hex}.tar.gz"
        try:
            with tmp.open("wb") as out:
                for chunk in f.chunks():
                    out.write(chunk)
            ok = BackupService.import_instance(instance.name, str(tmp))
            if not ok:
                return Response({"detail": "Falha ao restaurar backup."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            return Response({"status": "ok"})
        finally:
            tmp.unlink(missing_ok=True)

    # ── ADB Shell ──
    @action(detail=True, methods=["post"], url_path="shell")
    def adb_shell(self, request, pk=None):
        instance = self.get_object()
        command = request.data.get("command", "").strip()
        if not command:
            return Response({"detail": "Comando vazio."}, status=status.HTTP_400_BAD_REQUEST)
        serial = resolve_serial(instance)
        result = AdbShellService.execute(serial, command)
        return Response(result)

    @action(detail=True, methods=["put"], url_path="vpn")
    def update_vpn(self, request, pk=None):
        instance = self.get_object()
        ser = UpdateVpnSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data

        was_vpn_enabled = instance.vpn_enabled
        vpn_enabled = d.get("vpn_enabled", True)
        vpn_type = d.get("vpn_type", "openvpn")
        vpn_provider = d.get("vpn_provider", "custom")
        vpn_config = d.get("vpn_config", "")
        vpn_ca_cert = d.get("vpn_ca_cert", "")
        vpn_username = d.get("vpn_username", "")
        vpn_password = d.get("vpn_password", "")
        vpn_country = d.get("vpn_country", "")

        if vpn_enabled and vpn_config.strip():
            VpnService.store_config(instance.name, vpn_config, vpn_username, vpn_password, ca_cert=vpn_ca_cert)

        instance.vpn_enabled = vpn_enabled
        instance.vpn_type = vpn_type
        instance.vpn_provider = vpn_provider
        instance.vpn_username = vpn_username
        instance.vpn_password = vpn_password
        instance.vpn_country = vpn_country
        instance.save(update_fields=[
            "vpn_enabled", "vpn_type", "vpn_provider",
            "vpn_username", "vpn_password", "vpn_country",
        ])

        is_running = False
        try:
            is_running = DockerService.container_status(instance.name).get("running", False)
        except Exception:
            pass

        if is_running:
            if was_vpn_enabled:
                try:
                    VpnService.stop_sidecar(instance.name)
                except Exception:
                    logger.debug("update_vpn: stop_sidecar cleanup for %s (non-critical)", instance.name, exc_info=True)

            if vpn_enabled:
                try:
                    VpnService.start_sidecar(
                        instance.name,
                        vpn_type=vpn_type,
                        provider=vpn_provider,
                        username=vpn_username,
                        password=vpn_password,
                        country=vpn_country,
                    )
                    VpnService.wait_tunnel(instance.name, timeout=60, vpn_type=vpn_type)
                    VpnService.setup_gateway(instance.name)
                except RuntimeError as e:
                    logger.warning("update_vpn: sidecar restart failed for %s: %s", instance.name, e)
                    return Response(
                        {"detail": f"VPN salva mas falha ao ativar: {e}"},
                        status=status.HTTP_409_CONFLICT,
                    )
            else:
                VpnService.remove_config(instance.name)
        elif not vpn_enabled:
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
        serial = resolve_serial(instance)
        try:
            result = cmd_send_root(serial, "input tap 360 640", timeout=5)
            return Response({
                "status": "ok" if result else "failed",
                "message": "Tap command executed" if result else "Tap command failed - may still work",
                "instance": instance.name,
                "serial": serial,
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
