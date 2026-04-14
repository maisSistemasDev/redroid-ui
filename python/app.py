"""
API para gerenciar contêineres re-droid via Docker e instalar APKs via ADB.
Integra redroid-script para construir imagens com GApps, ARM translation, Magisk, Widevine.
"""
from __future__ import annotations

import io
import json
import os
import re
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

_build_lock = threading.Lock()
_build_status: dict[str, Any] = {"running": False, "log": "", "last_result": None}

app = FastAPI(title="re-droid UI API", version="1.0.0")

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


def _container_to_instance(inspect_data: dict[str, Any]) -> dict[str, Any]:
    name = inspect_data.get("Name", "").lstrip("/")
    state = inspect_data.get("State", {}) or {}
    status = state.get("Status", "unknown")
    running = bool(state.get("Running"))
    cid = inspect_data.get("Id", "")[:12]
    image = inspect_data.get("Config", {}).get("Image", "")
    return {
        "id": cid,
        "name": name,
        "image": image,
        "status": status,
        "running": running,
    }


class CreateInstanceBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="Nome do contêiner Docker")
    image: str = Field(
        default="redroid/redroid:12.0.0_64only-latest",
        description="Imagem Docker re-droid",
    )
    gpu_mode: str = Field(default="guest", description="GPU mode: guest, host, auto")
    extra_props: str = Field(default="", description="Propriedades extras (uma por linha, formato chave=valor)")


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


@app.get("/health")
def health() -> dict[str, Any]:
    _require_docker_cli()
    return {"status": "ok", "docker_via_sudo": _docker_use_sudo()}


@app.get("/instances")
def list_instances() -> dict[str, list[dict[str, Any]]]:
    _require_docker_cli()
    out: list[dict[str, Any]] = []
    for cid in _docker_ids():
        try:
            ins = _inspect_container(cid)
            out.append(_container_to_instance(ins))
        except HTTPException:
            continue
    out.sort(key=lambda x: x.get("name") or "")
    return {"instances": out}


@app.post("/instances")
def create_instance(body: CreateInstanceBody) -> dict[str, Any]:
    _require_docker_cli()
    name = _sanitize_name(body.name.strip())
    data_dir = _data_root() / name
    data_dir.mkdir(parents=True, exist_ok=True)

    cmd = _docker_cmd() + [
        "run",
        "-d",
        "--name",
        name,
        "--label",
        "redroid-ui.managed=1",
        "--privileged",
        "-v",
        f"{data_dir}:/data",
        body.image,
        f"androidboot.redroid_gpu_mode={body.gpu_mode}",
    ]
    for line in body.extra_props.strip().splitlines():
        prop = line.strip()
        if prop and "=" in prop and re.match(r"^[a-zA-Z0-9_.]+=[^\s]*$", prop):
            cmd.append(prop)
    p = _run(cmd, timeout=300)
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        raise HTTPException(status_code=400, detail=err or "Falha ao criar contêiner.")
    cid = p.stdout.strip()
    _setup_virtual_touch(name)
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
    _require_docker_cli()
    name = _sanitize_name(name)
    p = _run(_docker_cmd() + ["start", name])
    if p.returncode != 0:
        raise HTTPException(status_code=400, detail=(p.stderr or p.stdout).strip())
    _setup_virtual_touch(name)
    ins = _inspect_container(name)
    return {"instance": _container_to_instance(ins)}


@app.post("/instances/{name}/stop")
def stop_instance(name: str) -> dict[str, Any]:
    _require_docker_cli()
    name = _sanitize_name(name)

    serial = f"{name}:5555"
    for store in (_injectors, _cmd_shells):
        proc = store.pop(serial, None)
        if proc and proc.poll() is None:
            proc.terminate()

    p = _run(_docker_cmd() + ["stop", name])
    if p.returncode != 0:
        raise HTTPException(status_code=400, detail=(p.stderr or p.stdout).strip())
    ins = _inspect_container(name)
    return {"instance": _container_to_instance(ins)}


@app.delete("/instances/{name}")
def remove_instance(name: str) -> dict[str, str]:
    _require_docker_cli()
    name = _sanitize_name(name)
    p = _run(_docker_cmd() + ["rm", "-f", name])
    if p.returncode != 0:
        raise HTTPException(status_code=400, detail=(p.stderr or p.stdout).strip())
    data_dir = _data_root() / name
    try:
        if data_dir.exists():
            shutil.rmtree(data_dir)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Contêiner removido, mas falhou ao apagar dados: {exc}")
    return {"status": "removed", "name": name}


VIEWER_HTML = Path(__file__).resolve().parent / "viewer.html"
API_PORT = 8765


def _resolve_serial(name: str) -> str:
    """Get ADB serial for a running instance, connecting if needed."""
    ins = _inspect_container(name)
    instance = _container_to_instance(ins)
    if not instance.get("running"):
        raise HTTPException(status_code=400, detail="Inicie a instância primeiro.")
    serial = f"{name}:5555"
    _run(["adb", "connect", serial], timeout=15)
    return serial


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

    for _ in range(15):
        try:
            w = _run(["adb", "-s", serial, "shell", "getprop", "sys.boot_completed"], timeout=5)
            if w.returncode == 0 and "1" in w.stdout:
                break
        except subprocess.TimeoutExpired:
            pass
        time.sleep(2)

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
    ins = _inspect_container(name)
    instance = _container_to_instance(ins)
    port = instance.get("host_port")
    if port is None:
        raise HTTPException(status_code=400, detail="Porta ADB não encontrada no contêiner.")
    if not instance.get("running"):
        raise HTTPException(status_code=400, detail="Inicie a instância antes de instalar o APK.")

    suffix = Path(file.filename or "app.apk").suffix
    if suffix.lower() != ".apk":
        suffix = ".apk"
    tmp = Path(tempfile.gettempdir()) / f"redroid-ui-{uuid.uuid4().hex}{suffix}"
    try:
        content = await file.read()
        tmp.write_bytes(content)
        serial = f"127.0.0.1:{port}"
        c1 = _run(["adb", "connect", serial], timeout=30)
        if c1.returncode != 0 and "connected" not in (c1.stdout + c1.stderr).lower():
            # adb connect às vezes retorna 0 mesmo com avisos; seguimos para install
            pass
        c2 = _run(["adb", "-s", serial, "install", "-r", str(tmp)], timeout=300)
        if c2.returncode != 0:
            msg = (c2.stderr or c2.stdout or "Falha no adb install").strip()
            raise HTTPException(status_code=400, detail=msg)
        return {"status": "installed", "filename": file.filename, "serial": serial}
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
    p = _run(_docker_cmd() + [
        "images", "--format", fmt,
        "--filter", "reference=redroid/*",
    ])
    if p.returncode != 0:
        raise HTTPException(status_code=500, detail=(p.stderr or "").strip())
    images: list[dict[str, str]] = []
    for line in p.stdout.strip().splitlines():
        parts = line.split(sep)
        if len(parts) >= 3:
            tag = parts[0].strip()
            if "<none>" in tag:
                continue
            images.append({"tag": tag, "id": parts[1].strip(), "size": parts[2].strip()})
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


def _patch_build_prop(content: str) -> str:
    """Rewrite a build.prop file content with spoofed Pixel 6 values."""
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


def _do_spoof(base_image: str) -> None:
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
                log(f"  (não encontrado, pulando)")
                continue

            original = raw.decode("utf-8", errors="replace")
            patched = _patch_build_prop(original)

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

        # Tag: adicionar -spoofed ao tag da imagem base
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


class SpoofImageBody(BaseModel):
    base_image: str = Field(..., description="Tag da imagem base para fazer spoof")


@app.post("/images/spoof")
def spoof_image(body: SpoofImageBody) -> dict[str, Any]:
    _require_docker_cli()
    if not re.match(r"^[a-zA-Z0-9:._/-]+$", body.base_image):
        raise HTTPException(status_code=400, detail="Tag de imagem inválida.")
    if _spoof_status["running"]:
        raise HTTPException(status_code=409, detail="Já existe um spoof em andamento.")

    with _spoof_lock:
        _spoof_status["running"] = True
        _spoof_status["log"] = ""
        _spoof_status["last_result"] = None

    t = threading.Thread(target=_do_spoof, args=(body.base_image,), daemon=True)
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

ANDROID_VERSIONS = ["14.0.0", "13.0.0", "12.0.0", "12.0.0_64only", "11.0.0", "10.0.0", "9.0.0", "8.1.0"]


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
        args = [sys.executable, "redroid.py", "-a", body.android_version]
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


def main() -> None:
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8765, reload=False)


if __name__ == "__main__":
    main()
