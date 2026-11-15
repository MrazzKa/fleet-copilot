"""AgOpenWeb protocol EMULATOR (for testing the bridge without .NET). Speaks the same binary frames as
AgOpenWeb's RemoteServer (Hello / ControlState / Tick) and obeys sim.speedUp / speedDown / stop / setSteer
and the control dead-man. NOT the real app, use the real AgOpenWeb for the demo when possible.
  python scripts/fake_agopenweb.py 5174
"""
import asyncio
import math
import struct
import sys
import uuid

import websockets


def s(x: str) -> bytes:
    b = x.encode()
    return struct.pack("<i", len(b)) + b


STATE = {"speed": 0.0, "steer": 0.0, "e": 0.0, "n": 0.0, "h": 0.0, "holder": None, "presence": 0.0, "enabled": True}


async def handler(ws):
    cid = str(uuid.uuid4())
    await ws.send(bytes([7]) + s(cid) + bytes([0]))
    loop = asyncio.get_event_loop()
    if STATE["holder"] is None:
        STATE["holder"], STATE["presence"] = cid, loop.time()
    await ws.send(bytes([6, 1]) + s(STATE["holder"]) + s("Fleet Copilot"))

    async def ticker():
        seq = 0
        while True:
            await asyncio.sleep(0.1)
            if STATE["holder"] and loop.time() - STATE["presence"] > 1.5:
                STATE["speed"] = 0.0  # dead-man failsafe
            if STATE["enabled"]:
                v = STATE["speed"] / 3.6
                STATE["h"] += v * 0.1 * math.tan(math.radians(STATE["steer"])) / 3.0
                STATE["e"] += v * 0.1 * math.sin(STATE["h"])
                STATE["n"] += v * 0.1 * math.cos(STATE["h"])
            seq += 1
            await ws.send(bytes([2]) + struct.pack("<qddffB", seq, STATE["e"], STATE["n"], STATE["h"], STATE["speed"], 4) + b"\x00" * 40)
    t = asyncio.create_task(ticker())
    try:
        async for m in ws:
            cmd, _, arg = m.partition("|")
            mine = STATE["holder"] == cid
            if cmd == "control.presence" and mine:
                STATE["presence"] = loop.time()
            elif cmd == "control.release" and mine:
                STATE["holder"] = None
            elif not mine:
                continue
            elif cmd == "sim.speedUp":
                STATE["speed"] += 0.5
            elif cmd == "sim.speedDown":
                STATE["speed"] -= 0.5
            elif cmd == "sim.stop":
                STATE["speed"] = 0.0
            elif cmd == "sim.setSteer":
                STATE["steer"] = float(arg or 0)
            elif cmd == "sim.toggleEnable":
                STATE["enabled"] = not STATE["enabled"]
    finally:
        t.cancel()
        if STATE["holder"] == cid:
            STATE["holder"] = None
            STATE["speed"] = 0.0


async def main(port):
    async with websockets.serve(handler, "0.0.0.0", port):
        print(f"AgOpenWeb protocol emulator on ws://localhost:{port}/ws")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 5174))
