"""Integração opcional com Deep-Live-Cam (clone local + venv)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent.parent / "tools" / "camera"
PIPE_SCRIPT = TOOLS / "deeplive_live_pipe.py"


def resolve_root(cfg: dict[str, Any]) -> Path:
    raw = (cfg.get("deeplive_root") or os.environ.get("REDROID_DEEPLIVE_ROOT", "")).strip()
    return Path(raw).expanduser() if raw else Path()


def dlc_python(root: Path) -> Path | None:
    for name in ("python3", "python"):
        p = root / "venv" / "bin" / name
        if p.is_file():
            return p
    return None


def status(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or {}
    root = resolve_root(cfg)
    py = dlc_python(root) if root.is_dir() else None
    models = root / "models"
    inswapper = models / "inswapper_128_fp16.onnx"
    return {
        "configured": bool(root.is_dir() and py and PIPE_SCRIPT.is_file()),
        "root": str(root) if root else "",
        "python": str(py) if py else "",
        "pipe_script": str(PIPE_SCRIPT),
        "models_present": inswapper.is_file() or (models / "inswapper_128.onnx").is_file(),
        "hint": (
            "Clone https://github.com/hacksider/Deep-Live-Cam, instale venv+requirements, "
            "baixe modelos em models/, exporte REDROID_DEEPLIVE_ROOT=/caminho/Deep-Live-Cam"
        ),
    }


def build_live_cmd(
    cfg: dict[str, Any],
    *,
    source_face: Path,
    input_device: str,
    output_v4l2: str,
) -> list[str]:
    root = resolve_root(cfg)
    py = dlc_python(root)
    if not py:
        raise RuntimeError(
            "Deep-Live-Cam não encontrado. Defina REDROID_DEEPLIVE_ROOT ou deeplive_root na config."
        )
    if not PIPE_SCRIPT.is_file():
        raise RuntimeError(f"Script ausente: {PIPE_SCRIPT}")
    if not source_face.is_file():
        raise RuntimeError(f"Imagem do rosto (source) não encontrada: {source_face}")
    provider = (cfg.get("deeplive_execution_provider") or os.environ.get("REDROID_DEEPLIVE_PROVIDER") or "cuda").strip()
    cmd = [
        str(py),
        str(PIPE_SCRIPT),
        "--source",
        str(source_face.resolve()),
        "--input",
        input_device,
        "--output-v4l2",
        output_v4l2,
        "--execution-provider",
        provider,
    ]
    if cfg.get("deeplive_mouth_mask"):
        cmd.append("--mouth-mask")
    return cmd


def live_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["REDROID_DEEPLIVE_ROOT"] = str(root.resolve())
    env["PYTHONUNBUFFERED"] = "1"
    env["MPLBACKEND"] = "Agg"
    env["MPLCONFIGDIR"] = "/tmp/redroid-mpl-dlc"
    return env


def start_live_process(
    cmd: list[str], root: Path, log_path: Path | None = None
) -> subprocess.Popen[Any]:
    logf = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logf = open(log_path, "w", encoding="utf-8")
    return subprocess.Popen(
        cmd,
        cwd=str(root.resolve()),
        env=live_env(root),
        stdout=logf or subprocess.DEVNULL,
        stderr=subprocess.STDOUT if logf else subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def stop_process_group(proc: subprocess.Popen[Any] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        return
    try:
        os.killpg(pgid, 15)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
        try:
            os.killpg(pgid, 9)
        except OSError:
            pass
