#!/usr/bin/env python3
"""Alimenta o HAL de câmera vsock do Cuttlefish com quadros de um dispositivo V4L2 (webcam, loopback do Deep-Live-Cam).

Protocolo do HAL (device/google/cuttlefish/guest/hals/camera): cada mensagem é um int32 com o tamanho
seguido dos bytes. O cliente manda primeiro um JSON {width, height, frame_rate} e depois quadros I420.
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import subprocess
import sys
import threading
import time


def send_message(sock: socket.socket, data: bytes) -> None:
    sock.sendall(struct.pack("<i", len(data)) + data)


def read_events(sock: socket.socket, state: dict) -> None:
    try:
        while True:
            header = sock.recv(4, socket.MSG_WAITALL)
            if len(header) < 4:
                break
            (size,) = struct.unpack("<i", header)
            body = sock.recv(size, socket.MSG_WAITALL) if size > 0 else b""
            try:
                event = json.loads(body.decode()).get("event", "")
            except ValueError:
                continue
            print(f"[vsock-camera] evento: {event}", flush=True)
            if event == "VIRTUAL_DEVICE_START_CAMERA_SESSION":
                state["active"] = True
            elif event == "VIRTUAL_DEVICE_STOP_CAMERA_SESSION":
                state["active"] = False
            elif event == "VIRTUAL_DEVICE_CAPTURE_IMAGE":
                state["capture"] = True
    except OSError:
        pass
    state["closed"] = True


def start_ffmpeg(device: str, width: int, height: int, fps: int, fmt: str) -> subprocess.Popen:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if device.startswith("lavfi:"):
        cmd += ["-f", "lavfi", "-i", device[len("lavfi:"):]]
    else:
        cmd += ["-f", "v4l2", "-thread_queue_size", "512"]
        if fmt:
            cmd += ["-input_format", fmt]
        cmd += ["-i", device]
    cmd += ["-vf", f"scale={width}:{height},fps={fps}", "-pix_fmt", "yuv420p", "-f", "rawvideo", "pipe:1"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE)


def encode_jpeg(frame: bytes, width: int, height: int) -> bytes:
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "yuv420p",
         "-s", f"{width}x{height}", "-i", "pipe:0", "-frames:v", "1", "-f", "mjpeg", "pipe:1"],
        input=frame, capture_output=True, check=False,
    )
    return p.stdout


def connect(cid: int, port: int, retry_s: float) -> socket.socket:
    while True:
        try:
            sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            sock.connect((cid, port))
            return sock
        except OSError as exc:
            print(f"[vsock-camera] aguardando HAL em cid={cid} porta={port}: {exc}", flush=True)
            time.sleep(retry_s)


def read_source(args: argparse.Namespace) -> str:
    if args.source_file:
        try:
            text = open(args.source_file, encoding="utf-8").read().strip()
        except OSError:
            text = ""
        if text:
            return text
    return args.device


def run(args: argparse.Namespace) -> None:
    frame_size = args.width * args.height * 3 // 2
    while True:
        sock = connect(args.cid, args.port, 3.0)
        print(f"[vsock-camera] conectado cid={args.cid} porta={args.port}", flush=True)
        ff = None
        try:
            send_message(sock, json.dumps({"width": args.width, "height": args.height, "frame_rate": float(args.fps)}).encode())
            # Uma sessão já aberta no Android antes desta conexão não reenvia START_CAMERA_SESSION.
            state = {"active": True, "capture": False, "closed": False}
            threading.Thread(target=read_events, args=(sock, state), daemon=True).start()
            # Reconectar faz o HAL remover a câmera e congela o app aberto; trocar de fonte só reinicia o ffmpeg.
            device = read_source(args)
            checked = time.time()
            while not state["closed"]:
                if time.time() - checked > 1:
                    checked = time.time()
                    new_device = read_source(args)
                    if new_device != device:
                        print(f"[vsock-camera] fonte: {new_device}", flush=True)
                        device = new_device
                        if ff:
                            ff.kill()
                            ff = None
                if not state["active"]:
                    if ff:
                        ff.kill()
                        ff = None
                    time.sleep(0.1)
                    continue
                if ff is None:
                    ff = start_ffmpeg(device, args.width, args.height, args.fps, args.input_format)
                frame = ff.stdout.read(frame_size)
                if len(frame) < frame_size:
                    print("[vsock-camera] fonte sem quadros; tentando de novo em 3 s", flush=True)
                    ff.kill()
                    ff = None
                    time.sleep(3)
                    continue
                send_message(sock, frame)
                if state["capture"]:
                    state["capture"] = False
                    jpeg = encode_jpeg(frame, args.width, args.height)
                    if jpeg:
                        send_message(sock, jpeg)
        except OSError as exc:
            print(f"[vsock-camera] conexão caiu: {exc}", flush=True)
        finally:
            if ff:
                ff.kill()
            sock.close()
        time.sleep(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cid", type=int, required=True, help="CID vsock da VM (cvd-N usa 2+N)")
    ap.add_argument("--port", type=int, default=7700)
    ap.add_argument("--device", default="/dev/video14", help="dispositivo V4L2 ou lavfi:testsrc=...")
    ap.add_argument("--source-file", default="", help="arquivo com a fonte atual; relido a cada segundo")
    ap.add_argument("--input-format", default="", help="formato V4L2 de entrada (ex.: yuyv422)")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=30)
    try:
        run(ap.parse_args())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
