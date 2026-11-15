"""Route evaluation shared by the Fleet Agent (planner), the FCFS baseline and the Safety Agent.

Given a tractor's state at the moment it becomes free and an ordered list of jobs, build the concrete
task list (automatic refuel / implement-swap stops at the depot) and check every hard constraint.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .world import (EPS, FUEL_RESERVE, REFUEL_MIN, SWAP_MIN, Cell, JobDef, TractorSpec, World,
                    min_to_hhmm, travel_minutes, work_minutes)


@dataclass
class TractorState:
    spec: TractorSpec
    functioning: bool
    t: int  # time when the tractor is free to start new planned work
    pos: Cell
    fuel: float
    implement: str | None
    committed_job: str | None = None

    @property
    def id(self) -> str:
        return self.spec.id


@dataclass
class Task:
    kind: str  # job | refuel | swap
    target: Cell
    job_id: str | None = None
    implement: str | None = None  # swap: implement to pick up; job: implement used
    travel_min: int = 0
    est_depart: int = 0
    est_arrive: int = 0
    est_start: int = 0
    est_end: int = 0
    fuel_after: float = 0.0

    def to_json(self) -> dict:
        return {
            "kind": self.kind, "job_id": self.job_id, "implement": self.implement, "target": list(self.target),
            "travel_min": self.travel_min, "depart": self.est_depart, "arrive": self.est_arrive,
            "start": self.est_start, "end": self.est_end, "fuel_after": round(self.fuel_after, 1),
            "label": self.label(),
        }

    def label(self) -> str:
        if self.kind == "job":
            return f"{self.job_id}"
        if self.kind == "refuel":
            return "Refuel"
        return f"Swap->{self.implement}"


@dataclass
class RouteEval:
    feasible: bool
    reason: str | None = None
    failed_job: str | None = None
    tasks: list[Task] = field(default_factory=list)
    ends: dict[str, int] = field(default_factory=dict)
    travel: int = 0
    takes: Counter = field(default_factory=Counter)
    end_t: int = 0
    swaps: list = field(default_factory=list)  # (depart, end, take_impl, drop_impl)


def _fail(reason: str, job: str | None) -> RouteEval:
    return RouteEval(False, reason, job)


def evaluate_route(world: World, ts: TractorState, jobs: list[JobDef], remaining: dict[str, float],
                   pool_avail: dict[str, int]) -> RouteEval:
    spec = ts.spec
    if not ts.functioning:
        return _fail(f"{spec.id} is broken down", jobs[0].id if jobs else None)
    t, pos, fuel, impl = ts.t, ts.pos, ts.fuel, ts.implement
    depot = world.depot
    local_pool: Counter = Counter()
    takes: Counter = Counter()
    tasks: list[Task] = []
    ends: dict[str, int] = {}
    travel = 0
    swaps: list = []
    mpc = spec.min_per_cell

    for j in jobs:
        f = world.fields[j.field]
        if f.closed:
            return _fail(f"{f.id} {f.name} is closed by the operator", j.id)
        req = j.implement
        if req not in spec.capabilities:
            return _fail(f"{spec.id} {spec.name} can't carry a {req}", j.id)
        if impl != req:
            if local_pool[req] > 0:
                local_pool[req] -= 1
            elif pool_avail.get(req, 0) - takes[req] > 0:
                takes[req] += 1
            else:
                return _fail(f"no spare {req} at the depot for {spec.id}", j.id)
            d = world.dist(pos, depot)
            if d is None:
                return _fail(f"depot unreachable for {spec.id}", j.id)
            tm = travel_minutes(d, mpc)
            if fuel - spec.burn_travel * tm / 60 < -EPS:
                return _fail(f"{spec.id} would run dry before reaching the depot", j.id)
            arrive = t + tm
            tasks.append(Task("swap", depot, None, req, tm, t, arrive, arrive, arrive + SWAP_MIN, 100.0))
            swaps.append((t, arrive + SWAP_MIN, req, impl))
            if impl:
                local_pool[impl] += 1
            impl = req
            t, pos, fuel = arrive + SWAP_MIN, depot, 100.0
            travel += tm
        area = remaining.get(j.id, j.area_ha)
        wm = work_minutes(area, spec.rate(req))
        tm = 0
        for attempt in (0, 1):
            d = world.dist(pos, f.entry)
            if d is None:
                return _fail(f"{f.id} is unreachable, route blocked", j.id)
            back = world.dist(f.entry, depot)
            if back is None:
                return _fail(f"no route back to the depot from {f.id}", j.id)
            tm = travel_minutes(d, mpc)
            tb = travel_minutes(back, mpc)
            need = spec.burn_travel * (tm + tb) / 60 + spec.burn_work * wm / 60 + FUEL_RESERVE
            if fuel + EPS >= need:
                break
            if attempt == 1 or (pos == depot and fuel >= 100 - EPS):
                return _fail(f"{j.id} needs more than a full tank on {spec.id}", j.id)
            dd = world.dist(pos, depot)
            if dd is None:
                return _fail(f"depot unreachable for {spec.id}", j.id)
            tmd = travel_minutes(dd, mpc)
            if fuel - spec.burn_travel * tmd / 60 < -EPS:
                return _fail(f"{spec.id} would run dry before reaching the depot", j.id)
            arrive = t + tmd
            tasks.append(Task("refuel", depot, None, None, tmd, t, arrive, arrive, arrive + REFUEL_MIN, 100.0))
            t, pos, fuel = arrive + REFUEL_MIN, depot, 100.0
            travel += tmd
        arrive = t + tm
        start = arrive
        if j.type == "spray":
            s = world.earliest_spray_start(arrive, wm)
            if s is None:
                return _fail(f"no safe wind window left today for spraying {f.id}", j.id)
            start = s
        end = start + wm
        if end > world.day_end:
            return _fail(f"{spec.id} can't finish {j.id} before {min_to_hhmm(world.day_end)}", j.id)
        fuel -= spec.burn_travel * tm / 60 + spec.burn_work * wm / 60
        tasks.append(Task("job", f.entry, j.id, req, tm, t, arrive, start, end, fuel))
        ends[j.id] = end
        t, pos = end, f.entry
        travel += tm
    return RouteEval(True, None, None, tasks, ends, travel, takes, t, swaps)
