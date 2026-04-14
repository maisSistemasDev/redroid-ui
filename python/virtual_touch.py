"""
Cria um dispositivo de touchscreen virtual dentro de um contêiner redroid via /dev/uinput.
Isso faz com que apps como o WhatsApp reconheçam o dispositivo como tendo touch real.
"""
from __future__ import annotations

import ctypes
import fcntl
import os
import struct
import time

# ioctl numbers (linux/uinput.h)
UINPUT_MAX_NAME_SIZE = 80

# from linux/input.h
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
SYN_REPORT = 0x00
BTN_TOUCH = 0x14A
ABS_MT_SLOT = 0x2F
ABS_MT_TRACKING_ID = 0x39
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TOUCH_MAJOR = 0x30
ABS_MT_PRESSURE = 0x3A
ABS_X = 0x00
ABS_Y = 0x01
INPUT_PROP_DIRECT = 0x01

# ioctl codes
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567
UI_SET_PROPBIT = 0x4004556E
UI_DEV_SETUP = 0x405C5503
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502


def _pack_uinput_setup(name: str, id_bustype: int = 0x03, id_vendor: int = 0x1234,
                        id_product: int = 0x5678, id_version: int = 1,
                        ff_effects_max: int = 0) -> bytes:
    name_bytes = name.encode("utf-8")[:UINPUT_MAX_NAME_SIZE - 1]
    name_bytes = name_bytes + b"\x00" * (UINPUT_MAX_NAME_SIZE - len(name_bytes))
    # struct uinput_setup { struct input_id id; char name[80]; uint32_t ff_effects_max; }
    # struct input_id { uint16_t bustype, vendor, product, version; }
    return struct.pack(
        f"HHHH{UINPUT_MAX_NAME_SIZE}sI",
        id_bustype, id_vendor, id_product, id_version,
        name_bytes,
        ff_effects_max,
    )


def _pack_uinput_abs_setup(code: int, minimum: int = 0, maximum: int = 0,
                            fuzz: int = 0, flat: int = 0, resolution: int = 0) -> bytes:
    # struct uinput_abs_setup { uint16_t code; struct input_absinfo absinfo; }
    # struct input_absinfo { int32_t value, minimum, maximum, fuzz, flat, resolution; }
    return struct.pack("HiiiiII", code, 0, minimum, maximum, fuzz, flat, resolution)


UI_ABS_SETUP = 0x405C5504


def create_virtual_touchscreen(
    uinput_path: str = "/dev/uinput",
    width: int = 720,
    height: int = 1280,
    max_slots: int = 10,
) -> int:
    """
    Abre /dev/uinput e cria um dispositivo de touchscreen multitouch.
    Retorna o fd (manter aberto para o device existir).
    """
    fd = os.open(uinput_path, os.O_WRONLY | os.O_NONBLOCK)

    # Tipos de evento
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_SYN)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)

    # Botão de touch
    fcntl.ioctl(fd, UI_SET_KEYBIT, BTN_TOUCH)

    # Propriedade: dispositivo direto (touchscreen, não touchpad)
    fcntl.ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)

    # Eixos absolutos
    for code in (ABS_MT_SLOT, ABS_MT_TRACKING_ID, ABS_MT_POSITION_X,
                 ABS_MT_POSITION_Y, ABS_MT_TOUCH_MAJOR, ABS_MT_PRESSURE,
                 ABS_X, ABS_Y):
        fcntl.ioctl(fd, UI_SET_ABSBIT, code)

    # Configurar eixos
    abs_configs = [
        (ABS_X, 0, width - 1, 0, 0, 0),
        (ABS_Y, 0, height - 1, 0, 0, 0),
        (ABS_MT_POSITION_X, 0, width - 1, 0, 0, 0),
        (ABS_MT_POSITION_Y, 0, height - 1, 0, 0, 0),
        (ABS_MT_SLOT, 0, max_slots - 1, 0, 0, 0),
        (ABS_MT_TRACKING_ID, 0, 65535, 0, 0, 0),
        (ABS_MT_TOUCH_MAJOR, 0, 255, 0, 0, 0),
        (ABS_MT_PRESSURE, 0, 255, 0, 0, 0),
    ]
    for code, mn, mx, fuzz, flat, res in abs_configs:
        buf = _pack_uinput_abs_setup(code, mn, mx, fuzz, flat, res)
        fcntl.ioctl(fd, UI_ABS_SETUP, buf)

    # Setup do dispositivo
    setup = _pack_uinput_setup("redroid-virtual-touch")
    fcntl.ioctl(fd, UI_DEV_SETUP, setup)

    # Criar dispositivo
    fcntl.ioctl(fd, UI_DEV_CREATE)
    time.sleep(0.3)  # Esperar o kernel registar o device

    return fd


def destroy_virtual_touchscreen(fd: int) -> None:
    try:
        fcntl.ioctl(fd, UI_DEV_DESTROY)
    except OSError:
        pass
    os.close(fd)


if __name__ == "__main__":
    print("Criando touchscreen virtual...")
    fd = create_virtual_touchscreen()
    print(f"Dispositivo criado (fd={fd}). Pressione Ctrl+C para destruir.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        destroy_virtual_touchscreen(fd)
        print("Dispositivo destruído.")
