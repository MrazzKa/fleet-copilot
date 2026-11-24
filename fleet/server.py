"""FastAPI server: runs Fleet Copilot and the FCFS "ghost" baseline side by side in lockstep,
streams state over WebSocket, accepts operator / judge / voice events.

  python -m fleet.server            (or ./run.sh)  -> http://localhost:8000
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import socket
import time
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import explain, llm, nlp
from .agents import Orchestrator
from .agents import testcases
from .sim import Simulation
from .world import hhmm_to_min, load_scenario, min_to_hhmm

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
REPORTS = ROOT / "reports"


def scenario_path(v: str | None) -> Path:
    """FLEET_SCENARIO: a name in scenarios/ (demo, maricopa) or a path to a JSON file. Default: demo."""
    v = (v or "demo").strip()
    return Path(v) if ("/" in v or v.endswith(".json")) else ROOT / "scenarios" / f"{v}.json"


SCENARIO = scenario_path(os.getenv("FLEET_SCENARIO"))
TICK_S = 0.1
MEMORY = ROOT / "memory" / "shared_memory.json"


class Engine:
    def __init__(self, scenario_path: Path):
        self.scenario_path = scenario_path
        self.bridge = None
        url = os.getenv("FLEET_AGOPENWEB", "").strip()
        if url:
            from .integrations.agopenweb import AgOpenWebBridge
            self.bridge = AgOpenWebBridge(url, os.getenv("FLEET_AGOPENWEB_TRACTOR", "T1"))
        self.reset()

    def reset(self, sc: dict | None = None, script: list | None = None, case: str | None = None):
        self.sc = sc or load_scenario(self.scenario_path)
        self.copilot = Simulation(self.sc, "copilot", resilience=True, autoplan=False)
        self.ghost = Simulation(self.sc, "fcfs")
        self.orch = Orchestrator(self.copilot, memory_path=MEMORY, async_llm=True, bridge=self.bridge)
        self.orch.on_event = lambda c, src, r: (self.ghost.apply_event(dict(c), src) if r.get("ok") else None,
                                                 self._note_injected(c, src, r))
        self.script = list(script or [])
        self.case = case
        self.copilot.step_start_only()
        self.ghost.step_start_only()
        self.orch.tick()
        self.running = False
        self.speed = 1.0  # sim-minutes per tick (10 ticks / s)
        self._acc = 0.0
        self.injected: list[dict] = []
        self.version = 0
        self.toasts: list[dict] = []
        self.answers: list[dict] = []
        self._judge_last: dict[str, float] = {}
        self.pending_polish: list[int] = []
        self._place_twin()

    def _place_twin(self):
        """Real-farm scenario: put the AgOpenWeb vehicle on the field where its tractor starts work."""
        b, w = self.bridge, self.copilot.world
        if not b or not w.geo:
            return
        tr = self.copilot.tractors.get(b.tractor)
        if tr is None:
            return
        job = next((t for t in ([tr.cur] if tr.cur else []) + list(tr.queue) if t.kind == "job"), None)
        cell = tuple(job.target) if job else w.depot
        b.place(*w.cell_latlon(*cell))

    # ------------------------------------------------------------ time
    def tick(self):
        if not self.running:
            self.orch.tick()  # let background LLM results land even while paused
            return
        self._acc += self.speed
        while self._acc >= 1:
            self._acc -= 1
            while self.script and self.copilot.now >= self.script[0][0]:
                e = self.script.pop(0)[1]
                if "say" in e:
                    self.orch.operator_command(e["say"], "scripted operator")
                else:
                    self.event(e, "test case")
            alive = self.copilot.step()
            self.orch.tick()
            self.ghost.step()
            if not alive:
                self.running = False
                self.toast("Day complete, see the scoreboard and export the log", "info")
                break

    def jump_to(self, minute: int):
        while self.copilot.now < minute and not self.copilot.finished():
            self.copilot.step()
            self.orch.tick()
            self.ghost.step()

    def _note_injected(self, c, src, r):
        if r.get("ok"):
            self.injected.append({"t": self.copilot.now, "clock": min_to_hhmm(self.copilot.now), "source": src, "event": c})
            self.toast(f"{'👩‍⚖️ ' if src.startswith('judge') else ''}{r['msg']}", "event")

    # ------------------------------------------------------------ events
    def toast(self, msg: str, level: str = "info"):
        self.toasts.append({"id": time.time_ns(), "msg": msg, "level": level})
        self.toasts = self.toasts[-6:]

    def event(self, ev: dict, source: str = "operator") -> dict:
        res = self.copilot.apply_event(ev, source)
        if res.get("ok"):
            self.ghost.apply_event(dict(ev), source)
            self._note_injected(ev, source, res)
            if source not in ("test case",):
                self.orch.human_interventions += 1
        self.orch.tick()
        return res

    # ------------------------------------------------------------ state
    def tractor_json(self, sim: Simulation, lite=False) -> list[dict]:
        out = []
        for tr in sim.tractors.values():
            x, y = tr.pos
            ahead = []
            if tr.phase == "travel" and tr.path:
                f = tr.travel_elapsed / tr.spec.min_per_cell
                idx = min(len(tr.path) - 1, int(f + 1e-9))
                frac = f - idx
                if idx + 1 < len(tr.path):
                    nx, ny = tr.path[idx + 1]
                    x = tr.path[idx][0] + (nx - tr.path[idx][0]) * frac
                    y = tr.path[idx][1] + (ny - tr.path[idx][1]) * frac
                ahead = tr.path[idx:]
            cur = tr.cur.to_json() if tr.cur else None
            if cur and tr.cur.kind == "job":
                j = sim.jobs[tr.cur.job_id]
                cur["pct"] = round(100 * j.area_done / j.d.area_ha)
            d = {"id": tr.id, "name": tr.spec.name, "model": tr.spec.model, "x": x, "y": y, "status": tr.status(),
                 "fuel": round(tr.fuel, 1), "implement": tr.implement, "caps": tr.spec.capabilities, "cur": cur,
                 "hours": tr.spec.hours_since_service, "wait_until": tr.wait_until if tr.phase == "wait" else None}
            ll = sim.world.cell_latlon(x, y)
            if ll:
                d["lat"], d["lon"] = ll
                # planned route on the real map: rest of the current leg, then A* legs to the next 3 queued tasks
                route, at = [list(c) for c in ahead], (tr.path[-1] if ahead else tr.pos)
                for task in tr.queue[:3]:
                    leg = sim.world.path(tuple(at), tuple(task.target)) or []
                    route += [list(c) for c in leg[1:]]
                    at = task.target
                d["route"] = route
            if not lite:
                d["path"] = ahead
                d["queue"] = [t.to_json() for t in tr.queue]
                d["history"] = tr.history[-80:]
                d["stats"] = {"travel": tr.travel_min, "work": tr.work_min, "idle": tr.idle_min, "wait": tr.wait_min}
            out.append(d)
        return out

    def jobs_json(self, sim: Simulation) -> list[dict]:
        com = sim.committed_jobs()
        plan = sim.plan
        out = []
        for j in sim.jobs.values():
            tid = com.get(j.d.id) or (plan.assign.get(j.d.id) if plan else None) or sim.assigned.get(j.d.id)
            eta = None
            if j.d.id in com:
                eta = sim.tractor_state(sim.tractors[com[j.d.id]]).t
            elif plan and j.d.id in plan.ends:
                eta = plan.ends[j.d.id]
            status = "done" if j.status == "done" else ("active" if j.d.id in com else ("planned" if tid else "unscheduled"))
            out.append({"id": j.d.id, "type": j.d.type, "field": j.d.field, "priority": j.d.priority, "deadline": j.d.deadline,
                        "area": j.d.area_ha, "done_ha": round(j.area_done, 2), "pct": round(100 * j.area_done / j.d.area_ha),
                        "status": status, "tractor": j.completed_by if j.status == "done" else tid,
                        "eta": j.completed_at if j.status == "done" else eta,
                        "late": (j.completed_at or eta or 0) > j.d.deadline if (j.completed_at or eta) else False,
                        "reason": (plan.unassigned.get(j.d.id) if plan and status == "unscheduled" else None),
                        "pinned": sim.pins.get(j.d.id)})
        return out

    def state(self, lite=False) -> dict:
        c, g, w = self.copilot, self.ghost, self.copilot.world
        st = {
            "type": "state", "now": c.now, "clock": min_to_hhmm(c.now), "running": self.running, "speed": self.speed,
            "finished": c.finished(),
            "tractors": self.tractor_json(c, lite), "ghost": self.tractor_json(g, lite),
            "jobs": self.jobs_json(c), "ghost_jobs": {j.d.id: j.status for j in g.jobs.values()},
            "metrics": {"copilot": c.metrics(), "fcfs": g.metrics()},
            "blocked": sorted(w.blocked), "blocked_names": list(w.blocked_names),
            "closed_fields": [f.id for f in w.fields.values() if f.closed],
            "wind": w.wind, "wind_now": w.wind_at(c.now), "spray_ok": w.spray_ok(c.now), "windows": w.spray_windows(),
            "pool": c.pool, "resilience": c.resilience,
            "explanations": c.explanations[-6:], "toasts": self.toasts, "answers": (self.answers + self.orch.answers)[-3:],
            "agents": self.orch.agents_json(), "bus": self.orch.bus.recent(70 if not lite else 25),
            "orch": self.orch.metrics(), "incidents": [i.to_json() for i in list(self.orch.incidents.values())[-12:]],
            "case": self.case, "script_left": len(self.script),
            "agopenweb": ({**self.bridge.snapshot(), "note": self.orch.telemetry.note, "gps_fault_active": self.orch.telemetry.gps_broke}
                          if self.bridge and self.orch.telemetry else None),
            "violations": c.violations[-5:], "ghost_violations": len(g.violations),
            "llm": llm.status(),
        }
        if not lite:
            st["log"] = c.log[-80:]
        return st


app = FastAPI(title="Fleet Copilot")
ENGINE = Engine(SCENARIO)
CLIENTS: set = set()


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


async def polish_loop():
    """Rephrase new explanations with the LLM chain in the background (never blocks the sim)."""
    while True:
        await asyncio.sleep(0.3)
        q = ENGINE.orch.polish_queue
        if not q:
            continue
        eid = q.pop(0)
        exp = next((e for e in ENGINE.copilot.explanations if e["id"] == eid), None)
        if exp is None or exp.get("llm_text") or not llm.providers():
            continue
        if exp["title"].startswith("Re-planned:") and "No assignment changes" in exp["text"]:
            continue
        text, src = await asyncio.to_thread(llm.rephrase, exp["text"])
        if text:
            exp["llm_text"], exp["llm_source"] = text, src


async def sim_loop():
    n = 0
    while True:
        t0 = time.perf_counter()
        ENGINE.tick()
        n += 1
        if CLIENTS:
            full = json.dumps(ENGINE.state(lite=False), default=list)
            lite = None
            dead = []
            for ws in list(CLIENTS):
                try:
                    if getattr(ws, "_lite", False):
                        if n % 3 == 0:
                            lite = lite or json.dumps(ENGINE.state(lite=True), default=list)
                            await ws.send_text(lite)
                    else:
                        await ws.send_text(full)
                except Exception:  # noqa: BLE001
                    dead.append(ws)
            for ws in dead:
                CLIENTS.discard(ws)
        await asyncio.sleep(max(0.0, TICK_S - (time.perf_counter() - t0)))


PUBLIC_POST_OK = {"/api/event"}
PUBLIC_EVENT_KINDS = {"breakdown", "repair", "block", "unblock", "wind", "priority"}


PUBLIC_GET_BLOCKED = ("/api/memory", "/api/tests", "/api/proof", "/api/log.json", "/api/log.csv", "/api/whynot")
TRUSTED = {"127.0.0.1", "::1", "localhost", "testclient"} | {x.strip() for x in os.getenv("FLEET_TRUSTED", "").split(",") if x.strip()}


def is_public(request) -> bool:
    """Requests that arrive through a tunnel (cloudflared / any proxy) or from another machine are 'public'.
    Works for both HTTP requests and WebSockets (same headers / client fields)."""
    if os.getenv("FLEET_PUBLIC_OPEN") == "1":
        return False
    h = request.headers
    if h.get("cf-connecting-ip") or h.get("x-forwarded-for") or h.get("cf-ray"):
        return True
    host = request.client.host if request.client else ""
    return host not in TRUSTED


@app.middleware("http")
async def guard_public(request: Request, call_next):
    """Judges on the public QR link may only inject faults; control, reset, memory, tests, chaos stay local-only."""
    pub = is_public(request)
    if pub and request.method == "GET" and request.url.path.startswith(PUBLIC_GET_BLOCKED):
        return JSONResponse({"ok": False, "msg": "This page is only available on the presenter's laptop."}, status_code=403)
    if pub and request.method != "GET":
        if request.url.path not in PUBLIC_POST_OK:
            return JSONResponse({"ok": False, "msg": "This action is only available on the presenter's laptop."}, status_code=403)
        try:
            body = json.loads((await request.body()) or b"{}")
        except Exception:  # noqa: BLE001
            body = {}
        if body.get("kind") not in PUBLIC_EVENT_KINDS:
            return JSONResponse({"ok": False, "msg": "Judges can break/fix tractors, close bridges, call wind or rush a job."}, status_code=403)
    return await call_next(request)


@app.exception_handler(Exception)
async def _err(request: Request, exc: Exception):
    import traceback
    traceback.print_exc()
    return JSONResponse({"ok": False, "msg": f"Server error: {type(exc).__name__}: {exc}"}, status_code=200)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(sim_loop())
    asyncio.create_task(polish_loop())
    url = os.getenv("PUBLIC_URL") or f"http://localhost:{os.getenv('PORT', '8000')}"
    from .agents.llm_tools import config as _cfg
    c = _cfg()
    print(f"\n  🚜 Fleet Copilot running -> {url}   (agent LLM: {c['name'] + ' · ' + c['model'] if c else 'none, rules only'})\n")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    pub = is_public(ws)
    if pub and sum(1 for c in CLIENTS if getattr(c, "_public", False)) >= int(os.getenv("FLEET_MAX_PUBLIC_WS", "60")):
        await ws.close(code=1013)
        return
    await ws.accept()
    ws._public = pub  # type: ignore[attr-defined]
    ws._lite = pub or ws.query_params.get("lite") == "1"  # type: ignore[attr-defined]  # public viewers always get the small stream
    await ws.send_text(json.dumps({"type": "static", "world": ENGINE.copilot.world.static_json(),
                                   "tractor_specs": {t.id: {"name": t.spec.name, "caps": t.spec.capabilities} for t in ENGINE.copilot.tractors.values()}},
                                  default=list))
    CLIENTS.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        CLIENTS.discard(ws)


@app.post("/api/event")
async def api_event(req: Request):
    body = await req.json()
    source = body.pop("source", "operator")
    if is_public(req) and not source.startswith("judge"):
        source = "judge"
    if source.startswith("judge"):
        ip = req.client.host if req.client else "?"
        if time.time() - ENGINE._judge_last.get(ip, 0) < 2.0:
            return JSONResponse({"ok": False, "msg": "easy there, one event every 2 seconds 🙂"})
        ENGINE._judge_last[ip] = time.time()
    return ENGINE.event(body, source)


@app.post("/api/control")
async def api_control(req: Request):
    b = await req.json()
    a = b.get("action")
    if a == "play":
        ENGINE.running = True
    elif a == "pause":
        ENGINE.running = False
    elif a == "toggle":
        ENGINE.running = not ENGINE.running
    elif a == "speed":
        ENGINE.speed = max(0.25, min(30.0, float(b.get("value", 1))))
    elif a == "reset":
        ENGINE.reset()
        from . import explain as _e
        _e._seq[0] = 0
    elif a == "forget":
        ENGINE.orch.memory.forget_reflexes()
        ENGINE.toast("🧽 Learned reflexes forgotten, next incidents will deliberate again", "info")
    elif a == "jump":
        ENGINE.jump_to(int(b.get("value")))
    elif a == "offline":
        llm.set_offline(bool(b.get("value")))
        ENGINE.toast("☁️✖ Cloud LLM disabled, running on the local model / templates" if b.get("value") else "☁️ Cloud LLM re-enabled", "info")
    return {"ok": True, "running": ENGINE.running, "speed": ENGINE.speed}


@app.post("/api/command")
async def api_command(req: Request):
    b = await req.json()
    text = (b.get("text") or "").strip()
    if not text:
        return {"ok": False, "msg": "empty"}
    n = len(ENGINE.orch.answers)
    ENGINE.orch.operator_command(text, b.get("source", "operator"))
    new = ENGINE.orch.answers[n:]
    return {"ok": True, "results": [{"kind": "why", "text": a["text"], "whynot": a.get("whynot", []), "job": a.get("job")} for a in new]}


@app.get("/api/why")
async def api_why(job: str):
    return explain.why(ENGINE.copilot, job)


@app.get("/api/whynot")
async def api_whynot(job: str, tractor: str):
    return await asyncio.to_thread(explain.why_not, ENGINE.copilot, job, tractor)


@app.get("/health")
async def health():
    """For Docker / uptime checks: is the loop alive, which LLM, is AgOpenWeb connected."""
    from .agents.llm_tools import config as _cfg
    c, b = _cfg(), ENGINE.bridge
    return {"ok": True, "sim_clock": min_to_hhmm(ENGINE.copilot.now), "running": ENGINE.running,
            "agents": len(ENGINE.orch.agents_json()), "incidents": len(ENGINE.orch.incidents),
            "llm": f"{c['name']}:{c['model']}" if c else "rules only",
            "agopenweb": None if b is None else {"url": b.url, "connected": b.connected, "control": b.has_control}}


@app.get("/api/stories")
async def api_stories(n: int = 8):
    return {"stories": ENGINE.orch.stories(max(1, min(n, 30)))}


@app.get("/api/info")
async def api_info():
    port = os.getenv("PORT", "8000")
    return {"public_url": os.getenv("PUBLIC_URL"), "lan_url": f"http://{lan_ip()}:{port}", "llm": llm.status(),
            "scenario": ENGINE.copilot.world.name, "scenario_id": SCENARIO.stem, "geo": ENGINE.copilot.world.geo,
            "agopenweb_view": os.getenv("FLEET_AGOPENWEB_VIEW") or ("http://localhost:5174" if ENGINE.bridge else None)}


@app.get("/api/qr.svg")
async def api_qr(url: str):
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml")


def export_payload() -> dict:
    c, g = ENGINE.copilot, ENGINE.ghost
    return {
        "agents": ENGINE.orch.agents_json(), "orchestration_metrics": ENGINE.orch.metrics(),
        "incidents": [i.to_json() for i in ENGINE.orch.incidents.values()],
        "agent_messages": ENGINE.orch.bus.recent(4000),
        "shared_memory": ENGINE.orch.memory.to_json(),
        "scenario": c.world.name, "scenario_file": str(ENGINE.scenario_path.name), "exported_at_sim": min_to_hhmm(c.now),
        "injected_events": ENGINE.injected,
        "metrics": {"fleet_copilot": c.metrics(), "fcfs_baseline": g.metrics()},
        "violations": c.violations,
        "explanations": [{k: e[k] for k in ("clock", "title", "lines", "impact_text", "safety_line", "llm_text", "llm_source")} for e in c.explanations],
        "event_log": c.log,
        "baseline_event_log": g.log,
    }


@app.get("/api/log.json")
async def api_log_json():
    return JSONResponse(export_payload())


@app.get("/api/log.csv")
async def api_log_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["sim_time", "kind", "source", "message"])
    for e in ENGINE.copilot.log:
        w.writerow([e["clock"], e["kind"], e["source"], e["msg"]])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=event_log.csv"})


@app.post("/api/export")
async def api_export():
    REPORTS.mkdir(exist_ok=True)
    p = export_payload()
    (REPORTS / "event_log_demo.json").write_text(json.dumps(p, indent=2, default=list))
    with open(REPORTS / "event_log_demo.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["sim_time", "kind", "source", "message"])
        for e in ENGINE.copilot.log:
            w.writerow([e["clock"], e["kind"], e["source"], e["msg"]])
    m = p["metrics"]
    lines = ["# Demo run, Fleet Copilot vs FCFS (live, same events)", "", f"Events injected: {len(ENGINE.injected)}", "",
             "| Metric | Fleet Copilot | FCFS |", "|---|---|---|"]
    for k in ("done", "on_time", "high_on_time", "weighted_on_time", "area_done", "travel_min", "idle_min", "wait_min", "violations"):
        lines.append(f"| {k} | {m['fleet_copilot'][k]} | {m['fcfs_baseline'][k]} |")
    (REPORTS / "demo_run.md").write_text("\n".join(lines))
    ENGINE.toast("Saved reports/event_log_demo.json, .csv and demo_run.md", "info")
    return {"ok": True, "files": ["reports/event_log_demo.json", "reports/event_log_demo.csv", "reports/demo_run.md"]}


@app.post("/api/agow")
async def api_agow(req: Request):
    b = await req.json()
    t = ENGINE.orch.telemetry
    if t is None:
        return {"ok": False, "msg": "AgOpenWeb bridge not enabled (set FLEET_AGOPENWEB in .env)"}
    a = b.get("action")
    if a == "gps_fault":
        t.inject_gps_fault()
    elif a == "resume":
        t.resume()
    elif a == "toggle_sim":
        t.b.send("sim.toggleEnable")
    elif a == "take_control":
        t.b.send(f"control.acquire|{t.b.name}")
    elif a == "release":
        t.b.send("control.release")
    return {"ok": True}


@app.post("/api/policy")
async def api_policy(req: Request):
    b = await req.json()
    t = (b.get("text") or "").strip()
    if t:
        ENGINE.orch.memory.policies.append(t[:200])
        ENGINE.orch.memory.save()
    return {"ok": True}


@app.get("/api/memory")
async def api_memory():
    return ENGINE.orch.memory.to_json()


@app.get("/api/tests")
async def api_tests():
    p = REPORTS / "test_cases.json"
    last = json.loads(p.read_text()) if p.exists() else None
    return {"cases": [{"id": c["id"], "title": c["title"], "story": c["story"]} for c in testcases.CASES], "last": last}


@app.post("/api/tests/run")
async def api_tests_run():
    res = await asyncio.to_thread(testcases.run_all)
    return {"results": res}


@app.post("/api/tests/play")
async def api_tests_play(req: Request):
    b = await req.json()
    sc, ev = testcases.case_for_play(b.get("id"))
    if sc is None:
        return {"ok": False}
    ENGINE.reset(sc, ev, b.get("id"))
    ENGINE.running = True
    ENGINE.speed = 3.0
    ENGINE.toast(f"🧪 Playing test case: {next(c['title'] for c in testcases.CASES if c['id'] == b.get('id'))}", "info")
    return {"ok": True}


@app.get("/api/proof")
async def api_proof():
    out = {}
    for name in ("comparison.md",):
        p = REPORTS / name
        out[name] = p.read_text() if p.exists() else None
    p = REPORTS / "chaos.json"
    out["chaos"] = json.loads(p.read_text()) if p.exists() else None
    return out


@app.post("/api/chaos")
async def api_chaos(req: Request):
    b = await req.json()
    n = int(b.get("n", 200))

    def run():
        from multiprocessing import Pool
        from .bench import _task
        t0 = time.time()
        with Pool(max(1, (os.cpu_count() or 2) - 1)) as pool:
            res = pool.map(_task, [(s, True) for s in range(5000, 5000 + n)], chunksize=4)
        ok = [r for r in res if not r["error"]]
        return {"runs": n, "crashes": n - len(ok), "events": sum(r["events"] for r in ok),
                "violations": sum(r["copilot"]["violations"] for r in ok), "seconds": round(time.time() - t0, 1),
                "copilot_on_time": sum(r["copilot"]["weighted_on_time"] for r in ok),
                "fcfs_on_time": sum(r["fcfs"]["weighted_on_time"] for r in ok)}
    return await asyncio.to_thread(run)


def main():
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")), log_level="warning")


if __name__ == "__main__":
    main()
