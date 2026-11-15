"""Fault-injection test cases for the multi-agent orchestrator.

  python -m fleet.agents.testcases          -> reports/test_cases.md + reports/test_cases.json
Each case: a scenario + scripted faults/operator messages + checks on what the agents did.
"""
from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path

from ..sim import Simulation
from ..world import hhmm_to_min, load_scenario, min_to_hhmm
from ..gen import random_events, random_scenario
from .core import Orchestrator

ROOT = Path(__file__).resolve().parents[2]
DEMO = load_scenario(ROOT / "scenarios" / "demo.json")


def _night():
    sc = copy.deepcopy(DEMO)
    sc["name"] = "Night shift, 3 AM breakdown"
    sc["day"] = {"start": "02:00", "end": "16:00"}
    for j in sc["jobs"]:
        j["deadline"] = min_to_hhmm(hhmm_to_min(j["deadline"]) - 240)
    sc["depot"]["implements"]["mower"] = 1
    return sc


def _low_fuel():
    sc = copy.deepcopy(DEMO)
    sc["name"] = "Low fuel at dawn"
    sc["tractors"][1]["fuel"] = 18
    return sc


def _spare_mower():
    sc = copy.deepcopy(DEMO)
    sc["depot"]["implements"]["mower"] = 1
    return sc


def _chaos():
    return random_scenario(4242)


CASES = [
    {"id": "night_breakdown", "title": "3 AM breakdown, tractor B takes over",
     "story": "Night shift. At 03:00 T3 Colt breaks down mid-mowing a HIGH-priority field. Agents must hand the rest to another tractor (via the spare mower at the depot) with no human.",
     "scenario": _night, "events": [("03:00", {"kind": "breakdown", "tractor": "T3"})], "checks": ["no_violations", "no_humans",
     ("job_done_by_other", "J9", "T3"), ("progress_kept", "J9"), ("fast_safe_plan", 100)]},
    {"id": "sprayer_breakdown", "title": "Only sprayer tractor breaks mid-spray",
     "story": "07:40: T2 Bison (the sprayer) breaks at 47% of a HIGH-priority spray. Remaining hectares must move to another tractor and still meet the 10:30 deadline.",
     "scenario": lambda: copy.deepcopy(DEMO), "events": [("07:40", {"kind": "breakdown", "tractor": "T2"})],
     "checks": ["no_violations", "no_humans", ("job_done_by_other", "J5", "T2"), ("on_time", "J5"), ("progress_kept", "J5"), ("fast_safe_plan", 100)]},
    {"id": "bridge_closure", "title": "Bridge closes under moving tractors",
     "story": "06:20: North Bridge closes while tractors are driving east. Navigation must re-path everyone; nobody may cross the closed bridge.",
     "scenario": lambda: copy.deepcopy(DEMO), "events": [("06:20", {"kind": "block", "target": "north_bridge"})],
     "checks": ["no_violations", ("agent_talked", "planner", "navigation"), ("agent_talked", "navigation", "planner"), "all_done"]},
    {"id": "wind_gust", "title": "Wind gust during spraying",
     "story": "07:30: forecast jumps to 14 mph for 2 h (drift risk). Spraying must stop immediately, progress kept, and resume only in a safe window.",
     "scenario": lambda: copy.deepcopy(DEMO), "events": [("07:30", {"kind": "wind", "mph": 14, "hours": 2})],
     "checks": ["no_violations", "no_unsafe_spray", "all_done"]},
    {"id": "operator_rush", "title": "Operator rushes a job in plain English",
     "story": "08:00: the operator types “Field 6 is urgent, we need it by 10:30”. The Liaison agent must understand it and the plan must move F6 earlier.",
     "scenario": lambda: copy.deepcopy(DEMO), "events": [("08:00", {"say": "Field 6 is urgent, we need it by 10:30"})],
     "checks": ["no_violations", ("priority_is", "J7", "high"), ("finished_earlier_than_without", "J7")]},
    {"id": "double_fault", "title": "Two tractors fail within 90 minutes",
     "story": "T2 breaks at 07:40, T3 at 09:10. One tractor left, agents must triage by priority and still beat the baseline.",
     "scenario": _spare_mower, "events": [("07:40", {"kind": "breakdown", "tractor": "T2"}), ("09:10", {"kind": "breakdown", "tractor": "T3"})],
     "checks": ["no_violations", "no_humans", "beats_fcfs"]},
    {"id": "learned_reflex", "title": "Same fault twice -> learned reflex",
     "story": "T2 breaks at 07:40, is repaired at 09:00 and breaks again at 11:00. The first time the Planner deliberates; the second time it must reuse the learned reflex from shared memory instantly.",
     "scenario": lambda: copy.deepcopy(DEMO), "events": [("07:40", {"kind": "breakdown", "tractor": "T2"}), ("09:00", {"kind": "repair", "tractor": "T2"}),
                                                         ("11:00", {"kind": "breakdown", "tractor": "T2"})],
     "checks": ["no_violations", ("second_is_learned", "breakdown")]},
    {"id": "flooded_field", "title": "Flooded field closes and reopens",
     "story": "09:00: F5 floods. Its job must be postponed (never worked while closed) and rescheduled when F5 reopens at 13:00.",
     "scenario": lambda: copy.deepcopy(DEMO), "events": [("09:00", {"kind": "close_field", "field": "F5", "why": "flooded"}), ("13:00", {"kind": "open_field", "field": "F5"})],
     "checks": ["no_violations", ("done_after", "J2", "13:00")]},
    {"id": "low_fuel", "title": "Tractor starts nearly empty",
     "story": "T2 starts at 18% fuel. A refuel stop must be planned automatically; it must never run dry.",
     "scenario": _low_fuel, "events": [], "checks": ["no_violations", ("refueled", "T2"), "all_done"]},
    {"id": "chaos_burst", "title": "Chaos burst on a random farm",
     "story": "Random farm, 6+ random faults (breakdowns, closures, wind, rush jobs, closed fields). Every incident must be handled with zero violations.",
     "scenario": _chaos, "events": "chaos", "checks": ["no_violations", "all_incidents_handled", ("msgs_per_incident_below", 16)]},
]


def _run(sc, events, orch_mode=True, deliberate=True):
    sim = Simulation(sc, autoplan=not orch_mode, resilience=False) if orch_mode else Simulation(sc)
    orch = Orchestrator(sim, memory_path=None, async_llm=False, deliberate=deliberate,
                        use_llm=os.getenv("FLEET_TESTS_LLM", "0") == "1") if orch_mode else None
    if orch:
        sim.step_start_only()
        orch.tick()
    evs = list(events)
    unsafe = 0
    while True:
        while evs and sim.now >= evs[0][0]:
            e = evs.pop(0)[1]
            if orch is None:
                if "say" not in e:
                    sim.apply_event(e, "test")
                else:
                    from ..nlp import regex_parse
                    for c in regex_parse(e["say"], sim):
                        sim.apply_event(c, "test")
            elif "say" in e:
                orch.operator_command(e["say"], "test-operator")
                orch.human_interventions -= 1  # scripted operator line is part of the test, counted separately
            else:
                sim.apply_event(e, "test")
                orch.tick()
        if not sim.step():
            break
        if orch:
            orch.tick()
        for tr in sim.tractors.values():
            if tr.phase == "work" and tr.cur and sim.jobs[tr.cur.job_id].d.type == "spray" and not sim.world.spray_ok(sim.now - 1):
                unsafe += 1
    if orch:
        orch.collect()
        orch.drain()
    return sim, orch, unsafe


def run_case(case: dict) -> dict:
    t0 = time.time()
    sc = case["scenario"]()
    if case["events"] == "chaos":
        events = random_events(4242, sc, n=7, chaos=True)
    else:
        events = [(hhmm_to_min(t), e) for t, e in case["events"]]
    snap_progress = {}
    sim, orch, unsafe = _run(sc, events)
    base, _, _ = _run(sc, [(t, e) for t, e in events if "say" not in e] if case["id"] == "operator_rush" else events, orch_mode=False)
    fcfs = Simulation(sc, "fcfs")
    evs = list(events)
    while True:
        while evs and fcfs.now >= evs[0][0]:
            e = evs.pop(0)[1]
            if "say" not in e:
                fcfs.apply_event(e, "test")
        if not fcfs.step():
            break
    checks = []
    om = orch.metrics()
    incs = list(orch.incidents.values())

    def add(desc, ok, detail=""):
        checks.append({"check": desc, "ok": bool(ok), "detail": detail})
    for c in case["checks"]:
        name, args = (c, ()) if isinstance(c, str) else (c[0], c[1:])
        if name == "no_violations":
            add("0 invariant violations (no lost/duplicated work, fuel, routes, implements, wind)", not sim.violations,
                "; ".join(v["msg"] for v in sim.violations[:2]))
        elif name == "no_humans":
            add("handled with 0 human interventions", om["human_interventions"] == 0)
        elif name == "job_done_by_other":
            j = sim.jobs[args[0]]
            add(f"{args[0]} finished by a tractor other than {args[1]}", j.status == "done" and j.completed_by != args[1],
                f"done by {j.completed_by} at {min_to_hhmm(j.completed_at)}" if j.status == "done" else "not done")
        elif name == "on_time":
            j = sim.jobs[args[0]]
            add(f"{args[0]} on time (due {min_to_hhmm(j.d.deadline)})", j.status == "done" and j.completed_at <= j.d.deadline,
                f"finished {min_to_hhmm(j.completed_at)}" if j.completed_at else "not done")
        elif name == "progress_kept":
            j = sim.jobs[args[0]]
            worked = sum(tr.area_worked for tr in sim.tractors.values())
            add(f"{args[0]} partial progress kept, total worked area equals recorded area (no rework)",
                abs(worked - sum(x.area_done for x in sim.jobs.values())) < 1e-4)
        elif name == "fast_safe_plan":
            br = [i for i in incs if i.kind in ("breakdown",)]
            ms = br[0].safe_ms if br else None
            add(f"safe re-plan dispatched in < {args[0]} ms", ms is not None and ms < args[0], f"{ms:.1f} ms" if ms else "")
        elif name == "agent_talked":
            n = sum(1 for m in orch.bus.log if m.sender == args[0] and m.recipient == args[1] and m.kind not in ("tool", "thought"))
            add(f"{args[0]} -> {args[1]} message exchanged", n > 0, f"{n} message(s)")
        elif name == "all_done":
            nd = [j.d.id for j in sim.jobs.values() if j.status != "done"]
            add("all jobs completed by end of day", not nd, ", ".join(nd))
        elif name == "no_unsafe_spray":
            add("no spraying minute outside the safe wind window", unsafe == 0, f"{unsafe} unsafe minutes")
        elif name == "priority_is":
            add(f"Liaison understood the operator: {args[0]} priority = {args[1]}", sim.jobs[args[0]].d.priority == args[1])
        elif name == "finished_earlier_than_without":
            a, b = sim.jobs[args[0]].completed_at, base.jobs[args[0]].completed_at
            add(f"{args[0]} finished earlier than without the operator message", a is not None and b is not None and a < b,
                f"{min_to_hhmm(a)} vs {min_to_hhmm(b)}")
        elif name == "beats_fcfs":
            a, b = sim.metrics()["weighted_on_time"], fcfs.metrics()["weighted_on_time"]
            add("priority-weighted on-time work ≥ FCFS baseline", a >= b, f"{a} vs {b}")
        elif name == "second_is_learned":
            br = [i for i in incs if i.kind == args[0]]
            ok = len(br) >= 2 and br[1].path == "learned"
            add("2nd identical fault solved by a learned reflex (no deliberation)", ok,
                f"1st: {br[0].path} + deliberation {br[0].delib_ms or 0:.0f} ms; 2nd: {br[1].path} {br[1].safe_ms:.1f} ms" if len(br) >= 2 else "")
        elif name == "done_after":
            j = sim.jobs[args[0]]
            add(f"{args[0]} done after the field reopened ({args[1]})", j.status == "done" and j.started_at >= hhmm_to_min(args[1]),
                f"started {min_to_hhmm(j.started_at)}, done {min_to_hhmm(j.completed_at)}" if j.status == "done" else "not done")
        elif name == "refueled":
            add(f"{args[0]} refueled before running dry", any(e["kind"] == "refuel" and e.get("data", {}).get("tractor") == args[0] for e in sim.log))
        elif name == "all_incidents_handled":
            open_ = [i.id for i in incs if i.safe_ms is None]
            add("every incident got a safe plan decision", not open_, f"{len(incs)} incidents")
        elif name == "msgs_per_incident_below":
            v = om["msgs_per_incident"] or 0
            add(f"communication efficiency: < {args[0]} messages per incident", v < args[0], f"{v:.1f}")
    sm, fm = sim.metrics(), fcfs.metrics()
    return {"id": case["id"], "title": case["title"], "story": case["story"], "passed": all(c["ok"] for c in checks), "checks": checks,
            "agents": {k: om[k] for k in ("incidents", "reflex", "learned", "deliberations", "avg_safe_ms", "msgs_per_incident", "tool_calls", "llm_calls", "llm")},
            "copilot": {k: sm[k] for k in ("done", "on_time", "weighted_on_time", "violations")},
            "fcfs": {k: fm[k] for k in ("done", "on_time", "weighted_on_time")}, "seconds": round(time.time() - t0, 2),
            "events": [(min_to_hhmm(t), e) for t, e in events]}


def run_all(write: bool = True) -> list[dict]:
    res = [run_case(c) for c in CASES]
    if write:
        out = ROOT / "reports"
        out.mkdir(exist_ok=True)
        (out / "test_cases.json").write_text(json.dumps(res, indent=2, default=str))
        L = ["# Multi-agent fault-injection test cases", "",
             f"**{sum(r['passed'] for r in res)} / {len(res)} passed.** Every case runs the full agent loop "
             "(Fault Detection -> Planner -> Navigation -> Safety -> Execution -> Liaison) with zero human interventions unless the case scripts an operator message.", "",
             "| Case | Result | Incidents | ⚡ safe plan (avg) | Msgs / incident | Tool calls | On-time (agents vs FCFS) |", "|---|---|---|---|---|---|---|"]
        for r in res:
            a = r["agents"]
            L.append(f"| {r['title']} | {'✅ PASS' if r['passed'] else '❌ FAIL'} | {a['incidents']} | {a['avg_safe_ms'] or 0:.1f} ms | "
                     f"{a['msgs_per_incident'] or 0:.1f} | {a['tool_calls']} | {r['copilot']['on_time']} vs {r['fcfs']['on_time']} |")
        for r in res:
            L += ["", f"## {'✅' if r['passed'] else '❌'} {r['title']}", "", r["story"], ""]
            L += [f"- {'✅' if c['ok'] else '❌'} {c['check']}" + (f", {c['detail']}" if c["detail"] else "") for c in r["checks"]]
        (out / "test_cases.md").write_text("\n".join(L))
    return res


def case_for_play(case_id: str):
    c = next((x for x in CASES if x["id"] == case_id), None)
    if c is None:
        return None, None
    sc = c["scenario"]()
    ev = random_events(4242, sc, n=7, chaos=True) if c["events"] == "chaos" else [(hhmm_to_min(t), e) for t, e in c["events"]]
    return sc, ev


if __name__ == "__main__":
    for r in run_all():
        print(("PASS " if r["passed"] else "FAIL ") + r["title"])
        for c in r["checks"]:
            print("   ", "✓" if c["ok"] else "✗", c["check"], "-", c["detail"])
