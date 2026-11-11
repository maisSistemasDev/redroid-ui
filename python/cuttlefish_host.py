"""Instâncias Cuttlefish (VM KVM via launch_cvd) gerenciadas pela UI, com MindTheGapps opcional."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

IMAGE_PREFIX = "cuttlefish:"
ADB_BASE_PORT = 6520
OPERATOR_URL = "https://localhost:1443"

# Imagem Android 12 compilada localmente com o HAL de câmera vsock; precisa das ferramentas de host da mesma árvore.
CAM12_IMAGE = f"{IMAGE_PREFIX}android12-camera"
CAM12_VARIANT = "a12cam"
CAM12_WEBRTC_BASE_PORT = 8443
CAMERA_VSOCK_PORT = 7700
CAMERA_TEST_SOURCE = "test"
FEED_SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "cuttlefish" / "vsock_camera_feed.py"

MINDTHEGAPPS = {
    "14": (
        "https://github.com/s1204IT/MindTheGappsBuilder/releases/download/20240226/"
        "MindTheGapps-14.0.0-x86_64-20240226.zip"
    ),
    "13": (
        "https://github.com/s1204IT/MindTheGappsBuilder/releases/download/20240226/"
        "MindTheGapps-13.0.0-x86_64-20240226.zip"
    ),
    "12": (
        "https://github.com/s1204IT/MindTheGappsBuilder/releases/download/20240619/"
        "MindTheGapps-12.1.0-x86_64-20240619.zip"
    ),
}

_lock = threading.Lock()
_workers: dict[str, threading.Thread] = {}


def images_dir() -> Path:
    return Path(os.environ.get("REDROID_CF_IMAGES") or Path.home() / "cuttlefish")


def cam12_host_dir() -> Path:
    return Path(os.environ.get("REDROID_CF12_HOST") or Path.home() / "cuttlefish-a12")


def cam12_images_dir() -> Path:
    return Path(os.environ.get("REDROID_CF12_IMAGES") or Path.home() / "redroid" / "out" / "target" / "product" / "vsoc_x86_64")


ARM64_IMAGE = f"{IMAGE_PREFIX}arm64"
ARM64_VARIANT = "arm64"


def arm64_images_dir() -> Path:
    return Path(os.environ.get("REDROID_CF_ARM64_IMAGES") or Path.home() / "cuttlefish-arm64")


def _arm64_bootloader() -> Path:
    # u-boot aarch64 que acompanha o host package ARM64 do CI (casa com as imagens A16).
    return arm64_images_dir() / "etc" / "bootloader_aarch64" / "bootloader.qemu"


def arm64_present() -> bool:
    d = arm64_images_dir()
    return (
        (d / "super.img").is_file()
        and _arm64_bootloader().is_file()
        and (arm64_tools_dir() / "bin" / "cvd_internal_start").is_file()
    )


def cam12_present() -> bool:
    return (
        (cam12_host_dir() / "bin" / "launch_cvd").is_file()
        and (cam12_images_dir() / "super.img").is_file()
        and (cam12_images_dir() / "bootloader").is_file()
    )


def _is_cam12(meta: dict[str, Any]) -> bool:
    return meta.get("variant") == CAM12_VARIANT


def _is_arm64(meta: dict[str, Any]) -> bool:
    return meta.get("variant") == ARM64_VARIANT


def arm64_host_dir() -> Path:
    # ANDROID_HOST_OUT do ARM64: package CI gravável cujos binários são symlinks para
    # o pacote debian (o cvd refaz esses symlinks no launch via debian_substitution_marker).
    return Path(os.environ.get("REDROID_CF_ARM64_HOST") or arm64_images_dir())


def arm64_tools_dir() -> Path:
    # Binários de entrada x86_64 (o package CI é aarch64 e não executa neste host).
    return Path(os.environ.get("REDROID_CF_ARM64_TOOLS") or "/usr/lib/cuttlefish-common")


def _host_dir(meta: dict[str, Any]) -> Path:
    if _is_cam12(meta):
        return cam12_host_dir()
    if _is_arm64(meta):
        return arm64_host_dir()
    return images_dir()


def _system_dir(meta: dict[str, Any]) -> Path:
    if _is_cam12(meta):
        return cam12_images_dir()
    if _is_arm64(meta):
        return arm64_images_dir()
    return images_dir()


def _root() -> Path:
    root = Path.home() / ".local" / "share" / "redroid-ui" / "cuttlefish"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cache_dir() -> Path:
    d = Path.home() / ".cache" / "redroid-ui"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _home(name: str) -> Path:
    return _root() / name


def _cvd_home(name: str, meta: dict[str, Any]) -> Path:
    # As ferramentas do Android 12 criam sockets unix dentro do HOME e abortam acima de 108 caracteres.
    return Path.home() / ".cf" / name if _is_cam12(meta) else _home(name)


def _meta_path(name: str) -> Path:
    return _home(name) / "meta.json"


def _run(cmd: list[str], timeout: int = 120, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env)


def is_image(image: str) -> bool:
    return (image or "").startswith(IMAGE_PREFIX)


def images_present() -> bool:
    return (images_dir() / "bin" / "launch_cvd").is_file() and (images_dir() / "super.img").is_file()


def android_version() -> str:
    info = images_dir() / "fetcher_config.json"
    try:
        text = info.read_text(encoding="utf-8")
        m = re.search(r"aosp-android(\d+)", text)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "14"


def status() -> dict[str, Any]:
    present = images_present()
    ver = android_version() if present else ""
    return {
        "images_present": present,
        "images_dir": str(images_dir()),
        "android_version": ver,
        "image": f"{IMAGE_PREFIX}android{ver}" if present else "",
        "kvm": os.access("/dev/kvm", os.R_OK | os.W_OK),
        "operator_url": OPERATOR_URL,
        "gapps_available": ver in MINDTHEGAPPS,
        "camera12": {
            "present": cam12_present(),
            "image": CAM12_IMAGE,
            "host_dir": str(cam12_host_dir()),
            "images_dir": str(cam12_images_dir()),
        },
        "arm64": {
            "present": arm64_present(),
            "image": ARM64_IMAGE,
            "images_dir": str(arm64_images_dir()),
        },
    }


def load_meta(name: str) -> dict[str, Any]:
    path = _meta_path(name)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(name: str, meta: dict[str, Any]) -> dict[str, Any]:
    path = _meta_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def _update_meta(name: str, **changes: Any) -> dict[str, Any]:
    with _lock:
        meta = load_meta(name)
        if not meta:
            # Instância removida enquanto um worker rodava; não recriar um meta.json órfão.
            return {}
        meta.update(changes)
        return _write_meta(name, meta)


def exists(name: str) -> bool:
    return _meta_path(name).is_file()


def names() -> list[str]:
    return sorted(p.parent.name for p in _root().glob("*/meta.json") if load_meta(p.parent.name).get("instance_num"))


def adb_serial(meta: dict[str, Any]) -> str:
    return f"127.0.0.1:{ADB_BASE_PORT + int(meta['instance_num']) - 1}"


def _instance_runtime_dir(name: str, meta: dict[str, Any]) -> Path:
    num = int(meta["instance_num"])
    if _is_cam12(meta):
        return _cvd_home(name, meta) / f"cuttlefish_runtime.{num}"
    return _home(name) / "cuttlefish" / "instances" / f"cvd-{num}"


def _arm64_group_home(name: str, meta: dict[str, Any]) -> Path | None:
    # O cvd 1.57 roda os processos com caminhos /var/tmp/cvd/<uid>/<id>/home/...;
    # esse "home" é um symlink para o diretório real da instância.
    base = Path(f"/var/tmp/cvd/{os.getuid()}")
    try:
        target = _home(name).resolve()
    except OSError:
        return None
    try:
        candidates = base.glob("*/home")
    except OSError:
        return None
    for link in candidates:
        try:
            if link.resolve() == target:
                return link
        except OSError:
            continue
    return None


def running(name: str, meta: dict[str, Any] | None = None) -> bool:
    meta = meta or load_meta(name)
    if not meta:
        return False
    pattern = f"{_instance_runtime_dir(name, meta)}/"
    if _run(["pgrep", "-f", pattern], timeout=5).returncode == 0:
        return True
    if _is_arm64(meta):
        group_home = _arm64_group_home(name, meta)
        if group_home:
            alt = f"{group_home}/cuttlefish/instances/cvd-{int(meta['instance_num'])}/"
            return _run(["pgrep", "-f", alt], timeout=5).returncode == 0
    return False


def _num_in_use(num: int) -> bool:
    if _run(["pgrep", "-f", f"(/cvd-{num}/|/cuttlefish_runtime\\.{num}/)"], timeout=5).returncode == 0:
        return True
    return any(int(load_meta(n).get("instance_num") or 0) == num for n in names())


def _allocate_num() -> int:
    for num in range(1, 33):
        if not _num_in_use(num):
            return num
    raise RuntimeError("Nenhum número de instância Cuttlefish livre (1-32).")


def _cvd_env(name: str, meta: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = str(_cvd_home(name, meta))
    if _is_arm64(meta):
        # ANDROID_HOST_OUT aponta para o package CI gravável (symlinks → debian);
        # apontar direto para /usr/lib/cuttlefish-common faz a substituição de
        # symlinks do cvd escrever no pacote debian (read-only) e falhar com EPERM.
        env["ANDROID_HOST_OUT"] = str(arm64_host_dir())
        env["ANDROID_SOONG_HOST_OUT"] = str(arm64_host_dir())
        # O qemu do host aborta em `-device AC97` sem backend de áudio configurado.
        env["QEMU_AUDIO_DRV"] = "none"
    else:
        env["ANDROID_HOST_OUT"] = str(_host_dir(meta))
        env["ANDROID_SOONG_HOST_OUT"] = str(_host_dir(meta))
    env["ANDROID_PRODUCT_OUT"] = str(_system_dir(meta))
    return env


def guest_cid(meta: dict[str, Any]) -> int:
    return 2 + int(meta["instance_num"])


def _feed_pid_path(name: str) -> Path:
    return _home(name) / "camera_feed.pid"


def _feed_pid(name: str) -> int | None:
    try:
        pid = int(_feed_pid_path(name).read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except (OSError, ValueError):
        return None
    return pid if FEED_SCRIPT.name in cmdline else None


def camera_feed_running(name: str) -> bool:
    return _feed_pid(name) is not None


def stop_camera_feed(name: str) -> None:
    pid = _feed_pid(name)
    if pid is not None:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass
    _feed_pid_path(name).unlink(missing_ok=True)


def _feed_device(source: str) -> str:
    if not source or source == CAMERA_TEST_SOURCE:
        return "lavfi:testsrc2=size=1280x720:rate=30"
    if not re.fullmatch(r"/dev/video\d+", source):
        raise RuntimeError(f"Fonte de câmera inválida: {source}")
    return source


def _write_feed_source(name: str, meta: dict[str, Any]) -> Path:
    path = _home(name) / "camera_source"
    path.write_text(_feed_device(meta.get("camera_source") or CAMERA_TEST_SOURCE), encoding="utf-8")
    return path


def start_camera_feed(name: str) -> None:
    meta = load_meta(name)
    if not _is_cam12(meta):
        return
    stop_camera_feed(name)
    source_file = _write_feed_source(name, meta)
    cmd = [
        sys.executable, str(FEED_SCRIPT),
        "--cid", str(guest_cid(meta)),
        "--port", str(CAMERA_VSOCK_PORT),
        "--source-file", str(source_file),
    ]
    with (_home(name) / "camera_feed.log").open("w", encoding="utf-8") as fh:
        p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    _feed_pid_path(name).write_text(str(p.pid))


def set_camera_source(name: str, source: str) -> dict[str, Any]:
    meta = load_meta(name)
    if not _is_cam12(meta):
        raise RuntimeError("Esta instância Cuttlefish não tem o HAL de câmera vsock.")
    _feed_device(source)
    meta = _update_meta(name, camera_source=source or CAMERA_TEST_SOURCE)
    _write_feed_source(name, meta)
    if running(name, meta) and not camera_feed_running(name):
        start_camera_feed(name)
    return to_instance(name)


def to_instance(name: str) -> dict[str, Any]:
    meta = load_meta(name)
    is_running = running(name, meta)
    phase = meta.get("phase") or ""
    worker = _workers.get(name)
    busy = bool(worker and worker.is_alive())
    if busy and phase:
        status_text = phase
    elif is_running:
        status_text = "running"
    else:
        status_text = "exited"
    num = int(meta.get("instance_num") or 1)
    cam12 = _is_cam12(meta)
    feed = False
    if cam12 and is_running and not busy:
        feed = camera_feed_running(name)
        if not feed:
            try:
                start_camera_feed(name)
                feed = True
            except (OSError, RuntimeError):
                pass
    return {
        "id": f"cvd-{num}",
        "name": name,
        "kind": "cuttlefish",
        "image": meta.get("image") or f"{IMAGE_PREFIX}android{android_version()}",
        "status": status_text,
        "running": is_running and not busy,
        "busy": busy,
        "host_port": ADB_BASE_PORT + num - 1,
        "webrtc_url": (
            f"https://localhost:{CAM12_WEBRTC_BASE_PORT + num - 1}"
            if cam12
            else f"{OPERATOR_URL}/client.html?deviceId=cvd-{num}"
        ),
        "gapps": meta.get("gapps") or "none",
        "gapps_available": ("12" if cam12 else android_version()) in MINDTHEGAPPS,
        "last_error": meta.get("last_error") or "",
        "proxy_enabled": False,
        "intercept_enabled": False,
        "camera_enabled": cam12,
        "camera_mode": "vsock" if cam12 else "none",
        "camera_source": (meta.get("camera_source") or CAMERA_TEST_SOURCE) if cam12 else "",
        "camera_feed_running": feed,
        "phone_number": meta.get("phone_number") or "",
    }


def list_instances() -> list[dict[str, Any]]:
    return [to_instance(n) for n in names()]


def _adb(serial: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return _run(["adb", "-s", serial, *args], timeout=timeout)


def wait_boot(serial: str, timeout: float = 420.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _run(["adb", "connect", serial], timeout=15)
        try:
            p = _adb(serial, "shell", "getprop", "sys.boot_completed", timeout=10)
            if p.returncode == 0 and (p.stdout or "").strip() == "1":
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(3)
    raise RuntimeError(f"Android não terminou o boot em {int(timeout)}s ({serial}).")


def _adb_root(serial: str) -> None:
    p = _adb(serial, "root", timeout=20)
    if "restarting" in f"{p.stdout}{p.stderr}".lower():
        time.sleep(2)
        _run(["adb", "connect", serial], timeout=15)
        _adb(serial, "wait-for-device", timeout=60)


def _listening_ports() -> set[int]:
    p = _run(["ss", "-ltnH"], timeout=5)
    ports: set[int] = set()
    for line in (p.stdout or "").splitlines():
        cols = line.split()
        if len(cols) >= 4 and cols[3].rsplit(":", 1)[-1].isdigit():
            ports.add(int(cols[3].rsplit(":", 1)[-1]))
    return ports


def _unstick_rootcanal(home: Path, stuck_since: dict[int, float]) -> None:
    """root-canal trava ~25% das vezes a 100% de CPU sem abrir a porta HCI; se não for relançado
    em segundos, o HAL Bluetooth do guest desiste e o boot fica para sempre em BOOT_PENDING: Bluetooth."""
    p = _run(["pgrep", "-x", "root-canal"], timeout=5)
    ports = None
    for pid_s in (p.stdout or "").split():
        pid = int(pid_s)
        try:
            if not os.readlink(f"/proc/{pid}/fd/1").startswith(str(home)):
                continue
            args = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue
        hci = next((int(a.split("=", 1)[1]) for a in args if a.startswith("--hci_port=")), None)
        if hci is None and len(args) > 3 and args[1].isdigit() and args[2].isdigit():
            # Android 12: root-canal <test_port> <hci_port> <link_port>
            hci = int(args[2])
        if hci is None:
            continue
        if ports is None:
            ports = _listening_ports()
        if hci in ports:
            stuck_since.pop(pid, None)
            continue
        first = stuck_since.setdefault(pid, time.time())
        if time.time() - first > 6:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
            stuck_since.pop(pid, None)


def _boot_completed(serial: str) -> bool:
    _run(["adb", "connect", serial], timeout=15)
    try:
        p = _adb(serial, "shell", "getprop", "sys.boot_completed", timeout=10)
    except subprocess.TimeoutExpired:
        return False
    return p.returncode == 0 and (p.stdout or "").strip() == "1"


def _launch(name: str, meta: dict[str, Any]) -> None:
    home = _cvd_home(name, meta)
    home.mkdir(parents=True, exist_ok=True)
    host = _host_dir(meta)
    system = _system_dir(meta)
    if _is_arm64(meta):
        entry = arm64_tools_dir() / "bin" / "cvd_internal_start"
    else:
        entry = host / "bin" / "launch_cvd"
    cmd = [
        str(entry),
        "--daemon",
        f"--base_instance_num={int(meta['instance_num'])}",
        f"--system_image_dir={system}",
        f"--cpus={int(meta.get('cpus') or 2)}",
        f"--memory_mb={int(meta.get('memory_mb') or 4096)}",
        f"--gpu_mode={meta.get('gpu_mode') or 'auto'}",
        "--report_anonymous_usage_stats=n",
    ]
    if _is_cam12(meta):
        cmd += [
            f"--bootloader={system / 'bootloader'}",
            # O root-canal do Android 12 trava a 100% de CPU e nunca abre a porta HCI neste host.
            "--enable_host_bluetooth=false",
            "--extra_kernel_cmdline="
            f"androidboot.vsock_camera_port={CAMERA_VSOCK_PORT} androidboot.vsock_camera_cid={guest_cid(meta)}",
        ]
    if _is_arm64(meta):
        # Emulação ARM64 é pesada: dá mais RAM e não limita o boot a máquina de 4 núcleos.
        cmd += ["--memory_mb=4096"] if int(meta.get("memory_mb") or 0) < 4096 else []
        # A detecção de GPU do host package antigo trava neste kernel (gfxDtctCanSegv zumbi).
        cmd = [a for a in cmd if not a.startswith("--gpu_mode=")] + ["--gpu_mode=guest_swiftshader"]
        # Wrapper que remove `-device AC97` (qemu 8.2 aborta sem backend de áudio).
        cmd += [f"--qemu_binary_dir={arm64_images_dir() / 'qemu-bin'}"]
        # u-boot aarch64 do pacote ARM64 (o apt não inclui bootloader).
        cmd += [f"--bootloader={_arm64_bootloader()}"]
    serial = adb_serial(meta)
    log = _home(name) / "launch.log"
    with log.open("w", encoding="utf-8") as fh:
        # Sessão própria: fechar o app (que encerra a API) no meio do boot não pode derrubar a VM.
        p = subprocess.Popen(
            cmd, cwd=str(home), env=_cvd_env(name, meta), stdout=fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        started = time.time()
        deadline = started + 900
        stuck_since: dict[int, float] = {}
        last_probe = 0.0
        while p.poll() is None:
            now = time.time()
            if now > deadline:
                p.kill()
                raise RuntimeError("launch_cvd não terminou em 15 min.")
            _unstick_rootcanal(home, stuck_since)
            # Se o Bluetooth nunca reportar pronto, launch_cvd espera para sempre mesmo com o Android no ar.
            if now - started > 120 and now - last_probe > 15:
                last_probe = now
                if _boot_completed(serial):
                    threading.Thread(target=p.wait, daemon=True).start()
                    return
            time.sleep(2)
    if p.returncode != 0:
        tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-8:]
        raise RuntimeError("launch_cvd falhou: " + " | ".join(tail))


def stop(name: str) -> None:
    meta = load_meta(name)
    if not meta:
        return
    stop_camera_feed(name)
    if _is_arm64(meta):
        stop_bin = arm64_tools_dir() / "bin" / "cvd_internal_stop"
    else:
        stop_bin = _host_dir(meta) / "bin" / "stop_cvd"
    if stop_bin.is_file() and running(name, meta):
        _run([str(stop_bin)], timeout=120, env=_cvd_env(name, meta))
    _run(["adb", "disconnect", adb_serial(meta)], timeout=10)


def _mindthegapps_zip(version: str) -> Path:
    url = MINDTHEGAPPS.get(version)
    if not url:
        raise RuntimeError(f"Sem MindTheGapps x86_64 para Android {version}.")
    dest = _cache_dir() / url.rsplit("/", 1)[-1]
    if not dest.is_file() or dest.stat().st_size < 10_000_000:
        tmp = dest.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dest)
    return dest


def skip_setup_wizard(serial: str) -> None:
    pkg = "com.google.android.setupwizard"
    path = _adb(serial, "shell", "pm", "path", pkg, timeout=15)
    if "package:" not in (path.stdout or ""):
        return
    for cmd in (
        ["settings", "put", "global", "device_provisioned", "1"],
        ["settings", "put", "secure", "user_setup_complete", "1"],
        ["pm", "disable-user", "--user", "0", pkg],
        ["locksettings", "set-disabled", "true"],
        # SystemUI só relê device_provisioned no boot; sem reiniciar, o keyguard fica preso sobre a HOME.
        ["pkill", "-f", "com.android.systemui"],
    ):
        _adb(serial, "shell", *cmd, timeout=20)
    time.sleep(8)
    _adb(serial, "shell", "am", "start", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME", timeout=20)


def install_gapps(name: str) -> None:
    meta = load_meta(name)
    serial = adb_serial(meta)
    zip_path = _mindthegapps_zip("12" if _is_cam12(meta) else android_version())
    wait_boot(serial)
    _adb_root(serial)
    p = _adb(serial, "remount", timeout=120)
    out = f"{p.stdout}{p.stderr}".lower()
    if "reboot" in out:
        _adb(serial, "reboot", timeout=30)
        time.sleep(10)
        wait_boot(serial)
        _adb_root(serial)
        p = _adb(serial, "remount", timeout=120)
    if p.returncode != 0:
        raise RuntimeError("adb remount falhou: " + (p.stderr or p.stdout or "").strip())

    with zipfile.ZipFile(zip_path) as zf:
        parts = [part for part in ("product", "system_ext") if any(n.startswith(f"system/{part}/") for n in zf.namelist())]
    tmp = "/data/local/tmp/redroid-mtg"
    push = _adb(serial, "push", str(zip_path), f"{tmp}.zip", timeout=600)
    if push.returncode != 0:
        raise RuntimeError("adb push do MindTheGapps falhou: " + (push.stderr or push.stdout or "").strip())
    # O scratch do overlayfs é montado com "sync": em HD cada arquivo leva minutos sem o remount async.
    script = " && ".join(
        [
            f"rm -rf {tmp}",
            f"unzip -o -q {tmp}.zip " + " ".join(f"'system/{part}/*'" for part in parts) + f" -d {tmp}",
            "mount -o remount,async /mnt/scratch",
            *[f"cp -rf {tmp}/system/{part}/. /{part}/" for part in parts],
            *[f"restorecon -RF /{part}" for part in parts],
            f"rm -rf {tmp} {tmp}.zip",
            "sync",
            "echo REDROID_MTG_OK",
        ]
    )
    copy = _adb(serial, "shell", script, timeout=3600)
    if "REDROID_MTG_OK" not in (copy.stdout or ""):
        raise RuntimeError("Cópia do MindTheGapps falhou: " + (copy.stderr or copy.stdout or "").strip()[-400:])

    _adb(serial, "reboot", timeout=30)
    time.sleep(10)
    wait_boot(serial)
    _adb_root(serial)
    skip_setup_wizard(serial)


def _run_worker(name: str, fn) -> None:
    def work() -> None:
        try:
            fn()
            _update_meta(name, phase="", last_error="")
        except Exception as exc:
            _update_meta(name, phase="", last_error=str(exc))

    t = threading.Thread(target=work, daemon=True)
    _workers[name] = t
    t.start()


def _busy(name: str) -> bool:
    w = _workers.get(name)
    return bool(w and w.is_alive())


def create(
    name: str,
    *,
    gapps: bool,
    cpus: int = 2,
    memory_mb: int = 4096,
    gpu_mode: str = "auto",
    image: str = "",
    camera_source: str = CAMERA_TEST_SOURCE,
) -> dict[str, Any]:
    cam12 = image == CAM12_IMAGE
    arm64 = image == ARM64_IMAGE
    if cam12:
        if not cam12_present():
            raise RuntimeError(
                f"Imagem Android 12 com câmera ausente ({cam12_images_dir()}) ou ferramentas de host "
                f"ausentes ({cam12_host_dir()})."
            )
        _feed_device(camera_source)
    elif arm64:
        if not arm64_present():
            raise RuntimeError(f"Imagens Cuttlefish ARM64 ausentes em {arm64_images_dir()}.")
        # ARM64 não tem imagem MindTheGapps ARM64 neste projeto; o app-alvo é nativo ARM mesmo.
        gapps = False
    elif not images_present():
        raise RuntimeError(f"Imagens Cuttlefish ausentes em {images_dir()}. Rode: tools/cuttlefish/cf.sh fetch")
    if exists(name):
        raise RuntimeError(f"Já existe uma instância Cuttlefish chamada {name}.")
    with _lock:
        num = _allocate_num()
        data: dict[str, Any] = {
            "name": name,
            "instance_num": num,
            "image": ARM64_IMAGE if arm64 else (CAM12_IMAGE if cam12 else f"{IMAGE_PREFIX}android{android_version()}"),
            "cpus": cpus,
            "memory_mb": memory_mb,
            "gpu_mode": gpu_mode,
            "gapps": "pending" if gapps else "none",
            "phase": "starting",
            "created_at": int(time.time()),
        }
        if cam12:
            data["variant"] = CAM12_VARIANT
            data["camera_source"] = camera_source or CAMERA_TEST_SOURCE
        if arm64:
            data["variant"] = ARM64_VARIANT
        meta = _write_meta(name, data)

    def work() -> None:
        _launch(name, meta)
        start_camera_feed(name)
        if gapps:
            _update_meta(name, phase="installing gapps", gapps="installing")
            try:
                install_gapps(name)
            except Exception:
                _update_meta(name, gapps="error")
                raise
            _update_meta(name, gapps="installed")

    _run_worker(name, work)
    return to_instance(name)


def start(name: str) -> dict[str, Any]:
    if _busy(name):
        raise RuntimeError(f"{name} ainda está processando ({load_meta(name).get('phase')}).")
    meta = load_meta(name)
    if running(name, meta):
        return to_instance(name)
    _update_meta(name, phase="starting")

    def work() -> None:
        _launch(name, meta)
        start_camera_feed(name)
        # launch_cvd recria o iccprofile do chip a cada boot, então o número volta ao padrão.
        if meta.get("phone_number"):
            try:
                set_phone_number(name, meta["phone_number"])
            except RuntimeError:
                pass

    _run_worker(name, work)
    return to_instance(name)


def add_gapps(name: str) -> dict[str, Any]:
    if _busy(name):
        raise RuntimeError(f"{name} ainda está processando ({load_meta(name).get('phase')}).")
    meta = load_meta(name)
    if not running(name, meta):
        raise RuntimeError("Inicie a instância antes de instalar o MindTheGapps.")
    _update_meta(name, phase="installing gapps", gapps="installing")

    def work() -> None:
        try:
            install_gapps(name)
        except Exception:
            _update_meta(name, gapps="error")
            raise
        _update_meta(name, gapps="installed")

    _run_worker(name, work)
    return to_instance(name)


def remove(name: str) -> None:
    meta = load_meta(name)
    stop(name)
    for home in {_cvd_home(name, meta), _home(name)}:
        if home.exists():
            shutil.rmtree(home, ignore_errors=True)


def _sms_address(sender: str) -> str:
    sender = sender.strip()
    digits = re.sub(r"\D", "", sender)
    if digits and re.fullmatch(r"\+?[\d\s().-]+", sender):
        toa = "91" if sender.startswith("+") else "81"
        bcd = digits + ("F" if len(digits) % 2 else "")
        return f"{len(digits):02X}{toa}" + "".join(bcd[i + 1] + bcd[i] for i in range(0, len(bcd), 2))
    alpha = re.sub(r"[^A-Za-z0-9 ]", "", sender)[:11] or "SMS"
    # Remetente alfanumérico: GSM 7-bit empacotado; nesse subconjunto o código GSM é o próprio ASCII.
    bits = 0
    nbits = 0
    packed = bytearray()
    for ch in alpha:
        bits |= ord(ch) << nbits
        nbits += 7
        while nbits >= 8:
            packed.append(bits & 0xFF)
            bits >>= 8
            nbits -= 8
    if nbits:
        packed.append(bits & 0xFF)
    semi_octets = (len(alpha) * 7 + 3) // 4
    return f"{semi_octets:02X}D0" + packed.hex().upper()


def _sms_pdus(sender: str, text: str) -> list[str]:
    """PDUs SMS-SUBMIT no formato que o modem_simulator aceita em AT+REMOTESMS.

    DCS 0x19 (UCS2, classe 1): o parser do modem_simulator calcula errado o tamanho com DCS 0x08.
    """
    address = _sms_address(sender)
    units = text.encode("utf-16-be")
    if len(units) <= 140:
        chunks = [units]
    else:
        chunks = []
        start = 0
        while start < len(units):
            end = min(start + 134, len(units))
            if end < len(units) and 0xD8 <= units[end - 2] <= 0xDB:
                end -= 2  # não partir um par substituto (emoji) entre duas partes
            chunks.append(units[start:end])
            start = end
        if len(chunks) > 255:
            raise RuntimeError("SMS longo demais.")
    ref = int(time.time()) & 0xFF
    pdus = []
    for seq, chunk in enumerate(chunks, 1):
        udh = bytes([5, 0, 3, ref, len(chunks), seq]) if len(chunks) > 1 else b""
        ud = udh + chunk
        pdu_type = "41" if udh else "01"
        pdus.append(f"00{pdu_type}00{address}0019{len(ud):02X}{ud.hex().upper()}")
    return pdus


def _running_meta(name: str) -> dict[str, Any]:
    meta = load_meta(name)
    if not meta:
        raise RuntimeError(f"Instância Cuttlefish {name} não encontrada.")
    if not running(name, meta):
        raise RuntimeError("Inicie a instância antes.")
    return meta


def _modem_command(meta: dict[str, Any], command: str, want_reply: bool = False) -> str:
    """Envia um comando AT ao modem_simulator como se fosse outra instância (canal remoto)."""
    import socket

    host_port = ADB_BASE_PORT + int(meta["instance_num"]) - 1
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect(f"\0modem_simulator{host_port}")
        sock.sendall(b"REM0")
        time.sleep(0.1)
        sock.sendall(f"{command}\r".encode())
        if want_reply:
            return sock.recv(512).decode(errors="replace")
        time.sleep(0.3)
        return ""
    except OSError as exc:
        raise RuntimeError(f"modem_simulator{host_port} indisponível: {exc}") from exc
    finally:
        sock.close()


def send_sms(name: str, sender: str, text: str) -> dict[str, Any]:
    """Entrega um SMS real pelo modem simulado, fingindo ser outra instância Cuttlefish."""
    meta = _running_meta(name)
    if not text.strip():
        raise RuntimeError("Texto do SMS vazio.")
    pdus = _sms_pdus(sender, text)
    for pdu in pdus:
        _modem_command(meta, f"AT+REMOTESMS={pdu}")
    host_port = ADB_BASE_PORT + int(meta["instance_num"]) - 1
    return {"parts": len(pdus), "sender": sender, "modem_socket": f"modem_simulator{host_port}"}


def incoming_call(name: str, caller: str, ring_seconds: float = 4.0) -> dict[str, Any]:
    """Toca uma chamada recebida de `caller` e desliga sem atender (fica como chamada perdida)."""
    import socket

    meta = _running_meta(name)
    digits = re.sub(r"\D", "", caller)
    if not 3 <= len(digits) <= 20:
        raise RuntimeError(f"Número inválido: {caller}")
    number = f"+{digits}"
    ring_seconds = max(1.0, min(float(ring_seconds), 30.0))
    host_port = ADB_BASE_PORT + int(meta["instance_num"]) - 1
    # A chamada pertence a esta conexão: o modem só a mantém enquanto o socket estiver aberto.
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect(f"\0modem_simulator{host_port}")
        sock.sendall(b"REM0")
        time.sleep(0.1)
        sock.sendall(f'AT+REMOTECALL=4,0,0,"{number}",145\r'.encode())
        time.sleep(ring_seconds)
        sock.sendall(f'AT+REMOTECALL=6,0,0,"{number}",145\r'.encode())
        time.sleep(0.5)
    except OSError as exc:
        raise RuntimeError(f"modem_simulator{host_port} indisponível: {exc}") from exc
    finally:
        sock.close()
    return {"caller": number, "ring_seconds": ring_seconds}


def _msisdn_record(number: str) -> str:
    """Registro EF_MSISDN de 28 bytes: 14 de rótulo + tamanho, TON/NPI, 10 de BCD, CCP e EXT."""
    digits = re.sub(r"\D", "", number)
    if not 3 <= len(digits) <= 20:
        raise RuntimeError(f"Número inválido: {number}")
    bcd = digits + ("F" if len(digits) % 2 else "")
    swapped = "".join(bcd[i + 1] + bcd[i] for i in range(0, len(bcd), 2))
    nbytes = len(swapped) // 2
    return "FF" * 14 + f"{nbytes + 1:02X}91" + swapped + "FF" * (10 - nbytes) + "FFFF"


def set_phone_number(name: str, number: str) -> dict[str, Any]:
    """Grava o número no chip simulado (EF_MSISDN) e faz o Android relê-lo."""
    meta = _running_meta(name)
    number = "+" + re.sub(r"\D", "", number)
    reply = _modem_command(meta, f"AT+CRSM=220,28480,1,4,28,{_msisdn_record(number)}", want_reply=True)
    if "+CRSM: 144,0" not in reply:
        raise RuntimeError(f"Modem recusou gravar o número: {reply.strip() or 'sem resposta'}")
    _update_meta(name, phone_number=number)

    # O Android só relê o EF_MSISDN ao recarregar o chip. Reiniciar o RIL força isso; matar
    # com.android.phone também funciona, mas deixa os dados móveis em RADIO_NOT_AVAILABLE.
    serial = adb_serial(meta)
    _run(["adb", "connect", serial], timeout=15)
    _adb_root(serial)
    _adb(serial, "shell", "kill $(pidof libcuttlefish-rild)", timeout=20)
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(3)
        p = _adb(
            serial, "shell", "content", "query", "--uri", "content://telephony/siminfo",
            "--projection", "number", timeout=15,
        )
        if number in (p.stdout or ""):
            return {"phone_number": number, "applied": True}
    return {"phone_number": number, "applied": False, "note": "Gravado no chip; o Android ainda não releu."}


def resolve_serial(name: str) -> str:
    meta = load_meta(name)
    if not running(name, meta) or _busy(name):
        raise RuntimeError("Instância Cuttlefish não está pronta (inicie e aguarde o boot).")
    serial = adb_serial(meta)
    wait_boot(serial, timeout=60)
    return serial
