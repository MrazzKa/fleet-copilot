"""Safety Agent, independently re-checks a plan before it is executed.

It does NOT reuse the planner's route evaluation: it walks every tractor's task chain itself, re-deriving paths,
fuel, implements, wind windows and timing minute-by-minute. A plan with any hard issue is rejected.
"""
from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from .world import EPS, travel_minutes, work_minutes, min_to_hhmm

if TYPE_CHECKING:
    from .planner import Plan
    from .sim import Simulation

CHECKS = [
    "implements & tractor capabilities",
    "fuel on every leg incl. return to depot",
    "routes avoid closed bridges/roads",
    "spraying only inside safe wind windows",
    "no duplicate or already-completed work",
    "everything ends before shift end",
    "operator pins respected",
    "depot implement inventory",
]


def check_plan(sim: "Simulation", plan: "Plan") -> dict:
    w = sim.world
    hard: list[str] = []
    warn: list[str] = []
    seen: dict[str, str] = dict(sim.committed_jobs())
    inv_events = [(t_, 0, imp, 1) for t_, imp in sim.pending_drops()]
    for tid, tasks in plan.tasks.items():
        tr = sim.tractors[tid]
        spec = tr.spec
        if tr.broken:
            if tasks:
                hard.append(f"{tid} is broken but has {len(tasks)} planned tasks")
            continue
        ts = sim.tractor_state(tr)
        t, pos, fuel, impl = ts.t, ts.pos, ts.fuel, ts.implement
        for task in tasks:
            if task.est_depart < t:
                hard.append(f"{tid}: task {task.label()} starts before the previous one ends")
            path = w.path(pos, task.target)
            if path is None:
                hard.append(f"{tid}: no route to {task.label()}")
                break
            if any(c in w.blocked for c in path[1:]):
                hard.append(f"{tid}: route to {task.label()} crosses a closed cell")
            tm = travel_minutes(len(path) - 1, spec.min_per_cell)
            fuel -= spec.burn_travel * tm / 60
            if fuel < -EPS:
                hard.append(f"{tid}: runs out of fuel on the way to {task.label()}")
            if task.kind in ("refuel", "swap"):
                if task.target != w.depot:
                    hard.append(f"{tid}: {task.kind} away from the depot")
                if task.kind == "swap":
                    if task.implement not in spec.capabilities:
                        hard.append(f"{tid} can't carry {task.implement}")
                    inv_events.append((task.est_depart, 1, task.implement, -1))
                    if impl:
                        inv_events.append((task.est_end, 0, impl, 1))
                    impl = task.implement
                if task.est_end - task.est_arrive < (15 if task.kind == "refuel" else 20) - EPS:
                    hard.append(f"{tid}: {task.kind} too short")
                fuel = 100.0
            else:
                jid = task.job_id
                j = sim.jobs.get(jid)
                if j is None:
                    hard.append(f"{tid}: unknown job {jid}")
                    continue
                if j.status == "done":
                    hard.append(f"{tid}: {jid} is already completed")
                if jid in seen and seen[jid] != tid:
                    hard.append(f"{jid} assigned to both {seen[jid]} and {tid}")
                seen[jid] = tid
                if w.fields[j.d.field].closed:
                    hard.append(f"{tid}: {j.d.field} is closed")
                if impl != j.d.implement or impl not in spec.capabilities:
                    hard.append(f"{tid}: {jid} needs a {j.d.implement}, tractor has {impl}")
                pin = sim.pins.get(jid)
                if pin and pin != tid:
                    hard.append(f"{jid} is pinned to {pin} but planned on {tid}")
                if task.est_start < task.est_depart + tm:
                    hard.append(f"{tid}: {jid} starts before arrival")
                dur = task.est_end - task.est_start
                if impl in spec.capabilities and dur < work_minutes(j.remaining, spec.rate(impl)):
                    hard.append(f"{tid}: {jid} work time underestimated")
                if j.d.type == "spray":
                    for m in range(task.est_start, task.est_end):
                        if not w.spray_ok(m):
                            hard.append(f"{tid}: spraying {j.d.field} at {min_to_hhmm(m)} outside the safe wind window")
                            break
                if task.est_end > w.day_end:
                    hard.append(f"{tid}: {jid} ends after shift end")
                fuel -= spec.burn_work * dur / 60
                back = w.dist(task.target, w.depot)
                if back is None:
                    hard.append(f"{tid}: no way back to the depot from {j.d.field}")
                elif fuel - spec.burn_travel * travel_minutes(back, spec.min_per_cell) / 60 < -EPS:
                    hard.append(f"{tid}: can't make it back to the depot after {jid}")
                if task.est_end > j.d.deadline:
                    warn.append(f"{jid} will be late ({min_to_hhmm(task.est_end)} vs deadline {min_to_hhmm(j.d.deadline)})")
            t, pos = task.est_end, task.target
    inv = Counter(sim.pool)
    for t_, _, imp, d in sorted(inv_events):
        inv[imp] += d
        if inv[imp] < 0:
            hard.append(f"depot runs out of {imp} at {min_to_hhmm(t_)}")
            break
    for jid, why in plan.unassigned.items():
        warn.append(f"{jid} not scheduled today: {why}")
    status = "fail" if hard else ("warn" if warn else "pass")
    return {"status": status, "hard": hard, "warnings": warn, "checks": CHECKS}
