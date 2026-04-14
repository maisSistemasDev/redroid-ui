from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex

from channels.generic.websocket import AsyncWebsocketConsumer
from rest_framework_simplejwt.tokens import AccessToken

from .models import Instance
from .services import (
    EV_SYN, EV_KEY, EV_ABS,
    SYN_REPORT,
    ABS_X, ABS_Y,
    ABS_MT_SLOT, ABS_MT_TOUCH_MAJOR, ABS_MT_TRACKING_ID,
    ABS_MT_POSITION_X, ABS_MT_POSITION_Y,
    ABS_MT_PRESSURE,
    BTN_TOUCH,
    AdbService,
    clamp_coords,
    inject_events, inject_keyboard_events, cmd_send, cmd_send_direct, cmd_send_best_effort,
    cmd_send_root,
)

logger = logging.getLogger(__name__)
FORCE_ADB_INPUT = os.getenv("REDROID_FORCE_ADB_INPUT", "0").lower() in {"1", "true", "yes", "on"}
STREAM_SIZE = os.getenv("REDROID_STREAM_SIZE", "540x960")
STREAM_BITRATE = os.getenv("REDROID_STREAM_BITRATE", "2500000")

# Linux input event keycodes for keyboard
KEY_A = 30
KEY_B = 48
KEY_C = 46
KEY_D = 32
KEY_E = 18
KEY_F = 33
KEY_G = 34
KEY_H = 35
KEY_I = 23
KEY_J = 36
KEY_K = 37
KEY_L = 38
KEY_M = 50
KEY_N = 49
KEY_O = 24
KEY_P = 25
KEY_Q = 16
KEY_R = 19
KEY_S = 31
KEY_T = 20
KEY_U = 22
KEY_V = 47
KEY_W = 17
KEY_X = 45
KEY_Y = 21
KEY_Z = 44

KEY_0 = 11
KEY_1 = 2
KEY_2 = 3
KEY_3 = 4
KEY_4 = 5
KEY_5 = 6
KEY_6 = 7
KEY_7 = 8
KEY_8 = 9
KEY_9 = 10

KEY_SPACE = 57
KEY_MINUS = 12
KEY_EQUAL = 13
KEY_BACKSPACE = 14
KEY_TAB = 15
KEY_LEFTBRACE = 26
KEY_RIGHTBRACE = 27
KEY_ENTER = 28
KEY_SEMICOLON = 39
KEY_APOSTROPHE = 40
KEY_GRAVE = 41
KEY_BACKSLASH = 43
KEY_COMMA = 51
KEY_DOT = 52
KEY_SLASH = 53
KEY_LSHIFT = 42
KEY_RSHIFT = 54

# Map ASCII characters to Linux keycodes (unshifted)
ASCII_TO_KEYCODE = {
    ' ': KEY_SPACE,
    '0': KEY_0, '1': KEY_1, '2': KEY_2, '3': KEY_3, '4': KEY_4,
    '5': KEY_5, '6': KEY_6, '7': KEY_7, '8': KEY_8, '9': KEY_9,
    'a': KEY_A, 'b': KEY_B, 'c': KEY_C, 'd': KEY_D, 'e': KEY_E,
    'f': KEY_F, 'g': KEY_G, 'h': KEY_H, 'i': KEY_I, 'j': KEY_J,
    'k': KEY_K, 'l': KEY_L, 'm': KEY_M, 'n': KEY_N, 'o': KEY_O,
    'p': KEY_P, 'q': KEY_Q, 'r': KEY_R, 's': KEY_S, 't': KEY_T,
    'u': KEY_U, 'v': KEY_V, 'w': KEY_W, 'x': KEY_X, 'y': KEY_Y,
    'z': KEY_Z,
    '-': KEY_MINUS, '=': KEY_EQUAL, '[': KEY_LEFTBRACE, ']': KEY_RIGHTBRACE,
    ';': KEY_SEMICOLON, "'": KEY_APOSTROPHE, '`': KEY_GRAVE, '\\': KEY_BACKSLASH,
    ',': KEY_COMMA, '.': KEY_DOT, '/': KEY_SLASH,
}


def _validate_ws_token(query_string: bytes) -> tuple[int, int] | None:
    qs = query_string.decode()
    params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
    raw_token = params.get("token")
    if not raw_token:
        return None
    try:
        token = AccessToken(raw_token)
        return int(token["user_id"]), int(token["instance_id"])
    except Exception:
        return None


def _validate_user_token(query_string: bytes) -> int | None:
    qs = query_string.decode()
    params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
    raw_token = params.get("token")
    if not raw_token:
        return None
    try:
        token = AccessToken(raw_token)
        return int(token["user_id"])
    except Exception:
        return None


class JobsConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user_id = _validate_user_token(self.scope.get("query_string", b""))
        if not user_id:
            await self.close()
            return
        self.user_id = user_id
        self.group_name = f"jobs.user.{self.user_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        group_name = getattr(self, "group_name", "")
        if group_name:
            await self.channel_layer.group_discard(group_name, self.channel_name)

    async def job_message(self, event):
        payload = event.get("payload", {})
        await self.send(text_data=json.dumps(payload))


class ScreenConsumer(AsyncWebsocketConsumer):
    proc = None
    _stream_task = None

    async def connect(self):
        self.instance_id = self.scope["url_route"]["kwargs"]["instance_id"]
        auth = _validate_ws_token(self.scope.get("query_string", b""))
        if not auth or auth[1] != self.instance_id:
            await self.close()
            return

        try:
            instance = await asyncio.to_thread(Instance.objects.get, id=self.instance_id)
        except Instance.DoesNotExist:
            await self.close()
            return

        await self.accept()

        serial = instance.serial
        try:
            await asyncio.to_thread(AdbService.connect, serial)
            booted = await asyncio.wait_for(
                asyncio.to_thread(AdbService.wait_boot, serial, 15), timeout=20
            )
            if not booted:
                await self.send(bytes_data=b"")
                await self.close(code=4001)
                return
            # Dismiss system overlays that block touch in security-sensitive
            # apps like WhatsApp (filterTouchesWhenObscured).
            await asyncio.to_thread(AdbService.dismiss_overlays, serial)
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning("ScreenConsumer: ADB connect failed for %s: %s", serial, e)
            await self.close(code=4002)
            return

        try:
            self.proc = await asyncio.create_subprocess_exec(
                "adb", "-s", serial, "exec-out",
                "screenrecord", "--output-format=h264",
                "--size", STREAM_SIZE, "--bit-rate", STREAM_BITRATE, "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as e:
            logger.warning("ScreenConsumer: screenrecord failed for %s: %s", serial, e)
            await self.close(code=4003)
            return

        self._stream_task = asyncio.ensure_future(self._stream())

    async def _stream(self):
        try:
            while True:
                chunk = await self.proc.stdout.read(65536)
                if not chunk:
                    break
                try:
                    await self.send(bytes_data=chunk)
                except Exception:
                    break
        finally:
            await self._kill_proc()
            try:
                await self.close()
            except Exception:
                pass

    async def disconnect(self, code):
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
        await self._kill_proc()

    async def _kill_proc(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                self.proc.kill()
            self.proc = None


class InputConsumer(AsyncWebsocketConsumer):
    serial = None
    dev = None
    keyboard_dev = None
    tracking_id = 0
    fallback_touch_mode = False
    fallback_down_xy: tuple[int, int] | None = None
    fallback_last_xy: tuple[int, int] | None = None
    # Deferred tap: buffer "down" and only commit the injection method on
    # "move" (native drag) or "up" (ADB tap).  This lets simple taps go
    # through ``input tap`` which is accepted by all apps (including
    # WhatsApp with filterTouchesWhenObscured), while drags/scrolls keep
    # using the low-latency native uinput path.
    _pending_down: tuple[int, int] | None = None
    _had_move: bool = False

    async def connect(self):
        self.instance_id = self.scope["url_route"]["kwargs"]["instance_id"]
        auth = _validate_ws_token(self.scope.get("query_string", b""))
        if not auth or auth[1] != self.instance_id:
            await self.close()
            return

        try:
            instance = await asyncio.to_thread(Instance.objects.get, id=self.instance_id)
        except Instance.DoesNotExist:
            await self.close()
            return

        await self.accept()

        self.serial = instance.serial
        self.dev = "/dev/input/event18"
        try:
            await asyncio.to_thread(AdbService.connect, self.serial)
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning("InputConsumer: ADB connect failed for %s: %s", self.serial, e)
            await self.close(code=4002)
            return

        try:
            self.dev = await asyncio.wait_for(
                asyncio.to_thread(AdbService.detect_event_device, self.serial), timeout=10
            )
            logger.info("InputConsumer: detected touch device %s for %s", self.dev, self.serial)
            self.fallback_touch_mode = False
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(
                "InputConsumer: touch device detection failed for %s: %s; using ADB fallback mode",
                self.serial,
                e,
            )
            self.fallback_touch_mode = True

        # Also detect keyboard device (not critical if it fails)
        try:
            self.keyboard_dev = await asyncio.wait_for(
                asyncio.to_thread(AdbService.detect_keyboard_device, self.serial), timeout=10
            )
            logger.info("InputConsumer: detected keyboard device %s for %s", self.keyboard_dev, self.serial)
        except Exception as e:
            logger.warning("InputConsumer: keyboard device detection failed for %s: %s", self.serial, e)
            self.keyboard_dev = None

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data or not self.serial:
            return
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return
        evt_type = data.get("type", "")

        if evt_type == "down":
            self.tracking_id = (self.tracking_id + 1) % 65535
            ax, ay = clamp_coords(data["x"], data["y"])
            # Buffer the down position — we decide on "move" or "up"
            # whether to use native injection or ADB tap.
            self._pending_down = (ax, ay)
            self._had_move = False
            self.fallback_down_xy = (ax, ay)
            self.fallback_last_xy = (ax, ay)
            logger.debug("down: buffered at (%d, %d), tracking_id=%d", ax, ay, self.tracking_id)

        elif evt_type == "move":
            ax, ay = clamp_coords(data["x"], data["y"])
            self.fallback_last_xy = (ax, ay)

            if FORCE_ADB_INPUT or self.fallback_touch_mode:
                self._had_move = True
                if self.fallback_down_xy is None:
                    self.fallback_down_xy = (ax, ay)
                return

            # First move: flush the buffered "down" via native injection so
            # drags start immediately.
            if self._pending_down is not None:
                dx, dy = self._pending_down
                self._pending_down = None
                self._had_move = True
                logger.debug("move: first move after down, flushing native injection at (%d, %d)", dx, dy)
                ok = await asyncio.to_thread(inject_events, self.serial, self.dev, [
                    (EV_ABS, ABS_MT_SLOT, 0),
                    (EV_ABS, ABS_MT_TRACKING_ID, self.tracking_id),
                    (EV_ABS, ABS_X, dx),
                    (EV_ABS, ABS_Y, dy),
                    (EV_ABS, ABS_MT_POSITION_X, dx),
                    (EV_ABS, ABS_MT_POSITION_Y, dy),
                    (EV_ABS, ABS_MT_TOUCH_MAJOR, 5),
                    (EV_ABS, ABS_MT_PRESSURE, 50),
                    (EV_KEY, BTN_TOUCH, 1),
                    (EV_SYN, SYN_REPORT, 0),
                ])
                if not ok:
                    logger.warning("move: initial native inject failed, switching to fallback")
                    self.fallback_touch_mode = True
                    return

            self._had_move = True
            ok = await asyncio.to_thread(inject_events, self.serial, self.dev, [
                (EV_ABS, ABS_MT_SLOT, 0),
                (EV_ABS, ABS_X, ax),
                (EV_ABS, ABS_Y, ay),
                (EV_ABS, ABS_MT_POSITION_X, ax),
                (EV_ABS, ABS_MT_POSITION_Y, ay),
                (EV_ABS, ABS_MT_TOUCH_MAJOR, 5),
                (EV_ABS, ABS_MT_PRESSURE, 50),
                (EV_SYN, SYN_REPORT, 0),
            ])
            if not ok:
                logger.warning("move: native inject failed, switching to fallback touch mode")
                self.fallback_touch_mode = True

        elif evt_type == "up":
            start = self.fallback_down_xy or self.fallback_last_xy
            end = self.fallback_last_xy or start

            if self._pending_down is not None or not self._had_move or FORCE_ADB_INPUT or self.fallback_touch_mode:
                # ── TAP path: no significant movement detected ──
                # For WhatsApp input fields, use native event injection instead of
                # high-level "input tap" command, as it's more resistant to filterTouchesWhenObscured
                self._pending_down = None
                if start and end:
                    sx, sy = start
                    ex, ey = end

                    # Try native injection first (more reliable for sensitive apps)
                    if abs(ex - sx) <= 12 and abs(ey - sy) <= 12:
                        # When FORCE_ADB_INPUT, skip native inject (it returns
                        # True but events silently fail in docker environments)
                        # and go straight to cmd_send_root which works reliably.
                        if not (FORCE_ADB_INPUT or self.fallback_touch_mode):
                            # Real tap via native events
                            logger.info("tap: attempting native inject at (%d, %d) on device %s", ex, ey, self.dev)
                            ok = await asyncio.to_thread(inject_events, self.serial, self.dev, [
                                (EV_ABS, ABS_MT_SLOT, 0),
                                (EV_ABS, ABS_MT_TRACKING_ID, self.tracking_id),
                                (EV_ABS, ABS_X, ex),
                                (EV_ABS, ABS_Y, ey),
                                (EV_ABS, ABS_MT_POSITION_X, ex),
                                (EV_ABS, ABS_MT_POSITION_Y, ey),
                                (EV_ABS, ABS_MT_TOUCH_MAJOR, 5),
                                (EV_ABS, ABS_MT_PRESSURE, 50),
                                (EV_KEY, BTN_TOUCH, 1),
                                (EV_SYN, SYN_REPORT, 0),
                            ])
                            if ok:
                                logger.info("tap: native inject press succeeded, releasing touch")
                                # Release touch
                                await asyncio.sleep(0.05)
                                await asyncio.to_thread(inject_events, self.serial, self.dev, [
                                    (EV_ABS, ABS_MT_SLOT, 0),
                                    (EV_ABS, ABS_MT_TOUCH_MAJOR, 0),
                                    (EV_ABS, ABS_MT_PRESSURE, 0),
                                    (EV_ABS, ABS_MT_TRACKING_ID, -1),
                                    (EV_KEY, BTN_TOUCH, 0),
                                    (EV_SYN, SYN_REPORT, 0),
                                ])
                                logger.info("tap: native inject completed successfully")
                                return
                            # Fallback to input tap if native injection fails
                            logger.warning("tap: native inject failed, falling back to input tap command at (%d, %d)", ex, ey)
                            await asyncio.sleep(0.1)
                        ok = await asyncio.to_thread(cmd_send_root, self.serial, f"input tap {ex} {ey}")
                        if ok:
                            logger.info("tap: input tap succeeded at (%d, %d)", ex, ey)
                        else:
                            logger.error("tap: input tap failed at (%d, %d)", ex, ey)
                    else:
                        # Swipe
                        logger.info("swipe: detected movement from (%d, %d) to (%d, %d)", sx, sy, ex, ey)
                        await asyncio.to_thread(
                            cmd_send_root, self.serial,
                            f"input swipe {sx} {sy} {ex} {ey} 120",
                        )
            else:
                # ── DRAG path: had movement, complete native sequence ──
                ok = await asyncio.to_thread(inject_events, self.serial, self.dev, [
                    (EV_ABS, ABS_MT_SLOT, 0),
                    (EV_ABS, ABS_MT_TOUCH_MAJOR, 0),
                    (EV_ABS, ABS_MT_PRESSURE, 0),
                    (EV_ABS, ABS_MT_TRACKING_ID, -1),
                    (EV_KEY, BTN_TOUCH, 0),
                    (EV_SYN, SYN_REPORT, 0),
                ])
                if not ok and start and end:
                    sx, sy = start
                    ex, ey = end
                    await asyncio.to_thread(
                        cmd_send_direct, self.serial,
                        f"input swipe {sx} {sy} {ex} {ey} 120",
                    )

            self._pending_down = None
            self._had_move = False
            self.fallback_down_xy = None
            self.fallback_last_xy = None
            self.fallback_touch_mode = False

        elif evt_type == "scroll":
            ax, ay = clamp_coords(data["x"], data["y"])
            scroll_down = data.get("dir") == "down"
            if FORCE_ADB_INPUT or self.fallback_touch_mode:
                end_y = max(0, min(1279, ay + (420 if scroll_down else -420)))
                await asyncio.to_thread(cmd_send_root, self.serial, f"input swipe {ax} {ay} {ax} {end_y} 140")
                return
            step = 80
            dist = step if scroll_down else -step
            self.tracking_id = (self.tracking_id + 1) % 65535
            events = [
                (EV_ABS, ABS_MT_SLOT, 0),
                (EV_ABS, ABS_MT_TRACKING_ID, self.tracking_id),
                (EV_ABS, ABS_X, ax),
                (EV_ABS, ABS_Y, ay),
                (EV_ABS, ABS_MT_POSITION_X, ax),
                (EV_ABS, ABS_MT_POSITION_Y, ay),
                (EV_ABS, ABS_MT_TOUCH_MAJOR, 5),
                (EV_ABS, ABS_MT_PRESSURE, 50),
                (EV_KEY, BTN_TOUCH, 1),
                (EV_SYN, SYN_REPORT, 0),
            ]
            for i in range(1, 6):
                ny = max(0, min(1279, ay + dist * i))
                events.extend([
                    (EV_ABS, ABS_Y, ny),
                    (EV_ABS, ABS_MT_POSITION_Y, ny),
                    (EV_SYN, SYN_REPORT, 0),
                ])
            events.extend([
                (EV_ABS, ABS_MT_TOUCH_MAJOR, 0),
                (EV_ABS, ABS_MT_PRESSURE, 0),
                (EV_ABS, ABS_MT_TRACKING_ID, -1),
                (EV_KEY, BTN_TOUCH, 0),
                (EV_SYN, SYN_REPORT, 0),
            ])
            ok = await asyncio.to_thread(inject_events, self.serial, self.dev, events)
            if ok:
                return
            end_y = max(0, min(1279, ay + (420 if scroll_down else -420)))
            await asyncio.to_thread(cmd_send_root, self.serial, f"input swipe {ax} {ay} {ax} {end_y} 140")
        elif evt_type == "key":
            kc = int(data.get("keycode", 0))
            if kc:
                await asyncio.to_thread(cmd_send_root, self.serial, f"input keyevent {kc}")
        elif evt_type == "keycombo":
            modifier = int(data.get("modifier", 0))
            kc = int(data.get("keycode", 0))
            if modifier and kc:
                await asyncio.to_thread(
                    cmd_send_root, self.serial,
                    f"input keycombination {modifier} {kc}",
                )
        elif evt_type == "text":
            txt = data.get("text", "")
            if txt:
                # Add delay to ensure input field is focused (important for WhatsApp)
                await asyncio.sleep(0.3)

                # Try to use virtual keyboard if available (skip when
                # FORCE_ADB_INPUT — native inject silently fails in docker)
                if self.keyboard_dev and not (FORCE_ADB_INPUT or self.fallback_touch_mode):
                    logger.info("text: using virtual keyboard device %s for '%s'", self.keyboard_dev, txt)
                    for char in txt:
                        keycode = ASCII_TO_KEYCODE.get(char.lower())
                        if keycode:
                            # Send keypress + key release events
                            events = [
                                (EV_KEY, keycode, 1),  # Key press
                                (EV_SYN, SYN_REPORT, 0),
                            ]
                            await asyncio.sleep(0.05)
                            events.append((EV_KEY, keycode, 0))  # Key release
                            events.append((EV_SYN, SYN_REPORT, 0))
                            ok = await asyncio.to_thread(
                                inject_keyboard_events, self.serial, self.keyboard_dev, events
                            )
                            if not ok:
                                logger.warning("text: failed to inject keycode %d for char '%s'", keycode, char)
                        else:
                            # Unsupported character, log warning
                            logger.debug("text: unsupported char '%s', code=%s", char, ord(char))
                else:
                    # Fallback to input text command if keyboard device not available
                    logger.info("text: using input text command for '%s'", txt)
                    ok = await asyncio.to_thread(cmd_send_root, self.serial, f"input text {shlex.quote(txt)}")
                    if not ok:
                        logger.error("text: input text command failed for '%s' on %s", txt, self.serial)

    async def disconnect(self, code):
        pass
