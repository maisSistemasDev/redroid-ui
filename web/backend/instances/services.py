from __future__ import annotations

import io
import json
import logging
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile
import time
import uuid
from pathlib import Path
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Architecture detection
# ---------------------------------------------------------------------------

def get_host_arch() -> str:
    """Return normalised host CPU arch: 'amd64' or 'arm64'."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return machine


def is_arm64() -> bool:
    return get_host_arch() == "arm64"
FORCE_DOCKER_INSTALL = (
    os.getenv("FORCE_DOCKER_INSTALL", os.getenv("REDROID_FORCE_DOCKER_INSTALL", "0"))
    .lower() in {"1", "true", "yes", "on"}
)

# ---------------------------------------------------------------------------
# Docker CLI helpers
# ---------------------------------------------------------------------------

DOCKER_PS_FILTER = "label=virtuale.managed=1"


def _docker_exe() -> str:
    return shutil.which("docker") or "docker"


def _docker_cmd() -> list[str]:
    exe = _docker_exe()
    if settings.USE_SUDO_DOCKER:
        return ["sudo", "-n", exe]
    return [exe]


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


_cached_app_network: str | None = None


def _detect_app_network() -> str:
    """Auto-detect a Docker network reachable from this container.

    Inspects the current container (by hostname, which Docker sets to the
    container ID) and returns the first non-bridge network found.  This
    allows the sidecar to be attached to a shared network even when
    ``DOCKER_NETWORK`` is not explicitly configured.
    """
    global _cached_app_network
    if _cached_app_network is not None:
        return _cached_app_network

    dc = _docker_cmd()
    cid = socket.gethostname()
    p = _run(dc + ["inspect", cid], timeout=10)
    if p.returncode != 0:
        logger.debug("_detect_app_network: cannot inspect self (%s): %s", cid, _short(p.stderr))
        _cached_app_network = ""
        return ""
    try:
        data = json.loads(p.stdout)
        if isinstance(data, list) and data:
            data = data[0]
        nets = (data.get("NetworkSettings") or {}).get("Networks") or {}
        for name in nets:
            if name not in ("bridge", "host", "none"):
                logger.info("_detect_app_network: auto-detected network '%s'", name)
                _cached_app_network = name
                return name
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    _cached_app_network = ""
    return ""


def _get_network() -> str:
    """Return the Docker network name for sidecar connectivity.

    Prefers the explicit ``DOCKER_NETWORK`` setting, falls back to
    auto-detection from the current container's networks.
    """
    net = getattr(settings, "DOCKER_NETWORK", "")
    if net:
        return net
    return _detect_app_network()


# ---------------------------------------------------------------------------
# Docker service
# ---------------------------------------------------------------------------

class DockerService:
    @staticmethod
    def _is_not_found_error(detail: str) -> bool:
        text = (detail or "").lower()
        return "no such container" in text or "no such object" in text

    @staticmethod
    def _ids() -> list[str]:
        p = _run(_docker_cmd() + ["ps", "-a", "-q", "--filter", DOCKER_PS_FILTER])
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "").strip())
        return [x for x in p.stdout.strip().split() if x]

    @staticmethod
    def inspect(name_or_id: str) -> dict[str, Any]:
        p = _run(_docker_cmd() + ["inspect", name_or_id])
        if p.returncode != 0:
            raise RuntimeError("Contêiner não encontrado.")
        return json.loads(p.stdout)[0]

    @staticmethod
    def container_status(name: str) -> dict[str, Any]:
        data = DockerService.inspect(name)
        state = data.get("State", {}) or {}
        return {
            "running": bool(state.get("Running")),
            "status": state.get("Status", "unknown"),
            "container_id": data.get("Id", "")[:12],
        }

    @staticmethod
    def create_container(name: str, image: str, gpu_mode: str, extra_props: str, user_id: int) -> str:
        data_dir = Path(settings.DATA_ROOT) / name
        # Best-effort local cleanup (works when backend has direct host access).
        if data_dir.exists():
            try:
                shutil.rmtree(data_dir)
            except Exception:
                pass

        # Wipe stale host data using a lightweight temp container BEFORE
        # starting Android.  This handles root-owned files and works in
        # Portainer / Docker-in-Docker where the backend cannot access the
        # host filesystem.  We must NOT wipe /data inside a running Android
        # container — that would corrupt the data partition mid-boot.
        dc = _docker_cmd()
        cleanup_done = False
        for cleanup_img in ("busybox", image):
            r = _run(dc + [
                "run", "--rm", "--entrypoint", "sh",
                "-v", f"{data_dir}:/mnt/stale",
                cleanup_img, "-c",
                "rm -rf /mnt/stale/* /mnt/stale/.[!.]*",
            ], timeout=120)
            if r.returncode == 0:
                cleanup_done = True
                break
        if not cleanup_done:
            logger.warning("create_container: pre-start data cleanup failed for %s", name)

        data_dir.mkdir(parents=True, exist_ok=True)
        cmd = dc + [
            "run", "-d",
            "--name", name,
            "--label", "virtuale.managed=1",
            "--label", f"virtuale.user={user_id}",
            "--privileged",
            "-v", f"{data_dir}:/data",
        ]
        network = _get_network()
        if network:
            cmd += ["--network", network]
        cmd += [
            image,
            f"androidboot.redroid_gpu_mode={gpu_mode}",
        ]
        image_lower = image.lower()

        if any(k in image_lower for k in ("gapps", "litegapps", "mindthegapps")):
            cmd += [
                "ro.setupwizard.mode=DISABLED",
                "ro.setupwizard.skipsetupwizard=1",
                "persist.sys.disable_rescue=true",
                "ro.opa.eligible_device=false",
            ]
        for line in extra_props.strip().splitlines():
            prop = line.strip()
            if prop and "=" in prop and re.match(r"^[a-zA-Z0-9_.]+=[^\s]*$", prop):
                cmd.append(prop)
        p = _run(cmd, timeout=300)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "").strip() or "Falha ao criar contêiner.")
        cid = p.stdout.strip()[:12]
        return cid

    @staticmethod
    def start(name: str) -> None:
        p = _run(_docker_cmd() + ["start", name])
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout).strip())

    @staticmethod
    def stop(name: str) -> None:
        p = _run(_docker_cmd() + ["stop", name])
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout).strip())

    @staticmethod
    def remove(name: str) -> None:
        p = _run(_docker_cmd() + ["rm", "-f", name])
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout).strip())

    @staticmethod
    def remove_instance_storage(name: str) -> None:
        dc = _docker_cmd()

        # 1) Wipe /data inside the container BEFORE removing it.
        #    This works even when the backend cannot access the host
        #    bind-mount path (e.g. running inside Docker / Portainer).
        try:
            inspected = DockerService.inspect(name)
            running = (inspected.get("State") or {}).get("Running", False)
            if not running:
                _run(dc + ["start", name], timeout=60)
            _run(dc + ["exec", name, "sh", "-c", "rm -rf /data/*"], timeout=60)
        except RuntimeError as e:
            if not DockerService._is_not_found_error(str(e)):
                logger.warning("remove_instance_storage: wipe /data failed for %s: %s", name, e)

        # 2) Remove the container.
        try:
            DockerService.remove(name)
        except RuntimeError as e:
            if not DockerService._is_not_found_error(str(e)):
                raise

        # 3) Best-effort removal of host-side data dir (works when backend
        #    has direct access to DATA_ROOT, e.g. local dev or same mount).
        try:
            DockerService.remove_instance_data(name)
        except Exception:
            pass

    @staticmethod
    def remove_instance_data(name: str) -> None:
        data_root = Path(settings.DATA_ROOT).resolve()
        target = (data_root / name).resolve()
        # Safety guard: only allow deleting inside configured DATA_ROOT.
        if target == data_root or data_root not in target.parents:
            raise RuntimeError("Caminho de dados inválido para remoção.")
        if target.exists():
            # Docker/Android creates files as root; Python shutil.rmtree may
            # lack permissions.  Try the easy way first, then escalate.
            try:
                shutil.rmtree(target)
            except PermissionError:
                if settings.USE_SUDO_DOCKER:
                    p = _run(["sudo", "-n", "rm", "-rf", str(target)], timeout=30)
                    if p.returncode != 0:
                        raise RuntimeError(
                            f"Falha ao remover dados (sudo): {(p.stderr or p.stdout or '').strip()}"
                        )
                else:
                    raise

    @staticmethod
    def cleanup_orphan_instance_data() -> list[str]:
        from .models import Instance

        data_root = Path(settings.DATA_ROOT).resolve()
        if not data_root.exists():
            return []
        active_names = set(Instance.objects.values_list("name", flat=True))
        removed: list[str] = []
        for child in data_root.iterdir():
            if not child.is_dir():
                continue
            if child.name.startswith("_"):
                continue
            if child.name in active_names:
                continue
            try:
                shutil.rmtree(child)
                removed.append(child.name)
            except PermissionError:
                if settings.USE_SUDO_DOCKER:
                    p = _run(["sudo", "-n", "rm", "-rf", str(child)], timeout=30)
                    if p.returncode == 0:
                        removed.append(child.name)
                # best effort cleanup; caller can still continue
            except Exception:
                continue
        return removed

    @staticmethod
    def list_images() -> list[dict[str, str]]:
        sep = "|||"
        fmt = sep.join(["{{.Repository}}:{{.Tag}}", "{{.ID}}", "{{.Size}}"])
        seen: set[str] = set()
        images: list[dict[str, str]] = []
        for ref in ("redroid/*", "redroid:*"):
            p = _run(_docker_cmd() + ["images", "--format", fmt, "--filter", f"reference={ref}"])
            if p.returncode != 0:
                raise RuntimeError((p.stderr or "").strip())
            for line in p.stdout.strip().splitlines():
                parts = line.split(sep)
                if len(parts) < 3:
                    continue
                tag = parts[0].strip()
                if "<none>" in tag or tag in seen:
                    continue
                seen.add(tag)
                images.append({"tag": tag, "id": parts[1].strip(), "size": parts[2].strip()})
        images.sort(key=lambda row: (0 if row["tag"] == "redroid:12-camera" else 1, row["tag"]))
        return images

    @staticmethod
    def remove_image(image_id: str) -> None:
        # Remove managed containers using this image and their data.
        p_containers = _run(
            _docker_cmd()
            + [
                "ps",
                "-a",
                "--filter",
                f"ancestor={image_id}",
                "--filter",
                DOCKER_PS_FILTER,
                "--format",
                "{{.Names}}",
            ],
            timeout=60,
        )
        if p_containers.returncode != 0:
            raise RuntimeError((p_containers.stderr or p_containers.stdout or "").strip())
        for cname in [x.strip() for x in (p_containers.stdout or "").splitlines() if x.strip()]:
            DockerService.remove_instance_storage(cname)

        p = _run(_docker_cmd() + ["rmi", "-f", image_id])
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "").strip())


# ---------------------------------------------------------------------------
# ADB service
# ---------------------------------------------------------------------------

def _short(msg: str, limit: int = 400) -> str:
    text = (msg or "").strip().replace("\n", " | ")
    if len(text) > limit:
        return text[:limit] + "..."
    return text


class AdbService:
    @staticmethod
    def connection_status(serial: str) -> str:
        """Return ADB connection status for a serial.

        Possible values: connected, offline, unauthorized, disconnected, unknown.
        """
        try:
            p = _run(["adb", "devices"], timeout=10)
        except Exception:
            return "unknown"

        if p.returncode != 0:
            return "unknown"

        for ln in (p.stdout or "").splitlines():
            line = ln.strip()
            if not line.startswith(serial + "\t"):
                continue
            state = line.split("\t", 1)[1].strip().lower()
            if state in {"device", "offline", "unauthorized"}:
                return "connected" if state == "device" else state
            return "unknown"

        return "disconnected"

    @staticmethod
    def connect(serial: str, retries: int = 6, interval: int = 5) -> None:
        """Connect ADB to the given serial, retrying on timeout.

        Freshly-created containers (especially with VPN sidecar) can take
        30-60 s before the ADB daemon starts listening on port 5555.
        """
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                p = _run(["adb", "connect", serial], timeout=15)
                out = f"{p.stdout}\n{p.stderr}".lower()
                if "connected to" in out or "already connected" in out:
                    return
                # Some adb versions may return exit code 0 even when
                # the target is not actually available yet. Confirm via
                # `adb devices` before considering the connect successful.
                d = _run(["adb", "devices"], timeout=10)
                lines = (d.stdout or "").splitlines()
                serial_ok = any(
                    ln.strip().startswith(serial + "\tdevice")
                    for ln in lines
                )
                if serial_ok:
                    return
                last_exc = RuntimeError(
                    f"Falha ao conectar ADB em {serial}: {(p.stderr or p.stdout).strip()}"
                )
            except subprocess.TimeoutExpired as exc:
                last_exc = exc
            if attempt < retries:
                logger.info("ADB connect attempt %d/%d failed for %s, retrying in %ds", attempt, retries, serial, interval)
                time.sleep(interval)
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def wait_boot(serial: str, timeout: int = 30) -> bool:
        for _ in range(timeout // 2):
            try:
                p = _run(["adb", "-s", serial, "shell", "getprop", "sys.boot_completed"], timeout=5)
                if p.returncode == 0 and "1" in p.stdout:
                    return True
            except subprocess.TimeoutExpired:
                pass
            time.sleep(2)
        return False

    @staticmethod
    def install_apk(serial: str, apk_path: str, container_hint: str = "") -> str:
        adb_detail = ""
        if FORCE_DOCKER_INSTALL:
            ok, docker_detail = _install_apk_via_docker_exec(serial, apk_path, container_hint=container_hint)
            if ok:
                return "installed"
            raise RuntimeError(f"Falha no install de APK via docker exec (modo forçado): {_short(docker_detail)}")

        try:
            AdbService.connect(serial)
            # Keep ADB path reasonably bounded to avoid reverse-proxy 502 by timeout.
            try:
                p = _run(["adb", "-s", serial, "install", "-r", apk_path], timeout=90)
            except subprocess.TimeoutExpired:
                logger.warning("install_apk: adb install timed out for %s", serial)
                adb_detail = "adb install timeout (90s)"
            else:
                if p.returncode == 0:
                    return "installed"
                adb_detail = _short(f"rc={p.returncode} out={(p.stdout or '').strip()} err={(p.stderr or '').strip()}")
                logger.warning(
                    "install_apk: adb install failed for %s, fallback to docker exec. out=%s err=%s",
                    serial,
                    (p.stdout or "").strip(),
                    (p.stderr or "").strip(),
                )
        except Exception as exc:
            adb_detail = _short(str(exc))
            logger.warning("install_apk: adb path failed for %s, fallback to docker exec: %s", serial, exc)

        ok, docker_detail = _install_apk_via_docker_exec(serial, apk_path, container_hint=container_hint)
        if ok:
            return "installed"
        raise RuntimeError(
            "Falha no install de APK (adb e fallback docker exec). "
            f"ADB={_short(adb_detail)} | Docker={_short(docker_detail)}"
        )

    @staticmethod
    def dismiss_overlays(serial: str) -> None:
        """Dismiss system overlays that trigger filterTouchesWhenObscured.

        WhatsApp (and other security-sensitive apps) refuse touch input
        when any overlay window is drawn on top.  After a fresh GApps
        install, Google Play Services often shows setup dialogs/alerts
        that count as overlays.
        """
        for cmd in [
            # Close any visible system dialog ("app has stopped", etc.)
            "am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS",
            # Revoke overlay permission from GMS so it can't create new ones
            "appops set com.google.android.gms SYSTEM_ALERT_WINDOW deny",
            "appops set com.google.android.gms RUN_ANY_IN_BACKGROUND deny",
            # Dismiss immersive mode confirmation banner
            "settings put secure immersive_mode_confirmations confirmed",
            # Kill GMS overlay processes to close existing overlays
            "am force-stop com.google.android.gms.setup",
            # Revoke overlay permissions from WhatsApp as well
            "appops set com.whatsapp SYSTEM_ALERT_WINDOW deny 2>/dev/null || true",
            # Disable gesture navigation hints that can interfere with touch
            "settings put secure navbar_gesture_hint_enabled 0",
        ]:
            try:
                _run(["adb", "-s", serial, "shell", cmd], timeout=8)
            except Exception:
                pass

    @staticmethod
    def detect_event_device(serial: str) -> str:
        """Detect goodix-ts touch device by checking /sys/class/input/event*/device/name.

        More robust than shell for-loop which can fail on some adb versions.
        """
        # Try event devices in range 0-31 (common range)
        for event_num in range(32):
            try:
                result = subprocess.run(
                    ["adb", "-s", serial, "shell", f"cat /sys/class/input/event{event_num}/device/name 2>/dev/null"],
                    capture_output=True, text=True, timeout=2, check=False
                )
                if "goodix-ts" in result.stdout:
                    return f"/dev/input/event{event_num}"
            except Exception:
                continue
        return "/dev/input/event18"

    @staticmethod
    def detect_keyboard_device(serial: str) -> str:
        """Detect qemu-keyboard device by checking /sys/class/input/event*/device/name.

        More robust than shell for-loop which can fail on some adb versions.
        """
        # Try event devices in range 0-31 (common range)
        for event_num in range(32):
            try:
                result = subprocess.run(
                    ["adb", "-s", serial, "shell", f"cat /sys/class/input/event{event_num}/device/name 2>/dev/null"],
                    capture_output=True, text=True, timeout=2, check=False
                )
                if "qemu-keyboard" in result.stdout:
                    dev = f"/dev/input/event{event_num}"
                    logger.info("detect_keyboard_device: found keyboard as event%d for %s", event_num, serial)
                    return dev
            except Exception:
                continue
        logger.warning("detect_keyboard_device: qemu-keyboard device not found for %s (create_keyboard may not have run)", serial)
        return "/dev/input/event19"


# ---------------------------------------------------------------------------
# Touch / virtual input setup
# ---------------------------------------------------------------------------

class TouchService:
    @staticmethod
    def setup_virtual_touch(name: str) -> None:
        binary = Path(settings.TOOLS_DIR) / "create_touch"
        if not binary.exists():
            logger.error(
                "setup_virtual_touch: create_touch binary NOT FOUND at %s "
                "(TOOLS_DIR=%s) — touch will NOT work!",
                binary, settings.TOOLS_DIR,
            )
            return
        logger.info("setup_virtual_touch: deploying create_touch to %s", name)
        dc = _docker_cmd()
        p = _run(dc + ["cp", str(binary), f"{name}:/create_touch"])
        if p.returncode != 0:
            logger.error("setup_virtual_touch: docker cp failed: %s", (p.stderr or p.stdout or "").strip())
            return
        _run(dc + ["exec", name, "chmod", "+x", "/create_touch"])
        _run(dc + ["exec", name, "sh", "-c", "pkill -f create_touch 2>/dev/null || true"])
        time.sleep(0.5)
        _run(dc + ["exec", name, "sh", "-c", "rm -f /dev/input/event*"])
        _run(dc + ["exec", "-d", name, "sh", "-c",
             "while true; do /create_touch; sleep 1; done"])
        logger.info("setup_virtual_touch: create_touch daemon started for %s", name)
        time.sleep(1.5)
        _run(dc + ["exec", name, "sh", "-c", """
for d in /sys/devices/virtual/input/input*/; do
  n=$(cat "$d/name" 2>/dev/null)
  if [ "$n" = "goodix-ts" ]; then
    for ev in "$d"/event*/; do
      devnums=$(cat "$ev/dev" 2>/dev/null)
      evname=$(basename "$ev")
      major=$(echo "$devnums" | cut -d: -f1)
      minor=$(echo "$devnums" | cut -d: -f2)
      rm -f "/dev/input/$evname"
      mknod "/dev/input/$evname" c "$major" "$minor"
      chmod 660 "/dev/input/$evname"
      chown root:input "/dev/input/$evname"
    done
  fi
done
"""])

    @staticmethod
    def setup_virtual_keyboard(name: str) -> None:
        """Setup virtual keyboard device for keyboard input injection."""
        binary = Path(settings.TOOLS_DIR) / "create_keyboard"
        if not binary.exists():
            logger.error("setup_virtual_keyboard: create_keyboard binary not found at %s - keyboard injection will not work!", binary)
            return
        logger.info("setup_virtual_keyboard: deploying create_keyboard binary to %s", name)
        dc = _docker_cmd()

        # Deploy binary
        p = _run(dc + ["cp", str(binary), f"{name}:/create_keyboard"])
        if p.returncode != 0:
            logger.error("setup_virtual_keyboard: docker cp failed for %s: %s", name, p.stderr or p.stdout)
            return

        # Make executable
        p = _run(dc + ["exec", name, "chmod", "+x", "/create_keyboard"])
        if p.returncode != 0:
            logger.error("setup_virtual_keyboard: chmod failed for %s: %s", name, p.stderr or p.stdout)
            return

        _run(dc + ["exec", name, "sh", "-c", "pkill -f create_keyboard 2>/dev/null || true"])
        time.sleep(0.5)

        p = _run(dc + ["exec", "-d", name, "sh", "-c",
                 "while true; do /create_keyboard; sleep 1; done"])
        if p.returncode != 0:
            logger.error("setup_virtual_keyboard: failed to start create_keyboard daemon for %s: %s", name, p.stderr or p.stdout)
            return
        logger.info("setup_virtual_keyboard: create_keyboard daemon started for %s", name)
        time.sleep(1.0)

        # Ensure device node exists with correct permissions
        p = _run(dc + ["exec", name, "sh", "-c", """
for d in /sys/devices/virtual/input/input*/; do
  n=$(cat "$d/name" 2>/dev/null)
  if [ "$n" = "qemu-keyboard" ]; then
    for ev in "$d"/event*/; do
      devnums=$(cat "$ev/dev" 2>/dev/null)
      evname=$(basename "$ev")
      major=$(echo "$devnums" | cut -d: -f1)
      minor=$(echo "$devnums" | cut -d: -f2)
      rm -f "/dev/input/$evname"
      mknod "/dev/input/$evname" c "$major" "$minor"
      chmod 660 "/dev/input/$evname"
      chown root:input "/dev/input/$evname"
    done
  fi
done
"""])
        if p.returncode != 0:
            logger.warning("setup_virtual_keyboard: device node setup may have issues for %s: %s", name, p.stderr or p.stdout)
        else:
            logger.info("setup_virtual_keyboard: keyboard device node verified for %s", name)

    @staticmethod
    def deploy_touch_inject(serial: str) -> None:
        binary = Path(settings.TOOLS_DIR) / "touch_inject"
        if not binary.exists():
            return
        check = _run(["adb", "-s", serial, "shell", "test -x /touch_inject && echo OK"], timeout=5)
        if "OK" in (check.stdout or ""):
            return
        container_name = _serial_container_name(serial)
        if not container_name:
            return
        dc = _docker_cmd()
        _run(dc + ["cp", str(binary), f"{container_name}:/touch_inject"])
        _run(dc + ["exec", container_name, "chmod", "+x", "/touch_inject"])





# ---------------------------------------------------------------------------
# Persistent process managers for input injection
# ---------------------------------------------------------------------------

_injectors: dict[str, subprocess.Popen] = {}
_cmd_shells: dict[str, subprocess.Popen] = {}

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
SYN_REPORT = 0
ABS_X, ABS_Y = 0, 1
ABS_MT_SLOT, ABS_MT_TOUCH_MAJOR, ABS_MT_TRACKING_ID = 47, 48, 57
ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 53, 54
ABS_MT_PRESSURE = 58
BTN_TOUCH = 330


def get_injector(serial: str, dev: str) -> subprocess.Popen:
    proc = _injectors.get(serial)
    if proc and proc.poll() is None:
        return proc
    TouchService.deploy_touch_inject(serial)
    proc = subprocess.Popen(
        ["adb", "-s", serial, "shell", "-T", f"/touch_inject {dev}"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _injectors[serial] = proc
    return proc


def get_cmd_shell(serial: str) -> subprocess.Popen:
    proc = _cmd_shells.get(serial)
    if proc and proc.poll() is None:
        return proc
    proc = subprocess.Popen(
        ["adb", "-s", serial, "shell"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _cmd_shells[serial] = proc
    return proc


def inject_events(serial: str, dev: str, events: list[tuple[int, int, int]]) -> bool:
    try:
        proc = get_injector(serial, dev)
        if proc.poll() is not None:
            logger.warning("inject_events: injector exited for %s (%s), restarting", serial, dev)
            _injectors.pop(serial, None)
            proc = get_injector(serial, dev)
            if proc.poll() is not None:
                logger.warning("inject_events: injector restart failed for %s (%s)", serial, dev)
                _injectors.pop(serial, None)
                return False
        if proc.stdin is None:
            logger.warning("inject_events: injector stdin unavailable for %s (%s)", serial, dev)
            _injectors.pop(serial, None)
            return False
        lines = "".join(f"{t} {c} {v}\n" for t, c, v in events)
        proc.stdin.write(lines.encode())
        proc.stdin.flush()
        return True
    except (BrokenPipeError, OSError):
        _injectors.pop(serial, None)
        logger.warning("inject_events: broken pipe while sending to %s (%s)", serial, dev)
        return False


def inject_keyboard_events(serial: str, dev: str, events: list[tuple[int, int, int]]) -> bool:
    """Inject keyboard events (EV_KEY) via uinput device.

    Similar to inject_events but for keyboard input.
    Events format: (EV_KEY, keycode, 1=pressed/0=released)
    """
    try:
        proc = get_injector(serial, dev)
        if proc.poll() is not None:
            logger.warning("inject_keyboard_events: injector exited for %s (%s), restarting", serial, dev)
            _injectors.pop(serial, None)
            proc = get_injector(serial, dev)
            if proc.poll() is not None:
                logger.warning("inject_keyboard_events: injector restart failed for %s (%s)", serial, dev)
                _injectors.pop(serial, None)
                return False
        if proc.stdin is None:
            logger.warning("inject_keyboard_events: injector stdin unavailable for %s (%s)", serial, dev)
            _injectors.pop(serial, None)
            return False
        lines = "".join(f"{t} {c} {v}\n" for t, c, v in events)
        proc.stdin.write(lines.encode())
        proc.stdin.flush()
        return True
    except (BrokenPipeError, OSError):
        _injectors.pop(serial, None)
        logger.warning("inject_keyboard_events: broken pipe while sending to %s (%s)", serial, dev)
        return False


def cmd_send(serial: str, cmd: str) -> None:
    try:
        proc = get_cmd_shell(serial)
        if proc.poll() is not None:
            _cmd_shells.pop(serial, None)
            proc = get_cmd_shell(serial)
            if proc.poll() is not None:
                raise BrokenPipeError("persistent adb shell exited")
        if proc.stdin is None:
            raise BrokenPipeError("persistent adb shell stdin unavailable")
        proc.stdin.write((cmd + "\n").encode())
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        _cmd_shells.pop(serial, None)
        logger.warning("cmd_send: persistent shell failed for %s; cmd=%s", serial, cmd)


def cmd_send_direct(serial: str, cmd: str, timeout: int = 12) -> bool:
    """
    Execute one adb shell command and return success.
    More reliable as fallback when persistent shell is unstable.
    """
    try:
        AdbService.connect(serial)
    except Exception:
        # Keep going; adb shell can still work without a fresh connect.
        pass
    # IMPORTANT: pass a single remote shell string to adb.
    # Using split args with `sh -c` can drop command arguments in some environments.
    remote = f"sh -lc {shlex.quote(cmd)}"
    p = _run(["adb", "-s", serial, "shell", remote], timeout=timeout)
    ok = p.returncode == 0
    if not ok:
        logger.warning(
            "cmd_send_direct failed for %s (rc=%s): %s | out=%s | err=%s",
            serial,
            p.returncode,
            cmd,
            (p.stdout or "").strip(),
            (p.stderr or "").strip(),
        )
    return ok


def _serial_container_name(serial: str) -> str:
    """Extract the Android container name from an ADB serial.

    When VPN is active the serial looks like ``{name}-vpn:5555`` but the
    Android container is simply ``{name}``.  Strip the ``-vpn`` suffix so
    that ``docker exec`` targets the correct container.
    """
    host = serial.split(":", 1)[0].strip()
    if not host:
        return ""
    if not re.match(r"^[a-zA-Z0-9_.-]+$", host):
        return ""
    # VPN sidecar names end with '-vpn'; the Android container is without.
    if host.endswith("-vpn"):
        host = host[:-4]
    return host


def _container_ref(serial: str, container_hint: str = "") -> str:
    hint = (container_hint or "").strip()
    if hint and re.match(r"^[a-zA-Z0-9_.-]+$", hint):
        return hint
    return _serial_container_name(serial)


def _install_apk_via_docker_exec(serial: str, apk_path: str, container_hint: str = "") -> tuple[bool, str]:
    cname = _container_ref(serial, container_hint=container_hint)
    if not cname:
        return False, "container target inválido"
    src = Path(apk_path)
    if not src.exists():
        return False, f"arquivo local não encontrado: {src}"

    dst = f"/data/local/tmp/virtuale-{uuid.uuid4().hex}.apk"
    try:
        cp = _run(_docker_cmd() + ["cp", str(src), f"{cname}:{dst}"], timeout=120)
    except subprocess.TimeoutExpired:
        logger.warning("install_apk fallback: docker cp timed out for %s", cname)
        return False, "docker cp timeout (120s)"
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip()
        logger.warning(
            "install_apk fallback: docker cp failed for %s: %s",
            cname,
            detail,
        )
        return False, f"docker cp falhou para {cname}: {detail}"

    try:
        # pm install should output "Success" on success.
        cmd = f"pm install -r {shlex.quote(dst)}"
        try:
            p = _run(_docker_cmd() + ["exec", cname, "sh", "-c", cmd], timeout=300)
        except subprocess.TimeoutExpired:
            logger.warning("install_apk fallback: pm install timed out for %s", cname)
            return False, "pm install timeout (300s)"
        out = f"{p.stdout}\n{p.stderr}".strip()
        ok = p.returncode == 0 and "Success" in out
        if not ok:
            logger.warning(
                "install_apk fallback: pm install failed for %s (rc=%s): %s",
                cname,
                p.returncode,
                out,
            )
            return False, f"pm install rc={p.returncode}: {out}"
        return True, "ok"
    finally:
        _run(_docker_cmd() + ["exec", cname, "sh", "-c", f"rm -f {shlex.quote(dst)}"], timeout=30)


def cmd_send_best_effort(serial: str, cmd: str, timeout: int = 12) -> bool:
    """
    Try adb shell first; if it fails, fallback to docker exec in the target container.
    Useful when container-to-container ADB routing is unstable in production.
    """
    if cmd_send_direct(serial, cmd, timeout=timeout):
        return True

    cname = _serial_container_name(serial)
    if not cname:
        logger.warning("cmd_send_best_effort: invalid container name from serial %s", serial)
        return False

    p = _run(_docker_cmd() + ["exec", cname, "sh", "-c", cmd], timeout=timeout)
    ok = p.returncode == 0
    if not ok:
        logger.warning(
            "cmd_send_best_effort docker-exec failed for %s (rc=%s): %s | out=%s | err=%s",
            cname,
            p.returncode,
            cmd,
            (p.stdout or "").strip(),
            (p.stderr or "").strip(),
        )
    return ok


def cmd_send_root(serial: str, cmd: str, timeout: int = 12) -> bool:
    """Execute command as root via docker exec, with adb fallback.

    Some apps (e.g. WhatsApp) use ``filterTouchesWhenObscured`` and reject
    touch events injected from non-root UIDs.  Running ``input tap`` via
    ``docker exec`` sends the event as UID 0 (root), which Android treats
    as a trusted source, bypassing the filter.

    For input tap commands, docker exec is required - adb shell fallback
    will be silently rejected by WhatsApp.
    """
    cname = _serial_container_name(serial)
    if cname:
        p = _run(_docker_cmd() + ["exec", cname, "sh", "-c", cmd], timeout=timeout)
        if p.returncode == 0:
            return True
        # Log failure for debugging
        if cmd.startswith("input "):
            logger.warning(
                "cmd_send_root: docker exec failed for %s (rc=%s), cmd=%s, err=%s",
                serial,
                p.returncode,
                cmd,
                (p.stderr or p.stdout or "").strip()[:200],
            )
        # For input commands, don't fallback to adb — docker exec may have
        # already executed the command despite non-zero exit code (sh -l
        # profile issues), and adb shell runs with a different UID which
        # gets rejected by apps using filterTouchesWhenObscured.
        if cmd.startswith("input "):
            return False
    return cmd_send_direct(serial, cmd, timeout=timeout)


def cmd_sendevent_tap(serial: str, dev: str, x: int, y: int,
                      tracking_id: int = 1, timeout: int = 8) -> bool:
    """Tap via ``sendevent`` through ADB — bypasses filterTouchesWhenObscured.

    ``sendevent`` writes directly to the kernel input device, so Android's
    WindowManager overlay/obscured checks are never consulted.  Unlike
    ``input tap`` (which uses InputManager.injectInputEvent and is subject
    to the obscured flag), ``sendevent`` events arrive as genuine hardware
    events from the kernel's perspective.

    Uses ADB shell (not docker exec) because the Android runtime manages
    its own ``/dev`` mount — device nodes created by ueventd are only
    visible inside the Android namespace, which ADB's adbd runs in.
    """
    if not dev:
        return False

    script = (
        f"sendevent {dev} 3 47 0 && sendevent {dev} 3 57 {tracking_id} && "
        f"sendevent {dev} 3 53 {x} && sendevent {dev} 3 54 {y} && "
        f"sendevent {dev} 3 48 5 && sendevent {dev} 3 58 50 && "
        f"sendevent {dev} 1 330 1 && sendevent {dev} 0 0 0 && "
        "sleep 0.06 && "
        f"sendevent {dev} 3 48 0 && sendevent {dev} 3 58 0 && "
        f"sendevent {dev} 3 57 4294967295 && "
        f"sendevent {dev} 1 330 0 && sendevent {dev} 0 0 0"
    )

    try:
        AdbService.connect(serial)
    except Exception:
        pass
    p = _run(["adb", "-s", serial, "shell", script], timeout=timeout)
    if p.returncode != 0:
        logger.warning(
            "cmd_sendevent_tap: failed for %s dev=%s (%d,%d) rc=%s err=%s",
            serial, dev, x, y, p.returncode,
            (p.stderr or p.stdout or "").strip()[:200],
        )
    return p.returncode == 0


def sendevent_down(serial: str, dev: str, x: int, y: int, tracking_id: int) -> None:
    """Send touch-down via persistent adb shell + sendevent (real-time drag)."""
    if not dev:
        return
    cmd_send(serial, (
        f"sendevent {dev} 3 47 0;"
        f"sendevent {dev} 3 57 {tracking_id};"
        f"sendevent {dev} 3 53 {x};"
        f"sendevent {dev} 3 54 {y};"
        f"sendevent {dev} 3 48 5;"
        f"sendevent {dev} 3 58 50;"
        f"sendevent {dev} 1 330 1;"
        f"sendevent {dev} 0 0 0"
    ))


def sendevent_move(serial: str, dev: str, x: int, y: int) -> None:
    """Send position update via persistent adb shell + sendevent (real-time drag)."""
    if not dev:
        return
    cmd_send(serial, (
        f"sendevent {dev} 3 47 0;"
        f"sendevent {dev} 3 53 {x};"
        f"sendevent {dev} 3 54 {y};"
        f"sendevent {dev} 3 48 5;"
        f"sendevent {dev} 3 58 50;"
        f"sendevent {dev} 0 0 0"
    ))


def sendevent_up(serial: str, dev: str) -> None:
    """Send touch-up via persistent adb shell + sendevent (real-time drag)."""
    if not dev:
        return
    cmd_send(serial, (
        f"sendevent {dev} 3 48 0;"
        f"sendevent {dev} 3 58 0;"
        f"sendevent {dev} 3 57 4294967295;"
        f"sendevent {dev} 1 330 0;"
        f"sendevent {dev} 0 0 0"
    ))


def clamp_coords(nx: float, ny: float) -> tuple[int, int]:
    return (min(int(nx * 719), 719), min(int(ny * 1279), 1279))


def cleanup_procs(serial: str) -> None:
    for store in (_injectors, _cmd_shells):
        proc = store.pop(serial, None)
        if proc and proc.poll() is None:
            proc.terminate()


# ---------------------------------------------------------------------------
# Port allocation
# ---------------------------------------------------------------------------
# Device profiles & spoof logic
# ---------------------------------------------------------------------------
# Magisk support
# ---------------------------------------------------------------------------

MAGISK_APK_URL = (
    "https://cdn.jsdelivr.net/gh/topjohnwu/magisk-files"
    "@1cea72840fbf690f9a95512d03721f6a710fe02e/app-debug.apk"
)

MAGISK_INIT_RC = """\
on early-init
\texport PATH /sbin:/product/bin:/apex/com.android.runtime/bin:/apex/com.android.art/bin:/system_ext/bin:/system/bin:/system/xbin:/odm/bin:/vendor/bin:/vendor/xbin
\tchmod 0700 /magisk.tar
\tchown root root /magisk.tar
\tchmod 0700 /setup.sh
\tchown root root /setup.sh
\texec root root -- /setup.sh
service magisk-d /sbin/magisk --daemon
\tuser root
\toneshot
on boot
\tstart magisk-d
on post-fs-data
\tstart logd
\trm /dev/.magisk-unblock
\tstart s1
\twait /dev/.magisk-unblock 5
\trm /dev/.magisk-unblock
service s1 /sbin/magisk --post-fs-data
\tuser root
\toneshot
service s2 /sbin/magisk --service
\tclass late_start
\tuser root
\toneshot
on property:sys.boot_completed=1
\texec /sbin/magisk --boot-complete
on property:init.svc.zygote=restarting
\texec /sbin/magisk --zygote-restart
on property:init.svc.zygote=stopped
\texec /sbin/magisk --zygote-restart
"""

MAGISK_SETUP_SH = """\
#!/system/bin/sh
tmpPushed=/magisk
rm -rf $tmpPushed
mkdir $tmpPushed
tar -xf /magisk.tar --no-same-owner -C $tmpPushed
umount /magisk.tar ; rm -f /magisk.tar

# Remove ALL pre-existing su binaries to avoid "Abnormal State" error.
# Redroid userdebug images ship their own su that conflicts with Magisk.
rm -f /system/xbin/su /system/bin/su /system/bin/.ext/.su \
      /product/bin/su /vendor/bin/su /odm/bin/su \
      /system/xbin/daemonsu /system/bin/daemonsu \
      /sbin/su 2>/dev/null

mkdir -p /sbin
chown root:root /sbin
chmod 0751 /sbin
cp $tmpPushed/magisk /sbin/
cp $tmpPushed/app-debug.apk /sbin/stub.apk
find /sbin -type f -exec chmod 0755 {} \\;
find /sbin -type f -exec chown root:root {} \\;
ln -f -s /sbin/magisk /system/xbin/su
mkdir -p /product/bin
chmod 751 /product/bin
ln -f -s /sbin/magisk /product/bin/su
mkdir -p /data/adb/magisk
chmod 700 /data/adb
mv $tmpPushed/busybox /data/adb/magisk/
chmod -R 755 /data/adb/magisk
chown -R root:root /data/adb/magisk
rm -rf $tmpPushed
"""


class MagiskService:

    @staticmethod
    def apply_magisk(base_image: str) -> str:
        """Create a Magisk-enabled Docker image via docker commit.

        Flow: docker create -> docker cp (inject files) -> docker commit.
        Returns the new image tag (e.g. 'redroid:14.0.0-latest-magisk').
        Reuses existing image if already built.
        """
        if ":" in base_image:
            new_tag = f"{base_image}-magisk"
        else:
            new_tag = f"{base_image}:magisk"

        dc = _docker_cmd()

        p = _run(dc + ["image", "inspect", new_tag], timeout=15)
        if p.returncode == 0:
            logger.info("apply_magisk: image %s already exists, reusing", new_tag)
            return new_tag

        logger.info("apply_magisk: downloading Magisk APK...")
        import urllib.request
        with tempfile.TemporaryDirectory(prefix="magisk-") as tmpdir:
            tmpdir_p = Path(tmpdir)
            apk_path = tmpdir_p / "app-debug.apk"
            try:
                urllib.request.urlretrieve(MAGISK_APK_URL, str(apk_path))
            except Exception as e:
                raise RuntimeError(f"Failed to download Magisk APK: {e}")

            magisk_bin_dir = tmpdir_p / "magisk_bin"
            magisk_bin_dir.mkdir()
            abi = "arm64-v8a" if is_arm64() else "x86_64"
            logger.info("apply_magisk: extracting binaries for ABI %s", abi)
            with zipfile.ZipFile(str(apk_path)) as zf:
                for member in (f"lib/{abi}/libmagisk64.so", f"lib/{abi}/libbusybox.so"):
                    data = zf.read(member)
                    out_name = "magisk" if "magisk64" in member else "busybox"
                    out_path = magisk_bin_dir / out_name
                    out_path.write_bytes(data)
                    out_path.chmod(0o755)

            tar_path = tmpdir_p / "magisk.tar"
            p = subprocess.run(
                ["tar", "--transform", "s/.*\\///g", "-cf", str(tar_path),
                 str(magisk_bin_dir / "magisk"),
                 str(magisk_bin_dir / "busybox"),
                 str(apk_path)],
                timeout=30, capture_output=True, text=True,
            )
            if p.returncode != 0:
                raise RuntimeError(f"Failed to create magisk.tar: {p.stderr.strip()}")

            tmp_name = f"virtuale-magisk-{uuid.uuid4().hex[:8]}"
            try:
                logger.info("apply_magisk: creating temp container from %s", base_image)
                p = _run(dc + ["create", "--name", tmp_name, base_image], timeout=120)
                if p.returncode != 0:
                    raise RuntimeError(f"Failed to create temp container: {(p.stderr or p.stdout or '').strip()}")

                rc_content = MAGISK_INIT_RC.encode()
                setup_content = MAGISK_SETUP_SH.encode()
                tar_content = tar_path.read_bytes()

                ok = _docker_cp_write(tmp_name, "system/etc/init", "magisk_init.rc", rc_content, mode=0o644)
                if not ok:
                    raise RuntimeError("Failed to copy magisk_init.rc")
                ok = _docker_cp_write(tmp_name, "", "setup.sh", setup_content, mode=0o700)
                if not ok:
                    raise RuntimeError("Failed to copy setup.sh")
                ok = _docker_cp_write(tmp_name, "", "magisk.tar", tar_content, mode=0o700)
                if not ok:
                    raise RuntimeError("Failed to copy magisk.tar")

                # Replace stock su binaries with symlinks to Magisk's su.
                # Redroid userdebug ships its own su in /system/xbin/su
                # which triggers Magisk's "Abnormal State" detection.
                _replace_stock_su(tmp_name)

                p = _run(dc + ["commit", tmp_name, new_tag], timeout=300)
                if p.returncode != 0:
                    raise RuntimeError(f"Commit failed: {(p.stderr or p.stdout or '').strip()}")

                logger.info("apply_magisk: created image %s", new_tag)
                return new_tag
            finally:
                _run(dc + ["rm", "-f", tmp_name], timeout=30)


# ---------------------------------------------------------------------------
# Device profiles & spoof logic
# ---------------------------------------------------------------------------

_COMMON_ANTI_DETECT = {
    # Build identity: production device, not eng/userdebug
    "ro.build.tags": "release-keys",
    "ro.build.type": "user",

    # Verified boot
    "ro.boot.verifiedbootstate": "green",
    "ro.boot.flash.locked": "1",
    "ro.boot.vbmeta.device_state": "locked",
    "ro.boot.veritymode": "enforcing",

    # Security patch level
    "ro.build.version.security_patch": "2025-03-05",
    "ro.vendor.build.security_patch": "2025-03-05",

    # Boot revision (cosmetic, safe)
    "ro.boot.hardware.revision": "MP1.0",

    # Telephony
    "ro.telephony.default_network": "9",
    "gsm.version.baseband": "1.0",
    "gsm.version.ril-impl": "android qualcomm RIL 1.0",

    # Misc anti-fingerprinting
    "ro.setupwizard.mode": "OPTIONAL",
    "ro.com.google.gmsversion": "14_202408",
    "persist.sys.timezone": "America/Sao_Paulo",
    "ro.url.legal": "http://www.google.com/intl/%s/mobile/android/basic/phone-legal.html",
    "ro.com.google.clientidbase": "android-google",
    "ro.boot.warranty_bit": "0",
    "ro.warranty_bit": "0",
    "ro.is_ever_orange": "0",

    # NOTE: These properties are intentionally NOT set here because
    # Redroid relies on its own values to boot correctly:
    #   ro.kernel.qemu        - Redroid needs its internal value
    #   ro.hardware.egl       - must match actual GPU (swiftshader/mesa)
    #   ro.hardware.chipname  - breaks hardware init
    #   ro.board.platform     - breaks hardware init
    #   ro.opengles.version   - must match actual GL capability
    #   ro.build.selinux      - breaks userdebug boot
    #   ro.build.flavor       - breaks userdebug boot
    #   ro.hardware.wifi      - no real wifi hw in container
    #   wifi.interface        - no wlan0 device exists
}

DEVICE_PROFILES: dict[str, dict[str, str]] = {
    "pixel6": {
        "label": "Google Pixel 6",
        "ro.product.brand": "google",
        "ro.product.manufacturer": "Google",
        "ro.product.model": "Pixel 6",
        "ro.product.device": "oriole",
        "ro.product.name": "oriole",
        "ro.build.display.id": "AP2A.240805.005",
        "ro.build.id": "AP2A.240805.005",
        "ro.build.fingerprint": "google/oriole/oriole:14/AP2A.240805.005/12025142:user/release-keys",
        "ro.build.version.incremental": "12025142",
        "ro.build.description": "oriole-user 14 AP2A.240805.005 12025142 release-keys",
        "ro.product.first_api_level": "31",
        "ro.product.board": "oriole",
        "ro.soc.manufacturer": "Google",
        "ro.soc.model": "Tensor",
        **_COMMON_ANTI_DETECT,
    },
    "pixel7": {
        "label": "Google Pixel 7",
        "ro.product.brand": "google",
        "ro.product.manufacturer": "Google",
        "ro.product.model": "Pixel 7",
        "ro.product.device": "panther",
        "ro.product.name": "panther",
        "ro.build.display.id": "AP2A.240805.005",
        "ro.build.id": "AP2A.240805.005",
        "ro.build.fingerprint": "google/panther/panther:14/AP2A.240805.005/12025142:user/release-keys",
        "ro.build.version.incremental": "12025142",
        "ro.build.description": "panther-user 14 AP2A.240805.005 12025142 release-keys",
        "ro.product.first_api_level": "33",
        "ro.product.board": "panther",
        "ro.soc.manufacturer": "Google",
        "ro.soc.model": "Tensor G2",
        **_COMMON_ANTI_DETECT,
    },
    "pixel8": {
        "label": "Google Pixel 8",
        "ro.product.brand": "google",
        "ro.product.manufacturer": "Google",
        "ro.product.model": "Pixel 8",
        "ro.product.device": "shiba",
        "ro.product.name": "shiba",
        "ro.build.display.id": "AP2A.240805.005",
        "ro.build.id": "AP2A.240805.005",
        "ro.build.fingerprint": "google/shiba/shiba:14/AP2A.240805.005/12025142:user/release-keys",
        "ro.build.version.incremental": "12025142",
        "ro.build.description": "shiba-user 14 AP2A.240805.005 12025142 release-keys",
        "ro.product.first_api_level": "34",
        "ro.product.board": "shiba",
        "ro.soc.manufacturer": "Google",
        "ro.soc.model": "Tensor G3",
        **_COMMON_ANTI_DETECT,
    },
    "samsung_s23": {
        "label": "Samsung Galaxy S23",
        "ro.product.brand": "samsung",
        "ro.product.manufacturer": "samsung",
        "ro.product.model": "SM-S911B",
        "ro.product.device": "dm1q",
        "ro.product.name": "dm1qxxx",
        "ro.build.display.id": "TP1A.220624.014.S911BXXS7CXF3",
        "ro.build.id": "TP1A.220624.014",
        "ro.build.fingerprint": "samsung/dm1qxxx/dm1q:14/TP1A.220624.014/S911BXXS7CXF3:user/release-keys",
        "ro.build.version.incremental": "S911BXXS7CXF3",
        "ro.build.description": "dm1qxxx-user 14 TP1A.220624.014 S911BXXS7CXF3 release-keys",
        "ro.product.first_api_level": "33",
        "ro.product.board": "kalama",
        "ro.soc.manufacturer": "Qualcomm",
        "ro.soc.model": "SM8550",
        **_COMMON_ANTI_DETECT,
    },
    "samsung_s24": {
        "label": "Samsung Galaxy S24",
        "ro.product.brand": "samsung",
        "ro.product.manufacturer": "samsung",
        "ro.product.model": "SM-S921B",
        "ro.product.device": "e1q",
        "ro.product.name": "e1qxxx",
        "ro.build.display.id": "UP1A.231005.007.S921BXXS3AXE2",
        "ro.build.id": "UP1A.231005.007",
        "ro.build.fingerprint": "samsung/e1qxxx/e1q:14/UP1A.231005.007/S921BXXS3AXE2:user/release-keys",
        "ro.build.version.incremental": "S921BXXS3AXE2",
        "ro.build.description": "e1qxxx-user 14 UP1A.231005.007 S921BXXS3AXE2 release-keys",
        "ro.product.first_api_level": "34",
        "ro.product.board": "sun",
        "ro.soc.manufacturer": "Samsung",
        "ro.soc.model": "s5e9945",
        **_COMMON_ANTI_DETECT,
    },
    "samsung_a54": {
        "label": "Samsung Galaxy A54",
        "ro.product.brand": "samsung",
        "ro.product.manufacturer": "samsung",
        "ro.product.model": "SM-A546B",
        "ro.product.device": "a54x",
        "ro.product.name": "a54xnsxx",
        "ro.build.display.id": "UP1A.231005.007.A546BXXS9CXE2",
        "ro.build.id": "UP1A.231005.007",
        "ro.build.fingerprint": "samsung/a54xnsxx/a54x:14/UP1A.231005.007/A546BXXS9CXE2:user/release-keys",
        "ro.build.version.incremental": "A546BXXS9CXE2",
        "ro.build.description": "a54xnsxx-user 14 UP1A.231005.007 A546BXXS9CXE2 release-keys",
        "ro.product.first_api_level": "33",
        "ro.product.board": "s5e8835",
        "ro.soc.manufacturer": "Samsung",
        "ro.soc.model": "Exynos 1380",
        **_COMMON_ANTI_DETECT,
    },
    "xiaomi_14": {
        "label": "Xiaomi 14",
        "ro.product.brand": "Xiaomi",
        "ro.product.manufacturer": "Xiaomi",
        "ro.product.model": "23127PN0CC",
        "ro.product.device": "houji",
        "ro.product.name": "houji",
        "ro.build.display.id": "UKQ1.231003.002",
        "ro.build.id": "UKQ1.231003.002",
        "ro.build.fingerprint": "Xiaomi/houji/houji:14/UKQ1.231003.002/V816.0.9.0.UNCCNXM:user/release-keys",
        "ro.build.version.incremental": "V816.0.9.0.UNCCNXM",
        "ro.build.description": "houji-user 14 UKQ1.231003.002 V816.0.9.0.UNCCNXM release-keys",
        "ro.product.first_api_level": "34",
        "ro.product.board": "kalama",
        "ro.soc.manufacturer": "Qualcomm",
        "ro.soc.model": "SM8650",
        **_COMMON_ANTI_DETECT,
    },
    "oneplus_12": {
        "label": "OnePlus 12",
        "ro.product.brand": "OnePlus",
        "ro.product.manufacturer": "OnePlus",
        "ro.product.model": "CPH2583",
        "ro.product.device": "aston",
        "ro.product.name": "aston",
        "ro.build.display.id": "CPH2583_14.0.0.800",
        "ro.build.id": "UP1A.231005.007",
        "ro.build.fingerprint": "OnePlus/aston/aston:14/UP1A.231005.007/T.18c3716_11551:user/release-keys",
        "ro.build.version.incremental": "T.18c3716_11551",
        "ro.build.description": "aston-user 14 UP1A.231005.007 T.18c3716_11551 release-keys",
        "ro.product.first_api_level": "34",
        "ro.product.board": "kalama",
        "ro.soc.manufacturer": "Qualcomm",
        "ro.soc.model": "SM8650",
        **_COMMON_ANTI_DETECT,
    },
}


def get_device_profiles() -> list[dict[str, str]]:
    """Return list of available profiles for the API/frontend."""
    return [{"value": k, "label": v["label"]} for k, v in DEVICE_PROFILES.items()]


def device_profile_props(profile_key: str) -> list[str]:
    """Return list of 'key=value' props for a given device profile."""
    profile = DEVICE_PROFILES.get(profile_key)
    if not profile:
        return []
    return [f"{k}={v}" for k, v in profile.items() if k.startswith("ro.")]


def _patch_build_prop_with_profile(content: str, profile_key: str) -> str:
    """Patch build.prop content using a device profile's props.

    Expands product/build props across all Android partition prefixes so the
    identity is consistent in system, vendor, product, etc.
    """
    profile = DEVICE_PROFILES.get(profile_key)
    if not profile:
        return content

    replacements: dict[str, str] = {}

    brand = profile.get("ro.product.brand", "")
    device = profile.get("ro.product.device", "")
    manufacturer = profile.get("ro.product.manufacturer", "")
    model = profile.get("ro.product.model", "")
    pname = profile.get("ro.product.name", "")
    fingerprint = profile.get("ro.build.fingerprint", "")
    incremental = profile.get("ro.build.version.incremental", "")

    fp_prefixes = [
        "ro.build", "ro.system.build", "ro.vendor.build", "ro.product.build",
        "ro.odm.build", "ro.system_ext.build", "ro.bootimage.build",
    ]
    if fingerprint:
        for pfx in fp_prefixes:
            replacements[f"{pfx}.fingerprint"] = fingerprint
    if incremental:
        for pfx in fp_prefixes:
            replacements[f"{pfx}.version.incremental"] = incremental

    product_prefixes = [
        "ro.product", "ro.product.system", "ro.product.vendor",
        "ro.product.odm", "ro.product.system_ext", "ro.product.product",
        "ro.product.bootimage",
    ]
    for pfx in product_prefixes:
        if brand:
            replacements[f"{pfx}.brand"] = brand
        if device:
            replacements[f"{pfx}.device"] = device
        if manufacturer:
            replacements[f"{pfx}.manufacturer"] = manufacturer
        if model:
            replacements[f"{pfx}.model"] = model
        if pname:
            replacements[f"{pfx}.name"] = pname

    # Keys that must NEVER be overwritten — Redroid needs its own values
    # for these to boot and render correctly.
    _protected = {
        "label",
        "ro.hardware", "ro.hardware.egl", "ro.hardware.gralloc",
        "ro.hardware.hwcomposer", "ro.hardware.vulkan",
        "ro.hardware.chipname", "ro.hardware.wifi",
        "ro.board.platform", "ro.kernel.qemu", "ro.kernel.qemu.gles",
        "ro.opengles.version", "ro.build.selinux", "ro.build.flavor",
        "ro.debuggable", "ro.secure", "ro.adb.secure",
        "wifi.interface",
    }
    for key, val in profile.items():
        if key not in _protected and key not in replacements and "." in key:
            replacements[key] = val

    lines = content.splitlines(keepends=True)
    result: list[str] = []
    seen_keys: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0]
            if key in replacements:
                result.append(f"{key}={replacements[key]}\n")
                seen_keys.add(key)
                continue
        result.append(line)
    for key, val in replacements.items():
        if key not in seen_keys:
            result.append(f"{key}={val}\n")
    return "".join(result)


def apply_device_profile(base_image: str, profile_key: str) -> str:
    """Create a spoofed Docker image by patching build.prop files with a
    device profile.  Returns the new image tag.

    Flow: docker create (stopped) -> docker cp read/write -> docker commit.
    This works because docker cp can write to stopped containers even if the
    filesystem is read-only at runtime.
    """
    if not profile_key or profile_key not in DEVICE_PROFILES:
        return base_image

    profile_label = DEVICE_PROFILES[profile_key].get("label", profile_key)
    if ":" in base_image:
        base, tag = base_image.rsplit(":", 1)
        new_tag = f"{base}:{tag}-{profile_key}"
    else:
        new_tag = f"{base_image}:{profile_key}"

    dc = _docker_cmd()

    p = _run(dc + ["image", "inspect", new_tag], timeout=15)
    if p.returncode == 0:
        logger.info("apply_device_profile: image %s already exists, reusing", new_tag)
        return new_tag

    tmp_name = f"virtuale-profile-{uuid.uuid4().hex[:8]}"
    try:
        logger.info("apply_device_profile: creating temp container from %s for profile %s", base_image, profile_label)
        p = _run(dc + ["create", "--name", tmp_name, base_image], timeout=120)
        if p.returncode != 0:
            logger.warning("apply_device_profile: failed to create temp container: %s", (p.stderr or p.stdout or "").strip())
            return base_image

        patched = 0
        for bp_path in SPOOF_BUILD_PROP_FILES:
            raw = _docker_cp_read(tmp_name, bp_path)
            if raw is None:
                continue
            original = raw.decode("utf-8", errors="replace")
            patched_content = _patch_build_prop_with_profile(original, profile_key)
            if patched_content == original:
                continue
            dest_dir = str(Path(bp_path).parent)
            filename = Path(bp_path).name
            if _docker_cp_write(tmp_name, dest_dir, filename, patched_content.encode("utf-8")):
                patched += 1

        if patched == 0:
            logger.warning("apply_device_profile: no build.prop files were patched for %s", profile_key)
            return base_image

        p = _run(dc + ["commit", tmp_name, new_tag], timeout=300)
        if p.returncode != 0:
            logger.warning("apply_device_profile: commit failed: %s", (p.stderr or p.stdout or "").strip())
            return base_image

        logger.info("apply_device_profile: created image %s (%d files patched)", new_tag, patched)
        return new_tag
    finally:
        _run(dc + ["rm", "-f", tmp_name], timeout=30)


SPOOF_PROPS = {
    "fingerprint": "google/oriole/oriole:14/AP2A.240805.005/12025142:user/release-keys",
    "brand": "google",
    "manufacturer": "Google",
    "model": "Pixel 6",
    "device": "oriole",
    "name": "oriole",
    "build_id": "AP2A.240805.005",
    "incremental": "12025142",
}

SPOOF_BUILD_PROP_FILES = [
    "system/build.prop",
    "vendor/build.prop",
    "system/product/etc/build.prop",
    "system/system_ext/etc/build.prop",
    "vendor/odm/etc/build.prop",
    "vendor/default.prop",
    "default.prop",
]

_spoof_lock = threading.Lock()
_spoof_status: dict[str, Any] = {"running": False, "log": "", "last_result": None}

_build_lock = threading.Lock()
_build_status: dict[str, Any] = {"running": False, "log": "", "last_result": None}


def get_spoof_status() -> dict[str, Any]:
    return dict(_spoof_status)


def get_build_status() -> dict[str, Any]:
    return dict(_build_status)


def patch_build_prop(content: str) -> str:
    sp = SPOOF_PROPS
    replacements: dict[str, str] = {}
    fp_prefixes = [
        "ro.build", "ro.system.build", "ro.vendor.build", "ro.product.build",
        "ro.odm.build", "ro.system_ext.build", "ro.bootimage.build",
    ]
    for pfx in fp_prefixes:
        replacements[f"{pfx}.fingerprint"] = sp["fingerprint"]
    replacements["ro.build.display.id"] = sp["build_id"]
    replacements["ro.build.id"] = sp["build_id"]
    replacements["ro.build.tags"] = "release-keys"
    replacements["ro.build.type"] = "user"
    replacements["ro.build.version.incremental"] = sp["incremental"]
    replacements["ro.system.build.tags"] = "release-keys"
    replacements["ro.system.build.type"] = "user"
    replacements["ro.system.build.version.incremental"] = sp["incremental"]
    product_prefixes = [
        "ro.product", "ro.product.system", "ro.product.vendor",
        "ro.product.odm", "ro.product.system_ext", "ro.product.product",
        "ro.product.bootimage",
    ]
    for pfx in product_prefixes:
        replacements[f"{pfx}.brand"] = sp["brand"]
        replacements[f"{pfx}.device"] = sp["device"]
        replacements[f"{pfx}.manufacturer"] = sp["manufacturer"]
        replacements[f"{pfx}.model"] = sp["model"]
        replacements[f"{pfx}.name"] = sp["name"]
    replacements["ro.hardware"] = sp["device"]

    lines = content.splitlines(keepends=True)
    result: list[str] = []
    seen_keys: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0]
            if key in replacements:
                result.append(f"{key}={replacements[key]}\n")
                seen_keys.add(key)
                continue
        result.append(line)
    for key, val in replacements.items():
        if key not in seen_keys:
            result.append(f"{key}={val}\n")
    return "".join(result)


def _docker_cp_read(container: str, path: str) -> bytes | None:
    dc = _docker_cmd()
    p = subprocess.run(dc + ["cp", f"{container}:/{path}", "-"], capture_output=True, timeout=30)
    if p.returncode != 0:
        return None
    try:
        tar = tarfile.open(fileobj=io.BytesIO(p.stdout))
        name = path.rsplit("/", 1)[-1]
        member = tar.extractfile(name)
        return member.read() if member else None
    except Exception:
        return None


def _replace_stock_su(container: str) -> None:
    """Overwrite stock su binaries inside a stopped container with symlinks
    pointing to /sbin/magisk, so Magisk doesn't see a competing su."""
    dc = _docker_cmd()
    su_paths = [
        ("system/xbin", "su"),
        ("system/bin", "su"),
        ("product/bin", "su"),
        ("vendor/bin", "su"),
    ]
    for dest_dir, name in su_paths:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=name)
            info.type = tarfile.SYMTYPE
            info.linkname = "/sbin/magisk"
            info.mode = 0o755
            tar.addfile(info)
        dest = f"/{dest_dir}/"
        p = subprocess.run(
            dc + ["cp", "-", f"{container}:{dest}"],
            input=buf.getvalue(), capture_output=True, timeout=30,
        )
        if p.returncode == 0:
            logger.info("_replace_stock_su: replaced %s/%s -> /sbin/magisk", dest_dir, name)
        else:
            logger.debug("_replace_stock_su: %s/%s not found or failed (ok)", dest_dir, name)


def _docker_cp_write(container: str, dest_dir: str, filename: str, data: bytes, mode: int = 0o644) -> bool:
    dc = _docker_cmd()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=filename)
        info.size = len(data)
        info.mode = mode
        tar.addfile(info, io.BytesIO(data))
    dest = f"/{dest_dir}/" if dest_dir else "/"
    p = subprocess.run(dc + ["cp", "-", f"{container}:{dest}"], input=buf.getvalue(), capture_output=True, timeout=120)
    if p.returncode != 0:
        logger.warning("_docker_cp_write: failed to write %s to %s:%s%s: %s",
                        filename, container, dest, filename, (p.stderr or b"").decode(errors="replace").strip()[:200])
    return p.returncode == 0


def do_spoof(base_image: str) -> None:
    global _spoof_status
    tmp_name = f"virtuale-spoof-tmp-{uuid.uuid4().hex[:8]}"
    dc = _docker_cmd()
    log_lines: list[str] = []

    def log(msg: str) -> None:
        log_lines.append(msg + "\n")
        _spoof_status["log"] = "".join(log_lines)

    try:
        log(f"Criando contêiner temporário a partir de {base_image}...")
        p = _run(dc + ["create", "--name", tmp_name, base_image], timeout=120)
        if p.returncode != 0:
            raise RuntimeError(f"Falha ao criar contêiner: {(p.stderr or p.stdout).strip()}")

        patched_count = 0
        for prop_path in SPOOF_BUILD_PROP_FILES:
            log(f"Lendo /{prop_path}...")
            raw = _docker_cp_read(tmp_name, prop_path)
            if raw is None:
                log("  (não encontrado, pulando)")
                continue
            original = raw.decode("utf-8", errors="replace")
            patched = patch_build_prop(original)
            if patched == original:
                log("  (sem alterações)")
                continue
            dest_dir = str(Path(prop_path).parent)
            filename = Path(prop_path).name
            log(f"Escrevendo /{prop_path} com props spoofed...")
            ok = _docker_cp_write(tmp_name, dest_dir, filename, patched.encode())
            if not ok:
                log(f"  ERRO ao escrever /{prop_path}")
            else:
                patched_count += 1

        if patched_count == 0:
            raise RuntimeError("Nenhum build.prop foi modificado.")

        # NOTE: virtual touch setup (create_touch) is handled at runtime by
        # TouchService.setup_virtual_touch() when the container starts.
        # Replacing /init with init_wrapper inside the image breaks boot.

        if ":" in base_image:
            new_tag = base_image.rsplit(":", 1)[0] + ":" + base_image.rsplit(":", 1)[1] + "-spoofed"
        else:
            new_tag = base_image + ":spoofed"

        log(f"Commitando imagem como {new_tag}...")
        p = _run(dc + ["commit", tmp_name, new_tag], timeout=300)
        if p.returncode != 0:
            raise RuntimeError(f"Falha no commit: {(p.stderr or p.stdout).strip()}")

        log(f"Imagem {new_tag} criada com sucesso!")
        _spoof_status["last_result"] = {"success": True, "message": f"Imagem spoofed criada: {new_tag}", "tag": new_tag}
    except Exception as exc:
        log(f"ERRO: {exc}")
        _spoof_status["last_result"] = {"success": False, "message": str(exc)}
    finally:
        _run(dc + ["rm", "-f", tmp_name], timeout=30)
        _spoof_status["running"] = False


def start_spoof(base_image: str) -> bool:
    if _spoof_status["running"]:
        return False
    with _spoof_lock:
        _spoof_status["running"] = True
        _spoof_status["log"] = ""
        _spoof_status["last_result"] = None
    threading.Thread(target=do_spoof, args=(base_image,), daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# Build via redroid-script
# ---------------------------------------------------------------------------

ANDROID_VERSIONS = ["14.0.0", "13.0.0", "12.0.0", "12.0.0_64only", "11.0.0", "10.0.0", "9.0.0", "8.1.0"]


def do_build(params: dict[str, Any]) -> None:
    global _build_status
    try:
        script_dir = Path(settings.BUILD_SCRIPT_DIR)
        script_file = script_dir / "redroid.py"
        if not script_file.exists():
            candidates = [
                Path("/opt/virtuale/redroid-script/redroid.py"),
                Path("/opt/redroid-script/redroid.py"),
            ]
            for cand in candidates:
                if cand.exists():
                    script_file = cand
                    script_dir = cand.parent
                    break
        if not script_file.exists():
            raise RuntimeError(
                "redroid.py não encontrado. Verifique o volume do build-script "
                f"(esperado em {settings.BUILD_SCRIPT_DIR}/redroid.py)."
            )

        args = [sys.executable, "redroid.py", "-a", params["android_version"]]
        if settings.USE_SUDO_DOCKER:
            args += ["-c", "docker"]
        for flag, opt in [("gapps", "-g"), ("litegapps", "-lg"), ("mindthegapps", "-mtg"),
                          ("ndk", "-n"), ("houdini", "-i"), ("magisk", "-m"), ("widevine", "-w")]:
            if params.get(flag):
                args.append(opt)

        env = {**os.environ}
        env.setdefault("USER", "root")
        env.setdefault("HOME", "/root")
        if settings.USE_SUDO_DOCKER:
            docker_exe = _docker_exe()
            wrapper_dir = Path(tempfile.mkdtemp(prefix="virtuale-docker-wrapper-"))
            wrapper = wrapper_dir / "docker"
            wrapper.write_text(f"#!/bin/sh\nexec sudo -n {docker_exe} \"$@\"\n")
            wrapper.chmod(0o755)
            env["PATH"] = f"{wrapper_dir}:{env['PATH']}"

        proc = subprocess.Popen(
            args,
            cwd=str(script_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        log_lines: list[str] = []
        for line in iter(proc.stdout.readline, ""):
            log_lines.append(line)
            _build_status["log"] = "".join(log_lines)
        proc.wait()
        if proc.returncode == 0:
            # redroid-script tags follow this deterministic format:
            # redroid/redroid:<android>[_gapps][_litegapps][_mindthegapps][_ndk][_houdini][_magisk][_widevine]
            built_parts = [str(params.get("android_version", "")).strip()]
            for flag in ("gapps", "litegapps", "mindthegapps", "ndk", "houdini", "magisk", "widevine"):
                if params.get(flag):
                    built_parts.append(flag)
            built_tag = f"redroid/redroid:{'_'.join([p for p in built_parts if p])}"
            result_message = f"Imagem construída com sucesso: {built_tag}"
            if params.get("auto_spoof"):
                base_tag = str(params.get("spoof_base_image", "")).strip()
                if not base_tag:
                    base_tag = built_tag
                if base_tag:
                    if _spoof_status.get("running"):
                        result_message += " Spoof automático não iniciado (já existe spoof em andamento)."
                    else:
                        with _spoof_lock:
                            _spoof_status["running"] = True
                            _spoof_status["log"] = ""
                            _spoof_status["last_result"] = None
                        do_spoof(base_tag)
                        spoof_result = _spoof_status.get("last_result") or {}
                        if spoof_result.get("success"):
                            spoof_tag = str(spoof_result.get("tag", "")).strip()
                            result_message += f" Spoof concluído: {spoof_tag or 'ok'}."
                            cleanup_intermediate = bool(params.get("cleanup_intermediate", True))
                            if cleanup_intermediate and base_tag == built_tag and spoof_tag and spoof_tag != built_tag:
                                try:
                                    DockerService.remove_image(built_tag)
                                    result_message += f" Intermediária removida: {built_tag}."
                                except Exception as e:
                                    result_message += f" Não foi possível remover intermediária ({built_tag}): {e}."
                        else:
                            result_message += f" Spoof automático falhou: {spoof_result.get('message', 'erro desconhecido')}."
                else:
                    result_message += " Spoof automático não iniciado (imagem base não encontrada)."
            _build_status["last_result"] = {"success": True, "message": result_message}
        else:
            _build_status["last_result"] = {"success": False, "message": f"Build falhou (código {proc.returncode})."}
    except Exception as exc:
        _build_status["last_result"] = {"success": False, "message": str(exc)}
    finally:
        _build_status["running"] = False


def start_build(params: dict[str, Any]) -> bool:
    if _build_status["running"]:
        return False
    with _build_lock:
        _build_status["running"] = True
        _build_status["log"] = ""
        _build_status["last_result"] = None
    threading.Thread(target=do_build, args=(params,), daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# VPN (Alpine + OpenVPN sidecar) service
# ---------------------------------------------------------------------------

VPN_IMAGE = "virtuale/openvpn-client"
VPN_DOCKERFILE = """\
FROM alpine:3.20
RUN apk add --no-cache openvpn iptables
"""

# ---------------------------------------------------------------------------
# Tor transparent-proxy sidecar (Whonix-like)
# ---------------------------------------------------------------------------

TOR_IMAGE = "virtuale/tor-client"
TOR_DOCKERFILE = """\
FROM alpine:3.20
RUN apk add --no-cache tor redsocks iptables
"""

# Startup script embedded in the container command.
# Architecture:
#   1. Start tor.  Tor uses eth0 (default bridge) to bootstrap via Tor network.
#   2. Wait for bootstrap (SOCKS5 port 9050 opens).
#   3. Start redsocks forwarding TCP → tor SOCKS5.
#   4. Apply iptables NAT REDIRECT *after* tor has ESTABLISHED relay
#      connections.  Conntrack skips NAT for ESTABLISHED flows, so tor's
#      relay traffic is not re-routed.  Only NEW outgoing connections (from
#      the Android container) hit the REDIRECT rule.
#   5. DNS: redirect UDP/53 → tor's DNSPort (5353).
#
# Country selection: pass TOR_COUNTRY env var (ISO 3166-1 alpha-2).
# No xt_mark or xt_owner needed — uses only xt_REDIRECT in the NAT table.

TOR_STARTUP_SCRIPT = """\
#!/bin/sh
set -e
COUNTRY="${TOR_COUNTRY:-}"

mkdir -p /var/lib/tor /run/tor
chown -R tor:tor /var/lib/tor /run/tor
chmod 700 /var/lib/tor /run/tor

# Gateway mode: bind to 0.0.0.0 so forwarded traffic from the Android
# container (separate network namespace) can reach TransPort/DNSPort.
printf '%s\\n' \\
    'SocksPort 0.0.0.0:9050' \\
    'TransPort 0.0.0.0:9040' \\
    'DNSPort 0.0.0.0:5353' \\
    'DataDirectory /var/lib/tor' \\
    'User tor' \\
    'Log notice stderr' > /etc/tor/torrc

if [ -n "$COUNTRY" ]; then
    printf 'ExitNodes {%s}\\nStrictNodes 0\\n' "$COUNTRY" >> /etc/tor/torrc
fi

tor -f /etc/tor/torrc &
TOR_PID=$!

echo "Waiting for Tor bootstrap (SOCKS port 9050)..."
i=0
while [ "$i" -lt 180 ]; do
    nc -z 127.0.0.1 9050 2>/dev/null && break
    sleep 2
    i=$((i+2))
done
nc -z 127.0.0.1 9050 2>/dev/null || { echo "Tor bootstrap timed out" >&2; exit 1; }
sleep 3

echo "Applying gateway iptables rules (ip_forward via --sysctl)..."
iptables -P FORWARD ACCEPT 2>/dev/null || true

# PREROUTING intercepts forwarded traffic from Android.
# Private/local subnets are exempted so ADB/management passes through.
iptables -t nat -A PREROUTING -d 127.0.0.0/8   -j RETURN
iptables -t nat -A PREROUTING -d 10.0.0.0/8    -j RETURN
iptables -t nat -A PREROUTING -d 172.16.0.0/12  -j RETURN
iptables -t nat -A PREROUTING -d 192.168.0.0/16 -j RETURN

for cidr in $(ip -o -4 route show scope link | awk '{print $1}' | sort -u); do
    [ -n "$cidr" ] || continue
    iptables -t nat -A PREROUTING -d "$cidr" -j RETURN 2>/dev/null || true
done

# Redirect forwarded TCP to Tor TransPort.
MYIP=$(ip -4 addr show eth0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)
if ! iptables -t nat -A PREROUTING -p tcp -j REDIRECT --to-ports 9040 2>/dev/null; then
    iptables -t nat -A PREROUTING -p tcp -j DNAT --to-destination ${MYIP:-127.0.0.1}:9040
fi

# Redirect forwarded DNS to Tor DNSPort.
if ! iptables -t nat -A PREROUTING -p udp --dport 53 -j REDIRECT --to-ports 5353 2>/dev/null; then
    iptables -t nat -A PREROUTING -p udp --dport 53 -j DNAT --to-destination ${MYIP:-127.0.0.1}:5353
fi

echo "Tor gateway active. country=${COUNTRY:-any}"
wait $TOR_PID
"""


# ---------------------------------------------------------------------------
# GPS fake
# ---------------------------------------------------------------------------

class GpsService:
    @staticmethod
    def set_location(serial: str, lat: float, lng: float) -> bool:
        cmds = [
            "settings put secure location_providers_allowed +gps",
            "settings put secure location_providers_allowed +network",
            f"appops set com.android.shell android:mock_location allow",
            f"am start-foreground-service "
            f"-a com.android.internal.location.action.MOCK_LOCATION "
            f"--ef lat {lat} --ef lng {lng} 2>/dev/null; "
            f"cmd location providers set-test-provider-enabled gps true 2>/dev/null; "
            f"cmd location providers set-test-provider-location gps "
            f"--latitude {lat} --longitude {lng} --altitude 10 --accuracy 1.0 2>/dev/null || true",
        ]
        ok = True
        for c in cmds:
            try:
                _run(["adb", "-s", serial, "shell", c], timeout=8)
            except Exception:
                ok = False
        return ok

    @staticmethod
    def clear_location(serial: str) -> bool:
        cmds = [
            "cmd location providers remove-test-provider gps 2>/dev/null || true",
            "settings put secure location_providers_allowed -gps",
        ]
        for c in cmds:
            try:
                _run(["adb", "-s", serial, "shell", c], timeout=8)
            except Exception:
                pass
        return True


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------

class ClipboardService:
    @staticmethod
    def get_clipboard(serial: str) -> str:
        try:
            p = _run(["adb", "-s", serial, "shell",
                       "service call clipboard 2 i32 1 i32 0 2>/dev/null | "
                       "grep -oP \"'[^']*'\" | tr -d \"'\" | head -1"],
                      timeout=8)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def set_clipboard(serial: str, text: str) -> bool:
        import shlex as _shlex
        escaped = _shlex.quote(text)
        try:
            p = _run(["adb", "-s", serial, "shell",
                       f"am broadcast -a clipper.set -e text {escaped} 2>/dev/null; "
                       f"input text {escaped}; true"],
                      timeout=8)
            return p.returncode == 0
        except Exception:
            return False


# ---------------------------------------------------------------------------
# File management
# ---------------------------------------------------------------------------

class FileService:
    @staticmethod
    def push_file(serial: str, local_path: str, remote_path: str = "/sdcard/Download/") -> bool:
        try:
            p = _run(["adb", "-s", serial, "push", local_path, remote_path], timeout=120)
            return p.returncode == 0
        except Exception:
            return False

    @staticmethod
    def pull_file(serial: str, remote_path: str, local_path: str) -> bool:
        try:
            p = _run(["adb", "-s", serial, "pull", remote_path, local_path], timeout=120)
            return p.returncode == 0
        except Exception:
            return False

    @staticmethod
    def list_files(serial: str, remote_dir: str = "/sdcard/") -> list[dict]:
        try:
            p = _run(["adb", "-s", serial, "shell",
                       f"ls -la {remote_dir} 2>/dev/null"],
                      timeout=10)
            if p.returncode != 0:
                return []
            entries = []
            for line in (p.stdout or "").strip().splitlines():
                parts = line.split(None, 7)
                if len(parts) < 8:
                    continue
                entries.append({
                    "permissions": parts[0],
                    "size": parts[4] if len(parts) > 4 else "",
                    "date": f"{parts[5]} {parts[6]}" if len(parts) > 6 else "",
                    "name": parts[7] if len(parts) > 7 else parts[-1],
                    "is_dir": parts[0].startswith("d"),
                })
            return entries
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Backup / Restore
# ---------------------------------------------------------------------------

class BackupService:
    @staticmethod
    def export_instance(name: str, output_path: str) -> bool:
        dc = _docker_cmd()
        data_dir = Path(settings.DATA_ROOT) / name
        try:
            p = _run(dc + [
                "run", "--rm",
                "-v", f"{data_dir}:/mnt/data:ro",
                "busybox",
                "tar", "czf", "-", "-C", "/mnt/data", ".",
            ], timeout=600)
            if p.returncode == 0 and p.stdout:
                Path(output_path).write_bytes(p.stdout.encode("latin-1") if isinstance(p.stdout, str) else p.stdout)
                return True
        except Exception as e:
            logger.warning("BackupService.export: failed for %s: %s", name, e)
        return False

    @staticmethod
    def export_instance_stream(name: str) -> subprocess.Popen | None:
        dc = _docker_cmd()
        data_dir = Path(settings.DATA_ROOT) / name
        try:
            proc = subprocess.Popen(
                dc + [
                    "run", "--rm",
                    "-v", f"{data_dir}:/mnt/data:ro",
                    "busybox",
                    "tar", "czf", "-", "-C", "/mnt/data", ".",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            return proc
        except Exception as e:
            logger.warning("BackupService.export_stream: failed for %s: %s", name, e)
            return None

    @staticmethod
    def import_instance(name: str, archive_path: str) -> bool:
        dc = _docker_cmd()
        data_dir = Path(settings.DATA_ROOT) / name
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(archive_path, "rb") as f:
                p = subprocess.run(
                    dc + [
                        "run", "--rm", "-i",
                        "-v", f"{data_dir}:/mnt/data",
                        "busybox",
                        "tar", "xzf", "-", "-C", "/mnt/data",
                    ],
                    stdin=f,
                    capture_output=True,
                    timeout=600,
                )
            return p.returncode == 0
        except Exception as e:
            logger.warning("BackupService.import: failed for %s: %s", name, e)
            return False


# ---------------------------------------------------------------------------
# ADB Shell (interactive terminal)
# ---------------------------------------------------------------------------

class AdbShellService:
    @staticmethod
    def execute(serial: str, command: str, timeout: int = 15) -> dict:
        try:
            p = _run(["adb", "-s", serial, "shell", command], timeout=timeout)
            return {
                "stdout": (p.stdout or "").strip(),
                "stderr": (p.stderr or "").strip(),
                "returncode": p.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": "Command timed out", "returncode": -1}
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "returncode": -1}


class VpnService:
    _cached_data_volume: str | None = None

    @staticmethod
    def _vpn_dir(name: str) -> Path:
        return Path(settings.DATA_ROOT) / "_vpn" / name

    @staticmethod
    def _detect_data_volume() -> str:
        """Auto-detect the Docker named volume backing DATA_ROOT.

        Inspects the current container (via HOSTNAME) to find which volume
        is mounted at DATA_ROOT.  Falls back to the DATA_VOLUME setting.
        """
        if VpnService._cached_data_volume is not None:
            return VpnService._cached_data_volume

        hostname = os.environ.get("HOSTNAME", "")
        if hostname:
            dc = _docker_cmd()
            p = _run(dc + ["inspect", "--format", "{{json .Mounts}}", hostname])
            if p.returncode == 0:
                try:
                    mounts = json.loads(p.stdout)
                    data_root = settings.DATA_ROOT.rstrip("/")
                    for mount in mounts:
                        dest = (mount.get("Destination") or "").rstrip("/")
                        if dest == data_root and mount.get("Type") == "volume":
                            vol = mount.get("Name", "")
                            if vol:
                                logger.info("VpnService: auto-detected DATA_VOLUME=%s", vol)
                                VpnService._cached_data_volume = vol
                                return vol
                except (json.JSONDecodeError, TypeError):
                    pass

        vol = getattr(settings, "DATA_VOLUME", "")
        VpnService._cached_data_volume = vol
        return vol

    @staticmethod
    def _sidecar_name(name: str) -> str:
        return f"{name}-vpn"

    @staticmethod
    def _sanitize_ovpn(ovpn_content: str) -> str:
        """Sanitize a custom .ovpn config for the Alpine+OpenVPN sidecar.

        1. Resolve hostnames in ``remote`` directives to IPs.
        2. Remove file-path directives (ca, cert, key, tls-auth, tls-crypt)
           that reference external files — they don't exist in the sidecar.
        3. Add persist-tun/persist-key for stable reconnection.
        4. Add explicit host routes for VPN server IPs via the original
           gateway so that reconnection works after redirect-gateway.
        """
        # --- Step 1: resolve hostnames in 'remote' lines ---
        resolved_lines: list[str] = []
        for ln in ovpn_content.splitlines():
            stripped = ln.strip()
            if stripped.startswith("remote "):
                parts = stripped.split()
                if len(parts) >= 2:
                    host = parts[1]
                    try:
                        socket.inet_aton(host)
                    except OSError:
                        try:
                            ip = socket.gethostbyname(host)
                            logger.info("VPN _sanitize_ovpn: resolved %s -> %s", host, ip)
                            parts[1] = ip
                            ln = " ".join(parts)
                        except socket.gaierror:
                            logger.warning("VPN _sanitize_ovpn: could not resolve %s, keeping hostname", host)
            resolved_lines.append(ln)
        ovpn_content = "\n".join(resolved_lines) + "\n"

        # --- Step 2: remove external file-path directives ---
        file_directives = {"ca", "cert", "key", "tls-auth", "tls-crypt"}
        cleaned: list[str] = []
        for ln in ovpn_content.splitlines():
            s = ln.strip()
            skip = False
            if not s.startswith("<"):
                for directive in file_directives:
                    if s == directive or s.startswith(directive + " "):
                        logger.info("VPN _sanitize_ovpn: removing file-path directive: %s", s)
                        skip = True
                        break
            if not skip:
                cleaned.append(ln)
        ovpn_content = "\n".join(cleaned) + "\n"

        # --- Step 3: add persist-tun / persist-key ---
        existing = ovpn_content.lower()
        extras: list[str] = []
        if "persist-tun" not in existing:
            extras.append("persist-tun")
        if "persist-key" not in existing:
            extras.append("persist-key")

        # --- Step 4: force redirect-gateway def1 ---
        # def1 adds 0.0.0.0/1 + 128.0.0.0/1 instead of deleting the
        # default route.  This is far more resilient when Docker adds
        # interfaces/routes via "docker network connect".
        rg_lines: list[str] = []
        for ln in ovpn_content.splitlines():
            s = ln.strip()
            if s.startswith("redirect-gateway"):
                rg_lines.append(s)
        for old_rg in rg_lines:
            if "def1" not in old_rg:
                new_rg = old_rg + " def1"
                ovpn_content = ovpn_content.replace(old_rg, new_rg)
                logger.info("VPN _sanitize_ovpn: forced def1: %s -> %s", old_rg, new_rg)

        # --- Step 5: add host routes for VPN server IPs ---
        # When OpenVPN uses redirect-gateway, all traffic goes via tun0.
        # On reconnection the VPN server becomes unreachable.  Adding an
        # explicit route via ``net_gateway`` (OpenVPN macro for the
        # original default gateway) keeps the VPN server reachable.
        server_ips: list[str] = []
        for ln in ovpn_content.splitlines():
            s = ln.strip()
            if s.startswith("remote "):
                parts = s.split()
                if len(parts) >= 2:
                    try:
                        socket.inet_aton(parts[1])
                        server_ips.append(parts[1])
                    except OSError:
                        pass
        for sip in server_ips:
            route_line = f"route {sip} 255.255.255.255 net_gateway"
            if route_line not in ovpn_content:
                extras.append(route_line)

        if extras:
            ovpn_content += "\n".join(extras) + "\n"

        return ovpn_content

    @staticmethod
    def store_config(
        name: str,
        ovpn_content: str,
        username: str = "",
        password: str = "",
        ca_cert: str = "",
    ) -> Path:
        """Store a custom .ovpn config for the given instance."""
        vpn_dir = VpnService._vpn_dir(name)
        vpn_dir.mkdir(parents=True, exist_ok=True)

        ovpn_content = VpnService._sanitize_ovpn(ovpn_content)

        # If a CA certificate was provided separately, inject it inline.
        if ca_cert.strip():
            ovpn_content += f"\n<ca>\n{ca_cert.strip()}\n</ca>\n"

        config_path = vpn_dir / "custom.conf"
        config_path.write_text(ovpn_content, encoding="utf-8")

        if username and password:
            auth_path = vpn_dir / "auth.txt"
            auth_path.write_text(f"{username}\n{password}\n", encoding="utf-8")
            auth_path.chmod(0o600)
            # Remove any existing auth-user-pass directives, then add one
            # pointing to auth.txt (relative path — openvpn uses --cd).
            text = config_path.read_text(encoding="utf-8")
            lines = [
                ln for ln in text.splitlines()
                if not ln.strip().startswith("auth-user-pass")
            ]
            lines.append("auth-user-pass auth.txt")
            config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        config_path.chmod(0o600)
        return vpn_dir

    @staticmethod
    def remove_config(name: str) -> None:
        vpn_dir = VpnService._vpn_dir(name)
        if vpn_dir.exists():
            shutil.rmtree(vpn_dir, ignore_errors=True)

    @staticmethod
    def ensure_image() -> None:
        """Build the Alpine+OpenVPN image if it doesn't exist."""
        dc = _docker_cmd()
        p = _run(dc + ["image", "inspect", VPN_IMAGE])
        if p.returncode == 0:
            return
        logger.info("VpnService: building %s image", VPN_IMAGE)
        p = subprocess.run(
            dc + ["build", "-t", VPN_IMAGE, "-"],
            input=VPN_DOCKERFILE,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if p.returncode != 0:
            raise RuntimeError(
                f"Falha ao construir imagem VPN: {(p.stderr or p.stdout or '').strip()}"
            )

    @staticmethod
    def ensure_tor_image() -> None:
        """Build the Alpine+Tor+redsocks image if it doesn't exist."""
        dc = _docker_cmd()
        p = _run(dc + ["image", "inspect", TOR_IMAGE])
        if p.returncode == 0:
            return
        logger.info("VpnService: building %s image", TOR_IMAGE)
        p = subprocess.run(
            dc + ["build", "-t", TOR_IMAGE, "-"],
            input=TOR_DOCKERFILE,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if p.returncode != 0:
            raise RuntimeError(
                f"Falha ao construir imagem Tor: {(p.stderr or p.stdout or '').strip()}"
            )

    @staticmethod
    def start_sidecar(
        name: str,
        vpn_type: str = "openvpn",
        provider: str = "custom",
        username: str = "",
        password: str = "",
        country: str = "",
    ) -> None:
        if vpn_type == "tor":
            VpnService._start_tor_sidecar(name, country=country)
        else:
            VpnService._start_openvpn_sidecar(name, provider=provider, username=username, password=password)

    @staticmethod
    def _start_tor_sidecar(name: str, country: str = "") -> None:
        """Start an Alpine+Tor transparent-proxy sidecar."""
        dc = _docker_cmd()
        sidecar = VpnService._sidecar_name(name)

        VpnService.ensure_tor_image()

        logger.info("_start_tor_sidecar: removing old sidecar %s (if any)", sidecar)
        _run(dc + ["rm", "-f", sidecar], timeout=30)

        cmd = dc + [
            "run", "-d",
            "--name", sidecar,
            "--label", "virtuale.managed=1",
            "--label", "virtuale.role=vpn",
            "--label", f"virtuale.parent={name}",
            "--cap-add=NET_ADMIN",
            "--cap-add=NET_RAW",
            "--sysctl", "net.ipv4.ip_forward=1",
            "-e", f"TOR_COUNTRY={country.strip()[:2].upper() if country else ''}",
            TOR_IMAGE, "sh", "-c", TOR_STARTUP_SCRIPT,
        ]
        logger.info("_start_tor_sidecar: %s", " ".join(cmd))

        p = _run(cmd, timeout=120)
        if p.returncode != 0:
            raise RuntimeError(
                f"Falha ao criar sidecar Tor: {(p.stderr or p.stdout or '').strip()}"
            )

        VpnService._wait_running(name)

        network = _get_network()
        if network:
            connected = False
            last_err = ""
            for attempt in range(3):
                p = _run(dc + ["network", "connect", network, sidecar], timeout=30)
                if p.returncode == 0:
                    connected = True
                    logger.info("_start_tor_sidecar: connected %s to %s", sidecar, network)
                    break
                last_err = (p.stderr or p.stdout or "").strip()
                logger.warning(
                    "_start_tor_sidecar: network connect attempt %d/3 failed for %s: %s",
                    attempt + 1, sidecar, last_err,
                )
                time.sleep(2 ** attempt)
            if not connected:
                raise RuntimeError(
                    f"Falha ao conectar sidecar {sidecar} à rede {network}: {last_err}"
                )

    @staticmethod
    def _start_openvpn_sidecar(
        name: str,
        provider: str = "custom",
        username: str = "",
        password: str = "",
    ) -> None:
        dc = _docker_cmd()
        sidecar = VpnService._sidecar_name(name)
        vpn_dir = VpnService._vpn_dir(name)

        if provider != "custom":
            raise RuntimeError(
                "Atualmente apenas VPN custom OpenVPN é suportada. "
                "Providers nativos requerem o módulo xt_mark do kernel."
            )

        config_file = vpn_dir / "custom.conf"

        # Handle legacy filename.
        legacy = vpn_dir / "config.ovpn"
        if legacy.exists() and not config_file.exists():
            legacy.rename(config_file)

        if not config_file.exists():
            raise RuntimeError("Arquivo de configuração VPN (.ovpn) não encontrado.")

        # Re-sanitize on every start.
        raw = config_file.read_text(encoding="utf-8")
        sanitized = VpnService._sanitize_ovpn(raw)
        if sanitized != raw:
            config_file.write_text(sanitized, encoding="utf-8")
            logger.info("start_sidecar: re-sanitized config for %s", name)

        VpnService.ensure_image()

        logger.info("start_sidecar: removing old sidecar %s (if any)", sidecar)
        _run(dc + ["rm", "-f", sidecar], timeout=30)

        # --- Volume mount ---
        data_volume = VpnService._detect_data_volume()
        if data_volume:
            vol_flags = ["-v", f"{data_volume}:{settings.DATA_ROOT}"]
            vpn_path = f"{settings.DATA_ROOT}/_vpn/{name}"
        else:
            vol_flags = ["-v", f"{vpn_dir}:/vpn"]
            vpn_path = "/vpn"

        cmd = dc + [
            "run", "-d",
            "--name", sidecar,
            "--label", "virtuale.managed=1",
            "--label", "virtuale.role=vpn",
            "--label", f"virtuale.parent={name}",
            "--cap-add=NET_ADMIN",
            "--cap-add=NET_RAW",
            "--sysctl", "net.ipv4.ip_forward=1",
            "--device=/dev/net/tun",
        ]
        cmd += vol_flags
        # Gateway mode: the sidecar runs in its OWN network namespace and
        # acts as a router/gateway for the Android container (which also
        # has its own namespace on the same Docker network).
        #
        # This avoids sharing the namespace with Android — Android's netd
        # cannot interfere with the sidecar's routing/iptables.
        #
        # The wrapper:
        # 1) Starts OpenVPN, waits for tun0.
        # 2) Enables ip_forward so forwarded packets traverse the sidecar.
        # 3) iptables FORWARD ACCEPT + MASQUERADE on tun0.
        # 4) Maintenance loop keeps Docker routes alive.
        wrapper = (
            "GW=$(ip route | awk '/^default/{print $3}'); "
            "SIPS=$(grep -E '^remote ' " + vpn_path + "/custom.conf"
            " | awk '{print $2}'); "
            "DOCKER_ROUTES=$(ip -o -4 route show scope link | awk '{print $1, $3}'); "
            "openvpn --cd " + vpn_path + " --config custom.conf --verb 3 & "
            "PID=$!; "

            # Wait up to 60s for tun0
            "T=0; while [ $T -lt 60 ]; do "
            "  ip addr show tun0 2>/dev/null | grep -q 'inet ' && break; "
            "  sleep 1; T=$((T+1)); "
            "done; "

            "VPN_GW=$(ip route | awk '/via.*dev tun0/{print $3; exit}'); "
            "if [ -n \"$VPN_GW\" ]; then "
            "  iptables -P FORWARD ACCEPT 2>/dev/null || true; "
            "  iptables -t nat -A POSTROUTING -o tun0 -j MASQUERADE 2>/dev/null || true; "
            "  echo 'VPN gateway active (FORWARD ACCEPT + MASQUERADE)'; "
            "fi; "

            # Background maintenance: keep VPN server and Docker routes alive
            "( while kill -0 $PID 2>/dev/null; do "
            "    for s in $SIPS; do "
            "      ip route replace $s/32 via $GW dev eth0 2>/dev/null || true; "
            "    done; "
            "    echo \"$DOCKER_ROUTES\" | while read cidr dev; do "
            "      [ -n \"$cidr\" ] && [ -n \"$dev\" ] && [ \"$dev\" != tun0 ] && "
            "        ip route replace $cidr dev $dev 2>/dev/null || true; "
            "    done; "
            "    sleep 5; "
            "  done "
            ") & "
            "wait $PID"
        )
        cmd += [VPN_IMAGE, "sh", "-c", wrapper]

        # Log (mask auth file content)
        logger.info("start_sidecar: docker run command: %s", " ".join(cmd))

        p = _run(cmd, timeout=120)
        if p.returncode != 0:
            raise RuntimeError(
                f"Falha ao criar sidecar VPN: {(p.stderr or p.stdout or '').strip()}"
            )

        VpnService._wait_running(name)

        # Attach to the internal app network so the backend can reach
        # the sidecar by container name (for ADB, streaming, etc.).
        network = _get_network()
        if network:
            connected = False
            last_err = ""
            for attempt in range(3):
                p = _run(dc + ["network", "connect", network, sidecar], timeout=30)
                if p.returncode == 0:
                    connected = True
                    logger.info("start_sidecar: connected %s to %s", sidecar, network)
                    break
                last_err = (p.stderr or p.stdout or "").strip()
                logger.warning(
                    "start_sidecar: network connect attempt %d/3 failed for %s: %s",
                    attempt + 1, sidecar, last_err,
                )
                time.sleep(2 ** attempt)
            if not connected:
                raise RuntimeError(
                    f"Falha ao conectar sidecar {sidecar} à rede {network}: {last_err}"
                )

    @staticmethod
    def _wait_running(name: str, timeout: int = 10) -> None:
        """Wait until the sidecar container is confirmed running."""
        dc = _docker_cmd()
        sidecar = VpnService._sidecar_name(name)
        for _ in range(timeout):
            try:
                data = DockerService.inspect(sidecar)
                state = data.get("State", {}) or {}
                if state.get("Running"):
                    return
                if state.get("Status") in ("exited", "dead"):
                    logs = _run(dc + ["logs", "--tail", "30", sidecar], timeout=10)
                    detail = (logs.stdout or logs.stderr or "").strip()[-500:]
                    raise RuntimeError(
                        f"Sidecar VPN saiu inesperadamente. Verifique a configuração.\n{detail}"
                    )
            except RuntimeError:
                raise
            except Exception:
                pass
            time.sleep(1)
        raise RuntimeError("Sidecar VPN não iniciou a tempo.")

    @staticmethod
    def _tunnel_ready_once(name: str, vpn_type: str = "openvpn") -> bool:
        """Single non-blocking tunnel readiness probe."""
        dc = _docker_cmd()
        sidecar = VpnService._sidecar_name(name)

        if vpn_type == "tor":
            p = _run(dc + ["exec", sidecar, "nc", "-z", "127.0.0.1", "9050"], timeout=5)
            if p.returncode != 0:
                return False
            p2 = _run(
                dc + ["exec", sidecar, "iptables", "-t", "nat", "-L", "PREROUTING", "-n"],
                timeout=5,
            )
            out = p2.stdout or ""
            return ("REDIRECT" in out or "DNAT" in out) and ("tcp" in out.lower())

        p = _run(dc + ["exec", sidecar, "ip", "addr", "show", "tun0"], timeout=5)
        return p.returncode == 0 and "inet " in (p.stdout or "")

    @staticmethod
    def connection_status(name: str, vpn_type: str = "openvpn") -> str:
        """Return VPN sidecar status: connected, starting, disconnected or unknown."""
        if not VpnService.sidecar_running(name):
            return "disconnected"
        try:
            return "connected" if VpnService._tunnel_ready_once(name, vpn_type=vpn_type) else "starting"
        except Exception:
            return "unknown"

    @staticmethod
    def wait_tunnel(name: str, timeout: int = 30, vpn_type: str = "openvpn") -> bool:
        """Wait for the VPN tunnel to be ready.

        - openvpn: check for tun0 interface with an inet address.
                - tor: check that the SOCKS5 port (9050) is open AND iptables
                    has a TCP redirect rule (REDIRECT or DNAT).
        """
        dc = _docker_cmd()
        for _ in range(max(1, timeout // 2)):
            if VpnService._tunnel_ready_once(name, vpn_type=vpn_type):
                return True
            time.sleep(2)
        return False

    @staticmethod
    def sidecar_ip(name: str, retries: int = 3, delay: float = 2) -> str:
        """Get the sidecar container's IP via ``docker inspect`` JSON output.

        Prefers the IP on the configured/auto-detected app network; falls back
        to any network with an IP.  Retries handle transient Docker state
        inconsistencies right after ``docker network connect``.
        """
        dc = _docker_cmd()
        sidecar = VpnService._sidecar_name(name)
        network = _get_network()

        for attempt in range(1, retries + 1):
            p = _run(dc + ["inspect", sidecar], timeout=10)
            if p.returncode == 0:
                try:
                    data = json.loads(p.stdout)
                    if isinstance(data, list) and data:
                        data = data[0]
                    nets = (data.get("NetworkSettings") or {}).get("Networks") or {}
                    # Prefer the app network
                    if network and network in nets:
                        ip = (nets[network].get("IPAddress") or "").strip()
                        if ip:
                            return ip
                    # Fallback: first network with an IP (skip bridge)
                    bridge_ip = ""
                    for net_name, net_data in nets.items():
                        ip = (net_data.get("IPAddress") or "").strip()
                        if not ip:
                            continue
                        if net_name == "bridge":
                            bridge_ip = ip
                            continue
                        return ip
                    if bridge_ip:
                        return bridge_ip
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass

            if attempt < retries:
                logger.debug(
                    "sidecar_ip: attempt %d/%d failed for %s, retrying in %.0fs",
                    attempt, retries, sidecar, delay,
                )
                time.sleep(delay)
            else:
                logger.warning(
                    "sidecar_ip: all %d attempts failed for %s (network=%s, "
                    "last inspect rc=%d, stdout=%s, stderr=%s)",
                    retries, sidecar, network or "<not set>",
                    p.returncode,
                    _short(p.stdout, 200),
                    _short(p.stderr, 200),
                )
        return ""

    @staticmethod
    def _container_ip(name: str) -> str:
        """Get a container's IP on the configured Docker network."""
        dc = _docker_cmd()
        network = _get_network()
        p = _run(dc + ["inspect", name], timeout=10)
        if p.returncode != 0:
            return ""
        try:
            data = json.loads(p.stdout)
            if isinstance(data, list) and data:
                data = data[0]
            nets = (data.get("NetworkSettings") or {}).get("Networks") or {}
            if network and network in nets:
                ip = (nets[network].get("IPAddress") or "").strip()
                if ip:
                    return ip
            for net_data in nets.values():
                ip = (net_data.get("IPAddress") or "").strip()
                if ip:
                    return ip
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        return ""

    @staticmethod
    def setup_gateway(name: str) -> None:
        """Redirect Android container's internet traffic through the sidecar.

        Android's ``netd`` aggressively controls in-container routing via
        fwmark policy rules and named tables — any ``ip route``/``ip rule``
        or ``iptables mangle`` manipulation inside the Android namespace is
        ignored or unsupported (``iptable_mangle`` not loaded).

        Instead, we add **host-level** routing rules via a lightweight
        ephemeral container with ``--network host``.  This works because
        traffic from the Android container is *forwarded* through the
        Docker bridge on the host kernel, where our ``ip rule`` is
        evaluated **before** the packet ever reaches Android's netd.

        Flow: Android → Docker bridge → host ip rule (from <android_ip>
        table N) → sidecar → VPN/Tor tunnel → internet.
        """
        dc = _docker_cmd()
        sidecar_ip = VpnService.sidecar_ip(name)
        android_ip = VpnService._container_ip(name)
        if not sidecar_ip or not android_ip:
            logger.warning(
                "setup_gateway: missing IPs for %s (sidecar=%s, android=%s)",
                name, sidecar_ip, android_ip,
            )
            return

        table_id = 1000 + int(android_ip.split(".")[-1])

        host_script = (
            f"set -e; "
            # Clean previous rules for this instance
            f"ip rule del from {android_ip} table {table_id} 2>/dev/null || true; "
            f"ip route flush table {table_id} 2>/dev/null || true; "
            # Exempt private/Docker subnets (use main table)
            f"ip rule add from {android_ip} to 10.0.0.0/8 priority 99 table main 2>/dev/null || true; "
            f"ip rule add from {android_ip} to 172.16.0.0/12 priority 99 table main 2>/dev/null || true; "
            f"ip rule add from {android_ip} to 192.168.0.0/16 priority 99 table main 2>/dev/null || true; "
            # All other traffic → sidecar
            f"ip route add default via {sidecar_ip} table {table_id}; "
            f"ip rule add from {android_ip} table {table_id} priority 100; "
            f"echo 'host gateway: {android_ip} → {sidecar_ip} (table {table_id})'"
        )

        p = _run(
            dc + [
                "run", "--rm",
                "--network", "host",
                "--cap-add", "NET_ADMIN",
                "alpine", "sh", "-c", host_script,
            ],
            timeout=30,
        )
        if p.returncode != 0:
            logger.warning(
                "setup_gateway: host routing failed for %s: %s",
                name, (p.stderr or p.stdout or "").strip(),
            )
        else:
            logger.info(
                "setup_gateway: host route %s (%s) → sidecar %s, table %d",
                name, android_ip, sidecar_ip, table_id,
            )

    @staticmethod
    def cleanup_gateway(name: str) -> None:
        """Remove host-level routing rules for a VPN instance."""
        dc = _docker_cmd()
        android_ip = VpnService._container_ip(name)
        if not android_ip:
            logger.debug("cleanup_gateway: cannot resolve IP for %s, skipping", name)
            return

        table_id = 1000 + int(android_ip.split(".")[-1])

        cleanup_script = (
            f"ip rule del from {android_ip} table {table_id} 2>/dev/null || true; "
            f"ip rule del from {android_ip} to 10.0.0.0/8 priority 99 2>/dev/null || true; "
            f"ip rule del from {android_ip} to 172.16.0.0/12 priority 99 2>/dev/null || true; "
            f"ip rule del from {android_ip} to 192.168.0.0/16 priority 99 2>/dev/null || true; "
            f"ip route flush table {table_id} 2>/dev/null || true; "
            f"echo 'cleaned up gateway for {android_ip} (table {table_id})'"
        )

        p = _run(
            dc + [
                "run", "--rm",
                "--network", "host",
                "--cap-add", "NET_ADMIN",
                "alpine", "sh", "-c", cleanup_script,
            ],
            timeout=15,
        )
        if p.returncode != 0:
            logger.warning(
                "cleanup_gateway: failed for %s: %s",
                name, (p.stderr or p.stdout or "").strip(),
            )

    @staticmethod
    def stop_sidecar(name: str) -> None:
        VpnService.cleanup_gateway(name)
        dc = _docker_cmd()
        sidecar = VpnService._sidecar_name(name)
        _run(dc + ["stop", sidecar], timeout=30)

    @staticmethod
    def remove_sidecar(name: str) -> None:
        dc = _docker_cmd()
        sidecar = VpnService._sidecar_name(name)
        _run(dc + ["rm", "-f", sidecar], timeout=30)

    @staticmethod
    def sidecar_running(name: str) -> bool:
        try:
            data = DockerService.inspect(VpnService._sidecar_name(name))
            return bool((data.get("State") or {}).get("Running"))
        except RuntimeError:
            return False


def resolve_serial(instance) -> str:
    """Resolve the ADB serial for an instance.

    Prefers hostname-based serial (``name:5555``) because ``cmd_send_root``
    extracts the container name from the serial for ``docker exec``.  Falls
    back to IP resolution only when the hostname approach is not viable.
    """
    return instance.serial
