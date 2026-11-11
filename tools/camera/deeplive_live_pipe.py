#!/usr/bin/env python3
"""
Face-swap ao vivo (Deep-Live-Cam) → ffmpeg → v4l2loopback.

Execute com o Python/venv do Deep-Live-Cam e REDROID_DEEPLIVE_ROOT apontando para o clone:

  REDROID_DEEPLIVE_ROOT=~/Deep-Live-Cam \\
  ~/Deep-Live-Cam/venv/bin/python3 tools/camera/deeplive_live_pipe.py \\
    --source /caminho/rosto.jpg --input /dev/video0 --output-v4l2 /dev/video12

Requer modelos em $REDROID_DEEPLIVE_ROOT/models/ (inswapper_128_fp16.onnx, etc.).
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _setup_dlc_root(root: Path) -> None:
    root = root.resolve()
    if not (root / "modules").is_dir():
        raise SystemExit(f"REDROID_DEEPLIVE_ROOT inválido (sem modules/): {root}")
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def main() -> None:
    ap = argparse.ArgumentParser(description="Deep-Live-Cam → v4l2loopback")
    ap.add_argument("--source", required=True, help="Imagem do rosto (source face)")
    ap.add_argument("--input", default="/dev/video0", help="Webcam de entrada (/dev/videoN)")
    ap.add_argument("--output-v4l2", required=True, help="Loopback de saída (/dev/videoN)")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument(
        "--execution-provider",
        default=os.environ.get("REDROID_DEEPLIVE_PROVIDER", "cuda"),
        help="cpu | cuda | coreml | … (mesmo do run.py)",
    )
    ap.add_argument("--mouth-mask", action="store_true")
    args = ap.parse_args()

    root = Path(os.environ.get("REDROID_DEEPLIVE_ROOT", ".")).expanduser()
    _setup_dlc_root(root)

    source = Path(args.source).expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"Source face não encontrada: {source}")

    import modules.globals as g  # noqa: E402
    from modules.processors.frame.core import get_frame_processors_modules  # noqa: E402
    from modules.face_analyser import get_one_face, detect_one_face_fast  # noqa: E402
    from modules import imread_unicode  # noqa: E402
    import cv2  # noqa: E402
    import numpy as np  # noqa: E402

    g.source_path = str(source)
    g.frame_processors = ["face_swapper"]
    g.mouth_mask = args.mouth_mask
    g.many_faces = False
    g.map_faces = False
    g.live_mirror = True
    from modules.core import decode_execution_providers  # noqa: E402

    g.execution_providers = decode_execution_providers([args.execution_provider])
    g.execution_threads = 2 if args.execution_provider == "cuda" else 4

    print(f"[DLC] carregando processadores ({args.execution_provider})…", flush=True)
    processors = get_frame_processors_modules(g.frame_processors)
    for mod in processors:
        if not mod.pre_check():
            raise SystemExit(f"Pré-check falhou: {mod.NAME}")
        if not mod.pre_start():
            raise SystemExit(f"pre_start falhou: {mod.NAME}")

    source_face = get_one_face(imread_unicode(g.source_path))
    if source_face is None:
        print("[DLC] sem rosto na source; passando o vídeo direto (sem face-swap).", flush=True)
        source_face = None
    else:
        print("[DLC] source face ok", flush=True)

    cpu = args.execution_provider.lower() == "cpu"
    proc_w, proc_h = (640, 360) if cpu else (args.width, args.height)
    out_w, out_h = args.width, args.height
    fps = 8 if cpu else args.fps
    det_interval = 8 if cpu else max(1, round(args.fps * 0.08))

    in_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "v4l2", "-thread_queue_size", "512", "-i", args.input,
        "-vf", f"scale={proc_w}:{proc_h}", "-pix_fmt", "bgr24",
        "-f", "rawvideo", "pipe:1",
    ]
    cap = subprocess.Popen(in_cmd, stdout=subprocess.PIPE, stderr=sys.stderr)
    time.sleep(0.4)
    if cap.poll() is not None:
        raise SystemExit(f"ffmpeg não abriu {args.input}")

    ff_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{out_w}x{out_h}", "-r", str(fps), "-i", "pipe:0",
        "-vf", f"scale={out_w}:{out_h}",
        "-pix_fmt", "yuyv422",
        "-r", str(fps),
        "-f", "v4l2", args.output_v4l2,
    ]
    ff = subprocess.Popen(ff_cmd, stdin=subprocess.PIPE, stderr=sys.stderr)
    time.sleep(0.3)
    if ff.poll() is not None:
        raise SystemExit(f"ffmpeg não abriu {args.output_v4l2} (HAL pode estar com o device aberto)")

    print(
        f"[DLC] {args.input} {proc_w}x{proc_h} → swap → {args.output_v4l2} {out_w}x{out_h} yuyv422",
        flush=True,
    )

    stop = False

    def _sig(_n: int, _f: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    frame_size = proc_w * proc_h * 3
    det_count = 0
    cached_target = None
    wrote = 0
    last_wrote = time.monotonic()

    def _watchdog() -> None:
        while not stop:
            time.sleep(5)
            quiet = time.monotonic() - last_wrote
            if quiet > 8:
                print(f"[DLC] STALL: {quiet:.0f}s sem frame (entrada parou? Iriun?)", flush=True)

    import threading

    threading.Thread(target=_watchdog, daemon=True).start()

    try:
        while not stop:
            if cap.poll() is not None:
                raise SystemExit("ffmpeg captura encerrou")
            if ff.poll() is not None:
                raise SystemExit("ffmpeg sink encerrou (v4l2 recusou o formato?)")
            t0 = time.monotonic()
            raw = cap.stdout.read(frame_size) if cap.stdout else b""
            t_read = time.monotonic() - t0
            if not raw or len(raw) < frame_size:
                time.sleep(0.01)
                continue
            t1 = time.monotonic()
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((proc_h, proc_w, 3)).copy()
            temp = cv2.flip(frame, 1)
            if source_face is not None:
                det_count += 1
                if cached_target is None or det_count % det_interval == 0:
                    cached_target = detect_one_face_fast(temp)
                for processor in processors:
                    temp = processor.process_frame(source_face, temp, cached_target)
            if temp.shape[1] != out_w or temp.shape[0] != out_h:
                temp = cv2.resize(temp, (out_w, out_h))
            if not temp.flags["C_CONTIGUOUS"]:
                temp = np.ascontiguousarray(temp)
            t_proc = time.monotonic() - t1
            try:
                ff.stdin.write(temp.tobytes())
                ff.stdin.flush()
            except BrokenPipeError:
                raise SystemExit("ffmpeg sink quebrou (BrokenPipe)")
            t_write = time.monotonic() - t1 - t_proc
            wrote += 1
            last_wrote = time.monotonic()
            if wrote <= 5 or wrote % 15 == 0:
                print(
                    f"[DLC] frames={wrote} read={t_read*1000:.0f}ms "
                    f"proc={t_proc*1000:.0f}ms write={t_write*1000:.0f}ms",
                    flush=True,
                )
    finally:
        if cap.poll() is None:
            cap.terminate()
        if ff.stdin:
            try:
                ff.stdin.close()
            except Exception:
                pass
        if ff.poll() is None:
            try:
                ff.wait(timeout=5)
            except subprocess.TimeoutExpired:
                ff.kill()
        if cap.poll() is None:
            cap.kill()


if __name__ == "__main__":
    main()
