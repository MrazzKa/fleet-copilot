"""Bridge to a real AgOpenWeb (AgOpenGPS) guidance stack, the "digital twin" of tractor T1.

Protocol (from AgOpenWeb's RemoteServer source: WebSocketHub.cs / WireCodec.cs / ControlAuthority.cs):
  * ws://<host>:5174/ws ; server->client frames are binary: [u8 type][payload], little-endian.
      7 Hello        : str clientId (i32 len + utf8), u8 features
      6 ControlState : u8 held, str holderId, str holderName
      2 Tick (~10Hz) : i64 sceneVersion, f64 E, f64 N, f32 heading(rad), f32 speed, u8 fix, ...
  * client->server commands are text frames "id" or "id|arg":
      control.acquire|name, control.presence (dead-man: must refresh < 1.5 s), control.release
      sim.toggleEnable, sim.speedUp (+0.5 kph), sim.speedDown, sim.stop, sim.setSteer|deg, sim.reset,
      sim.setCoords|lat,lon : moves and stops the vehicle; E/N stay relative to AgOpenWeb's local origin.
        Note: the next sim.speedUp from standstill restarts the simulator AT that local origin, so the live
        vehicle drives around the origin (set by the first coordinates AgOpenWeb received).
  * The FIRST client to connect holds control; later clients only observe.
"""
from __future__ import annotations

import collections
import math
import queue
import struct
import threading
import time

TICK, CONTROL, HELLO = 2, 6, 7


def _str(b: bytes, off: int) -> tuple[str, int]:
    n = struct.unpack_from("<i", b, off)[0]
    off += 4
    return b[off:off + n].decode("utf-8", "replace"), off + n


class AgOpenWebBridge:
    def __init__(self, url: str, tractor: str = "T1", name: str = "Fleet Copilot"):
        self.url = url
        self.tractor = tractor
        self.name = name
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.connected = False
        self.ever_connected = False
        self.client_id = ""
        self.holder = ""
        self.has_control = False
        self.pose = None  # (E, N, heading, speed)
        self.fix = 0
        self.ticks = 0
        self.last_tick = 0.0
        self.disconnected_at = None
        self.trail: collections.deque = collections.deque(maxlen=600)
        self.dist_m = 0.0
        self.sent: collections.deque = collections.deque(maxlen=30)
        self.error = None
        self.home = None  # (lat, lon) where we place the vehicle on connect / reset (real-farm scenarios)
        self.anchor = None  # [lat, lon, e0, n0]: the E/N reported right after placing the vehicle at (lat, lon)
        self._need_place = False
        self._anchor_due = None
        self.place_seq = 0  # bumps each time we send setCoords (it also stops the vehicle)
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    # ------------------------------------------------------------------ public
    def send(self, *cmds: str):
        for c in cmds:
            self.q.put(c)

    def place(self, lat: float, lon: float):
        """Put the AgOpenWeb vehicle at a real lat/lon (sent as soon as we hold control)."""
        self.home = (round(lat, 7), round(lon, 7))
        self._need_place = True

    def speed(self) -> float:
        return abs(self.pose[3]) if self.pose else 0.0

    def fresh(self, max_age: float = 2.0) -> bool:
        return self.connected and (time.time() - self.last_tick) < max_age

    def snapshot(self) -> dict:
        with self.lock:
            p = self.pose
            return {"url": self.url, "tractor": self.tractor, "connected": self.connected, "has_control": self.has_control,
                    "holder": self.holder, "ticks": self.ticks, "fresh": self.fresh(),
                    "pose": None if p is None else {"e": round(p[0], 2), "n": round(p[1], 2), "heading_deg": round(math.degrees(p[2]) % 360, 1),
                                                    "speed": round(p[3], 2)},
                    "fix": self.fix, "dist_m": round(self.dist_m, 1), "trail": list(self.trail)[-300:],
                    "sent": list(self.sent)[-8:], "error": self.error, "home": self.home, "anchor": self.anchor}

    # ------------------------------------------------------------------ loop
    def _run(self):
        try:
            from websockets.sync.client import connect
        except Exception as e:  # noqa: BLE001
            self.error = f"websockets package missing: {e}"
            return
        while not self._stop:
            try:
                with connect(self.url, open_timeout=3, max_size=None, ping_interval=None) as ws:
                    self.connected = self.ever_connected = True
                    self.disconnected_at = None
                    self.error = None
                    ws.send(f"control.acquire|{self.name}")
                    self._need_place = self.home is not None  # re-place after every (re)connect
                    last_presence = last_acquire = 0.0
                    while not self._stop:
                        try:
                            msg = ws.recv(timeout=0.2)
                            if isinstance(msg, (bytes, bytearray)):
                                self._parse(bytes(msg))
                        except TimeoutError:
                            pass
                        now = time.time()
                        if now - last_presence > 0.5:
                            ws.send("control.presence")
                            last_presence = now
                        if not self.has_control and not self.holder and now - last_acquire > 1.0:
                            ws.send(f"control.acquire|{self.name}")  # control is free: take it back
                            last_acquire = now
                        if self._need_place and self.has_control and self.home:
                            c = f"sim.setCoords|{self.home[0]},{self.home[1]}"
                            ws.send(c)
                            self.sent.append({"t": time.strftime("%H:%M:%S"), "cmd": c})
                            self._need_place, self._anchor_due = False, time.time() + 0.5
                            self.place_seq += 1
                        if self._anchor_due and now - self._anchor_due > 2.0:
                            self._anchor_due = None  # no tick arrived, stop holding commands
                        while not self.q.empty() and not self._anchor_due:  # hold drive commands until the anchor is read
                            c = self.q.get_nowait()
                            ws.send(c)
                            self.sent.append({"t": time.strftime("%H:%M:%S"), "cmd": c})
            except Exception as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {str(e)[:120]}"
            if self.connected or self.disconnected_at is None:
                self.disconnected_at = time.time()
            self.connected = False
            self.has_control = False
            time.sleep(2.0)

    def _parse(self, b: bytes):
        t = b[0]
        try:
            if t == HELLO:
                self.client_id, _ = _str(b, 1)
            elif t == CONTROL:
                held = b[1] == 1
                hid, off = _str(b, 2)
                name, _ = _str(b, off)
                self.holder = name if held else ""
                self.has_control = held and hid == self.client_id
            elif t == TICK and len(b) >= 34:
                _, e, n, hd, sp, fix = struct.unpack_from("<qddffB", b, 1)
                with self.lock:
                    if self.pose is not None:
                        d = math.hypot(e - self.pose[0], n - self.pose[1])
                        if d < 50:
                            self.dist_m += d
                        else:
                            self.trail.clear()  # teleport (setCoords, or AgOpenWeb's sim restarting at its local origin)
                    if not self.trail or math.hypot(e - self.trail[-1][0], n - self.trail[-1][1]) > 0.3:
                        self.trail.append((round(e, 2), round(n, 2)))
                    self.pose = (e, n, hd, sp)
                    if self._anchor_due and time.time() >= self._anchor_due and self.home:
                        self.anchor = [self.home[0], self.home[1], round(e, 2), round(n, 2)]
                        self._anchor_due = None
                        self.trail.clear()
                    self.fix = fix
                    self.ticks += 1
                    self.last_tick = time.time()
        except Exception as e:  # noqa: BLE001
            self.error = f"parse type {t}: {e}"
