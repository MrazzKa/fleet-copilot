"""Core guarantees: no lost/duplicated work, constraints respected, Safety Agent catches bad plans, Copilot beats FCFS."""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fleet.bench import DEMO_EVENTS, run_one  # noqa: E402
from fleet.gen import random_events, random_scenario  # noqa: E402
from fleet.planner import Plan  # noqa: E402
from fleet.routing import Task  # noqa: E402
from fleet import safety  # noqa: E402
from fleet.sim import Simulation  # noqa: E402
from fleet.world import hhmm_to_min, load_scenario  # noqa: E402

SC = load_scenario(Path(__file__).resolve().parent.parent / "scenarios" / "demo.json")


def run_until(sim, hhmm):
    while sim.now < hhmm_to_min(hhmm):
        sim.step()


def test_demo_no_violations_and_beats_baseline():
    ev = [(hhmm_to_min(t), e) for t, e in DEMO_EVENTS]
    c, f = run_one(SC, ev, "copilot"), run_one(SC, ev, "fcfs")
    assert c["violations"] == 0 and f["violations"] == 0
    assert c["weighted_on_time"] > f["weighted_on_time"]
    assert c["high_on_time"] >= f["high_on_time"]


def test_breakdown_keeps_progress_and_reassigns():
    sim = Simulation(SC)
    run_until(sim, "07:40")
    j5 = sim.jobs["J5"]
    done_before = j5.area_done
    assert done_before > 0
    sim.apply_event({"kind": "breakdown", "tractor": "T2"})
    assert sim.jobs["J5"].area_done == done_before  # progress kept
    assert sim.plan.assign.get("J5") in ("T1", "T3")  # reassigned to a working tractor
    assert not sim.tractors["T2"].queue
    sim.run_to_end()
    assert sim.jobs["J5"].status == "done"
    assert not sim.violations
    # conservation: the tractors together worked exactly the job area
    assert abs(sum(t.area_worked for t in sim.tractors.values()) - sum(j.area_done for j in sim.jobs.values())) < 1e-4


def test_bridge_closure_is_never_crossed():
    sim = Simulation(SC)
    run_until(sim, "06:30")
    sim.apply_event({"kind": "block", "target": "north_bridge"})
    cell = sim.world.bridges["north_bridge"]["cell"]
    while sim.step():
        for tr in sim.tractors.values():
            if tr.pos == cell:
                assert (tr.id, cell) in sim.grandfathered
    assert not sim.violations


def test_safety_rejects_bad_plans():
    sim = Simulation(SC)
    sim.step_start_only()
    good = sim.plan
    assert safety.check_plan(sim, good)["status"] != "fail"
    # duplicate assignment
    bad = copy.deepcopy(good)
    j = bad.tasks["T1"][-1]
    bad.tasks["T3"].append(Task("job", j.target, j.job_id, j.implement, 0, 1100, 1100, 1100, 1110, 50))
    assert any("both" in h or "needs a" in h for h in safety.check_plan(sim, bad)["hard"])
    # spraying in the 12:00-15:00 no-spray window
    bad2 = Plan(created_at=sim.now, tasks={"T1": [], "T2": [Task("job", (16, 8), "J5", "sprayer", 0, 720, 720, 720, 800, 50)], "T3": []})
    sim2 = Simulation(SC)
    sim2.step_start_only()
    sim2.tractors["T2"].cur = None
    sim2.tractors["T2"].pos = (16, 8)
    assert any("wind" in h for h in safety.check_plan(sim2, bad2)["hard"])


def test_invariant_checker_catches_duplicates():
    sim = Simulation(SC)
    sim.step()
    t1 = sim.tractors["T1"]
    sim.tractors["T3"].queue.append(copy.deepcopy(next(t for t in t1.queue if t.job_id)))
    sim.check_invariants()
    assert sim.violations


def test_chaos_small():
    for seed in range(1, 41):
        sc = random_scenario(seed)
        ev = random_events(seed, sc, chaos=True)
        m = run_one(sc, ev, "copilot")
        assert m["violations"] == 0, (seed, m["violation_msgs"])


def test_agent_fault_cases_all_pass():
    import os
    os.environ.setdefault("FLEET_TESTS_LLM", "0")
    from fleet.agents.testcases import CASES, run_case
    for c in CASES:
        r = run_case(c)
        assert r["passed"], (c["id"], [k for k in r["checks"] if not k["ok"]])


def test_agents_message_flow():
    from fleet.agents import Orchestrator
    sim = Simulation(SC, autoplan=False)
    o = Orchestrator(sim, async_llm=False, use_llm=False)
    sim.step_start_only()
    o.tick()
    run_until(sim, "07:40")
    sim.apply_event({"kind": "breakdown", "tractor": "T2"})
    o.tick()
    flow = [(m.sender, m.recipient, m.kind) for m in o.bus.log if m.kind not in ("tool", "thought")]
    assert ("monitor", "planner", "fault") in flow
    assert ("planner", "safety", "proposal") in flow
    assert ("safety", "execution", "approved") in flow
    assert ("execution", "monitor", "ack") in flow
    inc = [i for i in o.incidents.values() if i.kind == "breakdown"][0]
    assert inc.safe_ms is not None and inc.safe_ms < 200


def test_agopenweb_bridge_with_protocol_emulator():
    import subprocess
    import time as _t
    from fleet.integrations.agopenweb import AgOpenWebBridge
    root = Path(__file__).resolve().parent.parent
    p = subprocess.Popen([sys.executable, str(root / "scripts" / "fake_agopenweb.py"), "5199"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _t.sleep(1.0)
        b = AgOpenWebBridge("ws://127.0.0.1:5199/ws")
        for _ in range(40):
            _t.sleep(0.1)
            if b.ticks > 3:
                break
        assert b.connected and b.has_control and b.ticks > 3
        b.send(*["sim.speedUp"] * 10)
        _t.sleep(1.0)
        assert b.speed() >= 4.5 and b.dist_m > 0
        b.send("sim.stop")
        _t.sleep(0.5)
        assert b.speed() < 0.1
    finally:
        p.terminate()


def _orch(**kw):
    from fleet.agents.core import Orchestrator
    sim = Simulation(load_scenario(Path(__file__).resolve().parent.parent / "scenarios" / "demo.json"), "copilot", autoplan=False)
    o = Orchestrator(sim, None, async_llm=False, use_llm=False, **kw)
    o.tick()
    return sim, o


def test_incident_story_has_every_stage():
    sim, o = _orch()
    for _ in range(45):
        sim.step(); o.tick()
    sim.apply_event({"kind": "breakdown", "tractor": "T2"}, "operator"); o.tick(); o.tick()
    for _ in range(15):
        sim.step(); o.tick()
    sim.apply_event({"kind": "repair", "tractor": "T2"}, "operator"); o.tick(); o.tick()
    story = next(s for s in o.stories() if s["kind"] == "breakdown")
    keys = [x["key"] for x in story["steps"]]
    for k in ("broke", "detected", "planned", "verified", "executed", "second", "recovered", "normal"):
        assert k in keys, (k, keys)
    ms = [x["ms"] for x in story["steps"]]
    assert ms == sorted(ms)
    assert keys.index("detected") < keys.index("planned") < keys.index("verified") < keys.index("executed") < keys.index("recovered")


def test_public_link_is_limited():
    from fastapi.testclient import TestClient
    from fleet.server import app
    c = TestClient(app)
    pub = {"cf-ray": "test"}
    assert c.get("/health").json()["ok"] is True
    assert c.get("/health", headers=pub).status_code == 200
    for path in ("/api/memory", "/api/tests", "/api/log.json", "/api/log.csv", "/api/proof"):
        assert c.get(path, headers=pub).status_code == 403, path
        assert c.get(path).status_code == 200, path
    assert c.post("/api/control", json={"action": "reset"}, headers=pub).status_code == 403
    assert c.post("/api/event", json={"kind": "add_job"}, headers=pub).status_code == 403
    assert c.post("/api/event", json={"kind": "breakdown", "tractor": "T3"}, headers=pub).status_code == 200


def test_gps_stall_detected_from_telemetry_and_recovers():
    import subprocess
    import time as _t
    from fleet.integrations.agopenweb import AgOpenWebBridge
    root = Path(__file__).resolve().parent.parent
    p = subprocess.Popen([sys.executable, str(root / "scripts" / "fake_agopenweb.py"), "5198"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _t.sleep(1.0)
        b = AgOpenWebBridge("ws://127.0.0.1:5198/ws", "T1")
        sim, o = _orch(bridge=b)
        while sim.tractors["T1"].status() not in ("travel", "work"):
            sim.step(); o.tick()

        def run(sec, until=None):
            end = _t.time() + sec
            while _t.time() < end:
                o.tick(); _t.sleep(0.1)
                if until and until():
                    return True
            return False
        assert run(4, lambda: b.speed() > 1.0)
        o.telemetry.inject_gps_fault()
        assert run(9, lambda: sim.tractors["T1"].broken), "stall not detected"
        o.tick(); o.tick()
        story = next(s for s in o.stories() if s["kind"] == "breakdown")
        assert story["steps"][0]["key"] == "gps" and story["steps"][0]["ms"] < -3000
        o.telemetry.resume()
        assert run(9, lambda: not sim.tractors["T1"].broken), "no recovery after resume"
    finally:
        p.terminate()
