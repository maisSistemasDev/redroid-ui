"""Pacote HAL V4L2 (bind-mount) e extração a partir de imagens Docker customizadas."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent.parent / "tools" / "camera"
VENDOR = TOOLS / "vendor"

HAL_SO = VENDOR / "lib64" / "hw" / "camera.v4l2.so"
LINKER_RC = VENDOR / "etc" / "init" / "v4l2_camera.rc"
VINTF_XML = VENDOR / "etc" / "vintf" / "manifest" / "camera.v4l2.xml"
PROVIDER_RC = VENDOR / "etc" / "init" / "camera.provider.v4l2.rc"
PROVIDER_BIN = VENDOR / "bin" / "hw" / "android.hardware.camera.provider@2.4-service_64"
PROVIDER_BIN_ALIAS = VENDOR / "bin" / "hw" / "android.hardware.camera.provider@2.4-service"

# Caminhos comuns em imagens reDroid compiladas com USE_CAMERA_V4L2_HAL.
EXTRACT_HAL_CANDIDATES = (
    "/vendor/lib64/hw/camera.v4l2.so",
    "/system/lib64/hw/camera.v4l2.so",
    "/vendor/lib/hw/camera.v4l2.so",
    "/system/lib/hw/camera.v4l2.so",
)
EXTRACT_PROVIDER_CANDIDATES = (
    "/vendor/bin/hw/android.hardware.camera.provider@2.4-service_64",
    "/vendor/bin/hw/android.hardware.camera.provider@2.4-service",
)
EXTRACT_PROVIDER_LIBS = (
    "/vendor/lib64/hw/android.hardware.camera.provider@2.4-legacy.so",
    "/vendor/lib64/hw/android.hardware.camera.provider@2.4-impl.so",
    "/vendor/lib64/hw/camera.default.so",
)


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def hal_so_present() -> bool:
    return HAL_SO.is_file() and HAL_SO.stat().st_size > 4096


def provider_present() -> bool:
    return PROVIDER_BIN.is_file() and PROVIDER_BIN.stat().st_size > 4096


def overlay_present() -> bool:
    return LINKER_RC.is_file() and VINTF_XML.is_file()


def bundle_status() -> dict[str, Any]:
    level = "none"
    if hal_so_present():
        level = "hal_only" if not provider_present() else "full"
    return {
        "level": level,
        "hal_so": hal_so_present(),
        "provider_service": provider_present(),
        "overlay_files": overlay_present(),
        "paths": {
            "hal_so": str(HAL_SO),
            "provider": str(PROVIDER_BIN),
        },
        "recommended_image": "redroid/redroid:12.0.0_64only-latest",
        "build_hint": (
            "Compile reDroid com USE_CAMERA_V4L2_HAL := true e os pacotes camera.provider 2.4 "
            "(ver tools/camera/build-redroid-camera.sh), depois POST /camera/hal/extract."
        ),
    }


def docker_hal_volume_flags() -> list[str]:
    flags: list[str] = []
    if hal_so_present():
        hal = HAL_SO.resolve()
        # System: atualiza HAL da imagem; vendor: legacy provider só carrega daqui (linker patch no bootstrap).
        flags.extend(
            [
                "-v",
                f"{hal}:/system/lib64/hw/camera.v4l2.so:ro",
                "-v",
                f"{hal}:/vendor/lib64/hw/camera.v4l2.so:ro",
            ]
        )
    mount_init_rc = os.environ.get("REDROID_CAMERA_MOUNT_INIT_RC", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if mount_init_rc and LINKER_RC.is_file():
        flags.extend(["-v", f"{LINKER_RC.resolve()}:/vendor/etc/init/v4l2_camera.rc:ro"])
    # Imagem custom (redroid:12-camera) já inclui VINTF; bind-mount duplicado quebra o boot (ADB offline).
    mount_vintf = os.environ.get("REDROID_CAMERA_MOUNT_VINTF", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if mount_vintf and VINTF_XML.is_file():
        flags.extend(["-v", f"{VINTF_XML.resolve()}:/vendor/etc/vintf/manifest/camera.v4l2.xml:ro"])
    if provider_present():
        flags.extend(
            [
                "-v",
                f"{PROVIDER_BIN.resolve()}:/vendor/bin/hw/android.hardware.camera.provider@2.4-service_64:ro",
            ]
        )
    if mount_init_rc and PROVIDER_RC.is_file() and provider_present():
        flags.extend(["-v", f"{PROVIDER_RC.resolve()}:/vendor/etc/init/camera.provider.v4l2.rc:ro"])
    for name in (
        "android.hardware.camera.provider@2.4-legacy.so",
        "android.hardware.camera.provider@2.4-impl.so",
    ):
        local = VENDOR / "lib64" / "hw" / name
        if local.is_file():
            rel = f"lib64/hw/{name}"
            flags.extend(["-v", f"{local.resolve()}:/vendor/{rel}:ro"])
    return flags


def _docker_cp(cid: str, container_path: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    p = _run(["docker", "cp", f"{cid}:{container_path}", str(dest)], timeout=180)
    return p.returncode == 0 and dest.is_file() and dest.stat().st_size > 4096


def extract_from_image(image: str) -> dict[str, Any]:
    image = (image or "").strip()
    if not image:
        raise ValueError("Informe o nome da imagem Docker (ex.: redroid-camera:local).")
    if shutil.which("docker") is None:
        raise RuntimeError("Docker CLI não encontrado.")

    created = _run(["docker", "create", image], timeout=300)
    if created.returncode != 0:
        err = (created.stderr or created.stdout or "").strip()
        raise RuntimeError(err or f"Falha ao criar contêiner a partir de {image}")
    cid = (created.stdout or "").strip()
    if not cid:
        raise RuntimeError("docker create não retornou ID.")

    copied: list[str] = []
    try:
        for src in EXTRACT_HAL_CANDIDATES:
            if _docker_cp(cid, src, HAL_SO):
                copied.append(f"hal:{src}")
                break

        for src in EXTRACT_PROVIDER_CANDIDATES:
            if _docker_cp(cid, src, PROVIDER_BIN):
                copied.append(f"provider:{src}")
                break

        for src in EXTRACT_PROVIDER_LIBS:
            name = Path(src).name
            dest = VENDOR / "lib64" / "hw" / name
            if _docker_cp(cid, src, dest):
                copied.append(f"lib:{src}")
    finally:
        _run(["docker", "rm", "-f", cid], timeout=60)

    status = bundle_status()
    if not status["hal_so"]:
        raise RuntimeError(
            f"Nenhum camera.v4l2.so encontrado em {image}. "
            "A imagem precisa ser compilada com USE_CAMERA_V4L2_HAL."
        )
    return {"copied": copied, "bundle": status}


def download_hal_url(url: str) -> dict[str, Any]:
    url = (url or "").strip()
    if not url:
        raise ValueError("URL vazia.")
    HAL_SO.parent.mkdir(parents=True, exist_ok=True)
    tmp = HAL_SO.with_suffix(".so.part")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            data = resp.read()
        if len(data) < 4096:
            raise RuntimeError("Arquivo baixado muito pequeno; verifique a URL.")
        tmp.write_bytes(data)
        tmp.replace(HAL_SO)
    except Exception as exc:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Download falhou: {exc}") from exc
    return {"bundle": bundle_status()}


def fetch_hal_from_env() -> dict[str, Any] | None:
    url = os.environ.get("REDROID_CAMERA_HAL_URL", "").strip()
    if not url or hal_so_present():
        return None
    return download_hal_url(url)


_BOOTSTRAP_SCRIPT = TOOLS / "bootstrap-camera-runtime.sh"


def _adb_run(serial: str, *adb_args: str, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    return _run(["adb", "-s", serial, *adb_args], timeout=timeout)


def bootstrap_runtime(serial: str, v4l2_device: str = "") -> dict[str, Any]:
    """
    Após boot: patch linker ([vendor]), espelha HAL em /vendor e sobe camera provider.
    Init .rc via bind-mount é ignorado no Android 12 ('Skipping insecure file').
    """
    serial = (serial or "").strip()
    if not serial:
        return {"ok": False, "detail": "serial ADB vazio"}

    if not _BOOTSTRAP_SCRIPT.is_file():
        return {"ok": False, "detail": f"script ausente: {_BOOTSTRAP_SCRIPT}"}
    script = _BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")
    dev = (v4l2_device or "").strip()
    if re.match(r"^/dev/video[0-9]+$", dev):
        script = f'V4L2_DEV="{dev}"\n' + script
    p = subprocess.run(
        ["adb", "-s", serial, "shell", "su", "0", "sh"],
        input=script,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    out = f"{p.stdout or ''}\n{p.stderr or ''}".strip()

    cam = _adb_run(serial, "shell", "dumpsys", "media.camera", timeout=25)
    text = cam.stdout or ""
    count = 0
    for line in text.splitlines():
        if "Number of camera devices:" in line:
            try:
                count = int(line.split(":")[-1].strip())
            except ValueError:
                pass
            break

    if count == 0:
        _adb_run(serial, "shell", "su", "0", "setprop", "ctl.restart", "cameraserver", timeout=15)
        cam = _adb_run(serial, "shell", "dumpsys", "media.camera", timeout=25)
        text = cam.stdout or ""
        for line in text.splitlines():
            if "Number of camera devices:" in line:
                try:
                    count = int(line.split(":")[-1].strip())
                except ValueError:
                    pass
                break

    ok = count > 0
    detail = f"camera_devices={count}"
    if p.returncode == 2:
        detail = "provider ausente (build-provider-only.sh ou imagem com service_64)"
        ok = False
    elif p.returncode == 3:
        detail = "provider não ficou no ar (SIGHUP/crash)"
        ok = False
    elif not ok and out:
        detail = f"{detail}; log={out[:240]}"

    return {"ok": ok, "detail": detail, "camera_devices": count}
