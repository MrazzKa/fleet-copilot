"""Benchmark (Fleet Copilot vs FCFS baseline) and chaos test.

  python -m fleet.bench                 # 100 random farms + demo scenario -> reports/comparison.md
  python -m fleet.bench --chaos 1000    # 1000 nasty random runs, checks every invariant every minute
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import time
import traceback
from multiprocessing import Pool
from pathlib import Path

from .gen import random_events, random_scenario
from .sim import Simulation
from .world import hhmm_to_min, load_scenario

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"

DEMO_EVENTS = [("07:40", {"kind": "breakdown", "tractor": "T2"}),
               ("10:00", {"kind": "block", "target": "north_bridge"}),
               ("11:00", {"kind": "priority", "job": "F8", "priority": "high", "deadline": "15:00"}),
               ("12:30", {"kind": "wind", "mph": 14, "hours": 2})]


def run_one(sc: dict, events: list, mode: str, agents: bool = False) -> dict:
    """agents=True runs the copilot through the full multi-agent loop (deterministic agents, no LLM)."""
    orch = None
    if agents and mode == "copilot":
        from .agents import Orchestrator
        sim = Simulation(sc, mode=mode, explain_enabled=False, resilience=False, autoplan=False)
        orch = Orchestrator(sim, memory_path=None, async_llm=False, deliberate=True, use_llm=False)
        sim.step_start_only()
        orch.tick()
    else:
        sim = Simulation(sc, mode=mode, explain_enabled=False, resilience=False)
    evs = list(events)
    while True:
        while evs and sim.now >= evs[0][0]:
            sim.apply_event(evs.pop(0)[1], source="bench")
            if orch:
                orch.tick()
        if not sim.step():
            break
        if orch:
            orch.tick()
    if orch:
        orch.collect()
        orch.drain()
    safety_rejections = sum(1 for e in sim.log if e["kind"] == "safety")
    m = sim.metrics()
    if orch:
        om = orch.metrics()
        m.update({"incidents": om["incidents"], "messages": om["messages"], "tool_calls": om["tool_calls"],
                  "learned": om["learned"], "unhandled": sum(1 for i in orch.incidents.values() if i.safe_ms is None)})
    m["safety_rejections"] = safety_rejections
    m["violation_msgs"] = [v["msg"] for v in sim.violations][:5]
    return m


def _task(args):
    seed, chaos = args
    try:
        sc = random_scenario(seed)
        events = random_events(seed, sc, chaos=chaos)
        return {"seed": seed, "events": len(events), "copilot": run_one(sc, events, "copilot", agents=True),
                "fcfs": run_one(sc, events, "fcfs"), "error": None}
    except Exception:  # noqa: BLE001, a crash is a chaos-test failure, record it
        return {"seed": seed, "error": traceback.format_exc()}


def demo_rows() -> dict:
    sc = load_scenario(ROOT / "scenarios" / "demo.json")
    ev = [(hhmm_to_min(t), e) for t, e in DEMO_EVENTS]
    return {"no_events": {"copilot": run_one(sc, [], "copilot", agents=True), "fcfs": run_one(sc, [], "fcfs")},
            "demo_events": {"copilot": run_one(sc, ev, "copilot", agents=True), "fcfs": run_one(sc, ev, "fcfs")}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=100)
    ap.add_argument("--chaos", type=int, default=0, help="run N chaos scenarios instead of the benchmark")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    a = ap.parse_args()
    REPORTS.mkdir(exist_ok=True)
    chaos = a.chaos > 0
    n = a.chaos if chaos else a.seeds
    t0 = time.time()
    with Pool(a.workers) as pool:
        res = pool.map(_task, [(s, chaos) for s in range(1, n + 1)], chunksize=4)
    dt = time.time() - t0
    errors = [r for r in res if r["error"]]
    ok = [r for r in res if not r["error"]]
    if chaos:
        out = {
            "runs": n, "seconds": round(dt, 1), "crashes": len(errors),
            "copilot_violations": sum(r["copilot"]["violations"] for r in ok),
            "fcfs_violations": sum(r["fcfs"]["violations"] for r in ok),
            "events_injected": sum(r["events"] for r in ok),
            "safety_rejections": sum(r["copilot"]["safety_rejections"] for r in ok),
            "agent_incidents": sum(r["copilot"].get("incidents", 0) for r in ok),
            "agent_messages": sum(r["copilot"].get("messages", 0) for r in ok),
            "agent_tool_calls": sum(r["copilot"].get("tool_calls", 0) for r in ok),
            "unhandled_incidents": sum(r["copilot"].get("unhandled", 0) for r in ok),
            "violation_examples": [(r["seed"], r["copilot"]["violation_msgs"]) for r in ok if r["copilot"]["violations"]][:10],
            "crash_examples": [(r["seed"], r["error"][-600:]) for r in errors][:3],
        }
        (REPORTS / "chaos.json").write_text(json.dumps(out, indent=2))
        print(json.dumps({k: v for k, v in out.items() if k not in ("crash_examples",)}, indent=2))
        if errors:
            print(errors[0]["error"])
        return
    demo = demo_rows()
    keys = ["on_time", "weighted_on_time", "high_on_time", "done", "area_done", "travel_min", "idle_min", "wait_min", "violations"]
    agg = {m: {k: st.mean(r[m][k] for r in ok) for k in keys} for m in ("copilot", "fcfs")}
    wins = sum(1 for r in ok if r["copilot"]["weighted_on_time"] > r["fcfs"]["weighted_on_time"])
    ties = sum(1 for r in ok if r["copilot"]["weighted_on_time"] == r["fcfs"]["weighted_on_time"])
    losses = len(ok) - wins - ties
    tot = {m: {k: sum(r[m][k] for r in ok) for k in ("done", "travel_min", "idle_min", "wait_min", "area_done", "on_time")} for m in ("copilot", "fcfs")}
    out = {"seeds": n, "seconds": round(dt, 1), "crashes": len(errors), "mean": agg, "totals": tot,
           "wins": wins, "ties": ties, "losses": losses, "demo": demo, "runs": ok}
    (REPORTS / "benchmark.json").write_text(json.dumps(out, indent=2))
    write_markdown(out)
    print((REPORTS / "comparison.md").read_text())


def _pct(a, b):
    return f"{(a - b) / b * 100:+.0f}%" if b else "n/a"


def write_markdown(out: dict):
    c, f = out["mean"]["copilot"], out["mean"]["fcfs"]
    tc, tf = out["totals"]["copilot"], out["totals"]["fcfs"]
    L = ["# Fleet Copilot vs simple baseline (first-come-first-served)", "",
         "Same simulator, same farms, same events, same physics. Baseline = when a tractor is idle it takes the first compatible job in submission order.", ""]
    for name, key in (("Demo scenario, no events", "no_events"), ("Demo scenario, scripted events (T2 breakdown 07:40, North Bridge closed 10:00, F8 rushed 11:00, wind 14 mph 12:30)", "demo_events")):
        d = out["demo"][key]
        L += [f"## {name}", "", "| Metric | Fleet Copilot | FCFS baseline |", "|---|---|---|"]
        for k, lbl in (("done", "Jobs completed"), ("on_time", "Jobs completed on time"), ("high_on_time", "High-priority on time"),
                       ("weighted_on_time", "Priority-weighted on-time score"), ("area_done", "Hectares worked"),
                       ("travel_min", "Travel time (tractor-min)"), ("idle_min", "Idle time (min, work was available)"),
                       ("wait_min", "Waiting at field for safe wind (min)"), ("violations", "Constraint violations")):
            L.append(f"| {lbl} | **{d['copilot'][k]}** | {d['fcfs'][k]} |")
        L.append("")
    L += [f"## {out['seeds']} random farms with random events", "",
          f"Copilot better on priority-weighted on-time work in **{out['wins']}** farms, tied in {out['ties']}, worse in {out['losses']}.", "",
          "| Mean per farm | Fleet Copilot | FCFS baseline | Δ |", "|---|---|---|---|"]
    for k, lbl in (("on_time", "Jobs on time"), ("weighted_on_time", "Priority-weighted on-time"), ("high_on_time", "High-priority on time"),
                   ("done", "Jobs completed"), ("area_done", "Hectares worked"), ("travel_min", "Travel (min)"),
                   ("idle_min", "Idle (min)"), ("wait_min", "Waiting for wind (min)"), ("violations", "Violations")):
        L.append(f"| {lbl} | **{c[k]:.2f}** | {f[k]:.2f} | {_pct(c[k], f[k])} |")
    tpj_c = tc["travel_min"] / max(1, tc["done"])
    tpj_f = tf["travel_min"] / max(1, tf["done"])
    L += ["", f"Travel per completed job: **{tpj_c:.1f} min** (Copilot) vs {tpj_f:.1f} min (FCFS), {_pct(tpj_c, tpj_f)}.",
          f"Unproductive time (idle + waiting) total: **{tc['idle_min'] + tc['wait_min']}** vs {tf['idle_min'] + tf['wait_min']} min, {_pct(tc['idle_min'] + tc['wait_min'], tf['idle_min'] + tf['wait_min'])}.",
          f"Crashes: {out['crashes']}. Runtime: {out['seconds']} s.", ""]
    chaos = REPORTS / "chaos.json"
    if chaos.exists():
        ch = json.loads(chaos.read_text())
        L += ["## Chaos test", "", f"{ch['runs']} random farms × {ch['events_injected']} injected events (breakdowns, repairs, bridge closures, wind gusts, rushed jobs, new jobs, closed fields, operator pins).",
              f"Every invariant checked every simulated minute: **{ch['copilot_violations']} violations, {ch['crashes']} crashes**. "
              f"The full agent loop handled **{ch.get('agent_incidents', 0)} incidents** with {ch.get('agent_messages', 0)} agent messages and "
              f"{ch.get('agent_tool_calls', 0)} tool calls; unhandled incidents: **{ch.get('unhandled_incidents', 0)}**.", ""]
    (REPORTS / "comparison.md").write_text("\n".join(L))


if __name__ == "__main__":
    main()
