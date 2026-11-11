"""
API para gerenciar contêineres re-droid via Docker e instalar APKs via ADB.
Integra redroid-script para construir imagens com GApps, ARM translation, Magisk, Widevine.
"""
from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import threading
import uuid
from pathlib import Path
from typing import Any

import asyncio

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

DOCKER_PS_FILTER = "label=redroid-ui.managed=1"
REDROID_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "redroid-script"

_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.is_file():
    try:
        from dotenv import load_dotenv

        load_dotenv(_env_path)
    except ImportError:
        pass

try:
    from camera_hal import fetch_hal_from_env

    fetch_hal_from_env()
except Exception:
    pass

_build_lock = threading.Lock()
_build_status: dict[str, Any] = {"running": False, "log": "", "last_result": None}

app = FastAPI(title="re-droid UI API", version="1.0.0")


@app.on_event("startup")
def _restore_camera_feeds_on_startup() -> None:
    import camera_host

    _reap_zombies()

    def _work() -> None:
        time.sleep(1.5)
        root = _data_root()
        if not root.is_dir():
            return
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            name = entry.name
            try:
                cfg = _load_camera_config(name)
                if cfg.get("enabled") and _container_running(name):
                    camera_host.ensure_feed(name, cfg)
            except Exception:
                pass
        _reap_zombies()

    threading.Thread(target=_work, daemon=True).start()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _docker_use_sudo() -> bool:
    """
    REDROID_USE_SUDO_DOCKER:
      0 / false / no / off → só `docker`
      1 / true / yes / on → `sudo -n` + caminho absoluto do binário (casa com NOPASSWD em /usr/bin/docker)
    Se não estiver definido: no Linux usa sudo (caso típico sem grupo docker); noutros SOs, não.
    """
    v = os.environ.get("REDROID_USE_SUDO_DOCKER", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    return sys.platform == "linux"


def _docker_exe() -> str:
    p = shutil.which("docker")
    return p or "docker"


def _docker_cmd() -> list[str]:
    exe = _docker_exe()
    if _docker_use_sudo():
        # -n: não pede senha no subprocesso; exige NOPASSWD no sudoers para o MESMO caminho de `which docker`
        return ["sudo", "-n", exe]
    return [exe]


def _require_bin(name: str) -> None:
    if shutil.which(name) is None:
        raise HTTPException(
            status_code=503,
            detail=f"Comando '{name}' não encontrado no PATH. Instale e tente novamente.",
        )


def _require_docker_cli() -> None:
    _require_bin("docker")
    if _docker_use_sudo():
        _require_bin("sudo")


def _docker_ids() -> list[str]:
    p = _run(_docker_cmd() + ["ps", "-a", "-q", "--filter", DOCKER_PS_FILTER])
    if p.returncode != 0:
        err = (p.stderr or "").strip() or (p.stdout or "").strip() or "Falha ao listar contêineres Docker."
        se = (p.stderr or "").lower()
        if "permission denied" in err.lower() and not _docker_use_sudo():
            err += " Ative sudo (REDROID_USE_SUDO_DOCKER=1) ou adicione o utilizador ao grupo docker."
        elif _docker_use_sudo() and ("password is required" in se or "a password is required" in se):
            err += f" Configure NOPASSWD no sudoers para o mesmo caminho do cliente: {_docker_exe()}"
        raise HTTPException(status_code=500, detail=err)
    return [x for x in p.stdout.strip().split() if x]


def _inspect_container(cid: str) -> dict[str, Any]:
    p = _run(_docker_cmd() + ["inspect", cid])
    if p.returncode != 0:
        raise HTTPException(status_code=404, detail="Contêiner não encontrado.")
    data = json.loads(p.stdout)
    return data[0]


def _container_running(name: str) -> bool:
    try:
        st = _inspect_container(name).get("State", {}) or {}
        return bool(st.get("Running"))
    except Exception:
        return False


def _reap_zombies() -> None:
    """Colhe filhos mortos (ffmpeg/deeplive) para não virarem <defunct> acumulados."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except OSError:
            return
        if pid <= 0:
            return


def _host_port_for_adb(inspect_data: dict[str, Any]) -> int | None:
    ports = inspect_data.get("NetworkSettings", {}).get("Ports", {}) or {}
    for key, bindings in ports.items():
        if "5555" in key and bindings:
            for b in bindings:
                hp = b.get("HostPort")
                if hp and str(hp).isdigit():
                    return int(hp)
    return None


def _container_ip(inspect_data: dict[str, Any]) -> str | None:
    networks = (inspect_data.get("NetworkSettings", {}) or {}).get("Networks", {}) or {}
    for net in networks.values():
        ip = str((net or {}).get("IPAddress", "")).strip()
        if ip:
            return ip
    ip = str((inspect_data.get("NetworkSettings", {}) or {}).get("IPAddress", "")).strip()
    return ip or None


def _container_to_instance(inspect_data: dict[str, Any]) -> dict[str, Any]:
    name = inspect_data.get("Name", "").lstrip("/")
    state = inspect_data.get("State", {}) or {}
    status = state.get("Status", "unknown")
    running = bool(state.get("Running"))
    cid = inspect_data.get("Id", "")[:12]
    image = inspect_data.get("Config", {}).get("Image", "")
    host_port = _host_port_for_adb(inspect_data)
    proxy = _public_proxy_summary(name)
    intercept = _public_intercept_summary(name)
    return {
        "id": cid,
        "name": name,
        "image": image,
        "status": status,
        "running": running,
        "host_port": host_port,
        **proxy,
        **intercept,
        **_public_camera_summary(name),
    }


def _instance_meta_path(name: str) -> Path:
    return _data_root() / name / "instance_meta.json"


def _camera_config_path(name: str) -> Path:
    return _data_root() / name / "camera.json"


def _default_camera_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "mode": "none",
        "webcam_device": "/dev/video0",
        "loopback_device": "",
        "media_path": "",
        "stream_url": "",
        "webcam_fallback_image": True,
    }


def _load_camera_config(name: str) -> dict[str, Any]:
    path = _camera_config_path(name)
    if not path.is_file():
        return _default_camera_config()
    try:
        import camera_host

        return camera_host.normalize_camera_config(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return _default_camera_config()


def _write_camera_config(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    import camera_host

    normalized = camera_host.normalize_camera_config(cfg)
    if normalized.get("enabled") and not (normalized.get("loopback_device") or "").strip():
        normalized["loopback_device"] = camera_host.loopback_for_instance(name)
    path = _camera_config_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalized, indent=2), encoding="utf-8")
    return normalized


def _delete_camera_config(name: str) -> None:
    path = _camera_config_path(name)
    if path.is_file():
        path.unlink()


def _write_instance_meta(name: str, meta: dict[str, Any]) -> None:
    path = _instance_meta_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _load_instance_meta(name: str) -> dict[str, Any]:
    path = _instance_meta_path(name)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _public_camera_summary(name: str) -> dict[str, Any]:
    import camera_host

    cfg = _load_camera_config(name)
    return {
        "camera_enabled": cfg.get("enabled"),
        "camera_mode": cfg.get("mode"),
        "camera_feed_running": camera_host.feed_running(name, _load_camera_config(name)),
        "camera_hal_bundle": camera_host.hal_bundle_present(),
    }


class CreateInstanceBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="Nome do contêiner Docker")
    host_port: int | None = Field(
        default=None,
        ge=1024,
        le=65535,
        description="Porta no host para ADB (mapeada para 5555). Se omitida, aloca automaticamente.",
    )
    image: str = Field(
        default="redroid/redroid:12.0.0_64only-latest",
        description="Imagem Docker re-droid",
    )
    gpu_mode: str = Field(default="guest", description="GPU mode: guest, host, auto")
    extra_props: str = Field(default="", description="Propriedades extras (uma por linha, formato chave=valor)")
    camera_enabled: bool = Field(default=False)
    camera_mode: str = Field(default="none", description="none | sim | webcam")
    camera_webcam_device: str = Field(default="/dev/video0")
    camera_loopback_device: str = Field(default="")
    proxy_enabled: bool = Field(default=False, description="Ativar proxy transparente HTTP/SOCKS5")
    proxy_type: str = Field(default="socks5", description="http ou socks5")
    proxy_host: str = Field(default="", description="Host/IP do proxy")
    proxy_port: int | None = Field(default=None, ge=1, le=65535, description="Porta do proxy")
    proxy_username: str = Field(default="", description="Usuário do proxy (opcional)")
    proxy_password: str = Field(default="", description="Senha do proxy (opcional)")
    cf_gapps: bool = Field(default=False, description="Cuttlefish: instalar MindTheGapps após o boot")
    cf_cpus: int = Field(default=2, ge=1, le=16)
    cf_memory_mb: int = Field(default=4096, ge=2048, le=32768)
    cf_camera_source: str = Field(default="test", description="Cuttlefish Android 12 + câmera: test ou /dev/videoN")


class ProxyConfigBody(BaseModel):
    enabled: bool = Field(default=False)
    type: str = Field(default="socks5", description="http ou socks5")
    host: str = Field(default="")
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str = Field(default="")
    password: str = Field(default="")


class CameraConfigBody(BaseModel):
    enabled: bool = Field(default=False)
    mode: str = Field(default="none", description="none | sim | webcam | image | video | stream | deeplive")
    webcam_device: str = Field(default="/dev/video0")
    loopback_device: str = Field(default="")
    media_path: str = Field(default="", description="Arquivo em camera/ da instância ou caminho absoluto")
    stream_url: str = Field(default="", description="URL http(s)/rtsp/rtmp para ffmpeg")
    webcam_fallback_image: bool = Field(default=True, description="Se webcam falhar, usa imagem padrão/upload")
    deeplive_root: str = Field(default="", description="Caminho do clone Deep-Live-Cam (ou REDROID_DEEPLIVE_ROOT)")
    deeplive_input_device: str = Field(default="/dev/video0", description="Webcam de entrada para face-swap ao vivo")
    deeplive_execution_provider: str = Field(default="", description="cpu | cuda | coreml …")
    deeplive_mouth_mask: bool = Field(default=False)


def _sanitize_name(name: str) -> str:
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$", name):
        raise HTTPException(
            status_code=400,
            detail="Nome inválido: use letras, números, ponto, hífen ou underscore.",
        )
    return name


def _data_root() -> Path:
    root = Path.home() / ".local" / "share" / "redroid-ui" / "instances"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _remove_path_via_docker(target: Path) -> None:
    """
    Remove a host directory that may contain root-owned files written by Android.
    Uses a helper container so we don't depend on sudo NOPASSWD.
    """
    target = target.resolve()
    parent = target.parent
    name = target.name
    if not name or name in {".", ".."}:
        raise RuntimeError("Caminho inválido para remoção.")
    root = _data_root().resolve()
    if root not in target.parents and target != root:
        raise RuntimeError(f"Remoção recusada fora de {root}.")
    if not target.exists():
        return
    last_err = ""
    for attempt in range(3):
        p = _run(
            _docker_cmd()
            + [
                "run",
                "--rm",
                "--privileged",
                "-v",
                f"{parent}:/instances",
                "alpine:3.20",
                "sh",
                "-c",
                f"rm -rf /instances/{name}",
            ],
            timeout=180,
        )
        if not target.exists():
            return
        last_err = (p.stderr or p.stdout or "").strip() or f"rc={p.returncode}"
        time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(last_err or "Falha ao apagar dados via Docker.")


def _docker_rm_force(name: str) -> None:
    p = _run(_docker_cmd() + ["rm", "-f", name], timeout=60)
    if p.returncode == 0:
        return
    err = (p.stderr or p.stdout or "").strip()
    low = err.lower()
    if "no such container" in low or "no such object" in low:
        return
    raise HTTPException(status_code=400, detail=err or f"Falha ao remover contêiner {name}.")


def _setup_container_network(name: str) -> None:
    """
    Redroid often boots without a default route/DNS inside the netns.
    Inject gateway + public DNS so apps can reach the internet.
    """
    try:
        ins = _inspect_container(name)
    except Exception:
        return
    gateway = ""
    networks = (ins.get("NetworkSettings", {}) or {}).get("Networks", {}) or {}
    for net in networks.values():
        gateway = str((net or {}).get("Gateway", "")).strip()
        if gateway:
            break
    if not gateway:
        gateway = str((ins.get("NetworkSettings", {}) or {}).get("Gateway", "")).strip()
    if not gateway:
        gateway = "172.17.0.1"

    script = (
        "mount -o remount,rw /etc 2>/dev/null || true; "
        f"ip route replace default via {gateway} 2>/dev/null || "
        f"ip route add default via {gateway} 2>/dev/null || true; "
        "mkdir -p /etc; "
        "printf 'nameserver 8.8.8.8\\nnameserver 1.1.1.1\\n' > /etc/resolv.conf; "
        "setprop net.dns1 8.8.8.8 2>/dev/null || true; "
        "setprop net.dns2 1.1.1.1 2>/dev/null || true; "
        "ndc resolver setnetdns eth0 '' 8.8.8.8 1.1.1.1 2>/dev/null || true"
    )
    _run(_docker_cmd() + ["exec", name, "sh", "-c", script], timeout=20)
    _hide_rw_root_mounts(name)


def _hide_rw_root_mounts(name: str) -> None:
    """RootBeer checkForRWPaths treats a rw /etc (Docker overlay) as root."""
    script = (
        "mount -o remount,ro /etc 2>/dev/null || true; "
        "mount -o remount,ro /sbin 2>/dev/null || true"
    )
    _run(_docker_cmd() + ["exec", name, "sh", "-c", script], timeout=15)


def _binder_ready() -> bool:
    # Follow symlinks; a dangling /dev/binder -> binderfs/binder must count as not ready.
    for candidate in (Path("/dev/binderfs/binder"), Path("/dev/binder")):
        try:
            if candidate.exists() and not candidate.is_symlink():
                return True
            if candidate.exists() and candidate.is_symlink() and candidate.resolve().exists():
                return True
        except Exception:
            continue
    return False


def _ensure_host_binder() -> None:
    """
    Redroid requires Android binder IPC on the host.
    Load binder_linux, mount binderfs, and allocate binder/hwbinder/vndbinder
    nodes (Anbox may already own binderfs with only anbox-* devices).
    """
    if _binder_ready():
        return
    dc = _docker_cmd()
    # Runs in the host mount namespace. Allocates missing binder nodes via BINDER_CTL_ADD.
    script = r"""
set -e
modprobe binder_linux devices=binder,hwbinder,vndbinder 2>/dev/null || modprobe binder_linux || true
mkdir -p /dev/binderfs
if ! mountpoint -q /dev/binderfs; then
  mount -t binder binder /dev/binderfs
fi

add_nodes() {
  if [ -e /dev/binderfs/binder ] && [ -e /dev/binderfs/hwbinder ] && [ -e /dev/binderfs/vndbinder ]; then
    return 0
  fi
  if [ ! -e /dev/binderfs/binder-control ]; then
    echo "binder-control ausente" >&2
    return 1
  fi
  cat > /tmp/redroid-binder-add.c <<'EOF'
#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <errno.h>
#include <linux/android/binderfs.h>
int main(void) {
  const char *names[] = {"binder", "hwbinder", "vndbinder"};
  int fd = open("/dev/binderfs/binder-control", O_RDONLY);
  if (fd < 0) { perror("open binder-control"); return 1; }
  for (int i = 0; i < 3; i++) {
    struct binderfs_device device;
    memset(&device, 0, sizeof(device));
    snprintf((char *)device.name, sizeof(device.name), "%s", names[i]);
    if (ioctl(fd, BINDER_CTL_ADD, &device) < 0) {
      if (errno != EEXIST) {
        fprintf(stderr, "BINDER_CTL_ADD %s: %s\n", names[i], strerror(errno));
      }
    }
  }
  close(fd);
  return 0;
}
EOF
  if ! command -v cc >/dev/null 2>&1; then
    apk add --no-cache gcc musl-dev linux-headers >/dev/null
  fi
  cc -O2 -o /tmp/redroid-binder-add /tmp/redroid-binder-add.c
  /tmp/redroid-binder-add
}

add_nodes

if [ -e /dev/binderfs/binder ]; then
  ln -sfn /dev/binderfs/binder /dev/binder
elif [ -e /dev/binderfs/anbox-binder ]; then
  ln -sfn /dev/binderfs/anbox-binder /dev/binder
fi
if [ -e /dev/binderfs/hwbinder ]; then
  ln -sfn /dev/binderfs/hwbinder /dev/hwbinder
elif [ -e /dev/binderfs/anbox-hwbinder ]; then
  ln -sfn /dev/binderfs/anbox-hwbinder /dev/hwbinder
fi
if [ -e /dev/binderfs/vndbinder ]; then
  ln -sfn /dev/binderfs/vndbinder /dev/vndbinder
elif [ -e /dev/binderfs/anbox-vndbinder ]; then
  ln -sfn /dev/binderfs/anbox-vndbinder /dev/vndbinder
fi

# Ready if classic binder node exists, or Anbox nodes are usable via /dev/binder.
test -e /dev/binderfs/binder -o -e /dev/binder
"""
    p = _run(
        dc
        + [
            "run",
            "--rm",
            "--privileged",
            "--pid=host",
            "alpine:3.20",
            "nsenter",
            "-t",
            "1",
            "-m",
            "-u",
            "-i",
            "-n",
            "sh",
            "-c",
            script,
        ],
        timeout=120,
    )
    if not _binder_ready():
        detail = (p.stderr or p.stdout or "").strip()
        raise HTTPException(
            status_code=500,
            detail=(
                "Kernel binder indisponível (/dev/binder). "
                "Redroid precisa do módulo binder_linux com nós binder/hwbinder/vndbinder. "
                f"Detalhe: {detail or 'falha ao montar/alocar binderfs'}"
            ),
        )


# ---------------------------------------------------------------------------
# Transparent HTTP/SOCKS5 proxy sidecar
# ---------------------------------------------------------------------------

PROXY_IMAGE = "redroid-ui/proxy-sidecar"
PROXY_DOCKERFILE = """\
FROM alpine:3.20
RUN apk add --no-cache redsocks iptables iproute2
"""

PROXY_STARTUP_SCRIPT = r"""
#!/bin/sh
set -e
TYPE="${PROXY_TYPE:-socks5}"
HOST="${PROXY_HOST:?proxy host required}"
PORT="${PROXY_PORT:?proxy port required}"
USER="${PROXY_USER:-}"
PASS="${PROXY_PASS:-}"

if [ "$TYPE" = "http" ]; then
  RTYPE="http-connect"
else
  RTYPE="socks5"
fi

mkdir -p /etc
{
  echo 'base {'
  echo '  log_debug = off;'
  echo '  log_info = on;'
  echo '  log = stderr;'
  echo '  daemon = off;'
  echo '  redirector = iptables;'
  echo '}'
  echo 'redsocks {'
  echo '  local_ip = 0.0.0.0;'
  echo '  local_port = 12345;'
  echo "  ip = ${HOST};"
  echo "  port = ${PORT};"
  echo "  type = ${RTYPE};"
  if [ -n "$USER" ]; then
    echo "  login = \"${USER}\";"
    echo "  password = \"${PASS}\";"
  fi
  echo '}'
} > /etc/redsocks.conf

redsocks -c /etc/redsocks.conf &
RSPID=$!
sleep 1
kill -0 "$RSPID" 2>/dev/null || { echo "redsocks failed to start" >&2; exit 1; }

iptables -P FORWARD ACCEPT 2>/dev/null || true
iptables -t nat -F PREROUTING 2>/dev/null || true

iptables -t nat -A PREROUTING -d 127.0.0.0/8 -j RETURN
iptables -t nat -A PREROUTING -d 10.0.0.0/8 -j RETURN
iptables -t nat -A PREROUTING -d 172.16.0.0/12 -j RETURN
iptables -t nat -A PREROUTING -d 192.168.0.0/16 -j RETURN

for cidr in $(ip -o -4 route show scope link | awk '{print $1}' | sort -u); do
  [ -n "$cidr" ] || continue
  iptables -t nat -A PREROUTING -d "$cidr" -j RETURN 2>/dev/null || true
done

MYIP=$(ip -4 addr show eth0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)
if ! iptables -t nat -A PREROUTING -p tcp -j REDIRECT --to-ports 12345 2>/dev/null; then
  iptables -t nat -A PREROUTING -p tcp -j DNAT --to-destination ${MYIP:-127.0.0.1}:12345
fi

# DNS UDP is not SOCKS/HTTP-capable; forward to public resolver via sidecar.
iptables -t nat -A PREROUTING -p udp --dport 53 -j DNAT --to-destination 8.8.8.8:53 2>/dev/null || true
iptables -t nat -A POSTROUTING -j MASQUERADE 2>/dev/null || true

echo "proxy gateway active type=${TYPE} ${HOST}:${PORT}"
wait "$RSPID"
"""


def _proxy_root() -> Path:
    root = Path.home() / ".local" / "share" / "redroid-ui" / "proxy"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _proxy_config_path(name: str) -> Path:
    return _proxy_root() / f"{name}.json"


def _proxy_sidecar_name(name: str) -> str:
    return f"redroid-proxy-{name}"


def _default_proxy_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "type": "socks5",
        "host": "",
        "port": None,
        "username": "",
        "password": "",
    }


def _normalize_proxy_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = _default_proxy_config()
    if not raw:
        return cfg
    cfg["enabled"] = bool(raw.get("enabled", False))
    ptype = str(raw.get("type", "socks5")).strip().lower()
    cfg["type"] = ptype if ptype in {"http", "socks5"} else "socks5"
    host = str(raw.get("host", "") or "").strip()
    username = str(raw.get("username", "") or "").strip()
    password = str(raw.get("password", "") or "")
    port = raw.get("port")

    # Accept accidental pastes like:
    #   socks5://user:pass@host:11000
    #   socks5://user@host:11000
    #   user field = "socks5://geonode_xxx"
    pasted = host
    if "://" in username and not host:
        pasted = username
        username = ""
    if "://" in pasted:
        try:
            from urllib.parse import urlparse

            # urlparse needs netloc; ensure scheme present
            u = urlparse(pasted if "://" in pasted else f"socks5://{pasted}")
            if u.scheme in {"http", "https", "socks5", "socks5h", "socks4"}:
                if u.scheme.startswith("socks"):
                    cfg["type"] = "socks5"
                elif u.scheme in {"http", "https"}:
                    cfg["type"] = "http"
            if u.hostname:
                host = u.hostname
            if u.port:
                port = u.port
            if u.username:
                username = u.username
            if u.password:
                password = u.password
        except Exception:
            pass
    # Strip scheme mistakenly typed into username only.
    for prefix in ("socks5h://", "socks5://", "socks4://", "http://", "https://"):
        if username.lower().startswith(prefix):
            username = username[len(prefix) :]

    cfg["host"] = host
    cfg["port"] = int(port) if port not in (None, "") else None
    cfg["username"] = username
    cfg["password"] = password
    return cfg


def _validate_proxy_config(cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = _normalize_proxy_config(cfg)
    if not cfg["enabled"]:
        return cfg
    if cfg["type"] not in {"http", "socks5"}:
        raise HTTPException(status_code=400, detail="proxy_type inválido. Use http ou socks5.")
    if not cfg["host"]:
        raise HTTPException(status_code=400, detail="proxy_host é obrigatório quando o proxy está ativo.")
    if not re.match(r"^[A-Za-z0-9._:-]+$", cfg["host"]):
        raise HTTPException(status_code=400, detail="proxy_host inválido.")
    if not cfg["port"] or not (1 <= int(cfg["port"]) <= 65535):
        raise HTTPException(status_code=400, detail="proxy_port é obrigatório (1-65535).")
    # Prevent breaking redsocks.conf quoting.
    for key in ("username", "password"):
        if '"' in cfg[key] or "\n" in cfg[key] or "\\" in cfg[key]:
            raise HTTPException(status_code=400, detail=f"Caracteres inválidos em proxy_{key}.")
    return cfg


def _load_proxy_config(name: str) -> dict[str, Any]:
    path = _proxy_config_path(name)
    if not path.exists():
        return _default_proxy_config()
    try:
        return _normalize_proxy_config(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return _default_proxy_config()


def _write_proxy_config(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = _validate_proxy_config(cfg)
    path = _proxy_config_path(name)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def _delete_proxy_config(name: str) -> None:
    path = _proxy_config_path(name)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _public_proxy_summary(name: str) -> dict[str, Any]:
    cfg = _load_proxy_config(name)
    return {
        "proxy_enabled": bool(cfg.get("enabled")),
        "proxy_type": cfg.get("type") or "socks5",
        "proxy_host": cfg.get("host") or "",
        "proxy_port": cfg.get("port"),
    }


def _ensure_proxy_image() -> None:
    dc = _docker_cmd()
    check = _run(dc + ["image", "inspect", PROXY_IMAGE], timeout=30)
    if check.returncode == 0:
        return
    with tempfile.TemporaryDirectory(prefix="redroid-proxy-img-") as tmp:
        df = Path(tmp) / "Dockerfile"
        df.write_text(PROXY_DOCKERFILE, encoding="utf-8")
        p = _run(dc + ["build", "-t", PROXY_IMAGE, tmp], timeout=300)
        if p.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Falha ao construir imagem de proxy: {(p.stderr or p.stdout or '').strip()}",
            )


def _sidecar_ip(name: str) -> str:
    sidecar = _proxy_sidecar_name(name)
    try:
        ins = _inspect_container(sidecar)
    except HTTPException:
        return ""
    return _container_ip(ins) or ""


def _android_ip(name: str) -> str:
    try:
        ins = _inspect_container(name)
    except HTTPException:
        return ""
    return _container_ip(ins) or ""


def _stop_proxy_sidecar(name: str) -> None:
    _run(_docker_cmd() + ["rm", "-f", _proxy_sidecar_name(name)], timeout=60)


def _start_proxy_sidecar(name: str, cfg: dict[str, Any]) -> None:
    cfg = _validate_proxy_config(cfg)
    if not cfg["enabled"]:
        _stop_proxy_sidecar(name)
        return
    _ensure_proxy_image()
    dc = _docker_cmd()
    sidecar = _proxy_sidecar_name(name)
    _run(dc + ["rm", "-f", sidecar], timeout=30)
    cmd = dc + [
        "run",
        "-d",
        "--name",
        sidecar,
        "--label",
        "redroid-ui.proxy=1",
        "--label",
        f"redroid-ui.parent={name}",
        "--cap-add=NET_ADMIN",
        "--cap-add=NET_RAW",
        "--sysctl",
        "net.ipv4.ip_forward=1",
        "-e",
        f"PROXY_TYPE={cfg['type']}",
        "-e",
        f"PROXY_HOST={cfg['host']}",
        "-e",
        f"PROXY_PORT={cfg['port']}",
        "-e",
        f"PROXY_USER={cfg.get('username') or ''}",
        "-e",
        f"PROXY_PASS={cfg.get('password') or ''}",
        PROXY_IMAGE,
        "sh",
        "-c",
        PROXY_STARTUP_SCRIPT,
    ]
    p = _run(cmd, timeout=120)
    if p.returncode != 0:
        raise HTTPException(
            status_code=400,
            detail=f"Falha ao criar sidecar de proxy: {(p.stderr or p.stdout or '').strip()}",
        )
    # Wait briefly for eth0 IP.
    for _ in range(10):
        if _sidecar_ip(name):
            break
        time.sleep(0.4)


def _cleanup_proxy_gateway(name: str) -> None:
    android_ip = _android_ip(name)
    if not android_ip:
        # Container may already be gone; try to drop common table by scanning is hard.
        return
    table_id = 1000 + int(android_ip.split(".")[-1])
    host_script = (
        f"ip rule del from {android_ip} table {table_id} 2>/dev/null || true; "
        f"ip rule del from {android_ip} to 10.0.0.0/8 priority 99 table main 2>/dev/null || true; "
        f"ip rule del from {android_ip} to 172.16.0.0/12 priority 99 table main 2>/dev/null || true; "
        f"ip rule del from {android_ip} to 192.168.0.0/16 priority 99 table main 2>/dev/null || true; "
        f"ip route flush table {table_id} 2>/dev/null || true; "
        f"echo cleaned {android_ip} table {table_id}"
    )
    _run(
        _docker_cmd()
        + [
            "run",
            "--rm",
            "--network",
            "host",
            "--cap-add",
            "NET_ADMIN",
            "alpine:3.20",
            "sh",
            "-c",
            host_script,
        ],
        timeout=30,
    )


def _setup_proxy_gateway(name: str) -> None:
    sidecar_ip = _sidecar_ip(name)
    android_ip = _android_ip(name)
    if not sidecar_ip or not android_ip:
        raise HTTPException(
            status_code=400,
            detail=f"Não foi possível configurar gateway de proxy (android={android_ip or '?'}, sidecar={sidecar_ip or '?'}).",
        )
    table_id = 1000 + int(android_ip.split(".")[-1])
    host_script = (
        f"set -e; "
        f"ip rule del from {android_ip} table {table_id} 2>/dev/null || true; "
        f"ip route flush table {table_id} 2>/dev/null || true; "
        f"ip rule add from {android_ip} to 10.0.0.0/8 priority 99 table main 2>/dev/null || true; "
        f"ip rule add from {android_ip} to 172.16.0.0/12 priority 99 table main 2>/dev/null || true; "
        f"ip rule add from {android_ip} to 192.168.0.0/16 priority 99 table main 2>/dev/null || true; "
        f"ip route add default via {sidecar_ip} table {table_id}; "
        f"ip rule add from {android_ip} table {table_id} priority 100; "
        f"echo 'host gateway: {android_ip} → {sidecar_ip} (table {table_id})'"
    )
    p = _run(
        _docker_cmd()
        + [
            "run",
            "--rm",
            "--network",
            "host",
            "--cap-add",
            "NET_ADMIN",
            "alpine:3.20",
            "sh",
            "-c",
            host_script,
        ],
        timeout=30,
    )
    if p.returncode != 0:
        raise HTTPException(
            status_code=400,
            detail=f"Falha no roteamento host→proxy: {(p.stderr or p.stdout or '').strip()}",
        )


def _apply_proxy_runtime(name: str, cfg: dict[str, Any] | None = None) -> None:
    cfg = _validate_proxy_config(cfg if cfg is not None else _load_proxy_config(name))
    _cleanup_proxy_gateway(name)
    if not cfg["enabled"]:
        _stop_proxy_sidecar(name)
        return
    _start_proxy_sidecar(name, cfg)
    _setup_proxy_gateway(name)


def _teardown_proxy_runtime(name: str) -> None:
    try:
        _cleanup_proxy_gateway(name)
    except Exception:
        pass
    _stop_proxy_sidecar(name)


def _allocate_host_port(preferred: int | None = None) -> int:
    used: set[int] = set()
    p = _run(_docker_cmd() + ["ps", "-a", "--format", "{{.Ports}}"])
    for line in (p.stdout or "").splitlines():
        for chunk in line.split(","):
            chunk = chunk.strip()
            if "->5555" in chunk and ":" in chunk:
                hp = chunk.rsplit("->", 1)[0].rsplit(":", 1)[-1]
                if hp.isdigit():
                    used.add(int(hp))
    if preferred is not None and preferred not in used:
        return preferred
    for port in range(5555, 5656):
        if port not in used:
            return port
    raise HTTPException(status_code=500, detail="Nenhuma porta ADB disponível (5555-5655).")


@app.get("/health")
def health() -> dict[str, Any]:
    _require_docker_cli()
    return {"status": "ok", "docker_via_sudo": _docker_use_sudo()}


@app.get("/instances")
def list_instances() -> dict[str, list[dict[str, Any]]]:
    import cuttlefish_host

    _require_docker_cli()
    out: list[dict[str, Any]] = []
    for cid in _docker_ids():
        try:
            ins = _inspect_container(cid)
            out.append(_container_to_instance(ins))
        except HTTPException:
            continue
    out.extend(cuttlefish_host.list_instances())
    out.sort(key=lambda x: x.get("name") or "")
    return {"instances": out}


@app.get("/cuttlefish/status")
def cuttlefish_status() -> dict[str, Any]:
    import cuttlefish_host

    return cuttlefish_host.status()


def _cf(name: str) -> bool:
    import cuttlefish_host

    return cuttlefish_host.exists(name)


def _cf_call(fn, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/instances/{name}/gapps")
def install_instance_gapps(name: str) -> dict[str, Any]:
    import cuttlefish_host

    name = _sanitize_name(name)
    if not _cf(name):
        raise HTTPException(
            status_code=400,
            detail="Instalação de GApps em execução só existe para Cuttlefish; no reDroid use o Build de imagem.",
        )
    return {"instance": _cf_call(cuttlefish_host.add_gapps, name)}


class CuttlefishCameraBody(BaseModel):
    source: str = Field(default="test", description="test (padrão de teste) ou /dev/videoN")


@app.put("/instances/{name}/cf-camera")
def set_cuttlefish_camera(name: str, body: CuttlefishCameraBody) -> dict[str, Any]:
    import cuttlefish_host

    name = _sanitize_name(name)
    if not _cf(name):
        raise HTTPException(status_code=400, detail="Instância Cuttlefish não encontrada.")
    return {"instance": _cf_call(cuttlefish_host.set_camera_source, name, (body.source or "test").strip())}


class PhoneNumberBody(BaseModel):
    number: str = Field(..., min_length=3, max_length=24, description="Número E.164, ex. +5511987654321")


def _set_device_number(name: str, number: str) -> dict[str, Any]:
    """Cuttlefish: grava no chip simulado (número real p/ o Android). reDroid: só a propriedade soft."""
    import cuttlefish_host

    digits = re.sub(r"\D", "", number)
    if not 3 <= len(digits) <= 20:
        raise HTTPException(status_code=400, detail=f"Número inválido: {number}")
    if _cf(name):
        return {"mode": "sim", **_cf_call(cuttlefish_host.set_phone_number, name, digits)}
    _require_bin("adb")
    serial = _resolve_serial(name)
    ok, msg = _adb_setprop(serial, "persist.sys.phone_number", f"+{digits}")
    if not ok:
        raise HTTPException(status_code=400, detail=f"persist.sys.phone_number: {msg}")
    return {
        "mode": "soft",
        "phone_number": f"+{digits}",
        "applied": True,
        "note": "reDroid não tem modem: só a propriedade persist.sys.phone_number foi alterada.",
    }


@app.put("/instances/{name}/phone-number")
def put_instance_phone_number(name: str, body: PhoneNumberBody) -> dict[str, Any]:
    return _set_device_number(_sanitize_name(name), body.number)


class IncomingCallBody(BaseModel):
    number: str = Field(..., min_length=3, max_length=24, description="Número que liga, ex. +447700904831")
    ring_seconds: float = Field(default=4.0, ge=1, le=30)


FLASH_CALL_DEFAULT_PREFIX = "4477009"


def _flash_caller(code: str, prefix: str = "") -> str:
    """HeroSMS pode devolver o número inteiro que ligou ou só os últimos dígitos (o código)."""
    digits = re.sub(r"\D", "", code)
    if not digits:
        raise HTTPException(status_code=400, detail=f"Código de flash call sem dígitos: {code!r}")
    if len(digits) >= 8:
        return f"+{digits}"
    return f"+{re.sub(r'[^0-9]', '', prefix) or FLASH_CALL_DEFAULT_PREFIX}{digits}"


@app.post("/instances/{name}/call")
def post_instance_call(name: str, body: IncomingCallBody) -> dict[str, Any]:
    import cuttlefish_host

    name = _sanitize_name(name)
    if not _cf(name):
        raise HTTPException(status_code=400, detail="Chamada simulada só existe no Cuttlefish (modem real).")
    return _cf_call(cuttlefish_host.incoming_call, name, body.number, body.ring_seconds)


def _camera_config_from_create_body(body: CreateInstanceBody) -> dict[str, Any]:
    mode = (body.camera_mode or "none").strip().lower()
    import camera_host

    enabled = bool(body.camera_enabled) and mode in camera_host.FEED_MODES
    return {
        "enabled": enabled,
        "mode": mode if enabled else "none",
        "webcam_device": body.camera_webcam_device or "/dev/video0",
        "loopback_device": body.camera_loopback_device or "",
        "media_path": "",
        "stream_url": "",
        "webcam_fallback_image": True,
    }


def _native_bridge_props(image: str) -> list[str]:
    """As imagens compiladas daqui apontam ro.dalvik.vm.native.bridge para libnb.so, que não
    existe nelas; o redroid-script só copia a lib de tradução, então é preciso apontar para ela."""
    tag = image.rsplit(":", 1)[-1].lower()
    if "houdini" in tag:
        lib = "libhoudini.so"
    elif "ndk" in tag:
        lib = "libndk_translation.so"
    else:
        return []
    return [
        f"ro.dalvik.vm.native.bridge={lib}",
        "ro.dalvik.vm.isa.arm=x86",
        "ro.dalvik.vm.isa.arm64=x86_64",
        "ro.enable.native.bridge.exec=1",
        "ro.enable.native.bridge.exec64=1",
    ]


def _build_docker_run_cmd(
    name: str,
    data_dir: Path,
    host_port: int,
    image: str,
    gpu_mode: str,
    extra_props: str,
    camera_cfg: dict[str, Any],
) -> list[str]:
    import camera_host

    cmd = _docker_cmd() + [
        "run",
        "-d",
        "--name",
        name,
        "--label",
        "redroid-ui.managed=1",
        "--privileged",
        "--dns",
        "8.8.8.8",
        "--dns",
        "1.1.1.1",
        "-v",
        f"{data_dir}:/data",
        "-p",
        f"{host_port}:5555",
    ]
    cmd.extend(camera_host.docker_hal_volume_flags())
    cmd.extend(camera_host.docker_device_flags(camera_cfg, name))
    # guest = SwiftShader, que perde o contexto WebGL (página em branco).
    # host usa a GPU do PC; sem /dev/dri o Android não enxerga a placa.
    if gpu_mode == "host" and os.path.isdir("/dev/dri"):
        cmd.extend(["-v", "/dev/dri:/dev/dri"])
    cmd.append(image)
    cmd.append(f"androidboot.redroid_gpu_mode={gpu_mode}")
    cmd.extend(camera_host.redroid_camera_boot_args(camera_cfg))
    cmd.extend(_native_bridge_props(image))
    for line in extra_props.strip().splitlines():
        prop = line.strip()
        if prop and "=" in prop and re.match(r"^[a-zA-Z0-9_.]+=[^\s]*$", prop):
            cmd.append(prop)
    return cmd


def _bootstrap_camera_hal(name: str) -> None:
    import camera_hal

    if not camera_hal.hal_so_present() and not camera_hal.provider_present():
        return
    cfg = _load_camera_config(name)
    if not cfg.get("enabled"):
        return
    try:
        serial = _resolve_serial(name)
        _wait_for_android_boot(serial, name, attempts=60)
        cfg = _sync_camera_loopback_from_container(name, cfg)
        camera_hal.bootstrap_runtime(serial, v4l2_device=str(cfg.get("loopback_device") or ""))
    except HTTPException:
        pass


def _bootstrap_running_instance(name: str, proxy_cfg: dict[str, Any]) -> None:
    time.sleep(2)
    _setup_container_network(name)
    if proxy_cfg.get("enabled"):
        _setup_proxy_gateway(name)
    _setup_virtual_touch(name)
    try:
        _sync_adb_keys_to_container(name)
    except HTTPException:
        pass
    _bootstrap_camera_hal(name)


def _start_camera_pipeline(name: str, camera_cfg: dict[str, Any]) -> dict[str, Any]:
    import camera_host

    camera_cfg = _write_camera_config(name, camera_cfg)
    if not camera_cfg.get("enabled"):
        camera_host.stop_feed(name, camera_cfg)
        return {"feed": {"running": False}}
    try:
        feed = camera_host.start_feed(name, camera_cfg)
        return {"feed": feed}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _sync_camera_loopback_from_container(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Alinha feed com --device do contêiner (ex.: instâncias criadas antes do loopback estável)."""
    import camera_host

    if not cfg.get("enabled"):
        return cfg
    try:
        ins = _inspect_container(name)
    except HTTPException:
        return cfg
    for dev in ins.get("HostConfig", {}).get("Devices") or []:
        host = (dev.get("PathOnHost") or "").strip()
        if host.startswith("/dev/video") and camera_host._is_loopback_device(host):
            pinned = (cfg.get("loopback_device") or "").strip()
            if pinned and pinned != host:
                cfg = {**cfg, "loopback_device": host}
                _write_camera_config(name, cfg)
            elif not pinned:
                cfg = {**cfg, "loopback_device": host}
                _write_camera_config(name, cfg)
            break
    return cfg


def _recreate_instance_container(name: str) -> dict[str, Any]:
    """Recria o contêiner preservando /data (necessário para alterar --device de câmera)."""
    import camera_host

    _require_docker_cli()
    name = _sanitize_name(name)
    meta = _load_instance_meta(name)
    if not meta:
        try:
            ins = _inspect_container(name)
        except HTTPException as exc:
            raise HTTPException(
                status_code=400,
                detail="Metadados da instância ausentes; recrie manualmente.",
            ) from exc
        meta = {
            "image": ins.get("Config", {}).get("Image", ""),
            "gpu_mode": "guest",
            "extra_props": "",
            "host_port": _host_port_for_adb(ins),
        }
    proxy_cfg = _load_proxy_config(name)
    camera_cfg = _load_camera_config(name)
    data_dir = _data_root() / name
    host_port = int(meta.get("host_port") or _allocate_host_port(None))
    was_running = False
    try:
        st = _inspect_container(name).get("State", {}) or {}
        was_running = bool(st.get("Running"))
    except HTTPException:
        pass

    camera_host.stop_feed(name)
    _cleanup_proxy_gateway(name)
    _stop_proxy_sidecar(name)
    _run(_docker_cmd() + ["stop", name], timeout=120)
    _docker_rm_force(name)
    time.sleep(0.5)

    if proxy_cfg.get("enabled"):
        _start_proxy_sidecar(name, proxy_cfg)

    cmd = _build_docker_run_cmd(
        name,
        data_dir,
        host_port,
        str(meta.get("image") or "redroid/redroid:12.0.0_64only-latest"),
        str(meta.get("gpu_mode") or "guest"),
        str(meta.get("extra_props") or ""),
        camera_cfg,
    )
    p = _run(cmd, timeout=300)
    if p.returncode != 0:
        _teardown_proxy_runtime(name)
        raise HTTPException(status_code=400, detail=(p.stderr or p.stdout or "").strip())
    cid = p.stdout.strip()
    _write_instance_meta(
        name,
        {
            **meta,
            "host_port": host_port,
        },
    )
    _start_camera_pipeline(name, camera_cfg)
    if was_running:
        _bootstrap_running_instance(name, proxy_cfg)
    ins = _inspect_container(cid)
    return {"instance": _container_to_instance(ins)}


@app.post("/instances")
def create_instance(body: CreateInstanceBody) -> dict[str, Any]:
    import cuttlefish_host

    if cuttlefish_host.is_image(body.image):
        name = _sanitize_name(body.name.strip())
        if _run(_docker_cmd() + ["inspect", "--type", "container", name]).returncode == 0:
            raise HTTPException(status_code=400, detail=f"Já existe um contêiner chamado {name}.")
        return {
            "instance": _cf_call(
                cuttlefish_host.create,
                name,
                gapps=body.cf_gapps,
                cpus=body.cf_cpus,
                memory_mb=body.cf_memory_mb,
                gpu_mode=body.gpu_mode if body.gpu_mode in ("auto", "guest_swiftshader", "drm_virgl", "gfxstream") else "auto",
                image=body.image,
                camera_source=(body.cf_camera_source or "test").strip(),
            )
        }
    _require_docker_cli()
    _ensure_host_binder()
    name = _sanitize_name(body.name.strip())
    if _cf(name):
        raise HTTPException(status_code=400, detail=f"Já existe uma instância Cuttlefish chamada {name}.")
    data_dir = _data_root() / name
    data_dir.mkdir(parents=True, exist_ok=True)
    host_port = _allocate_host_port(body.host_port)

    proxy_cfg = _validate_proxy_config(
        {
            "enabled": body.proxy_enabled,
            "type": body.proxy_type,
            "host": body.proxy_host,
            "port": body.proxy_port,
            "username": body.proxy_username,
            "password": body.proxy_password,
        }
    )
    _write_proxy_config(name, proxy_cfg)
    camera_cfg = _camera_config_from_create_body(body)

    if proxy_cfg["enabled"]:
        try:
            _start_proxy_sidecar(name, proxy_cfg)
        except Exception:
            _delete_proxy_config(name)
            raise

    try:
        _start_camera_pipeline(name, camera_cfg)
    except HTTPException:
        _teardown_proxy_runtime(name)
        raise

    cmd = _build_docker_run_cmd(
        name,
        data_dir,
        host_port,
        body.image,
        body.gpu_mode,
        body.extra_props,
        camera_cfg,
    )
    p = _run(cmd, timeout=300)
    if p.returncode != 0:
        import camera_host

        camera_host.stop_feed(name)
        _teardown_proxy_runtime(name)
        err = (p.stderr or p.stdout or "").strip()
        raise HTTPException(status_code=400, detail=err or "Falha ao criar contêiner.")
    cid = p.stdout.strip()
    _write_instance_meta(
        name,
        {
            "image": body.image,
            "gpu_mode": body.gpu_mode,
            "extra_props": body.extra_props,
            "host_port": host_port,
        },
    )
    try:
        _bootstrap_running_instance(name, proxy_cfg)
    except Exception as exc:
        _teardown_proxy_runtime(name)
        _docker_rm_force(name)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ins = _inspect_container(cid)
    return {"instance": _container_to_instance(ins)}


TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"


def _setup_virtual_touch(name: str) -> None:
    """Copia e executa o binário create_touch dentro do contêiner e cria o device node."""
    binary = TOOLS_DIR / "create_touch"
    if not binary.exists():
        return

    dc = _docker_cmd()
    # Copiar binário
    _run(dc + ["cp", str(binary), f"{name}:/create_touch"])
    _run(dc + ["exec", name, "chmod", "+x", "/create_touch"])

    # Matar processo anterior se existir
    _run(dc + ["exec", name, "sh", "-c", "pkill -f /create_touch 2>/dev/null || true"])
    time.sleep(0.3)

    # Executar em background
    _run(dc + ["exec", "-d", name, "/create_touch"])
    time.sleep(1)

    # Criar o device node em /dev/input/
    _run(dc + ["exec", name, "sh", "-c", """
for d in /sys/devices/virtual/input/input*/; do
  name=$(cat "$d/name" 2>/dev/null)
  if [ "$name" = "redroid-virtual-touch" ]; then
    for ev in "$d"/event*/; do
      devnums=$(cat "$ev/dev" 2>/dev/null)
      evname=$(basename "$ev")
      major=$(echo "$devnums" | cut -d: -f1)
      minor=$(echo "$devnums" | cut -d: -f2)
      [ ! -e "/dev/input/$evname" ] && mknod "/dev/input/$evname" c "$major" "$minor" && chmod 666 "/dev/input/$evname"
    done
  fi
done
"""])


@app.post("/instances/{name}/start")
def start_instance(name: str) -> dict[str, Any]:
    import cuttlefish_host

    name = _sanitize_name(name)
    if _cf(name):
        return {"instance": _cf_call(cuttlefish_host.start, name)}
    _require_docker_cli()
    _ensure_host_binder()
    cfg = _refresh_intercept_proxy(name) if _is_intercepting(name) else _load_proxy_config(name)
    if cfg.get("enabled"):
        _start_proxy_sidecar(name, cfg)
    p = _run(_docker_cmd() + ["start", name])
    if p.returncode != 0:
        if cfg.get("enabled"):
            _teardown_proxy_runtime(name)
        raise HTTPException(status_code=400, detail=(p.stderr or p.stdout).strip())
    time.sleep(2)
    _setup_container_network(name)
    if cfg.get("enabled"):
        _setup_proxy_gateway(name)
    _setup_virtual_touch(name)
    camera_cfg = _load_camera_config(name)
    if camera_cfg.get("enabled"):
        try:
            _start_camera_pipeline(name, camera_cfg)
        except HTTPException:
            pass
        _bootstrap_camera_hal(name)
    ins = _inspect_container(name)
    return {"instance": _container_to_instance(ins)}


@app.post("/instances/{name}/stop")
def stop_instance(name: str) -> dict[str, Any]:
    import cuttlefish_host

    name = _sanitize_name(name)
    if _cf(name):
        _cf_call(cuttlefish_host.stop, name)
        return {"instance": cuttlefish_host.to_instance(name)}
    _require_docker_cli()

    try:
        ins = _inspect_container(name)
        for serial in _adb_candidates(ins):
            for store in (_injectors, _cmd_shells):
                proc = store.pop(serial, None)
                if proc and proc.poll() is None:
                    proc.terminate()
    except Exception:
        pass

    # Capture Android IP before stop for gateway cleanup.
    _cleanup_proxy_gateway(name)

    import camera_host

    camera_host.stop_feed(name, _load_camera_config(name))
    _reap_zombies()

    p = _run(_docker_cmd() + ["stop", name])
    if p.returncode != 0:
        raise HTTPException(status_code=400, detail=(p.stderr or p.stdout).strip())
    _stop_proxy_sidecar(name)
    ins = _inspect_container(name)
    return {"instance": _container_to_instance(ins)}


@app.delete("/instances/{name}")
def remove_instance(name: str) -> dict[str, str]:
    import cuttlefish_host

    name = _sanitize_name(name)
    if _cf(name):
        _cf_call(cuttlefish_host.remove, name)
        return {"status": "removed", "name": name, "data_dir": ""}
    _require_docker_cli()
    _teardown_proxy_runtime(name)
    _docker_rm_force(name)
    # Give Docker a moment to release the bind-mount before deleting host files.
    time.sleep(0.5)
    data_dir = _data_root() / name
    try:
        if data_dir.exists():
            _remove_path_via_docker(data_dir)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Contêiner removido, mas falhou ao apagar dados em {data_dir}: {exc}",
        )
    _delete_proxy_config(name)
    _clear_intercept_membership(name)
    import camera_host

    camera_host.stop_feed(name, _load_camera_config(name))
    _delete_camera_config(name)
    return {"status": "removed", "name": name, "data_dir": str(data_dir)}


@app.get("/instances/{name}/proxy")
def get_instance_proxy(name: str) -> dict[str, Any]:
    _require_docker_cli()
    name = _sanitize_name(name)
    # Ensure instance exists.
    _inspect_container(name)
    cfg = _load_proxy_config(name)
    return {
        "enabled": cfg["enabled"],
        "type": cfg["type"],
        "host": cfg["host"],
        "port": cfg["port"],
        "username": cfg.get("username") or "",
        # Never return password; indicate if one is stored.
        "has_password": bool(cfg.get("password")),
    }


@app.put("/instances/{name}/proxy")
def put_instance_proxy(name: str, body: ProxyConfigBody) -> dict[str, Any]:
    _require_docker_cli()
    name = _sanitize_name(name)
    ins = _inspect_container(name)
    running = bool((ins.get("State") or {}).get("Running"))

    current = _load_proxy_config(name)
    password = body.password
    # Keep previous password when UI sends empty and one already exists.
    if body.enabled and not password and current.get("password") and body.username == (current.get("username") or ""):
        password = current.get("password") or ""

    # Manual proxy edit cancels MITM intercept for this instance.
    if _is_intercepting(name):
        _clear_intercept_membership(name)

    cfg = _write_proxy_config(
        name,
        {
            "enabled": body.enabled,
            "type": body.type,
            "host": body.host,
            "port": body.port,
            "username": body.username,
            "password": password,
        },
    )

    if running:
        _apply_proxy_runtime(name, cfg)
    elif not cfg["enabled"]:
        _teardown_proxy_runtime(name)

    return {
        "status": "ok",
        "proxy": {
            "enabled": cfg["enabled"],
            "type": cfg["type"],
            "host": cfg["host"],
            "port": cfg["port"],
            "username": cfg.get("username") or "",
            "has_password": bool(cfg.get("password")),
        },
        "instance": _container_to_instance(_inspect_container(name)),
    }


# ---------------------------------------------------------------------------
# HTTP intercept via mitmproxy (shared Docker sidecar)
# ---------------------------------------------------------------------------

MITM_IMAGE = "mitmproxy/mitmproxy:10.4.2"
MITM_CONTAINER = "redroid-ui-mitm"
MITM_LISTEN_PORT = 18080
MITM_WEB_PORT = 18081
_mitm_lock = threading.Lock()


class InterceptBody(BaseModel):
    enabled: bool = True
    install_cert: bool = True


def _mitm_dir() -> Path:
    root = Path.home() / ".local" / "share" / "redroid-ui" / "mitm"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _mitm_state_path() -> Path:
    return _mitm_dir() / "state.json"


def _load_mitm_state() -> dict[str, Any]:
    path = _mitm_state_path()
    if not path.exists():
        return {"instances": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"instances": []}
        instances = data.get("instances") or []
        if not isinstance(instances, list):
            instances = []
        return {"instances": [str(x) for x in instances]}
    except Exception:
        return {"instances": []}


def _save_mitm_state(state: dict[str, Any]) -> None:
    _mitm_state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")


def _mitm_container_ip() -> str:
    p = _run(
        _docker_cmd()
        + [
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            MITM_CONTAINER,
        ],
        timeout=15,
    )
    ip = (p.stdout or "").strip()
    if p.returncode != 0 or not ip:
        raise HTTPException(status_code=500, detail="Não foi possível obter o IP do mitmproxy.")
    return ip


def _mitm_proxy_endpoint() -> tuple[str, int]:
    """
    Address the redsocks sidecar should use.
    Prefer the mitm container IP + internal port (avoids docker hairpin via published ports).
    """
    return _mitm_container_ip(), 8080


def _mitm_proxy_host() -> str:
    try:
        return _mitm_container_ip()
    except Exception:
        return "172.17.0.1"


def _mitm_running() -> bool:
    p = _run(_docker_cmd() + ["inspect", "-f", "{{.State.Running}}", MITM_CONTAINER], timeout=15)
    return p.returncode == 0 and (p.stdout or "").strip().lower() == "true"


def _mitm_ca_path() -> Path:
    return _mitm_dir() / "mitmproxy-ca-cert.pem"


def _wait_mitm_ca(timeout: float = 20.0) -> Path:
    deadline = time.time() + timeout
    ca = _mitm_ca_path()
    while time.time() < deadline:
        if ca.exists() and ca.stat().st_size > 0:
            return ca
        time.sleep(0.4)
    raise HTTPException(
        status_code=500,
        detail="mitmproxy iniciou, mas o certificado CA ainda não foi gerado.",
    )


def _start_mitm_container(*, force_recreate: bool = False) -> None:
    dc = _docker_cmd()
    # Ensure image exists (pull on demand).
    insp = _run(dc + ["image", "inspect", MITM_IMAGE], timeout=30)
    if insp.returncode != 0:
        pull = _run(dc + ["pull", MITM_IMAGE], timeout=300)
        if pull.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Falha ao baixar imagem mitmproxy: {(pull.stderr or pull.stdout or '').strip()}",
            )

    if _mitm_running() and not force_recreate:
        # Recreate if an older HTTP-proxy mode container is still running,
        # or if mitmweb is alive but its proxy listener died.
        args = _run(dc + ["inspect", "-f", "{{json .Args}}", MITM_CONTAINER], timeout=15)
        if "socks5" in (args.stdout or "") and _mitm_listener_ok():
            return

    _run(dc + ["rm", "-f", MITM_CONTAINER], timeout=30)
    mitm_dir = _mitm_dir()
    # SOCKS5 mode works reliably with transparent redsocks; plain HTTP CONNECT did not.
    cmd = dc + [
        "run",
        "-d",
        "--name",
        MITM_CONTAINER,
        "--label",
        "redroid-ui.mitm=1",
        "--restart",
        "unless-stopped",
        "-p",
        f"{MITM_LISTEN_PORT}:8080",
        "-p",
        f"{MITM_WEB_PORT}:8081",
        "-v",
        f"{mitm_dir}:/home/mitmproxy/.mitmproxy",
        MITM_IMAGE,
        "mitmweb",
        "--mode",
        "socks5",
        "--web-host",
        "0.0.0.0",
        "--web-port",
        "8081",
        "--listen-host",
        "0.0.0.0",
        "--listen-port",
        "8080",
        "--set",
        "block_global=false",
        "--set",
        "web_open_browser=false",
        "--set",
        "connection_strategy=lazy",
    ]
    p = _run(cmd, timeout=120)
    if p.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Falha ao iniciar mitmproxy: {(p.stderr or p.stdout or '').strip()}",
        )
    for _ in range(60):
        if _mitm_running() and _mitm_listener_ok():
            break
        time.sleep(0.25)
    else:
        raise HTTPException(status_code=500, detail="mitmproxy iniciou, mas o proxy SOCKS5 não responde.")
    _wait_mitm_ca()


def _mitm_listener_ok() -> bool:
    """SOCKS5 handshake against the container IP (the published port may accept TCP even when mitm is dead)."""
    import socket

    try:
        host, port = _mitm_proxy_endpoint()
        with socket.create_connection((host, port), timeout=2) as s:
            s.settimeout(2)
            s.sendall(b"\x05\x01\x00")
            return s.recv(2) == b"\x05\x00"
    except Exception:
        return False


def _refresh_intercept_proxy(name: str) -> dict[str, Any]:
    """Ensure mitm is healthy and the instance proxy points at its current IP."""
    with _mitm_lock:
        _start_mitm_container()
        host, port = _mitm_proxy_endpoint()
        return _write_proxy_config(
            name,
            {
                "enabled": True,
                "type": "socks5",
                "host": host,
                "port": port,
                "username": "",
                "password": "",
            },
        )


def _stop_mitm_container_if_unused() -> None:
    state = _load_mitm_state()
    if state.get("instances"):
        return
    _run(_docker_cmd() + ["rm", "-f", MITM_CONTAINER], timeout=30)


def _intercept_backup_path(name: str) -> Path:
    return _mitm_dir() / f"proxy-backup-{name}.json"


def _is_intercepting(name: str) -> bool:
    return name in (_load_mitm_state().get("instances") or [])


def _clear_intercept_membership(name: str) -> None:
    """Drop instance from mitm state without restoring previous proxy config."""
    state = _load_mitm_state()
    before = list(state.get("instances") or [])
    state["instances"] = [x for x in before if x != name]
    if state["instances"] != before:
        _save_mitm_state(state)
    backup = _intercept_backup_path(name)
    backup.unlink(missing_ok=True)
    _stop_mitm_container_if_unused()


def _public_intercept_summary(name: str) -> dict[str, Any]:
    active = _is_intercepting(name)
    listen = ""
    if active:
        try:
            host, port = _mitm_proxy_endpoint()
            listen = f"{host}:{port}"
        except Exception:
            listen = f"{_mitm_proxy_host()}:8080"
    return {
        "intercept_enabled": active,
        "mitm_web_url": f"http://127.0.0.1:{MITM_WEB_PORT}/" if active else "",
        "mitm_listen": listen,
    }


def _enable_intercept(name: str, install_cert: bool = True) -> dict[str, Any]:
    with _mitm_lock:
        _start_mitm_container(force_recreate=False)
        current = _load_proxy_config(name)
        # Backup previous proxy config once.
        backup = _intercept_backup_path(name)
        if not backup.exists():
            backup.write_text(json.dumps(current, indent=2), encoding="utf-8")

        mitm_host, mitm_port = _mitm_proxy_endpoint()
        cfg = _write_proxy_config(
            name,
            {
                "enabled": True,
                "type": "socks5",
                "host": mitm_host,
                "port": mitm_port,
                "username": "",
                "password": "",
            },
        )

        state = _load_mitm_state()
        if name not in state["instances"]:
            state["instances"].append(name)
            _save_mitm_state(state)

        # Apply transparent proxy if instance is running.
        try:
            ins = _inspect_container(name)
            running = bool((ins.get("State") or {}).get("Running"))
        except HTTPException:
            running = False
        if running:
            _apply_proxy_runtime(name, cfg)

        cert_result: dict[str, Any] = {"attempted": False}
        if install_cert and running:
            cert_result = _install_mitm_cert(name)

        return {
            "enabled": True,
            "web_url": f"http://127.0.0.1:{MITM_WEB_PORT}/",
            "proxy_host": mitm_host,
            "proxy_port": mitm_port,
            "cert": cert_result,
        }


def _disable_intercept(name: str) -> dict[str, Any]:
    with _mitm_lock:
        state = _load_mitm_state()
        state["instances"] = [x for x in state.get("instances", []) if x != name]
        _save_mitm_state(state)

        backup = _intercept_backup_path(name)
        if backup.exists():
            try:
                prev = json.loads(backup.read_text(encoding="utf-8"))
            except Exception:
                prev = _default_proxy_config()
            backup.unlink(missing_ok=True)
            cfg = _write_proxy_config(name, prev)
        else:
            cfg = _write_proxy_config(name, _default_proxy_config())

        try:
            ins = _inspect_container(name)
            running = bool((ins.get("State") or {}).get("Running"))
        except HTTPException:
            running = False
        if running:
            _apply_proxy_runtime(name, cfg)
        else:
            _teardown_proxy_runtime(name)

        _stop_mitm_container_if_unused()
        return {"enabled": False}


def _android_cert_hash(pem_path: Path) -> str:
    p = _run(
        ["openssl", "x509", "-inform", "PEM", "-subject_hash_old", "-in", str(pem_path)],
        timeout=15,
    )
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "").strip() or "openssl subject_hash_old falhou")
    line = (p.stdout or "").strip().splitlines()[0].strip()
    if not re.match(r"^[0-9a-fA-F]+$", line):
        raise RuntimeError(f"hash de certificado inválido: {line}")
    return line


def _install_mitm_cert(name: str) -> dict[str, Any]:
    """
    Push mitmproxy CA into the instance.
    Tries system trust store (userdebug/rooted). Always also copies to /sdcard/Download.
    """
    _require_bin("adb")
    ca = _wait_mitm_ca()
    result: dict[str, Any] = {
        "attempted": True,
        "system_installed": False,
        "sdcard_path": "",
        "detail": "",
    }
    try:
        serial = _resolve_serial(name)
    except HTTPException as exc:
        result["detail"] = str(exc.detail)
        return result

    # Always leave a copy for manual install.
    remote_sd = "/sdcard/Download/mitmproxy-ca-cert.cer"
    push = _run(["adb", "-s", serial, "push", str(ca), remote_sd], timeout=30)
    if push.returncode == 0:
        result["sdcard_path"] = remote_sd
    else:
        result["detail"] = (push.stderr or push.stdout or "").strip()

    # System CA (best effort).
    try:
        cert_hash = _android_cert_hash(ca)
        remote_sys = f"/system/etc/security/cacerts/{cert_hash}.0"
        _run(["adb", "-s", serial, "root"], timeout=15)
        time.sleep(0.8)
        # reconnect after adbd restart
        try:
            serial = _resolve_serial(name)
        except Exception:
            pass
        # Prefer remounting root (redroid often has no separate /system mount).
        _run(["adb", "-s", serial, "remount"], timeout=20)
        for mount_target in ("/", "/system"):
            _run(
                ["adb", "-s", serial, "shell", "mount", "-o", "rw,remount", mount_target],
                timeout=15,
            )
        tmp_remote = f"/data/local/tmp/{cert_hash}.0"
        p1 = _run(["adb", "-s", serial, "push", str(ca), tmp_remote], timeout=30)
        if p1.returncode != 0:
            raise RuntimeError((p1.stderr or p1.stdout or "").strip() or "push CA falhou")
        # Use separate adb shell calls — chained sh -c is fragile on some adbd builds.
        for cmd in (
            ["cp", "-f", tmp_remote, remote_sys],
            ["chmod", "644", remote_sys],
            ["chown", "root:root", remote_sys],
        ):
            p = _run(["adb", "-s", serial, "shell", *cmd], timeout=20)
            if p.returncode != 0:
                raise RuntimeError(
                    (p.stderr or p.stdout or "").strip() or f"falhou: {' '.join(cmd)}"
                )
        result["system_installed"] = True
        result["system_path"] = remote_sys
        if not result["detail"]:
            result["detail"] = "CA instalado no trust store do sistema. Reinicie apps para pegar o certificado."
    except Exception as exc:
        extra = str(exc)
        if result["sdcard_path"]:
            result["detail"] = (
                f"CA em {result['sdcard_path']} (instalação manual). "
                f"System CA falhou: {extra}"
            )
        else:
            result["detail"] = f"Falha ao instalar CA: {extra}"
    return result


@app.get("/instances/{name}/intercept")
def get_instance_intercept(name: str) -> dict[str, Any]:
    _require_docker_cli()
    name = _sanitize_name(name)
    _inspect_container(name)
    active = _is_intercepting(name)
    return {
        "enabled": active,
        "web_url": f"http://127.0.0.1:{MITM_WEB_PORT}/" if (active or _mitm_running()) else "",
        "proxy_host": _mitm_proxy_host() if active else "",
        "proxy_port": MITM_LISTEN_PORT if active else None,
        "mitm_running": _mitm_running(),
        "instances": _load_mitm_state().get("instances") or [],
    }


@app.post("/instances/{name}/intercept")
def set_instance_intercept(name: str, body: InterceptBody) -> dict[str, Any]:
    _require_docker_cli()
    name = _sanitize_name(name)
    _inspect_container(name)
    if body.enabled:
        info = _enable_intercept(name, install_cert=body.install_cert)
    else:
        info = _disable_intercept(name)
    return {
        "status": "ok",
        **info,
        "instance": _container_to_instance(_inspect_container(name)),
    }


@app.post("/instances/{name}/intercept/install_cert")
def install_instance_intercept_cert(name: str) -> dict[str, Any]:
    _require_docker_cli()
    name = _sanitize_name(name)
    _inspect_container(name)
    if not _mitm_running():
        _start_mitm_container()
    result = _install_mitm_cert(name)
    return {"status": "ok", "cert": result}


VIEWER_HTML = Path(__file__).resolve().parent / "viewer.html"
API_PORT = int(os.environ.get("REDROID_API_PORT", "8765"))


def _host_adb_public_key() -> str:
    path = Path.home() / ".android" / "adbkey.pub"
    if not path.is_file():
        raise HTTPException(
            status_code=503,
            detail="Chave ADB do host não encontrada (~/.android/adbkey.pub). Execute: adb start-server",
        )
    return path.read_text(encoding="utf-8").strip()


def _sync_adb_keys_to_container(name: str) -> None:
    """Após recriar o contêiner, o adbd costuma ficar unauthorized até registrar a chave do host."""
    pub = _host_adb_public_key()
    cmd = _docker_cmd() + [
        "exec",
        "-i",
        name,
        "sh",
        "-c",
        "mkdir -p /data/misc/adb && cat > /data/misc/adb/adb_keys && "
        "chmod 660 /data/misc/adb/adb_keys && chown system:shell /data/misc/adb/adb_keys && "
        "setprop ctl.restart adbd",
    ]
    subprocess.run(cmd, input=pub.encode("utf-8"), capture_output=True, timeout=45, check=False)
    time.sleep(2)


def _adb_get_state(serial: str) -> str:
    p = _run(["adb", "-s", serial, "get-state"], timeout=8)
    text = f"{p.stdout or ''}\n{p.stderr or ''}".strip().lower()
    if "unauthorized" in text:
        return "unauthorized"
    if "offline" in text:
        return "offline"
    if "device" in text and p.returncode == 0:
        return "device"
    return text or "unknown"


def _adb_candidates(inspect_data: dict[str, Any]) -> list[str]:
    """Prefer published host port; fall back to container bridge IP for legacy containers."""
    candidates: list[str] = []
    host_port = _host_port_for_adb(inspect_data)
    if host_port:
        candidates.append(f"127.0.0.1:{host_port}")
    ip = _container_ip(inspect_data)
    if ip:
        candidates.append(f"{ip}:5555")
    return candidates


def _adb_connect(serial: str) -> bool:
    _run(["adb", "connect", serial], timeout=15)
    return _adb_get_state(serial) == "device"


def _skip_setup_wizard(serial: str) -> None:
    """O Setup Wizard do MindTheGapps vira a HOME e morre sem permissão de Wi-Fi (tela preta)."""
    pkg = "com.google.android.setupwizard"
    try:
        root = _run(["adb", "-s", serial, "root"], timeout=15)
        text = f"{root.stdout or ''} {root.stderr or ''}".lower()
        if "restarting" in text:
            time.sleep(1.2)
            _run(["adb", "-s", serial, "wait-for-device"], timeout=20)
        path = _run(["adb", "-s", serial, "shell", "pm", "path", pkg], timeout=15)
        if path.returncode != 0 or "package:" not in (path.stdout or ""):
            return
        for cmd in (
            ["settings", "put", "global", "device_provisioned", "1"],
            ["settings", "put", "secure", "user_setup_complete", "1"],
            ["pm", "disable-user", "--user", "0", pkg],
            ["am", "force-stop", pkg],
            ["am", "start", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME"],
        ):
            _run(["adb", "-s", serial, "shell", *cmd], timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return


def _wait_for_android_boot(serial: str, name: str, attempts: int = 40) -> None:
    """Aguarda sys.boot_completed=1; corrige ADB unauthorized após recreate."""
    synced_keys = False
    for _ in range(attempts):
        state = _adb_get_state(serial)
        if state == "unauthorized":
            if not synced_keys:
                _sync_adb_keys_to_container(name)
                synced_keys = True
                _run(["adb", "disconnect", serial], timeout=5)
                _run(["adb", "connect", serial], timeout=15)
                time.sleep(2)
                continue
            raise HTTPException(
                status_code=400,
                detail=(
                    f"ADB não autorizado ({serial}). A chave do host foi reenviada; "
                    "aguarde alguns segundos e tente Visualizar de novo."
                ),
            )
        if state != "device":
            _run(["adb", "connect", serial], timeout=15)
            time.sleep(1.5)
            continue
        try:
            w = _run(["adb", "-s", serial, "shell", "getprop", "sys.boot_completed"], timeout=8)
            if w.returncode == 0 and "1" in (w.stdout or ""):
                _skip_setup_wizard(serial)
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1.5)
    raise HTTPException(
        status_code=400,
        detail=f"Android ainda não bootou (ADB={serial}). Aguarde e tente de novo.",
    )


def _resolve_serial(name: str) -> str:
    """Get ADB serial for a running instance, connecting if needed."""
    if _cf(name):
        import cuttlefish_host

        return _cf_call(cuttlefish_host.resolve_serial, name)
    ins = _inspect_container(name)
    instance = _container_to_instance(ins)
    if not instance.get("running"):
        raise HTTPException(status_code=400, detail="Inicie a instância primeiro.")
    candidates = _adb_candidates(ins)
    if not candidates:
        raise HTTPException(
            status_code=400,
            detail=(
                "Porta ADB não encontrada. Remova e recrie a instância "
                "(versões antigas foram criadas sem publicar a porta 5555)."
            ),
        )
    errors: list[str] = []
    synced_keys = False
    for serial in candidates:
        try:
            _run(["adb", "connect", serial], timeout=15)
            state = _adb_get_state(serial)
            if state == "unauthorized":
                if not synced_keys:
                    _sync_adb_keys_to_container(name)
                    synced_keys = True
                    _run(["adb", "disconnect", serial], timeout=5)
                    _run(["adb", "connect", serial], timeout=15)
                    time.sleep(2)
                    state = _adb_get_state(serial)
            if state == "device":
                return serial
            hint = ""
            if state == "offline":
                hint = (
                    " (Android pode não ter bootado: veja docker logs NOME — "
                    "conflito VINTF camera.v4l2.xml impede boot; recrie sem mount VINTF duplicado.)"
                )
            errors.append(f"{serial}: estado ADB={state}{hint}")
        except HTTPException:
            raise
        except Exception as exc:
            errors.append(f"{serial}: {exc}")
    raise HTTPException(
        status_code=400,
        detail="Falha ao conectar ADB. " + " | ".join(errors),
    )


def _detect_event_device(serial: str) -> str:
    """Find the redroid-virtual-touch event device path."""
    try:
        out = subprocess.check_output(
            ["adb", "-s", serial, "shell",
             "for d in /sys/class/input/event*; do "
             "name=$(cat $(dirname $(readlink -f $d))/name 2>/dev/null); "
             "[ \"$name\" = 'redroid-virtual-touch' ] && echo $(basename $d) && break; "
             "done"],
            text=True, timeout=10,
        ).strip()
        if out:
            return f"/dev/input/{out}"
    except Exception:
        pass
    return "/dev/input/event18"


@app.post("/instances/{name}/view")
def view_instance(name: str) -> dict[str, Any]:
    _require_docker_cli()
    _require_bin("adb")
    _require_bin("scrcpy")
    name = _sanitize_name(name)
    serial = _resolve_serial(name)
    if not _cf(name):
        _wait_for_android_boot(serial, name)
        _bootstrap_camera_hal(name)

    # Launch native scrcpy window for this instance and detect early failures.
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [
            "scrcpy",
            "-s", serial,
            "--window-title", f"re-droid {name}",
            "--stay-awake",
            "--no-audio",
        ],
        **popen_kwargs,
    )
    # If scrcpy fails instantly (DISPLAY/adb/codec), return the reason.
    time.sleep(0.8)
    if proc.poll() is not None:
        err = ""
        if proc.stderr:
            try:
                err = (proc.stderr.read() or "").strip()
            except Exception:
                err = ""
        detail = err or "scrcpy encerrou imediatamente."
        raise HTTPException(status_code=400, detail=f"Falha ao abrir scrcpy: {detail}")
    return {"status": "launched", "serial": serial, "mode": "scrcpy"}


@app.get("/view/{name}/page")
def viewer_page(name: str) -> HTMLResponse:
    html = VIEWER_HTML.read_text(encoding="utf-8")
    return HTMLResponse(content=html)


_screen_procs: dict[str, asyncio.subprocess.Process] = {}


@app.websocket("/ws/screen/{name}")
async def ws_screen(websocket: WebSocket, name: str):
    """Stream H264 video from the Android screen via ADB screenrecord."""
    await websocket.accept()
    name = _sanitize_name(name)
    serial = _resolve_serial(name)

    old = _screen_procs.pop(name, None)
    if old and old.returncode is None:
        old.terminate()

    proc = await asyncio.create_subprocess_exec(
        "adb", "-s", serial, "exec-out",
        "screenrecord", "--output-format=h264",
        "--size", "720x1280", "--bit-rate", "4000000", "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    _screen_procs[name] = proc
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            try:
                await websocket.send_bytes(chunk)
            except (WebSocketDisconnect, RuntimeError):
                break
    finally:
        _screen_procs.pop(name, None)
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()


_injectors: dict[str, subprocess.Popen] = {}
_cmd_shells: dict[str, subprocess.Popen] = {}


def _deploy_touch_inject(serial: str):
    """Copy touch_inject binary to the container if not already present."""
    binary = TOOLS_DIR / "touch_inject"
    if not binary.exists():
        return
    check = _run(["adb", "-s", serial, "shell", "test -x /touch_inject && echo OK"], timeout=5)
    if "OK" in (check.stdout or ""):
        return
    name = serial.split(":")[-1]
    for cid in _docker_ids():
        try:
            ins = _inspect_container(cid)
            if str(_host_port_for_adb(ins)) == name:
                dc = _docker_cmd()
                _run(dc + ["cp", str(binary), f"{ins['Name'].lstrip('/')}:/touch_inject"])
                _run(dc + ["exec", ins["Name"].lstrip("/"), "chmod", "+x", "/touch_inject"])
                return
        except Exception:
            continue


def _get_injector(serial: str, dev: str) -> subprocess.Popen:
    """Get or start a persistent touch_inject process for atomic event writes."""
    proc = _injectors.get(serial)
    if proc and proc.poll() is None:
        return proc
    _deploy_touch_inject(serial)
    proc = subprocess.Popen(
        ["adb", "-s", serial, "shell", "-T", f"/touch_inject {dev}"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _injectors[serial] = proc
    return proc


def _get_cmd_shell(serial: str) -> subprocess.Popen:
    """Persistent shell for non-touch commands (input keyevent, input text)."""
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


def _inject_events(serial: str, dev: str, events: list[tuple[int, int, int]]):
    """Write input events atomically via touch_inject."""
    try:
        proc = _get_injector(serial, dev)
        lines = "".join(f"{t} {c} {v}\n" for t, c, v in events)
        proc.stdin.write(lines.encode())
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        _injectors.pop(serial, None)


def _cmd_send(serial: str, cmd: str):
    """Send a shell command (input keyevent, input text) via persistent shell."""
    try:
        proc = _get_cmd_shell(serial)
        proc.stdin.write((cmd + "\n").encode())
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        _cmd_shells.pop(serial, None)


def _clamp_coords(nx: float, ny: float) -> tuple[int, int]:
    return (min(int(nx * 719), 719), min(int(ny * 1279), 1279))


EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
SYN_REPORT = 0
ABS_MT_SLOT, ABS_MT_TRACKING_ID = 47, 57
ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 53, 54
BTN_TOUCH = 330


@app.websocket("/ws/input/{name}")
async def ws_input(websocket: WebSocket, name: str):
    """Receive touch/key events and forward via touch_inject."""
    await websocket.accept()
    name = _sanitize_name(name)
    serial = _resolve_serial(name)
    dev = _detect_event_device(serial)
    tracking_id = 0

    try:
        while True:
            data = await websocket.receive_json()
            evt_type = data.get("type", "")

            if evt_type == "down":
                tracking_id = (tracking_id + 1) % 65535
                ax, ay = _clamp_coords(data["x"], data["y"])
                _inject_events(serial, dev, [
                    (EV_ABS, ABS_MT_SLOT, 0),
                    (EV_ABS, ABS_MT_TRACKING_ID, tracking_id),
                    (EV_ABS, ABS_MT_POSITION_X, ax),
                    (EV_ABS, ABS_MT_POSITION_Y, ay),
                    (EV_KEY, BTN_TOUCH, 1),
                    (EV_SYN, SYN_REPORT, 0),
                ])

            elif evt_type == "move":
                ax, ay = _clamp_coords(data["x"], data["y"])
                _inject_events(serial, dev, [
                    (EV_ABS, ABS_MT_SLOT, 0),
                    (EV_ABS, ABS_MT_POSITION_X, ax),
                    (EV_ABS, ABS_MT_POSITION_Y, ay),
                    (EV_SYN, SYN_REPORT, 0),
                ])

            elif evt_type == "up":
                _inject_events(serial, dev, [
                    (EV_ABS, ABS_MT_SLOT, 0),
                    (EV_ABS, ABS_MT_TRACKING_ID, -1),
                    (EV_KEY, BTN_TOUCH, 0),
                    (EV_SYN, SYN_REPORT, 0),
                ])

            elif evt_type == "scroll":
                ax, ay = _clamp_coords(data["x"], data["y"])
                scroll_down = data.get("dir") == "down"
                step = 80
                dist = step if scroll_down else -step
                tracking_id = (tracking_id + 1) % 65535
                events: list[tuple[int, int, int]] = [
                    (EV_ABS, ABS_MT_SLOT, 0),
                    (EV_ABS, ABS_MT_TRACKING_ID, tracking_id),
                    (EV_ABS, ABS_MT_POSITION_X, ax),
                    (EV_ABS, ABS_MT_POSITION_Y, ay),
                    (EV_KEY, BTN_TOUCH, 1),
                    (EV_SYN, SYN_REPORT, 0),
                ]
                for i in range(1, 6):
                    ny = max(0, min(1279, ay + dist * i))
                    events.extend([
                        (EV_ABS, ABS_MT_POSITION_Y, ny),
                        (EV_SYN, SYN_REPORT, 0),
                    ])
                events.extend([
                    (EV_ABS, ABS_MT_TRACKING_ID, -1),
                    (EV_KEY, BTN_TOUCH, 0),
                    (EV_SYN, SYN_REPORT, 0),
                ])
                _inject_events(serial, dev, events)

            elif evt_type == "key":
                kc = int(data.get("keycode", 0))
                if kc:
                    _cmd_send(serial, f"input keyevent {kc}")

            elif evt_type == "text":
                txt = data.get("text", "")
                if txt:
                    safe = txt.replace("'", "'\\''")
                    _cmd_send(serial, f"input text '{safe}'")

    except (WebSocketDisconnect, RuntimeError):
        pass


@app.post("/instances/{name}/install")
async def install_apk(name: str, file: UploadFile = File(...)) -> dict[str, Any]:
    _require_docker_cli()
    _require_bin("adb")
    name = _sanitize_name(name)
    if not _cf(name):
        ins = _inspect_container(name)
        instance = _container_to_instance(ins)
        if not instance.get("running"):
            raise HTTPException(status_code=400, detail="Inicie a instância antes de instalar o APK.")
    serial = _resolve_serial(name)

    suffix = Path(file.filename or "app.apk").suffix
    if suffix.lower() != ".apk":
        suffix = ".apk"
    tmp = Path(tempfile.gettempdir()) / f"redroid-ui-{uuid.uuid4().hex}{suffix}"
    try:
        content = await file.read()
        tmp.write_bytes(content)
        c2 = _run(["adb", "-s", serial, "install", "-r", str(tmp)], timeout=300)
        if c2.returncode != 0:
            msg = (c2.stderr or c2.stdout or "Falha no adb install").strip()
            raise HTTPException(status_code=400, detail=msg)
        return {"status": "installed", "filename": file.filename, "serial": serial}
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _safe_download_name(filename: str | None) -> str:
    raw = Path(filename or "documento").name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._") or "documento"
    return safe[-120:]


@app.post("/instances/{name}/files")
async def upload_instance_file(name: str, file: UploadFile = File(...)) -> dict[str, Any]:
    """Copia um arquivo para /sdcard/Download, visível no seletor de documentos do Android."""
    _require_docker_cli()
    _require_bin("adb")
    name = _sanitize_name(name)
    if not _cf(name):
        ins = _inspect_container(name)
        instance = _container_to_instance(ins)
        if not instance.get("running"):
            raise HTTPException(status_code=400, detail="Inicie a instância antes de enviar o arquivo.")
    serial = _resolve_serial(name)
    dest_name = _safe_download_name(file.filename)
    remote = f"/sdcard/Download/{dest_name}"

    suffix = Path(dest_name).suffix
    tmp = Path(tempfile.gettempdir()) / f"redroid-ui-{uuid.uuid4().hex}{suffix}"
    try:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="Arquivo vazio.")
        tmp.write_bytes(content)
        mk = _run(["adb", "-s", serial, "shell", "mkdir", "-p", "/sdcard/Download"], timeout=30)
        if mk.returncode != 0:
            raise HTTPException(status_code=400, detail=(mk.stderr or mk.stdout or "Falha ao criar Download").strip())
        pushed = _run(["adb", "-s", serial, "push", str(tmp), remote], timeout=180)
        if pushed.returncode != 0:
            raise HTTPException(status_code=400, detail=(pushed.stderr or pushed.stdout or "Falha no adb push").strip())
        _run(
            [
                "adb", "-s", serial, "shell", "am", "broadcast",
                "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                "-d", f"file://{remote}",
            ],
            timeout=20,
        )
        return {"status": "uploaded", "filename": dest_name, "path": remote, "serial": serial}
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Imagens: listar imagens redroid locais
# ---------------------------------------------------------------------------

@app.get("/images")
def list_images() -> dict[str, list[dict[str, str]]]:
    _require_docker_cli()
    sep = "|||"
    fmt = sep.join(["{{.Repository}}:{{.Tag}}", "{{.ID}}", "{{.Size}}"])
    seen: set[str] = set()
    images: list[dict[str, str]] = []
    for ref in ("redroid/*", "redroid:*"):
        p = _run(_docker_cmd() + ["images", "--format", fmt, "--filter", f"reference={ref}"])
        if p.returncode != 0:
            raise HTTPException(status_code=500, detail=(p.stderr or "").strip())
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
    return {"images": images}


@app.delete("/images/{image_id:path}")
def remove_image(image_id: str) -> dict[str, str]:
    _require_docker_cli()
    if not re.match(r"^[a-zA-Z0-9:._/-]+$", image_id):
        raise HTTPException(status_code=400, detail="ID/tag de imagem inválido.")
    p = _run(_docker_cmd() + ["rmi", "-f", image_id])
    if p.returncode != 0:
        raise HTTPException(status_code=400, detail=(p.stderr or p.stdout or "").strip())
    return {"status": "removed", "image": image_id}


# ---------------------------------------------------------------------------
# Spoof: criar imagem spoofed (Pixel 6) a partir de uma imagem base
# ---------------------------------------------------------------------------

SPOOF_PRESETS: dict[str, dict[str, str]] = {
    "pixel6": {
        "label": "Google Pixel 6 (Android 14)",
        "tag_suffix": "spoofed",
        "fingerprint": "google/oriole/oriole:14/AP2A.240805.005/12025142:user/release-keys",
        "brand": "google",
        "manufacturer": "Google",
        "model": "Pixel 6",
        "device": "oriole",
        "name": "oriole",
        "build_id": "AP2A.240805.005",
        "incremental": "12025142",
    },
    "galaxys23": {
        "label": "Samsung Galaxy S23 (Android 13)",
        "fingerprint": "samsung/dm1qxxx/dm1q:13/TP1A.220624.014/S911BXXU3AWI3:user/release-keys",
        "brand": "samsung",
        "manufacturer": "samsung",
        "model": "SM-S911B",
        "device": "dm1q",
        "name": "dm1qxxx",
        "build_id": "TP1A.220624.014",
        "incremental": "S911BXXU3AWI3",
    },
}

SPOOF_BUILD_PROP_FILES = [
    "system/build.prop",
    "vendor/build.prop",
    "system/product/etc/build.prop",
    "system/system_ext/etc/build.prop",
    "vendor/odm/etc/build.prop",
    "system/vendor_dlkm/etc/build.prop",
    "odm/etc/build.prop",
    "system/odm/etc/build.prop",
]

_spoof_lock = threading.Lock()
_spoof_status: dict[str, Any] = {"running": False, "log": "", "last_result": None}


def _sp(preset_id: str) -> dict[str, str]:
    if preset_id not in SPOOF_PRESETS:
        raise HTTPException(status_code=400, detail=f"Preset de spoof inválido: {preset_id}")
    return SPOOF_PRESETS[preset_id]


def _patch_build_prop(content: str, sp: dict[str, str]) -> str:
    """Rewrite a build.prop file content with spoofed device values."""
    replacements: dict[str, str] = {}

    fp_prefixes = [
        "ro.build", "ro.system.build", "ro.vendor.build", "ro.product.build",
        "ro.odm.build", "ro.system_ext.build", "ro.bootimage.build",
        "ro.vendor_dlkm.build", "ro.vendor.bootimage.build",
    ]
    for pfx in fp_prefixes:
        replacements[f"{pfx}.fingerprint"] = sp["fingerprint"]

    replacements["ro.build.display.id"] = sp["build_id"]
    replacements["ro.build.id"] = sp["build_id"]
    replacements["ro.build.tags"] = "release-keys"
    replacements["ro.build.type"] = "user"
    replacements["ro.build.version.incremental"] = sp["incremental"]
    replacements["ro.build.description"] = sp["fingerprint"].replace(":", " ")
    replacements["ro.build.flavor"] = f"{sp['name']}-user"
    replacements["ro.build.host"] = "android-build"
    replacements["ro.build.user"] = "android-build"

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
    replacements["ro.board.platform"] = sp["device"]
    replacements["ro.product.board"] = sp["device"]

    # Hide userdebug/root markers that root-detection libraries (RootBeer/JailMonkey) look for.
    replacements["ro.debuggable"] = "0"
    replacements["ro.secure"] = "1"

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
    """Read a file from a container via docker cp tar stream."""
    dc = _docker_cmd()
    p = subprocess.run(
        dc + ["cp", f"{container}:/{path}", "-"],
        capture_output=True, timeout=30,
    )
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
    """Write a file into a container via docker cp tar stream."""
    dc = _docker_cmd()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=filename)
        info.size = len(data)
        info.mode = mode
        tar.addfile(info, io.BytesIO(data))
    p = subprocess.run(
        dc + ["cp", "-", f"{container}:/{dest_dir}/"],
        input=buf.getvalue(), capture_output=True, timeout=30,
    )
    return p.returncode == 0


def _do_spoof(base_image: str, preset_id: str) -> None:
    global _spoof_status
    sp = _sp(preset_id)
    tmp_name = f"redroid-spoof-tmp-{uuid.uuid4().hex[:8]}"
    dc = _docker_cmd()
    log_lines: list[str] = []

    def log(msg: str) -> None:
        log_lines.append(msg + "\n")
        _spoof_status["log"] = "".join(log_lines)

    try:
        log(f"Preset: {preset_id} ({sp['label']})")
        # Preserve original ENTRYPOINT/CMD; docker commit otherwise inherits a
        # temporary --entrypoint and the resulting image will not boot Android.
        insp = _run(dc + ["inspect", "--format", "{{json .Config}}", base_image], timeout=30)
        if insp.returncode != 0:
            raise RuntimeError(f"Falha ao inspecionar imagem: {(insp.stderr or insp.stdout).strip()}")
        cfg = json.loads((insp.stdout or "{}").strip() or "{}")
        orig_entrypoint = cfg.get("Entrypoint") or ["/init", "qemu=1", "androidboot.hardware=redroid"]
        orig_cmd = cfg.get("Cmd") if cfg.get("Cmd") is not None else []

        log(f"Criando contêiner temporário a partir de {base_image}...")
        # Stopped container is enough for docker cp; Android binaries cannot
        # exec under a normal Docker runtime (bionic linker).
        p = _run(dc + ["create", "--name", tmp_name, base_image], timeout=120)
        if p.returncode != 0:
            raise RuntimeError(f"Falha ao criar contêiner: {(p.stderr or p.stdout).strip()}")

        patched_count = 0
        for prop_path in SPOOF_BUILD_PROP_FILES:
            log(f"Lendo /{prop_path}...")
            raw = _docker_cp_read(tmp_name, prop_path)
            if raw is None:
                log(f"  (não encontrado, pulando)")
                continue

            original = raw.decode("utf-8", errors="replace")
            patched = _patch_build_prop(original, sp)

            if patched == original:
                log(f"  (sem alterações)")
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

        # Embed virtual touchscreen: init_wrapper replaces /init and starts
        # create_touch before Android boot so EventHub detects the device.
        touch_bin = TOOLS_DIR / "create_touch"
        wrapper_bin = TOOLS_DIR / "init_wrapper"
        if touch_bin.exists() and wrapper_bin.exists():
            log("Instalando virtual touchscreen (init_wrapper + create_touch)...")
            _docker_cp_write(tmp_name, "system/bin", "create_touch", touch_bin.read_bytes(), mode=0o755)

            # /init is typically a symlink to /system/bin/init; read the real binary
            orig_init = _docker_cp_read(tmp_name, "system/bin/init")
            if orig_init:
                _docker_cp_write(tmp_name, "", "init_orig", orig_init, mode=0o755)
                _docker_cp_write(tmp_name, "", "init", wrapper_bin.read_bytes(), mode=0o755)
                log("  /init substituído por init_wrapper (original em /init_orig)")
            else:
                log("  AVISO: /system/bin/init não encontrado, pulando wrapper")
        else:
            log("  (binários create_touch/init_wrapper não encontrados)")

        # Tag: adicionar -spoofed-<preset> ao tag da imagem base
        suffix = f"spoofed-{preset_id}"
        if ":" in base_image:
            repo, tag = base_image.rsplit(":", 1)
            new_tag = f"{repo}:{tag}-{suffix}"
        else:
            new_tag = f"{base_image}:{suffix}"

        log(f"Exportando filesystem e removendo marcadores de root (su)...")
        su_skip = {
            "system/xbin/su",
            "system/bin/su",
            "sbin/su",
            "su/bin/su",
            "system/bin/failsafe/su",
            "system/sd/xbin/su",
            "system/app/Superuser.apk",
            "system/app/SuperSU.apk",
        }

        def _is_su_member(name: str) -> bool:
            return name.lstrip("./") in su_skip

        import_cmd = dc + [
            "import",
            "--change",
            f"ENTRYPOINT {json.dumps(orig_entrypoint)}",
            "--change",
            f"CMD {json.dumps(orig_cmd)}",
            "-",
            new_tag,
        ]
        log(f"Filtrando tar e importando {new_tag}...")
        export_p = subprocess.Popen(
            dc + ["export", tmp_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        import_p = subprocess.Popen(
            import_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        class _Pipe:
            def __init__(self, f):
                self._f = f

            def write(self, data):
                return self._f.write(data)

            def flush(self):
                try:
                    if not self._f.closed:
                        self._f.flush()
                except ValueError:
                    pass

            def close(self):
                try:
                    if not self._f.closed:
                        self._f.close()
                except ValueError:
                    pass

            @property
            def closed(self):
                return self._f.closed

        removed: list[str] = []
        try:
            assert export_p.stdout is not None and import_p.stdin is not None
            with tarfile.open(fileobj=export_p.stdout, mode="r|") as src:
                with tarfile.open(fileobj=_Pipe(import_p.stdin), mode="w|") as dst:
                    for member in src:
                        data = src.extractfile(member) if member.isreg() else None
                        if _is_su_member(member.name):
                            removed.append(member.name)
                            if data is not None:
                                while data.read(1024 * 1024):
                                    pass
                            continue
                        dst.addfile(member, data)
            try:
                import_p.stdin.close()
            except Exception:
                pass
            import_out, import_err = import_p.communicate(timeout=1200)
        except Exception:
            export_p.kill()
            import_p.kill()
            raise
        finally:
            if export_p.poll() is None:
                export_p.wait(timeout=30)

        if export_p.returncode not in (0, None):
            raise RuntimeError(f"Falha no export (rc={export_p.returncode})")
        if import_p.returncode != 0:
            err = (import_err or import_out or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Falha no import: {err}")
        for name in removed:
            log(f"  removido {name}")
        if not removed:
            log("  (nenhum su encontrado no tar; conferindo imagem)")

        chk_name = f"redroid-spoof-chk-{uuid.uuid4().hex[:8]}"
        try:
            _run(dc + ["create", "--name", chk_name, new_tag], timeout=120)
            if _docker_cp_read(chk_name, "system/xbin/su") is None:
                log("OK: /system/xbin/su removido")
            else:
                log("AVISO: /system/xbin/su ainda presente na imagem")
        finally:
            _run(dc + ["rm", "-f", chk_name], timeout=30)

        log(f"Imagem {new_tag} criada com sucesso!")
        _spoof_status["last_result"] = {"success": True, "message": f"Imagem spoofed criada: {new_tag}", "tag": new_tag}

    except Exception as exc:
        log(f"ERRO: {exc}")
        _spoof_status["last_result"] = {"success": False, "message": str(exc)}
    finally:
        _run(dc + ["rm", "-f", tmp_name], timeout=30)
        _spoof_status["running"] = False


class SpoofImageBody(BaseModel):
    base_image: str = Field(..., description="Tag da imagem base para fazer spoof")
    preset: str = Field(default="pixel6", description="Preset de spoof (ver /images/spoof/presets)")


@app.get("/images/spoof/presets")
def spoof_presets() -> dict[str, Any]:
    return {
        "presets": [
            {"id": k, "label": v["label"]} for k, v in SPOOF_PRESETS.items()
        ]
    }


@app.post("/images/spoof")
def spoof_image(body: SpoofImageBody) -> dict[str, Any]:
    _require_docker_cli()
    if not re.match(r"^[a-zA-Z0-9:._/-]+$", body.base_image):
        raise HTTPException(status_code=400, detail="Tag de imagem inválida.")
    if body.preset not in SPOOF_PRESETS:
        raise HTTPException(status_code=400, detail=f"Preset inválido: {body.preset}")
    if _spoof_status["running"]:
        raise HTTPException(status_code=409, detail="Já existe um spoof em andamento.")

    with _spoof_lock:
        _spoof_status["running"] = True
        _spoof_status["log"] = ""
        _spoof_status["last_result"] = None

    t = threading.Thread(target=_do_spoof, args=(body.base_image, body.preset), daemon=True)
    t.start()
    return {"status": "started"}


@app.get("/images/spoof/status")
def spoof_status() -> dict[str, Any]:
    return {
        "running": _spoof_status["running"],
        "log": _spoof_status["log"],
        "result": _spoof_status["last_result"],
    }


# ---------------------------------------------------------------------------
# Build: construir imagem customizada via redroid-script
# ---------------------------------------------------------------------------

ANDROID_VERSIONS = ["14.0.0", "13.0.0", "12.0.0", "12.0.0_64only", "12-camera", "11.0.0", "10.0.0", "9.0.0", "8.1.0"]

# Imagens locais: o pacote (GApps/ndk) segue a versão Android; o FROM é a tag local.
LOCAL_IMAGE_BUILDS: dict[str, dict[str, str]] = {
    "12-camera": {
        "android": "12.0.0_64only",
        "base_image": "redroid:12-camera",
        "output_prefix": "redroid:12-camera",
    },
}


class BuildImageBody(BaseModel):
    android_version: str = Field(default="11.0.0", description="Versão Android")
    gapps: bool = Field(default=False, description="OpenGapps (só 11.0.0)")
    litegapps: bool = Field(default=False, description="LiteGapps")
    mindthegapps: bool = Field(default=False, description="MindTheGapps")
    ndk: bool = Field(default=False, description="libndk ARM translation")
    houdini: bool = Field(default=False, description="Houdini ARM translation")
    magisk: bool = Field(default=False, description="Magisk (rootless)")
    widevine: bool = Field(default=False, description="Widevine DRM (L3)")


def _do_build(body: BuildImageBody) -> None:
    global _build_status
    try:
        spec = LOCAL_IMAGE_BUILDS.get(body.android_version)
        android = spec["android"] if spec else body.android_version
        args = [sys.executable, "redroid.py", "-a", android]
        if spec:
            args += ["-b", spec["base_image"], "--output-prefix", spec["output_prefix"]]
        if _docker_use_sudo():
            args += ["-c", "docker"]
        if body.gapps:
            args.append("-g")
        if body.litegapps:
            args.append("-lg")
        if body.mindthegapps:
            args.append("-mtg")
        if body.ndk:
            args.append("-n")
        if body.houdini:
            args.append("-i")
        if body.magisk:
            args.append("-m")
        if body.widevine:
            args.append("-w")

        env = {**os.environ}
        if _docker_use_sudo():
            docker_exe = _docker_exe()
            env["PATH"] = os.environ.get("PATH", "")
            wrapper_dir = Path(tempfile.mkdtemp(prefix="redroid-ui-docker-wrapper-"))
            wrapper = wrapper_dir / "docker"
            wrapper.write_text(f"#!/bin/sh\nexec sudo -n {docker_exe} \"$@\"\n")
            wrapper.chmod(0o755)
            env["PATH"] = f"{wrapper_dir}:{env['PATH']}"

        proc = subprocess.Popen(
            args,
            cwd=str(REDROID_SCRIPT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        log_lines: list[str] = []
        for line in iter(proc.stdout.readline, ""):  # type: ignore[union-attr]
            log_lines.append(line)
            _build_status["log"] = "".join(log_lines)
        proc.wait()
        if proc.returncode == 0:
            _build_status["last_result"] = {"success": True, "message": "Imagem construída com sucesso."}
        else:
            _build_status["last_result"] = {"success": False, "message": f"Build falhou (código {proc.returncode})."}
    except Exception as exc:
        _build_status["last_result"] = {"success": False, "message": str(exc)}
    finally:
        _build_status["running"] = False


@app.post("/images/build")
def build_image(body: BuildImageBody) -> dict[str, Any]:
    _require_docker_cli()
    if not REDROID_SCRIPT_DIR.is_dir():
        raise HTTPException(status_code=503, detail="redroid-script não encontrado. Clone para redroid-script/ na raiz do projeto.")
    if body.android_version not in ANDROID_VERSIONS:
        raise HTTPException(status_code=400, detail=f"Versão Android inválida. Opções: {', '.join(ANDROID_VERSIONS)}")

    if _build_status["running"]:
        raise HTTPException(status_code=409, detail="Já existe um build em andamento. Aguarde.")

    with _build_lock:
        _build_status["running"] = True
        _build_status["log"] = ""
        _build_status["last_result"] = None

    t = threading.Thread(target=_do_build, args=(body,), daemon=True)
    t.start()
    return {"status": "started"}


@app.get("/images/build/status")
def build_status() -> dict[str, Any]:
    return {
        "running": _build_status["running"],
        "log": _build_status["log"],
        "result": _build_status["last_result"],
    }


# ---------------------------------------------------------------------------
# Câmera (v4l2 / webcam / simulador)
# ---------------------------------------------------------------------------


@app.get("/camera/devices")
def camera_list_devices() -> dict[str, Any]:
    import camera_host

    out = camera_host.list_host_devices()
    out["hal_bundle_status"] = camera_host.hal_bundle_status()
    return out


@app.get("/camera/bundle")
def camera_bundle_status() -> dict[str, Any]:
    import camera_hal

    return camera_hal.bundle_status()


class CameraHalExtractBody(BaseModel):
    image: str = Field(..., description="Imagem Docker com camera.v4l2.so compilado")


@app.post("/camera/hal/extract")
def camera_hal_extract(body: CameraHalExtractBody) -> dict[str, Any]:
    import camera_hal

    _require_docker_cli()
    try:
        return camera_hal.extract_from_image(body.image)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/camera/deeplive/status")
def camera_deeplive_status() -> dict[str, Any]:
    import deeplive_host

    return {"deeplive": deeplive_host.status()}


@app.post("/camera/setup-loopback")
def camera_setup_loopback() -> dict[str, Any]:
    """Tenta carregar v4l2loopback; sem sudo retorna o comando para o host."""
    import camera_host

    host = camera_host.list_host_devices()
    if host.get("loopback_present") and any(
        camera_host._is_loopback_device(d.get("path", "")) for d in host.get("devices", [])
    ):
        return {"status": "ok", "message": "Loopback já disponível.", "host": host}

    script = Path(__file__).resolve().parent.parent / "tools" / "camera" / "setup-host.sh"
    if script.is_file():
        p = _run(["bash", str(script)], timeout=60)
        if p.returncode == 0:
            return {
                "status": "ok",
                "message": "v4l2loopback carregado.",
                "host": camera_host.list_host_devices(),
            }
        err = (p.stderr or p.stdout or "").strip()
        return {
            "status": "needs_sudo",
            "message": "Execute no host (senha de sudo):",
            "command": f"bash {script}",
            "detail": err,
            "host": host,
        }
    raise HTTPException(status_code=500, detail="tools/camera/setup-host.sh não encontrado.")


@app.get("/instances/{name}/camera")
def get_instance_camera(name: str) -> dict[str, Any]:
    import camera_host

    _require_docker_cli()
    name = _sanitize_name(name)
    cfg = _sync_camera_loopback_from_container(name, _load_camera_config(name))
    out: dict[str, Any] = {
        "config": cfg,
        "feed_running": camera_host.feed_running(name, cfg),
        "hal_bundle": camera_host.hal_bundle_present(),
        "host": camera_host.list_host_devices(),
    }
    try:
        ins = _inspect_container(name)
        if ins.get("State", {}).get("Running"):
            if cfg.get("enabled") and not out["feed_running"]:
                try:
                    out["feed"] = camera_host.ensure_feed(name, cfg)
                    out["feed_running"] = bool(out["feed"].get("running"))
                except RuntimeError as exc:
                    out["feed_error"] = str(exc)
            serial = _resolve_serial(name)

            def _adb(*args: str) -> subprocess.CompletedProcess[str]:
                return _run(["adb", "-s", serial, *args], timeout=20)

            out["android"] = camera_host.probe_android_cameras(_adb)
    except HTTPException:
        pass
    except Exception as exc:
        out["android_error"] = str(exc)
    bundle = camera_host.hal_bundle_status()
    out["hal_bundle_status"] = bundle
    if cfg.get("enabled"):
        if not bundle.get("hal_so"):
            out["warning"] = (
                "Falta camera.v4l2.so. Compile reDroid com USE_CAMERA_V4L2_HAL, "
                "POST /camera/hal/extract com a imagem, ou REDROID_CAMERA_HAL_URL no .env."
            )
        elif bundle.get("level") == "hal_only":
            out["warning"] = (
                "HAL presente, mas falta android.hardware.camera.provider@2.4-service. "
                "Extraia de uma imagem reDroid compilada com provider 2.4 ou use bind-mount completo."
            )
        elif out.get("android") and out["android"].get("camera_count", 0) == 0:
            out["warning"] = (
                "Android reporta 0 câmeras. Confira loopback (setup-host.sh), recrie a instância "
                "com Android 12+ e androidboot.redroid_camera=1."
            )
    return out


@app.post("/instances/{name}/camera/upload")
async def upload_camera_media(name: str, file: UploadFile = File(...)) -> dict[str, Any]:
    import camera_host

    _require_docker_cli()
    name = _sanitize_name(name)
    if not file.filename:
        raise HTTPException(status_code=400, detail="Arquivo inválido.")
    ext = Path(file.filename).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mkv", ".webm", ".mov"}:
        raise HTTPException(status_code=400, detail="Formato não suportado (imagem ou vídeo).")
    dest_dir = camera_host.instance_camera_dir(name)
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", Path(file.filename).name)
    dest = dest_dir / safe_name
    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Arquivo maior que 200 MB.")
    dest.write_bytes(data)
    cfg = _sync_camera_loopback_from_container(name, _load_camera_config(name))
    cfg["media_path"] = safe_name
    if cfg.get("enabled") and cfg.get("mode") not in ("image", "video"):
        cfg["mode"] = "image"
    _write_camera_config(name, cfg)
    feed: dict[str, Any] | None = None
    feed_error = ""
    if cfg.get("enabled"):
        try:
            feed = camera_host.start_feed(name, cfg)
        except RuntimeError as exc:
            feed_error = str(exc)
    out: dict[str, Any] = {"status": "ok", "media_path": safe_name, "path": str(dest)}
    if feed is not None:
        out["feed"] = feed
    if feed_error:
        out["feed_error"] = feed_error
    return out


@app.post("/instances/{name}/camera/open")
def open_camera_app(name: str) -> dict[str, Any]:
    _require_bin("adb")
    name = _sanitize_name(name)
    serial = _resolve_serial(name)
    _wait_for_android_boot(serial, name)
    _bootstrap_camera_hal(name)
    p = _run(
        [
            "adb",
            "-s",
            serial,
            "shell",
            "am",
            "start",
            "-a",
            "android.media.action.STILL_IMAGE_CAMERA",
        ],
        timeout=20,
    )
    if p.returncode != 0:
        p2 = _run(
            [
                "adb",
                "-s",
                serial,
                "shell",
                "am",
                "start",
                "-n",
                "com.android.camera2/com.android.camera.CaptureActivity",
            ],
            timeout=20,
        )
        if p2.returncode != 0:
            err = (p.stderr or p.stdout or p2.stderr or p2.stdout or "").strip()
            raise HTTPException(status_code=400, detail=err or "Falha ao abrir câmera.")
    return {"status": "ok", "serial": serial}


@app.put("/instances/{name}/camera")
def put_instance_camera(name: str, body: CameraConfigBody, recreate: bool = False) -> dict[str, Any]:
    _require_docker_cli()
    name = _sanitize_name(name)
    import camera_host

    old_cfg = _load_camera_config(name)
    loopback = (body.loopback_device or "").strip() or (old_cfg.get("loopback_device") or "").strip()
    cfg = _write_camera_config(
        name,
        {
            "enabled": body.enabled,
            "mode": body.mode,
            "webcam_device": body.webcam_device,
            "loopback_device": loopback,
            "media_path": body.media_path,
            "stream_url": body.stream_url,
            "webcam_fallback_image": body.webcam_fallback_image,
            "deeplive_root": body.deeplive_root,
            "deeplive_input_device": body.deeplive_input_device,
            "deeplive_execution_provider": body.deeplive_execution_provider,
            "deeplive_mouth_mask": body.deeplive_mouth_mask,
        },
    )
    cfg = _sync_camera_loopback_from_container(name, cfg)
    must_recreate = camera_host.container_recreate_needed(old_cfg, cfg)
    if recreate or must_recreate:
        result = _recreate_instance_container(name)
        result["config"] = cfg
        result["container_recreated"] = True
        result["recreate_reason"] = "forced" if recreate and not must_recreate else "config"
        return result
    feed = _start_camera_pipeline(name, cfg)
    try:
        ins = _inspect_container(name)
        if ins.get("State", {}).get("Running"):
            _bootstrap_camera_hal(name)
    except HTTPException:
        pass
    return {
        "config": cfg,
        "container_recreated": False,
        **feed,
    }


# ---------------------------------------------------------------------------
# Telefonia (SIM soft) · SMS local · HeroSMS
# ---------------------------------------------------------------------------

from hero_sms_client import HeroSMSClient, HeroSMSError  # noqa: E402

TELEPHONY_KEYS = (
    "gsm.sim.state",
    "gsm.operator.alpha",
    "gsm.operator.numeric",
    "gsm.sim.operator.alpha",
    "gsm.sim.operator.numeric",
    "gsm.operator.iso-country",
    "gsm.sim.operator.iso-country",
    "gsm.sim.operator.iso-country",
)


class TelephonyBody(BaseModel):
    sim_state: str = Field(default="READY", description="Ex.: READY, ABSENT")
    operator_name: str = Field(default="Vivo", description="Nome exibido da operadora")
    operator_numeric: str = Field(default="72406", description="MCC+MNC (ex. Vivo BR)")
    country_iso: str = Field(default="br", description="ISO país (minúsculo)")
    line1_number: str = Field(default="", description="Número E.164 opcional (nem sempre refletido em apps)")


class SmsInjectBody(BaseModel):
    from_number: str = Field(default="+5500000000000", alias="from", min_length=3, max_length=32)
    body: str = Field(..., min_length=1, max_length=2000)

    model_config = {"populate_by_name": True}


class SmsActivationCreateBody(BaseModel):
    service: str = Field(..., min_length=1, max_length=32, description="Código HeroSMS (tg, wa, …)")
    country: int = Field(..., ge=0, description="ID do país na HeroSMS")
    instance_name: str = Field(default="", description="Instância re-droid para injetar SMS/código")
    auto_inject: bool = Field(default=True, description="Injeta SMS/código via ADB quando chegar")
    mark_ready: bool = Field(default=True, description="Marca ativação pronta (status 1) após alugar número")
    max_price: float | None = Field(default=None, ge=0)
    set_device_number: bool = Field(default=True, description="Troca o número do aparelho pelo número alugado")
    flash_call: bool = Field(default=False, description="Código chega como ligação: simula chamada perdida")
    flash_prefix: str = Field(default="", max_length=20, description="Prefixo do número que liga, se o código vier só com os dígitos finais")


_sms_activation_lock = threading.Lock()
_sms_activations: dict[str, dict[str, Any]] = {}
_sms_poll_stop: dict[str, threading.Event] = {}


def _hero_client() -> HeroSMSClient:
    try:
        return HeroSMSClient()
    except HeroSMSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _adb_shell_prop(serial: str, key: str) -> str:
    p = _run(["adb", "-s", serial, "shell", "getprop", key], timeout=10)
    if p.returncode != 0:
        return ""
    return (p.stdout or "").strip()


def _adb_setprop(serial: str, key: str, value: str) -> tuple[bool, str]:
    if not re.match(r"^[a-zA-Z0-9_.-]+$", key):
        return False, "chave inválida"
    p = _run(["adb", "-s", serial, "shell", "setprop", key, value], timeout=15)
    err = (p.stderr or p.stdout or "").strip()
    if p.returncode != 0:
        return False, err or "setprop falhou"
    return True, ""


def _telephony_snapshot(serial: str) -> dict[str, str]:
    return {k: _adb_shell_prop(serial, k) for k in TELEPHONY_KEYS}


def _apply_telephony_soft(serial: str, body: TelephonyBody) -> dict[str, Any]:
    iso = body.country_iso.strip().lower()[:2] or "br"
    mappings = {
        "gsm.sim.state": body.sim_state.strip() or "READY",
        "gsm.operator.alpha": body.operator_name.strip() or "Vivo",
        "gsm.operator.numeric": body.operator_numeric.strip() or "72406",
        "gsm.sim.operator.alpha": body.operator_name.strip() or "Vivo",
        "gsm.sim.operator.numeric": body.operator_numeric.strip() or "72406",
        "gsm.operator.iso-country": iso,
        "gsm.sim.operator.iso-country": iso,
    }
    if body.line1_number.strip():
        mappings["gsm.sim.operator.iso-country"] = iso
    errors: list[str] = []
    for key, val in mappings.items():
        ok, msg = _adb_setprop(serial, key, val)
        if not ok:
            errors.append(f"{key}: {msg}")
    if body.line1_number.strip():
        ok, msg = _adb_setprop(serial, "persist.sys.phone_number", body.line1_number.strip())
        if not ok:
            errors.append(f"persist.sys.phone_number: {msg}")
    return {
        "applied": mappings,
        "snapshot": _telephony_snapshot(serial),
        "errors": errors,
        "note": (
            "SIM soft via setprop: alguns apps ignoram. Reiniciar a instância pode limpar props. "
            "Evite `adb root` no reDroid (pode derrubar o ADB)."
        ),
    }


MMSSMS_DB = "/data/user_de/0/com.android.providers.telephony/databases/mmssms.db"


def _adb_has_su(serial: str) -> bool:
    p = _run(["adb", "-s", serial, "shell", "su", "0", "id"], timeout=10)
    return p.returncode == 0 and "uid=0" in (p.stdout or "")


def _adb_sqlite_scalar(serial: str, sql: str) -> str:
    # Um único argumento para `adb shell` evita que `()` no SQL vire subshell no device.
    remote = f"su 0 sqlite3 {MMSSMS_DB} {shlex.quote(sql)}"
    p = _run(["adb", "-s", serial, "shell", remote], timeout=30)
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        raise HTTPException(status_code=400, detail=err or "Falha ao acessar banco SMS.")
    return (p.stdout or "").strip()


def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


def _inject_sms_inbox(serial: str, from_number: str, body: str) -> dict[str, Any]:
    """
    No reDroid, `content insert` não grava SMS (provider sem SIM). Gravamos via sqlite (su)
    e avisamos o sistema + usuário (broadcast / notificação / clipboard).
    """
    if not _adb_has_su(serial):
        raise HTTPException(
            status_code=400,
            detail="Este Android não tem `su` — não dá para gravar SMS na inbox do reDroid.",
        )
    ts = int(time.time() * 1000)
    addr = _sql_escape(from_number.strip())
    text = _sql_escape(body.strip())
    snippet = _sql_escape(body.strip()[:160])

    _adb_sqlite_scalar(serial, f"INSERT OR IGNORE INTO canonical_addresses(address) VALUES('{addr}');")
    addr_id = _adb_sqlite_scalar(
        serial,
        f"SELECT _id FROM canonical_addresses WHERE address='{addr}' LIMIT 1;",
    )
    if not addr_id.isdigit():
        raise HTTPException(status_code=400, detail="Falha ao registrar remetente SMS.")

    thread_id = _adb_sqlite_scalar(
        serial,
        f"SELECT _id FROM threads WHERE recipient_ids='{addr_id}' LIMIT 1;",
    )
    if not thread_id.isdigit():
        _adb_sqlite_scalar(
            serial,
            f"INSERT INTO threads(date,recipient_ids,snippet,read,message_count) "
            f"VALUES({ts},'{addr_id}','{snippet}',0,0);",
        )
        thread_id = _adb_sqlite_scalar(
            serial,
            f"SELECT _id FROM threads WHERE recipient_ids='{addr_id}' LIMIT 1;",
        )
    if not thread_id.isdigit():
        raise HTTPException(status_code=400, detail="Falha ao criar thread SMS.")

    _adb_sqlite_scalar(
        serial,
        f"INSERT INTO sms(thread_id,address,date,read,type,body) "
        f"VALUES({thread_id},'{addr}',{ts},0,1,'{text}');",
    )
    verify = _adb_sqlite_scalar(
        serial,
        f"SELECT body FROM sms WHERE thread_id={thread_id} ORDER BY _id DESC LIMIT 1;",
    )
    if verify != body.strip():
        raise HTTPException(status_code=400, detail="SMS gravado não confere com o esperado.")

    _run(
        [
            "adb",
            "-s",
            serial,
            "shell",
            "am",
            "broadcast",
            "-a",
            "android.intent.action.PROVIDER_CHANGED",
            "-d",
            "content://sms",
        ],
        timeout=15,
    )
    _adb_notify_sms(serial, from_number.strip(), body.strip())
    _adb_clipboard(serial, body.strip())

    return {
        "inbox": True,
        "thread_id": int(thread_id),
        "notification": True,
        "clipboard": True,
        "note": (
            "Sem app de SMS (Messages), a inbox não abre sozinha — veja a notificação no Android "
            "ou consulte com: adb shell content query --uri content://sms/inbox"
        ),
    }


def _adb_notify_sms(serial: str, sender: str, body: str) -> None:
    title = f"SMS de {sender}"
    preview = body if len(body) <= 120 else body[:117] + "..."
    _run(
        [
            "adb",
            "-s",
            serial,
            "shell",
            "cmd",
            "notification",
            "post",
            "-S",
            "bigtext",
            "-t",
            title,
            "redroid_ui_sms",
            preview,
        ],
        timeout=15,
    )


def _adb_clipboard(serial: str, text: str) -> None:
    p = _run(["adb", "-s", serial, "shell", "cmd", "clipboard", "set", "text", text], timeout=15)
    if p.returncode != 0:
        _run(["adb", "-s", serial, "shell", "service", "call", "clipboard", "2", "s16", text], timeout=15)


def _inject_sms(name: str, serial: str, from_number: str, body: str) -> dict[str, Any]:
    if _cf(name):
        import cuttlefish_host

        meta = _cf_call(cuttlefish_host.send_sms, name, from_number, body)
        _adb_clipboard(serial, body.strip())
        return {
            "inbox": True,
            "modem": True,
            "clipboard": True,
            **meta,
            "note": "SMS real pelo modem simulado (SMS_RECEIVED chega aos apps).",
        }
    return _inject_sms_inbox(serial, from_number, body)


def _deliver_code_to_instance(
    instance_name: str,
    from_number: str,
    sms_text: str,
    code: str,
    flash_call: bool = False,
    flash_prefix: str = "",
    flash_from: str = "",
) -> dict[str, Any]:
    import cuttlefish_host

    name = _sanitize_name(instance_name.strip())
    serial = _resolve_serial(name)
    result: dict[str, Any] = {"instance": name, "serial": serial}
    if flash_call and _cf(name):
        try:
            caller = _flash_caller(flash_from if len(re.sub(r"\D", "", flash_from)) >= 8 else code, flash_prefix)
            result["call"] = _cf_call(cuttlefish_host.incoming_call, name, caller)
        except HTTPException as exc:
            result["call_error"] = exc.detail
    else:
        try:
            inject_meta = _inject_sms(name, serial, from_number, sms_text or code)
            result["sms_injected"] = True
            result["sms_meta"] = inject_meta
        except HTTPException as exc:
            result["sms_injected"] = False
            result["sms_error"] = exc.detail
    try:
        _adb_clipboard(serial, code)
        result["clipboard"] = True
    except Exception as exc:
        result["clipboard"] = False
        result["clipboard_error"] = str(exc)
    return result


def _poll_activation_loop(activation_id: str) -> None:
    stop = _sms_poll_stop.setdefault(activation_id, threading.Event())
    client = _hero_client()
    deadline = time.time() + 600
    with _sms_activation_lock:
        is_call = bool((_sms_activations.get(activation_id) or {}).get("flash_call"))
    while not stop.is_set() and time.time() < deadline:
        try:
            st = client.get_status_v2(activation_id) if is_call else client.get_status(activation_id)
        except HeroSMSError as exc:
            with _sms_activation_lock:
                rec = _sms_activations.get(activation_id)
                if rec:
                    rec["last_error"] = str(exc)
            time.sleep(5)
            continue
        with _sms_activation_lock:
            rec = _sms_activations.get(activation_id)
            if not rec or rec.get("finished"):
                return
            rec["hero_status"] = st.get("status")
            rec["raw_status"] = st.get("raw")
        if st.get("status") == "ok":
            phone_from = str(st.get("phone_from") or "")
            code = str(st.get("code") or "") or re.sub(r"\D", "", phone_from)
            instance_name = ""
            auto_inject = False
            phone = "+0000000000"
            with _sms_activation_lock:
                rec = _sms_activations.get(activation_id)
                if not rec:
                    return
                rec["code"] = code
                if phone_from:
                    rec["phone_from"] = phone_from
                rec["finished"] = True
                instance_name = (rec.get("instance_name") or "").strip()
                auto_inject = bool(rec.get("auto_inject"))
                phone = rec.get("phone") or "+0000000000"
                flash_call = bool(rec.get("flash_call"))
                flash_prefix = rec.get("flash_prefix") or ""
            if auto_inject and instance_name and code:
                try:
                    delivery = _deliver_code_to_instance(
                        instance_name,
                        phone,
                        f"Codigo: {code}",
                        code,
                        flash_call=flash_call,
                        flash_prefix=flash_prefix,
                        flash_from=phone_from,
                    )
                    with _sms_activation_lock:
                        rec2 = _sms_activations.get(activation_id)
                        if rec2:
                            rec2["delivery"] = delivery
                except HTTPException as exc:
                    with _sms_activation_lock:
                        rec2 = _sms_activations.get(activation_id)
                        if rec2:
                            rec2["delivery_error"] = exc.detail
            return
        if st.get("status") == "cancelled":
            with _sms_activation_lock:
                rec = _sms_activations.get(activation_id)
                if rec:
                    rec["finished"] = True
                    rec["cancelled"] = True
            return
        time.sleep(3)


def _start_activation_poll(activation_id: str) -> None:
    stop = _sms_poll_stop.get(activation_id)
    if stop:
        stop.clear()
    else:
        _sms_poll_stop[activation_id] = threading.Event()
    threading.Thread(target=_poll_activation_loop, args=(activation_id,), daemon=True).start()


@app.get("/sms/config")
def sms_config() -> dict[str, Any]:
    key = os.environ.get("HERO_SMS_API_KEY", "").strip()
    out: dict[str, Any] = {
        "configured": bool(key),
        "api_url": os.environ.get("HERO_SMS_API_URL", "").strip() or "https://hero-sms.com/stubs/handler_api.php",
    }
    if key:
        try:
            out["balance"] = _hero_client().get_balance()
        except HeroSMSError as exc:
            out["balance_error"] = str(exc)
    return out


_hero_catalog_cache: dict[str, Any] = {"at": 0.0, "data": None}


@app.get("/sms/catalog")
def sms_catalog() -> dict[str, Any]:
    from hero_sms_client import public_catalog

    if _hero_catalog_cache["data"] and time.time() - _hero_catalog_cache["at"] < 6 * 3600:
        return _hero_catalog_cache["data"]
    try:
        data = public_catalog()
    except HeroSMSError as exc:
        if _hero_catalog_cache["data"]:
            return _hero_catalog_cache["data"]
        raise HTTPException(status_code=502, detail=f"HeroSMS: {exc}") from exc
    _hero_catalog_cache.update(at=time.time(), data=data)
    return data


@app.get("/sms/prices")
def sms_prices(service: str, type: str = "sms") -> dict[str, Any]:
    service = service.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", service):
        raise HTTPException(status_code=400, detail="Serviço inválido.")
    if type not in ("sms", "call"):
        raise HTTPException(status_code=400, detail="Tipo inválido (sms ou call).")
    try:
        client = _hero_client()
        prices = client.get_offers(service, "call") if type == "call" else client.get_prices(service)
    except HeroSMSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"service": service, "type": type, "prices": prices}


@app.get("/sms/activations")
def sms_list_activations() -> dict[str, list[dict[str, Any]]]:
    with _sms_activation_lock:
        items = list(_sms_activations.values())
    items.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return {"activations": items}


@app.post("/sms/activations")
def sms_create_activation(body: SmsActivationCreateBody) -> dict[str, Any]:
    client = _hero_client()
    try:
        if body.flash_call:
            rented = client.buy_activation(body.service, body.country, body.max_price, verification_type="call")
        else:
            rented = client.get_number(body.service, body.country, body.max_price)
    except HeroSMSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    activation_id = str(rented["activation_id"])
    phone = str(rented["phone"])
    if body.mark_ready:
        try:
            client.mark_ready(activation_id)
        except HeroSMSError as exc:
            try:
                client.cancel(activation_id)
            except HeroSMSError:
                pass
            raise HTTPException(status_code=400, detail=f"mark_ready: {exc}") from exc
    rec: dict[str, Any] = {
        "activation_id": activation_id,
        "phone": phone,
        "service": body.service,
        "country": body.country,
        "instance_name": body.instance_name.strip(),
        "auto_inject": body.auto_inject,
        "flash_call": body.flash_call,
        "flash_prefix": body.flash_prefix.strip(),
        "hero_status": "waiting",
        "code": None,
        "finished": False,
        "created_at": time.time(),
    }
    instance = rec["instance_name"]
    apply_number = body.set_device_number and bool(instance)
    if apply_number:
        rec["device_number"] = {"state": "applying", "phone_number": f"+{phone}"}
    with _sms_activation_lock:
        _sms_activations[activation_id] = rec
    if apply_number:
        threading.Thread(
            target=_apply_activation_number, args=(activation_id, instance, phone), daemon=True
        ).start()
    _start_activation_poll(activation_id)
    return {"activation": rec}


def _apply_activation_number(activation_id: str, instance: str, phone: str) -> None:
    try:
        result: dict[str, Any] = {"state": "ok", **_set_device_number(_sanitize_name(instance), phone)}
    except HTTPException as exc:
        result = {"state": "error", "phone_number": f"+{phone}", "error": str(exc.detail)}
    except Exception as exc:  # noqa: BLE001
        result = {"state": "error", "phone_number": f"+{phone}", "error": str(exc)}
    with _sms_activation_lock:
        rec = _sms_activations.get(activation_id)
        if rec is not None:
            rec["device_number"] = result


@app.get("/sms/activations/{activation_id}")
def sms_get_activation(activation_id: str) -> dict[str, Any]:
    activation_id = activation_id.strip()
    with _sms_activation_lock:
        rec = _sms_activations.get(activation_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Ativação não encontrada nesta sessão da API.")
    client = _hero_client()
    try:
        st = client.get_status_v2(activation_id) if rec.get("flash_call") else client.get_status(activation_id)
    except HeroSMSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with _sms_activation_lock:
        rec = _sms_activations.get(activation_id) or rec
        rec["hero_status"] = st.get("status")
        rec["raw_status"] = st.get("raw")
        if st.get("status") == "ok":
            rec["code"] = st.get("code") or re.sub(r"\D", "", str(st.get("phone_from") or ""))
            if st.get("phone_from"):
                rec["phone_from"] = st.get("phone_from")
            rec["finished"] = True
    return {"activation": rec}


@app.post("/sms/activations/{activation_id}/complete")
def sms_complete_activation(activation_id: str) -> dict[str, Any]:
    activation_id = activation_id.strip()
    client = _hero_client()
    try:
        raw = client.complete(activation_id)
    except HeroSMSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with _sms_activation_lock:
        rec = _sms_activations.get(activation_id)
        if rec:
            rec["finished"] = True
            rec["completed"] = True
    return {"status": "ok", "raw": raw}


@app.post("/sms/activations/{activation_id}/cancel")
def sms_cancel_activation(activation_id: str) -> dict[str, Any]:
    activation_id = activation_id.strip()
    stop = _sms_poll_stop.get(activation_id)
    if stop:
        stop.set()
    client = _hero_client()
    try:
        raw = client.cancel(activation_id)
    except HeroSMSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with _sms_activation_lock:
        rec = _sms_activations.get(activation_id)
        if rec:
            rec["finished"] = True
            rec["cancelled"] = True
    return {"status": "ok", "raw": raw}


@app.post("/sms/activations/{activation_id}/deliver")
def sms_deliver_activation(activation_id: str) -> dict[str, Any]:
    """Envia código/SMS para a instância vinculada (manual)."""
    activation_id = activation_id.strip()
    with _sms_activation_lock:
        rec = _sms_activations.get(activation_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Ativação não encontrada.")
    code = rec.get("code")
    if not code:
        client = _hero_client()
        st = client.get_status(activation_id)
        if st.get("status") != "ok":
            raise HTTPException(status_code=400, detail="Código ainda não disponível.")
        code = st.get("code")
        rec["code"] = code
    if not rec.get("instance_name"):
        raise HTTPException(status_code=400, detail="Nenhuma instância vinculada.")
    delivery = _deliver_code_to_instance(
        rec["instance_name"],
        rec.get("phone") or "+0000000000",
        f"Codigo: {code}",
        str(code),
        flash_call=bool(rec.get("flash_call")),
        flash_prefix=rec.get("flash_prefix") or "",
        flash_from=rec.get("phone_from") or "",
    )
    with _sms_activation_lock:
        rec["delivery"] = delivery
    return {"status": "ok", "delivery": delivery}


@app.get("/instances/{name}/telephony")
def get_instance_telephony(name: str) -> dict[str, Any]:
    _require_bin("adb")
    name = _sanitize_name(name)
    serial = _resolve_serial(name)
    return {"serial": serial, "properties": _telephony_snapshot(serial)}


@app.put("/instances/{name}/telephony")
def put_instance_telephony(name: str, body: TelephonyBody) -> dict[str, Any]:
    _require_bin("adb")
    name = _sanitize_name(name)
    serial = _resolve_serial(name)
    result = _apply_telephony_soft(serial, body)
    if result["errors"]:
        raise HTTPException(
            status_code=400,
            detail="Algumas props falharam: " + "; ".join(result["errors"]),
        )
    return {"status": "ok", **result}


@app.post("/instances/{name}/sms/inject")
def post_instance_sms_inject(name: str, body: SmsInjectBody) -> dict[str, Any]:
    _require_bin("adb")
    name = _sanitize_name(name)
    serial = _resolve_serial(name)
    meta = _inject_sms(name, serial, body.from_number, body.body)
    return {"status": "ok", "serial": serial, **meta}


def main() -> None:
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=API_PORT, reload=False)


if __name__ == "__main__":
    main()
