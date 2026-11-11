"""Câmera virtual no host (v4l2loopback + ffmpeg) para reDroid."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import camera_hal

TOOLS_CAMERA = Path(__file__).resolve().parent.parent / "tools" / "camera"
DEFAULT_IMAGE = TOOLS_CAMERA / "assets" / "default.jpg"

VALID_MODES = frozenset({"none", "sim", "webcam", "image", "video", "stream", "deeplive"})
FEED_MODES = frozenset({"sim", "webcam", "image", "video", "stream", "deeplive"})

_feed_lock = threading.Lock()
_feed_procs: dict[str, subprocess.Popen[Any]] = {}
_feed_session_groups: dict[str, bool] = {}


def hal_bundle_present() -> bool:
    return camera_hal.hal_so_present()


def hal_bundle_status() -> dict[str, Any]:
    return camera_hal.bundle_status()


def default_loopback_env() -> str:
    return os.environ.get("REDROID_CAMERA_LOOPBACK", "/dev/video10").strip() or "/dev/video10"


def loopback_for_instance(instance_name: str) -> str:
    custom = os.environ.get("REDROID_CAMERA_LOOPBACK", "").strip()
    if custom:
        return custom
    # hash() varia entre processos Python — usar digest estável por nome da instância.
    digest = hashlib.sha256(instance_name.encode("utf-8")).digest()
    idx = 10 + (digest[0] % 7)
    return f"/dev/video{idx}"


def _pick_loopback_device(preferred: str) -> str:
    if (
        preferred
        and Path(preferred).exists()
        and _is_loopback_device(preferred)
        and not _is_virtual_capture_source(preferred)
    ):
        return preferred
    for i in range(10, 17):
        path = f"/dev/video{i}"
        if (
            Path(path).exists()
            and _is_loopback_device(path)
            and not _is_virtual_capture_source(path)
        ):
            return path
    env_default = default_loopback_env()
    if (
        Path(env_default).exists()
        and _is_loopback_device(env_default)
        and not _is_virtual_capture_source(env_default)
    ):
        return env_default
    return preferred


def instance_camera_dir(instance_name: str) -> Path:
    d = Path.home() / ".local" / "share" / "redroid-ui" / "instances" / instance_name / "camera"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_default_image() -> Path:
    if DEFAULT_IMAGE.is_file():
        return DEFAULT_IMAGE
    DEFAULT_IMAGE.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=0x2a4d69:s=1280x720:d=1",
        "-frames:v",
        "1",
        str(DEFAULT_IMAGE),
    ]
    subprocess.run(cmd, check=False, timeout=30)
    return DEFAULT_IMAGE


def effective_loopback_device(instance_name: str, cfg: dict[str, Any]) -> str:
    pinned = (cfg.get("loopback_device") or "").strip()
    if pinned:
        return _pick_loopback_device(pinned)
    return _pick_loopback_device(loopback_for_instance(instance_name))


def normalize_camera_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "enabled": False,
        "mode": "none",
        "webcam_device": "/dev/video0",
        "loopback_device": "",
        "media_path": "",
        "stream_url": "",
        "webcam_fallback_image": True,
        "deeplive_root": "",
        "deeplive_input_device": "/dev/video0",
        "deeplive_execution_provider": "",
        "deeplive_mouth_mask": False,
    }
    if not raw:
        return cfg
    mode = str(raw.get("mode") or "none").strip().lower()
    if mode not in VALID_MODES:
        mode = "none"
    cfg["mode"] = mode
    cfg["enabled"] = bool(raw.get("enabled")) and mode != "none"
    dev = str(raw.get("webcam_device") or "/dev/video0").strip()
    if dev.startswith("/dev/video"):
        cfg["webcam_device"] = dev
    cfg["loopback_device"] = str(raw.get("loopback_device") or "").strip()
    cfg["media_path"] = str(raw.get("media_path") or "").strip()
    cfg["stream_url"] = str(raw.get("stream_url") or "").strip()
    cfg["deeplive_root"] = str(raw.get("deeplive_root") or "").strip()
    cfg["deeplive_input_device"] = str(raw.get("deeplive_input_device") or "/dev/video0").strip() or "/dev/video0"
    cfg["deeplive_execution_provider"] = str(raw.get("deeplive_execution_provider") or "").strip()
    cfg["deeplive_mouth_mask"] = bool(raw.get("deeplive_mouth_mask"))
    cfg["webcam_fallback_image"] = bool(raw.get("webcam_fallback_image", True))
    return cfg


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _v4l2_ctl_path() -> str | None:
    return shutil.which("v4l2-ctl")


def _sysfs_video_label(path: str) -> str:
    m = re.match(r"/dev/video(\d+)$", path)
    if not m:
        return ""
    name_path = Path(f"/sys/class/video4linux/video{m.group(1)}/name")
    if name_path.is_file():
        return name_path.read_text(encoding="utf-8", errors="replace").strip().lower()
    return ""


def _device_info(path: Path) -> dict[str, str]:
    info = {"path": str(path), "name": "", "driver": "", "caps": ""}
    if not path.exists():
        return info
    label = _sysfs_video_label(str(path))
    if label:
        info["name"] = label
        if "loopback" in label:
            info["driver"] = "v4l2 loopback"
    ctl_bin = _v4l2_ctl_path()
    if not ctl_bin:
        return info
    ctl = _run([ctl_bin, "--device", str(path), "--info"], timeout=8)
    if ctl.returncode != 0:
        return info
    for line in (ctl.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("Card type"):
            info["name"] = line.split(":", 1)[-1].strip()
        elif line.startswith("Driver name"):
            info["driver"] = line.split(":", 1)[-1].strip()
        elif line.startswith("Capabilities"):
            info["caps"] = line.split(":", 1)[-1].strip()
    return info


def _is_virtual_capture_source(path: str) -> bool:
    """Iriun/DroidCam/OBS: loopback usado como *entrada* (celular), não como sink do emulador."""
    info = _device_info(Path(path))
    name = (info.get("name") or "").lower()
    return any(token in name for token in ("iriun", "droidcam", "obs virtual", "obs-camera"))


def _is_loopback_device(path: str) -> bool:
    label = _sysfs_video_label(path)
    if "loopback" in label or "dummy" in label:
        return True
    info = _device_info(Path(path))
    driver = (info.get("driver") or "").lower()
    name = (info.get("name") or "").lower()
    if "loopback" in driver or "loopback" in name or "dummy" in name:
        return True
    # setup-host.sh usa video10–17; sem v4l2-ctl confiamos no número do nó
    if not _v4l2_ctl_path() and re.match(r"/dev/video(1[0-7])$", path):
        return True
    return False


def _webcam_capture_available(path: str) -> bool:
    if not Path(path).exists():
        return False
    if _is_virtual_capture_source(path):
        return True
    if _is_loopback_device(path):
        return False
    probe = _run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2", "-i", path, "-frames:v", "1", "-f", "null", "-"],
        timeout=8,
    )
    return probe.returncode == 0


def resolve_media_file(instance_name: str, cfg: dict[str, Any]) -> Path:
    ensure_default_image()
    raw = (cfg.get("media_path") or "").strip()
    if raw:
        p = Path(raw)
        if p.is_file():
            return p
        inst = instance_camera_dir(instance_name) / raw
        if inst.is_file():
            return inst
        inst2 = instance_camera_dir(instance_name) / Path(raw).name
        if inst2.is_file():
            return inst2
    return DEFAULT_IMAGE


def list_host_devices() -> dict[str, Any]:
    devices: list[dict[str, str]] = []
    for path in sorted(Path("/dev").glob("video*")):
        devices.append(_device_info(path))
    loopback_hint = default_loopback_env()
    has_loopback = any(
        Path(f"/dev/video{i}").exists() and _is_loopback_device(f"/dev/video{i}") for i in range(10, 17)
    ) or (Path(loopback_hint).exists() and _is_loopback_device(loopback_hint))
    webcams = [
        d
        for d in devices
        if _webcam_capture_available(d["path"])
    ]
    return {
        "devices": devices,
        "loopback_path": loopback_hint,
        "loopback_present": has_loopback,
        "webcam_candidates": webcams,
        "default_image": str(DEFAULT_IMAGE),
        "ffmpeg": bool(_run(["which", "ffmpeg"]).returncode == 0),
        "v4l2_ctl": bool(_v4l2_ctl_path()),
        "hal_bundle": hal_bundle_present(),
        "hal_bundle_status": hal_bundle_status(),
        "loopback_help": (
            "Para imagem/vídeo/stream/simulador: bash tools/camera/setup-host.sh "
            "(video10–17). Comando único video_nr=10 não basta para todas as instâncias."
        ),
    }


def _ffmpeg_v4l2_source(path: str) -> list[str]:
    """Iriun/loopback entrega YU12/YUYV — MJPEG só em webcam USB real."""
    common = ["-f", "v4l2", "-thread_queue_size", "512"]
    if _is_virtual_capture_source(path) or _is_loopback_device(path):
        return common + ["-i", path]
    return common + ["-input_format", "mjpeg", "-framerate", "30", "-i", path]


def _ffmpeg_v4l2_sink(host_dev: str) -> list[str]:
    return [
        "-vf",
        "scale=1280:720",
        "-pix_fmt",
        "yuyv422",
        "-r",
        "30",
        "-f",
        "v4l2",
        host_dev,
    ]


def _resolve_host_video(cfg: dict[str, Any], instance_name: str) -> tuple[str, str]:
    mode = cfg.get("mode") or "none"
    webcam = cfg.get("webcam_device") or "/dev/video0"
    loopback = effective_loopback_device(instance_name, cfg)

    if mode == "webcam" and not Path(loopback).exists():
        if _webcam_capture_available(webcam):
            return webcam, webcam

    if mode in FEED_MODES:
        loopback = _pick_loopback_device(loopback)
        if not Path(loopback).exists() or not _is_loopback_device(loopback):
            raise RuntimeError(
                f"Dispositivo loopback {loopback} não existe. "
                "Execute: bash tools/camera/setup-host.sh "
                "(cria /dev/video10–17; se já carregou v4l2loopback antes, o script recarrega o módulo)."
            )
        return loopback, loopback

    return "", ""


def _ffmpeg_lines_on_device(host_dev: str) -> list[str]:
    if not host_dev:
        return []
    p = _run(["pgrep", "-af", "ffmpeg"], timeout=5)
    if p.returncode != 0:
        return []
    return [ln for ln in (p.stdout or "").splitlines() if host_dev in ln]


def _kill_pids_on_device(host_dev: str) -> None:
    """Mata todo processo (ffmpeg / Deep-Live-Cam / python) cujo argv cita o dispositivo."""
    if not host_dev:
        return
    p = _run(["pgrep", "-af", host_dev], timeout=5)
    if p.returncode != 0:
        return
    for line in (p.stdout or "").splitlines():
        m = re.match(r"^(\d+)\s", line.strip())
        if not m:
            continue
        pid = int(m.group(1))
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    # segunda passada para quem ainda vive (filhos de sessão separada)
    time.sleep(0.5)
    p = _run(["pgrep", "-af", host_dev], timeout=5)
    if p.returncode != 0:
        return
    for line in (p.stdout or "").splitlines():
        m = re.match(r"^(\d+)\s", line.strip())
        if not m:
            continue
        pid = int(m.group(1))
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _kill_ffmpeg_on_device(host_dev: str) -> None:
    _kill_pids_on_device(host_dev)


def stop_feed(instance_name: str, cfg: dict[str, Any] | None = None) -> None:
    with _feed_lock:
        proc = _feed_procs.pop(instance_name, None)
        _feed_session_groups.pop(instance_name, None)
    if proc and proc.poll() is None:
        import deeplive_host

        deeplive_host.stop_process_group(proc)
    if cfg is None:
        return
    try:
        ncfg = normalize_camera_config(cfg)
        host_dev, _ = _resolve_host_video(ncfg, instance_name)
        _kill_pids_on_device(host_dev)
        extra = (ncfg.get("deeplive_input_device") or ncfg.get("webcam_device") or "").strip()
        if extra and extra != host_dev:
            _kill_pids_on_device(extra)
    except RuntimeError:
        pass


def _build_ffmpeg_cmd(instance_name: str, cfg: dict[str, Any], host_dev: str) -> tuple[list[str], dict[str, Any]]:
    meta: dict[str, Any] = {"requested_mode": cfg.get("mode")}
    mode = cfg.get("mode")
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]

    if mode == "sim":
        cmd = base + ["-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30"] + _ffmpeg_v4l2_sink(host_dev)
        return cmd, meta

    if mode == "image":
        img = resolve_media_file(instance_name, cfg)
        meta["media"] = str(img)
        cmd = (
            base
            + ["-re", "-stream_loop", "-1", "-i", str(img)]
            + _ffmpeg_v4l2_sink(host_dev)
        )
        return cmd, meta

    if mode == "video":
        vid = resolve_media_file(instance_name, cfg)
        if not vid.is_file():
            raise RuntimeError(f"Vídeo não encontrado: {cfg.get('media_path') or '(vazio)'}")
        meta["media"] = str(vid)
        cmd = (
            base
            + ["-re", "-stream_loop", "-1", "-i", str(vid)]
            + _ffmpeg_v4l2_sink(host_dev)
        )
        return cmd, meta

    if mode == "stream":
        url = (cfg.get("stream_url") or "").strip()
        if not url:
            raise RuntimeError("Informe a URL do stream (http, https, rtsp, rtmp…).")
        meta["stream_url"] = url
        cmd = base + ["-re", "-i", url] + _ffmpeg_v4l2_sink(host_dev)
        return cmd, meta

    if mode == "webcam":
        webcam = cfg.get("webcam_device") or "/dev/video0"
        if not _webcam_capture_available(webcam) and cfg.get("webcam_fallback_image", True):
            img = resolve_media_file(instance_name, cfg)
            meta["fallback"] = "image"
            meta["media"] = str(img)
            cmd = (
                base
                + ["-re", "-stream_loop", "-1", "-i", str(img)]
                + _ffmpeg_v4l2_sink(host_dev)
            )
            return cmd, meta
        if not _webcam_capture_available(webcam):
            raise RuntimeError(f"Webcam {webcam} indisponível e fallback desligado.")
        cmd = base + _ffmpeg_v4l2_source(webcam) + _ffmpeg_v4l2_sink(host_dev)
        return cmd, meta

    raise RuntimeError(f"Modo de câmera inválido: {mode}")


def start_feed(instance_name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = normalize_camera_config(cfg)
    stop_feed(instance_name, cfg)
    if not cfg["enabled"]:
        return {"running": False}

    if _run(["which", "ffmpeg"]).returncode != 0:
        raise RuntimeError("ffmpeg não encontrado no host.")

    host_dev, container_dev = _resolve_host_video(cfg, instance_name)
    mode = cfg["mode"]

    if mode == "webcam":
        webcam = cfg.get("webcam_device") or "/dev/video0"
        if host_dev == webcam and container_dev == webcam:
            return {
                "running": False,
                "direct_pass": True,
                "host_device": host_dev,
                "container_device": container_dev,
            }

    meta: dict[str, Any] = {"requested_mode": mode}
    if mode == "deeplive":
        import deeplive_host

        face = resolve_media_file(instance_name, cfg)
        meta["media"] = str(face)
        meta["deeplive_input"] = cfg.get("deeplive_input_device") or "/dev/video0"
        root = deeplive_host.resolve_root(cfg)
        st = deeplive_host.status(cfg)
        if not st.get("models_present"):
            raise RuntimeError(
                "Modelo Deep-Live-Cam ausente (inswapper_128_fp16.onnx). "
                "Baixe em ~/Deep-Live-Cam/models/ a partir de "
                "https://huggingface.co/hacksider/deep-live-cam"
            )
        cmd = deeplive_host.build_live_cmd(
            cfg,
            source_face=face,
            input_device=meta["deeplive_input"],
            output_v4l2=host_dev,
        )
        log_path = instance_camera_dir(instance_name) / "deeplive.log"
        meta["deeplive_log"] = str(log_path)
        proc = deeplive_host.start_live_process(cmd, root, log_path=log_path)
        with _feed_lock:
            _feed_procs[instance_name] = proc
            _feed_session_groups[instance_name] = True
        time.sleep(2.5)
        if proc.poll() is not None:
            with _feed_lock:
                _feed_procs.pop(instance_name, None)
                _feed_session_groups.pop(instance_name, None)
            err = ""
            try:
                err = log_path.read_text(encoding="utf-8", errors="replace")[-900:]
            except OSError:
                pass
            raise RuntimeError(
                f"Deep-Live-Cam encerrou (rc={proc.returncode}): {err.strip() or cmd}"
            )
        return {
            "running": True,
            "mode": mode,
            "host_device": host_dev,
            "container_device": container_dev,
            "pid": proc.pid,
            **meta,
        }

    cmd, meta = _build_ffmpeg_cmd(instance_name, cfg, host_dev)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    wait_s = 1.4 if mode == "webcam" else 0.6
    time.sleep(wait_s)
    if proc.poll() is not None:
        err = (proc.stderr.read() if proc.stderr else "") or ""
        raise RuntimeError(
            f"ffmpeg encerrou (rc={proc.returncode}) {host_dev}: {err.strip()[:400] or cmd}"
        )
    with _feed_lock:
        _feed_procs[instance_name] = proc
        _feed_session_groups[instance_name] = False

    return {
        "running": True,
        "mode": mode,
        "host_device": host_dev,
        "container_device": container_dev,
        "pid": proc.pid,
        **meta,
    }


def docker_device_flags(cfg: dict[str, Any], instance_name: str) -> list[str]:
    cfg = normalize_camera_config(cfg)
    if not cfg["enabled"]:
        return []
    host_dev, container_dev = _resolve_host_video(cfg, instance_name)
    if not host_dev:
        return []
    return ["--device", f"{host_dev}:{container_dev}"]


def docker_hal_volume_flags() -> list[str]:
    return camera_hal.docker_hal_volume_flags()


def redroid_camera_boot_args(cfg: dict[str, Any]) -> list[str]:
    cfg = normalize_camera_config(cfg)
    if not cfg["enabled"]:
        return []
    return ["androidboot.redroid_camera=1", "ro.hardware.camera=v4l2"]


def feed_running(instance_name: str, cfg: dict[str, Any] | None = None) -> bool:
    with _feed_lock:
        proc = _feed_procs.get(instance_name)
        if proc is not None:
            if proc.poll() is None:
                return True
            _feed_procs.pop(instance_name, None)
            _feed_session_groups.pop(instance_name, None)
    if not cfg or not cfg.get("enabled"):
        return False
    try:
        host_dev, _ = _resolve_host_video(normalize_camera_config(cfg), instance_name)
        return bool(_ffmpeg_lines_on_device(host_dev))
    except RuntimeError:
        return False


def ensure_feed(instance_name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Garante ffmpeg no loopback (reinicia após reload da API ou troca de mídia)."""
    cfg = normalize_camera_config(cfg)
    if not cfg["enabled"]:
        stop_feed(instance_name, cfg)
        return {"running": False}
    if feed_running(instance_name, cfg):
        return {"running": True, "reused": True}
    return start_feed(instance_name, cfg)


def container_recreate_needed(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Recriar só se muda o que o Docker monta (loopback / ligar-desligar). Troca webcam→sim é só ffmpeg."""
    old = normalize_camera_config(old)
    new = normalize_camera_config(new)
    if bool(old.get("enabled")) != bool(new.get("enabled")):
        return True
    if not new.get("enabled"):
        return False
    return (old.get("loopback_device") or "").strip() != (new.get("loopback_device") or "").strip()


def probe_android_cameras(run_adb: Any) -> dict[str, Any]:
    # dumpsys completo estoura o timeout e a UI mostra camera_count=0 à toa.
    p = run_adb(
        "shell",
        "sh",
        "-c",
        "dumpsys media.camera | grep -m 8 -E 'Number of camera devices:|REJECT device|CONNECT device |ADD device'",
    )
    text = (p.stdout or "") if p.returncode == 0 else (p.stderr or "")
    m = re.search(r"Number of camera devices:\s*(\d+)", text)
    count = int(m.group(1)) if m else 0
    return {
        "camera_count": count,
        "raw_excerpt": "\n".join(text.splitlines()[:12]),
        "hal_mounted": hal_bundle_present(),
        "provider_mounted": camera_hal.provider_present(),
        "bundle_level": hal_bundle_status().get("level"),
    }
