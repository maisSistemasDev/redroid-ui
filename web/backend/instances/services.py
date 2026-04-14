from __future__ import annotations

import io
import json
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)
FORCE_DOCKER_INSTALL = os.getenv("REDROID_FORCE_DOCKER_INSTALL", "0").lower() in {"1", "true", "yes", "on"}

# ---------------------------------------------------------------------------
# Docker CLI helpers
# ---------------------------------------------------------------------------

DOCKER_PS_FILTER = "label=redroid-ui.managed=1"


def _docker_exe() -> str:
    return shutil.which("docker") or "docker"


def _docker_cmd() -> list[str]:
    exe = _docker_exe()
    if settings.REDROID_USE_SUDO_DOCKER:
        return ["sudo", "-n", exe]
    return [exe]


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


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
    def create_container(name: str, image: str, gpu_mode: str, extra_props: str, user_id: int, vpn_enabled: bool = False) -> str:
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
        # host filesystem.  We must NOT wipe /data inside a running redroid
        # container — that would corrupt Android's data partition mid-boot.
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
            "--label", "redroid-ui.managed=1",
            "--label", f"redroid-ui.user={user_id}",
            "--privileged",
            "-v", f"{data_dir}:/data",
        ]
        network = getattr(settings, "REDROID_DOCKER_NETWORK", "")
        if vpn_enabled:
            cmd += ["--network", f"container:{name}-vpn"]
        elif network:
            cmd += ["--network", network]
        cmd += [
            image,
            f"androidboot.redroid_gpu_mode={gpu_mode}",
        ]
        image_lower = image.lower()

        # GApps images need extra properties to avoid SetupWizard crash loop
        # and prevent Google Play Services from creating overlay windows that
        # block touch in apps using filterTouchesWhenObscured (e.g. WhatsApp).
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
                if settings.REDROID_USE_SUDO_DOCKER:
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
                if settings.REDROID_USE_SUDO_DOCKER:
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
        p = _run(_docker_cmd() + ["images", "--format", fmt, "--filter", "reference=redroid/*"])
        if p.returncode != 0:
            raise RuntimeError((p.stderr or "").strip())
        images: list[dict[str, str]] = []
        for line in p.stdout.strip().splitlines():
            parts = line.split(sep)
            if len(parts) >= 3:
                tag = parts[0].strip()
                if "<none>" in tag:
                    continue
                images.append({"tag": tag, "id": parts[1].strip(), "size": parts[2].strip()})
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
                if p.returncode == 0:
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
            return
        dc = _docker_cmd()
        _run(dc + ["cp", str(binary), f"{name}:/create_touch"])
        _run(dc + ["exec", name, "chmod", "+x", "/create_touch"])
        _run(dc + ["exec", name, "sh", "-c", "pkill -f /create_touch 2>/dev/null || true"])
        time.sleep(0.5)
        _run(dc + ["exec", name, "sh", "-c", "rm -f /dev/input/event*"])
        _run(dc + ["exec", "-d", name, "/create_touch"])
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

        # Kill any existing daemon
        p = _run(dc + ["exec", name, "sh", "-c", "pkill -f /create_keyboard 2>/dev/null || true"])
        time.sleep(0.5)

        # Start daemon
        p = _run(dc + ["exec", "-d", name, "/create_keyboard"])
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
    """Extract the redroid container name from an ADB serial.

    When VPN is active the serial looks like ``{name}-vpn:5555`` but the
    Android container is simply ``{name}``.  Strip the ``-vpn`` suffix so
    that ``docker exec`` targets the correct container.
    """
    host = serial.split(":", 1)[0].strip()
    if not host:
        return ""
    if not re.match(r"^[a-zA-Z0-9_.-]+$", host):
        return ""
    # VPN sidecar names end with '-vpn'; the redroid container is without.
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

    dst = f"/data/local/tmp/redroid-ui-{uuid.uuid4().hex}.apk"
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
            p = _run(_docker_cmd() + ["exec", cname, "sh", "-lc", cmd], timeout=300)
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
        _run(_docker_cmd() + ["exec", cname, "sh", "-lc", f"rm -f {shlex.quote(dst)}"], timeout=30)


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

    p = _run(_docker_cmd() + ["exec", cname, "sh", "-lc", cmd], timeout=timeout)
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
        p = _run(_docker_cmd() + ["exec", cname, "sh", "-lc", cmd], timeout=timeout)
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
# Spoof logic
# ---------------------------------------------------------------------------

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


def _docker_cp_write(container: str, dest_dir: str, filename: str, data: bytes, mode: int = 0o644) -> bool:
    dc = _docker_cmd()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=filename)
        info.size = len(data)
        info.mode = mode
        tar.addfile(info, io.BytesIO(data))
    p = subprocess.run(dc + ["cp", "-", f"{container}:/{dest_dir}/"], input=buf.getvalue(), capture_output=True, timeout=30)
    return p.returncode == 0


def do_spoof(base_image: str) -> None:
    global _spoof_status
    tmp_name = f"redroid-spoof-tmp-{uuid.uuid4().hex[:8]}"
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
        script_dir = Path(settings.REDROID_SCRIPT_DIR)
        script_file = script_dir / "redroid.py"
        if not script_file.exists():
            candidates = [
                Path("/opt/redroid-ui/redroid-script/redroid.py"),
                Path("/opt/redroid-script/redroid.py"),
            ]
            for cand in candidates:
                if cand.exists():
                    script_file = cand
                    script_dir = cand.parent
                    break
        if not script_file.exists():
            raise RuntimeError(
                "redroid.py não encontrado. Verifique o volume do redroid-script "
                f"(esperado em {settings.REDROID_SCRIPT_DIR}/redroid.py)."
            )

        args = [sys.executable, "redroid.py", "-a", params["android_version"]]
        if settings.REDROID_USE_SUDO_DOCKER:
            args += ["-c", "docker"]
        for flag, opt in [("gapps", "-g"), ("litegapps", "-lg"), ("mindthegapps", "-mtg"),
                          ("ndk", "-n"), ("houdini", "-i"), ("magisk", "-m"), ("widevine", "-w")]:
            if params.get(flag):
                args.append(opt)

        env = {**os.environ}
        env.setdefault("USER", "root")
        env.setdefault("HOME", "/root")
        if settings.REDROID_USE_SUDO_DOCKER:
            docker_exe = _docker_exe()
            wrapper_dir = Path(tempfile.mkdtemp(prefix="redroid-ui-docker-wrapper-"))
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

VPN_IMAGE = "redroid-ui/openvpn-client"
VPN_DOCKERFILE = """\
FROM alpine:3.20
RUN apk add --no-cache openvpn
"""


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
    def start_sidecar(
        name: str,
        vpn_type: str = "openvpn",
        provider: str = "custom",
        username: str = "",
        password: str = "",
    ) -> None:
        dc = _docker_cmd()
        sidecar = VpnService._sidecar_name(name)
        vpn_dir = VpnService._vpn_dir(name)

        if provider != "custom" or vpn_type != "openvpn":
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
            "--label", "redroid-ui.managed=1",
            "--label", "redroid-ui.role=vpn",
            "--label", f"redroid-ui.parent={name}",
            "--cap-add=NET_ADMIN",
            "--device=/dev/net/tun",
        ]
        cmd += vol_flags
        # Start on default bridge (has internet gateway).  The redroid-ui
        # network is internal and has no internet access — OpenVPN would
        # fail to reach the VPN server.
        #
        # We use a wrapper script that:
        # 1) Saves the original default gateway before OpenVPN modifies it.
        # 2) Starts OpenVPN in the background.
        # 3) Runs a background route monitor that re-adds server routes
        #    every 5 seconds (survives docker network connect disruptions).
        # 4) Waits on the OpenVPN PID so the container stays alive.
        wrapper = (
            "GW=$(ip route | awk '/^default/{print $3}'); "
            "SIPS=$(grep -E '^remote ' " + vpn_path + "/custom.conf"
            " | awk '{print $2}'); "
            "openvpn --cd " + vpn_path + " --config custom.conf --verb 3 & "
            "PID=$!; "
            "( while kill -0 $PID 2>/dev/null; do "
            "    for s in $SIPS; do "
            "      ip route replace $s/32 via $GW dev eth0 2>/dev/null || true; "
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

        # Attach to the internal redroid network so the backend can reach
        # the sidecar by container name (for ADB, streaming, etc.).
        network = getattr(settings, "REDROID_DOCKER_NETWORK", "")
        if network:
            p = _run(dc + ["network", "connect", network, sidecar], timeout=30)
            if p.returncode != 0:
                logger.warning(
                    "start_sidecar: failed to connect %s to %s: %s",
                    sidecar, network, (p.stderr or p.stdout or "").strip(),
                )
            else:
                logger.info("start_sidecar: connected %s to %s", sidecar, network)

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
    def wait_tunnel(name: str, timeout: int = 30) -> bool:
        """Wait for the VPN tunnel interface (tun0) to come up."""
        dc = _docker_cmd()
        sidecar = VpnService._sidecar_name(name)
        for _ in range(timeout // 2):
            p = _run(dc + ["exec", sidecar, "ip", "addr", "show", "tun0"], timeout=5)
            if p.returncode == 0 and "inet " in (p.stdout or ""):
                return True
            time.sleep(2)
        return False

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
