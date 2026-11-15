"""Check the REAL AgOpenWeb connection before the demo.
  python scripts/agow_probe.py ws://localhost:5174/ws
Prints Hello / control / pose frames for ~6 s and nudges the simulator (+2 kph, then stop)."""
import sys
import time

sys.path.insert(0, ".")
from fleet.integrations.agopenweb import AgOpenWebBridge  # noqa: E402

url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:5174/ws"
b = AgOpenWebBridge(url, name="Fleet Copilot probe")
for i in range(30):
    time.sleep(0.2)
    if b.connected and b.ticks:
        break
print("connected:", b.connected, "| client id:", b.client_id or "-", "| has control:", b.has_control, "| holder:", b.holder or "-", "| error:", b.error)
if not b.connected:
    sys.exit("✖ cannot reach AgOpenWeb, is it running (dotnet run … -- --headless) and is the port 5174?")
b.send(*["sim.speedUp"] * 4)
for i in range(6):
    time.sleep(1)
    print(f"t+{i + 1}s ticks={b.ticks} pose={b.snapshot()['pose']} fix={b.fix}")
b.send("sim.stop")
time.sleep(1)
ok = b.ticks > 10
print("✅ telemetry OK" if ok else "✖ no Tick frames, open the AgOpenWeb UI once and enable the simulator")
print("✅ we hold control (commands will work)" if b.has_control else "⚠ observer only, close other AgOpenWeb browser tabs, then retry")
