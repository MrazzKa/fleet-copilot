"""Discrete-time (1 sim-minute) fleet simulator.

mode="copilot": Fleet Agent plans, Safety Agent validates, Explainer explains, replans on every event.
mode="fcfs":    the simple baseline, when a tractor is idle it takes the first compatible job in submission order.

Both modes share the same physics (routing.py / world.py) so the comparison is fair, and both are checked
every minute by the same invariant checker (no lost / duplicated work, fuel, routes, wind, implements).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from . import explain, safety
from .planner import Plan, PlanRequest, plan as run_planner, summarize
from .routing import Task, TractorState, evaluate_route
from .world import (EPS, REFUEL_MIN, SWAP_MIN, Cell, JobDef, TractorSpec, World, build, hhmm_to_min,
                    min_to_hhmm, travel_minutes, work_minutes)


@dataclass
class JobRT:
    d: JobDef
    area_done: float = 0.0
    status: str = "pending"  # pending | done
    completed_at: int | None = None
    completed_by: str | None = None
    started_at: int | None = None

    @property
    def remaining(self) -> float:
        return max(0.0, self.d.area_ha - self.area_done)


@dataclass
class TractorRT:
    spec: TractorSpec
    pos: Cell
    fuel: float
    implement: str | None
    broken: bool = False
    cur: Task | None = None
    phase: str | None = None  # travel | wait | work | action
    path: list[Cell] = field(default_factory=list)
    travel_total: int = 0
    travel_elapsed: int = 0
    wait_until: int = 0
    action_left: int = 0
    reserved: str | None = None
    queue: list[Task] = field(default_factory=list)
    travel_min: int = 0
    work_min: int = 0
    idle_min: int = 0
    wait_min: int = 0
    area_worked: float = 0.0
    history: list[list] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.spec.id

    def hist(self, now: int, kind: str, job: str | None = None):
        h = self.history
        if h and h[-1][2] == kind and h[-1][3] == job and h[-1][1] == now:
            h[-1][1] = now + 1
        else:
            h.append([now, now + 1, kind, job])

    def status(self) -> str:
        if self.broken:
            return "broken"
        if self.cur is None:
            return "idle"
        if self.phase == "action":
            return self.cur.kind  # refuel | swap
        return self.phase or "idle"


class Simulation:
    def __init__(self, sc: dict, mode: str = "copilot", explain_enabled: bool = True, resilience: bool = False,
                 local_search: bool = True, autoplan: bool = True):
        self.sc = copy.deepcopy(sc)
        self.mode = mode
        self.world, specs, jobdefs = build(self.sc)
        self.now = self.world.day_start
        self.jobs: dict[str, JobRT] = {j.id: JobRT(j) for j in jobdefs}
        self.tractors: dict[str, TractorRT] = {s.id: TractorRT(s, s.start, s.fuel, s.implement) for s in specs}
        self.pool: dict[str, int] = dict(self.world.depot_implements)
        self.assigned: dict[str, str] = {}
        self.pins: dict[str, str] = {}
        self.plan: Plan | None = None
        self.explain_enabled = explain_enabled
        self.resilience_enabled = resilience
        self.local_search = local_search
        self.resilience: dict = {}
        self.log: list[dict] = []
        self.explanations: list[dict] = []
        self.violations: list[dict] = []
        self._viol_keys: set = set()
        self._last_area: dict[str, float] = {j: 0.0 for j in self.jobs}
        self.grandfathered: set = set()
        self.replans = 0
        self.need_replan: str | None = None
        self.started = False
        self._seq = 0
        self.autoplan = autoplan  # False = agents decide when/how to plan (see fleet/agents)
        self.signals: list[dict] = []  # things that need a planning decision, consumed by the Fault Detection Agent
        self.plan_version = 0

    # ================================================================== logging
    def _log(self, kind: str, msg: str, source: str = "system", **data) -> dict:
        self._seq += 1
        e = {"id": self._seq, "t": self.now, "clock": min_to_hhmm(self.now), "kind": kind, "source": source, "msg": msg}
        if data:
            e["data"] = data
        self.log.append(e)
        return e

    def _violation(self, key: str, msg: str):
        if key in self._viol_keys:
            return
        self._viol_keys.add(key)
        self.violations.append({"t": self.now, "clock": min_to_hhmm(self.now), "msg": msg})
        self._log("violation", f"INVARIANT VIOLATION: {msg}")

    # ================================================================== state for planner
    def tractor_state(self, tr: TractorRT) -> TractorState:
        if tr.broken:
            return TractorState(tr.spec, False, self.now, tr.pos, tr.fuel, tr.implement)
        if tr.cur is None:
            return TractorState(tr.spec, True, self.now, tr.pos, tr.fuel, tr.implement)
        task = tr.cur
        t, fuel, impl = self.now, tr.fuel, tr.implement
        if tr.phase == "travel":
            rem = tr.travel_total - tr.travel_elapsed
            t += rem
            fuel -= tr.spec.burn_travel * rem / 60
        arrival = t
        committed = None
        if task.kind == "job":
            job = self.jobs[task.job_id]
            committed = job.d.id
            wm = work_minutes(job.remaining, tr.spec.rate(task.implement))
            if tr.phase == "work":
                start = self.now
            elif tr.phase == "wait":
                start = tr.wait_until
            else:
                start = arrival
                if job.d.type == "spray":
                    s = self.world.earliest_spray_start(arrival, wm)
                    start = s if s is not None else arrival
            end = start + wm
            fuel -= tr.spec.burn_work * wm / 60
        else:
            dur = REFUEL_MIN if task.kind == "refuel" else SWAP_MIN
            end = (self.now + tr.action_left) if tr.phase == "action" else arrival + dur
            fuel = 100.0
            if task.kind == "swap":
                impl = task.implement
        return TractorState(tr.spec, True, end, task.target, fuel, impl, committed)

    def committed_jobs(self) -> dict[str, str]:
        return {tr.cur.job_id: tr.id for tr in self.tractors.values() if tr.cur is not None and tr.cur.kind == "job"}

    def plannable_jobs(self) -> dict[str, JobDef]:
        com = self.committed_jobs()
        return {j.d.id: j.d for j in self.jobs.values() if j.status != "done" and j.d.id not in com}

    def plan_request(self, broken_override: str | None = None, pins: dict | None = None, explain_: bool = True,
                     strategy: dict | None = None) -> PlanRequest:
        from .planner import OBJECTIVES
        strategy = strategy or {}
        states = []
        jobs = self.plannable_jobs()
        deferred = {}
        for jid in strategy.get("defer", []) or []:
            if jid in jobs:
                jobs.pop(jid)
                deferred[jid] = f"deferred by Planner Agent, {strategy.get('rationale', 'strategy')[:80]}"
        avoid = set(strategy.get("avoid", []) or [])
        for tr in self.tractors.values():
            ts = self.tractor_state(tr)
            if broken_override == tr.id and ts.functioning:
                if ts.committed_job:
                    jobs[ts.committed_job] = self.jobs[ts.committed_job].d
                ts = TractorState(tr.spec, False, self.now, tr.pos, tr.fuel, tr.implement)
            elif tr.id in avoid and ts.functioning:
                ts = TractorState(tr.spec, False, ts.t, ts.pos, ts.fuel, ts.implement, ts.committed_job)
            states.append(ts)
        all_pins = dict(self.pins if pins is None else pins)
        all_pins.update({k: v for k, v in (strategy.get("pins") or {}).items() if k in jobs})
        return PlanRequest(world=self.world, now=self.now, tractors=states, jobs=jobs,
                           remaining={j: self.jobs[j].remaining for j in jobs},
                           pool=self._planning_pool(), prev_assign=dict(self.assigned),
                           pins=all_pins, local_search=self.local_search,
                           explain=explain_, pool_events=self.pending_drops(),
                           weights=dict(OBJECTIVES.get(strategy.get("objective", "on_time"), {})), deferred=deferred)

    def pending_drops(self) -> list[tuple[int, str]]:
        """Implements that tractors currently on a swap will leave at the depot (time, implement)."""
        out = []
        for tr in self.tractors.values():
            if not tr.broken and tr.cur is not None and tr.cur.kind == "swap" and tr.implement:
                out.append((self.tractor_state(tr).t, tr.implement))
        return out

    def _planning_pool(self) -> dict[str, int]:
        return dict(self.pool)

    # ================================================================== copilot replanning
    def replan(self, reason: str, cause: dict | None = None):
        if self.mode != "copilot":
            return
        if not self.autoplan:
            self.signals.append({"t": self.now, "reason": reason, "cause": cause})
            self.need_replan = None
            return
        new, report = self.propose()
        self.commit(new, report, reason, cause)

    def propose(self, strategy: dict | None = None) -> tuple[Plan, dict]:
        """Run the optimizer (optionally with a strategy) and the Safety Agent check. Does not change anything."""
        req = self.plan_request(explain_=self.explain_enabled, strategy=strategy)
        new = run_planner(req)
        new.strategy = dict(strategy or {"objective": "on_time"})
        return new, safety.check_plan(self, new)

    def commit(self, new: Plan, report: dict, reason: str, cause: dict | None = None, meta: dict | None = None) -> dict:
        """Apply a plan (Execution). Falls back to a hold-plan if the Safety report has hard issues."""
        old = self.plan
        req_jobs = self.plannable_jobs()
        if report["hard"]:
            self._log("safety", f"Safety Agent REJECTED the plan ({len(report['hard'])} issues), holding current tasks only",
                      issues=report["hard"])
            new = Plan(created_at=self.now, routes={t: [] for t in self.tractors}, tasks={t: [] for t in self.tractors},
                       unassigned={j: "held by Safety Agent" for j in req_jobs})
            report = safety.check_plan(self, new)
        for tr in self.tractors.values():
            tr.queue = [] if tr.broken else list(new.tasks.get(tr.id, []))
        self.assigned = {**self.committed_jobs(), **new.assign}
        self.plan = new
        self.replans += 1
        self.plan_version += 1
        self.need_replan = None
        if self.resilience_enabled:
            self.resilience = self.compute_resilience()
        exp = explain.plan_change(self, old, new, reason, cause, report)
        exp["version"] = self.plan_version
        exp["strategy"] = new.strategy
        if meta:
            exp.update(meta)
        self.explanations.append(exp)
        self._log("plan" if old is None else "replan", exp["title"], explanation_id=exp["id"], safety=report["status"])
        return exp

    def compute_resilience(self) -> dict:
        """N-1 contingency: for every working tractor, how many jobs would be at risk if it failed right now."""
        if self.plan is None:
            return {}
        base = summarize(self.plan, {j: self.jobs[j].d for j in self.jobs})
        com = self.committed_jobs()
        base_ok = set(j for j in self.plan.assign if j not in base["late"]) | {j for j in com}
        out = {}
        for tr in self.tractors.values():
            if tr.broken:
                continue
            req = self.plan_request(broken_override=tr.id, explain_=False)
            req.local_search = False
            p = run_planner(req)
            alt = summarize(p, {j: self.jobs[j].d for j in self.jobs})
            alt_ok = set(j for j in p.assign if j not in alt["late"]) | {j for j, t in com.items() if t != tr.id}
            at_risk = sorted(base_ok - alt_ok, key=lambda x: int(x[1:]) if x[1:].isdigit() else 0)
            out[tr.id] = {"at_risk": len(at_risk), "jobs": at_risk}
        return out

    # ================================================================== FCFS baseline
    def _dispatch_fcfs(self):
        pool_view = dict(self.pool)
        for tr in self.tractors.values():
            if tr.broken or tr.cur is not None or tr.queue:
                continue
            ts = self.tractor_state(tr)
            for j in sorted(self.jobs.values(), key=lambda x: x.d.order):
                if j.status == "done" or j.d.id in self.assigned:
                    continue
                ev = evaluate_route(self.world, ts, [j.d], {j.d.id: j.remaining}, pool_view)
                if ev.feasible:
                    for k, v in ev.takes.items():
                        pool_view[k] = pool_view.get(k, 0) - v
                    tr.queue = list(ev.tasks)
                    self.assigned[j.d.id] = tr.id
                    self._log("dispatch", f"FCFS: {tr.id} takes {j.d.id} ({j.d.type} {j.d.field})")
                    break

    # ================================================================== simulation step
    def finished(self) -> bool:
        return self.now >= self.world.day_end or all(j.status == "done" for j in self.jobs.values())

    def step(self) -> bool:
        if not self.started:
            self.started = True
            self._log("start", f"Day starts, {len(self.jobs)} jobs, {len(self.tractors)} tractors ({self.mode})")
            if self.mode == "copilot":
                self.replan("Initial plan for the day")
        if self.finished():
            return False
        if self.mode == "fcfs":
            self._dispatch_fcfs()
        for tr in self.tractors.values():
            self._tick(tr)
        self.now += 1
        self.check_invariants()
        if self.mode == "copilot" and self.need_replan:
            self.replan(self.need_replan)
        if self.finished():
            self._log("end", self._end_msg())
        return True

    def _end_msg(self) -> str:
        m = self.metrics()
        return f"Day over: {m['done']}/{m['jobs_total']} jobs done, {m['on_time']} on time, travel {m['travel_min']} min, idle {m['idle_min']} min"

    def run_to_end(self):
        while self.step():
            pass

    def _tick(self, tr: TractorRT):
        now = self.now
        if tr.broken:
            tr.hist(now, "broken")
            return
        if tr.cur is None and tr.queue:
            self._start_task(tr)
        if tr.cur is None:
            tr.hist(now, "idle")
            if self._could_work(tr):
                tr.idle_min += 1
            return
        task = tr.cur
        if tr.phase == "wait" and now >= tr.wait_until:
            tr.phase = "work"
        if tr.phase == "travel":
            tr.travel_elapsed += 1
            tr.fuel -= tr.spec.burn_travel / 60
            tr.travel_min += 1
            tr.hist(now, "travel", task.job_id)
            idx = min(len(tr.path) - 1, int(tr.travel_elapsed / tr.spec.min_per_cell + 1e-9))
            tr.pos = tr.path[idx]
            if tr.travel_elapsed >= tr.travel_total:
                tr.pos = tr.path[-1]
                self._arrive(tr, now + 1)
            return
        if tr.phase == "wait":
            tr.wait_min += 1
            tr.hist(now, "wait", task.job_id)
            return
        if tr.phase == "work":
            job = self.jobs[task.job_id]
            if job.d.type == "spray" and not self.world.spray_ok(now):
                self._violation(f"wind-{tr.id}-{now}", f"{tr.id} about to spray {job.d.field} outside the wind window, stopped")
                self._abort(tr, "wind outside safe window")
                return
            if job.started_at is None:
                job.started_at = now
                self._log("job_start", f"{tr.id} started {job.d.id} ({job.d.type} {job.d.field})", tractor=tr.id, job=job.d.id)
            rate = tr.spec.rate(tr.implement)
            delta = min(rate / 60, job.remaining)
            job.area_done += delta
            tr.area_worked += delta
            tr.fuel -= tr.spec.burn_work / 60
            tr.work_min += 1
            tr.hist(now, "work", job.d.id)
            if job.remaining <= 1e-6:
                tr.area_worked += job.d.area_ha - job.area_done
                job.area_done = job.d.area_ha
                job.status = "done"
                job.completed_at = now + 1
                job.completed_by = tr.id
                late = job.completed_at - job.d.deadline
                tag = "on time" if late <= 0 else f"LATE by {late} min"
                self._log("job_done", f"{tr.id} finished {job.d.id} ({job.d.type} {job.d.field}), {tag}", tractor=tr.id, job=job.d.id)
                self.assigned.pop(job.d.id, None)
                self._task_done(tr)
            return
        if tr.phase == "action":
            tr.action_left -= 1
            tr.hist(now, task.kind)
            if tr.action_left <= 0:
                if task.kind == "refuel":
                    tr.fuel = 100.0
                    self._log("refuel", f"{tr.id} refueled at the depot", tractor=tr.id)
                else:
                    if tr.implement:
                        self.pool[tr.implement] = self.pool.get(tr.implement, 0) + 1
                    old = tr.implement
                    tr.implement = task.implement
                    tr.reserved = None
                    tr.fuel = 100.0
                    self._log("swap", f"{tr.id} swapped {old} -> {task.implement} at the depot", tractor=tr.id)
                self._task_done(tr)

    def _could_work(self, tr: TractorRT) -> bool:
        """Idle counts only when there is unstarted work this tractor could do (capable + implement on board or at depot)."""
        com = self.committed_jobs()
        for j in self.jobs.values():
            if j.status == "done" or j.d.id in com or self.world.fields[j.d.field].closed:
                continue
            imp = j.d.implement
            if imp in tr.spec.capabilities and (tr.implement == imp or self.pool.get(imp, 0) > 0):
                return True
        return False

    def _task_done(self, tr: TractorRT):
        tr.cur = None
        tr.phase = None
        if self.mode == "copilot" and not tr.queue and any(
                j.status != "done" and j.d.id not in self.assigned for j in self.jobs.values()):
            self.need_replan = self.need_replan or f"{tr.id} finished its queue, checking unassigned work"

    def _start_task(self, tr: TractorRT):
        task = tr.queue.pop(0)
        if task.kind == "job":
            job = self.jobs[task.job_id]
            if job.status == "done":
                return
            if self.world.fields[job.d.field].closed:
                self._abort_task(tr, task, f"{job.d.field} is closed")
                return
        path = self.world.path(tr.pos, task.target)
        if path is None:
            self._abort_task(tr, task, "route blocked")
            return
        if task.kind == "swap":
            if self.pool.get(task.implement, 0) <= 0:
                self._abort_task(tr, task, f"no {task.implement} left at the depot")
                return
            self.pool[task.implement] -= 1
            tr.reserved = task.implement
        tr.cur = task
        tr.path = path
        tr.travel_total = travel_minutes(len(path) - 1, tr.spec.min_per_cell)
        tr.travel_elapsed = 0
        if tr.travel_total == 0:
            self._arrive(tr, self.now)
        else:
            tr.phase = "travel"

    def _arrive(self, tr: TractorRT, at: int):
        task = tr.cur
        if task.kind == "job":
            job = self.jobs[task.job_id]
            wm = work_minutes(job.remaining, tr.spec.rate(tr.implement))
            if job.d.type == "spray":
                s = self.world.earliest_spray_start(at, wm)
                if s is None:
                    self._abort(tr, "no safe wind window left")
                    return
                if s > at:
                    tr.phase = "wait"
                    tr.wait_until = s
                    return
            tr.phase = "work"
        else:
            tr.phase = "action"
            tr.action_left = REFUEL_MIN if task.kind == "refuel" else SWAP_MIN

    def _abort_task(self, tr: TractorRT, task: Task, reason: str):
        tr.cur = task
        self._abort(tr, reason)

    def _abort(self, tr: TractorRT, reason: str):
        task = tr.cur
        if task is not None and task.kind == "swap" and tr.reserved:
            self.pool[tr.reserved] = self.pool.get(tr.reserved, 0) + 1
            tr.reserved = None
        jid = task.job_id if task else None
        tr.cur = None
        tr.phase = None
        if self.mode == "fcfs":
            for t in tr.queue:
                if t.job_id:
                    self.assigned.pop(t.job_id, None)
            tr.queue = []
        if jid:
            self.assigned.pop(jid, None)
        what = f"{task.label()}" if task else "task"
        self._log("abort", f"{tr.id} stopped {what}: {reason}", tractor=tr.id, job=jid)
        if self.mode == "copilot":
            self.need_replan = self.need_replan or f"{tr.id} stopped {what}: {reason}"

    # ================================================================== events
    def resolve_job(self, ref: str) -> str | None:
        ref = (ref or "").strip().upper()
        if ref in self.jobs:
            return ref
        cands = [j for j in self.jobs.values() if j.d.field.upper() == ref and j.status != "done"]
        return cands[0].d.id if cands else None

    def apply_event(self, ev: dict, source: str = "operator") -> dict:
        """Apply an operator / judge / voice event. Returns {'ok': bool, 'msg': str}."""
        kind = ev.get("kind")
        w = self.world
        if not self.started:
            self.step_start_only()
        if kind == "breakdown":
            tr = self.tractors.get((ev.get("tractor") or "").upper())
            if tr is None or tr.broken:
                return {"ok": False, "msg": "unknown or already broken tractor"}
            where = ""
            if tr.cur and tr.cur.kind == "job":
                j = self.jobs[tr.cur.job_id]
                pct = round(100 * j.area_done / j.d.area_ha)
                where = f" while on {j.d.id} ({j.d.type} {j.d.field}, {pct}% done, progress kept)"
            elif tr.cur:
                where = f" during {tr.cur.label()}"
            if tr.cur:
                cur = tr.cur
                self._abort(tr, "breakdown")
                if cur.job_id:
                    self.assigned.pop(cur.job_id, None)
            tr.broken = True
            tr.queue = []
            for jid in [j for j, t in self.assigned.items() if t == tr.id]:
                self.assigned.pop(jid, None)
            why = f", {ev['why']}" if ev.get("why") else ""
            e = self._log("breakdown", f"{tr.id} {tr.spec.name} BROKE DOWN{where}{why}", source, tractor=tr.id)
            self.replan(f"{tr.id} broke down", e)
            return {"ok": True, "msg": e["msg"]}
        if kind == "repair":
            tr = self.tractors.get((ev.get("tractor") or "").upper())
            if tr is None or not tr.broken:
                return {"ok": False, "msg": "tractor is not broken"}
            tr.broken = False
            e = self._log("repair", f"{tr.id} {tr.spec.name} repaired and back in service", source, tractor=tr.id)
            self.replan(f"{tr.id} back in service", e)
            return {"ok": True, "msg": e["msg"]}
        if kind in ("block", "unblock"):
            target = ev.get("target", "")
            if kind == "block":
                cell = w.block(target)
                if cell is None:
                    return {"ok": False, "msg": f"unknown target {target}"}
                for tr in self.tractors.values():
                    if tr.pos == cell:
                        self.grandfathered.add((tr.id, cell))
                    self._reroute(tr)
                name = w.bridges.get(target, {}).get("name", target)
                e = self._log("block", f"{name} CLOSED, routes recalculated", source, target=target)
                self.replan(f"{name} closed", e)
            else:
                cell = w.unblock(target)
                if cell is None:
                    return {"ok": False, "msg": f"unknown target {target}"}
                name = w.bridges.get(target, {}).get("name", target)
                e = self._log("unblock", f"{name} reopened", source, target=target)
                self.replan(f"{name} reopened", e)
            return {"ok": True, "msg": e["msg"]}
        if kind == "priority":
            jid = self.resolve_job(ev.get("job", ""))
            pr = ev.get("priority", "high")
            if jid is None or pr not in ("high", "medium", "low"):
                return {"ok": False, "msg": "unknown job or priority"}
            j = self.jobs[jid]
            if j.status == "done":
                return {"ok": False, "msg": f"{jid} is already done"}
            old = j.d.priority
            j.d.priority = pr
            if ev.get("deadline"):
                j.d.deadline = hhmm_to_min(ev["deadline"])
            e = self._log("priority", f"{jid} ({j.d.type} {j.d.field}) priority {old} -> {pr}", source, job=jid)
            self.replan(f"{jid} priority changed to {pr}", e)
            return {"ok": True, "msg": e["msg"]}
        if kind == "add_job":
            fid = (ev.get("field") or "").upper()
            jtype = ev.get("type", "mow")
            if fid not in w.fields or jtype not in ("spray", "mow", "till", "seed"):
                return {"ok": False, "msg": "unknown field or job type"}
            n = 1 + max(int(k[1:]) for k in self.jobs if k[1:].isdigit())
            jid = f"J{n}"
            dl = hhmm_to_min(ev.get("deadline") or min_to_hhmm(min(w.day_end, self.now + 360)))
            jd = JobDef(jid, jtype, fid, ev.get("priority", "medium"), dl, float(w.fields[fid].area_ha), len(self.jobs), self.now)
            self.jobs[jid] = JobRT(jd)
            self._last_area[jid] = 0.0
            e = self._log("add_job", f"New job {jid}: {jtype} {fid} ({jd.priority}, due {min_to_hhmm(dl)})", source, job=jid)
            self.replan(f"new job {jid} added", e)
            return {"ok": True, "msg": e["msg"], "job": jid}
        if kind in ("close_field", "open_field"):
            fid = (ev.get("field") or "").upper()
            if fid not in w.fields:
                return {"ok": False, "msg": "unknown field"}
            w.fields[fid].closed = kind == "close_field"
            if kind == "close_field":
                for tr in self.tractors.values():
                    if tr.cur and tr.cur.kind == "job" and self.jobs[tr.cur.job_id].d.field == fid:
                        self._abort(tr, f"{fid} closed")
                e = self._log("close_field", f"{fid} {w.fields[fid].name} CLOSED ({ev.get('why', 'operator')})", source, field=fid)
            else:
                e = self._log("open_field", f"{fid} reopened", source, field=fid)
            self.replan(e["msg"], e)
            return {"ok": True, "msg": e["msg"]}
        if kind == "wind":
            mph = float(ev.get("mph", 14))
            hours = int(ev.get("hours", 2))
            w.set_wind(self.now, hours, mph)
            for tr in self.tractors.values():
                if tr.cur and tr.cur.kind == "job" and self.jobs[tr.cur.job_id].d.type == "spray" and tr.phase in ("work", "wait"):
                    j = self.jobs[tr.cur.job_id]
                    wm = work_minutes(j.remaining, tr.spec.rate(tr.implement))
                    if w.earliest_spray_start(self.now, wm) != (self.now if tr.phase == "work" else tr.wait_until):
                        self._abort(tr, f"wind {mph:g} mph, spraying paused (drift risk), progress kept")
            e = self._log("wind", f"Wind forecast update: {mph:g} mph for the next {hours} h", source, mph=mph, hours=hours)
            self.replan(f"wind {mph:g} mph for {hours} h", e)
            return {"ok": True, "msg": e["msg"]}
        if kind in ("pin", "unpin"):
            jid = self.resolve_job(ev.get("job", ""))
            if jid is None:
                return {"ok": False, "msg": "unknown job"}
            if kind == "pin":
                tid = (ev.get("tractor") or "").upper()
                if tid not in self.tractors:
                    return {"ok": False, "msg": "unknown tractor"}
                self.pins[jid] = tid
                e = self._log("pin", f"Operator pinned {jid} to {tid}", source, job=jid)
            else:
                self.pins.pop(jid, None)
                e = self._log("unpin", f"Operator unpinned {jid}", source, job=jid)
            self.replan(e["msg"], e)
            return {"ok": True, "msg": e["msg"]}
        return {"ok": False, "msg": f"unknown event {kind}"}

    def step_start_only(self):
        self.started = True
        self._log("start", f"Day starts, {len(self.jobs)} jobs, {len(self.tractors)} tractors ({self.mode})")
        if self.mode == "copilot":
            self.replan("Initial plan for the day")

    def _reroute(self, tr: TractorRT):
        if tr.broken or tr.cur is None or tr.phase != "travel":
            return
        idx = min(len(tr.path) - 1, int(tr.travel_elapsed / tr.spec.min_per_cell + 1e-9))
        ahead = tr.path[idx + 1:]
        if not any(c in self.world.blocked for c in ahead):
            return
        here = tr.path[idx]
        p = self.world.path(here, tr.cur.target)
        if p is None:
            tr.pos = here
            self._abort(tr, "route blocked, no detour")
            return
        tr.pos = here
        tr.path = p
        tr.travel_total = travel_minutes(len(p) - 1, tr.spec.min_per_cell)
        tr.travel_elapsed = 0
        self._log("reroute", f"{tr.id} rerouted around the closure (+{max(0, tr.travel_total - 0)} min to target)", tractor=tr.id)

    # ================================================================== invariants & metrics
    def check_invariants(self):
        seen = {}
        for tr in self.tractors.values():
            ids = ([tr.cur.job_id] if tr.cur and tr.cur.job_id else []) + [t.job_id for t in tr.queue if t.job_id]
            for jid in ids:
                if jid in seen and seen[jid] != tr.id:
                    self._violation(f"dup-{jid}", f"{jid} assigned to both {seen[jid]} and {tr.id}")
                seen[jid] = tr.id
                if self.jobs[jid].status == "done":
                    self._violation(f"redo-{jid}-{tr.id}", f"{tr.id} has already-completed {jid} in its queue")
            if tr.fuel < -1e-6:
                self._violation(f"fuel-{tr.id}", f"{tr.id} ran out of fuel")
            if tr.broken and (tr.cur or tr.queue):
                self._violation(f"broken-{tr.id}", f"broken {tr.id} still has tasks")
            if not tr.broken and tr.pos in self.world.blocked and (tr.id, tr.pos) not in self.grandfathered:
                self._violation(f"blocked-{tr.id}-{tr.pos}", f"{tr.id} drove onto a closed cell {tr.pos}")
            if tr.phase == "work" and tr.cur and tr.cur.kind == "job":
                j = self.jobs[tr.cur.job_id]
                if tr.implement != j.d.implement or tr.implement not in tr.spec.capabilities:
                    self._violation(f"impl-{tr.id}-{j.d.id}", f"{tr.id} working {j.d.id} with wrong implement {tr.implement}")
                if self.world.fields[j.d.field].closed:
                    self._violation(f"closed-{tr.id}-{j.d.id}", f"{tr.id} working on closed field {j.d.field}")
        total_done = 0.0
        for jid, j in self.jobs.items():
            if j.area_done > j.d.area_ha + 1e-6:
                self._violation(f"over-{jid}", f"{jid} over-worked ({j.area_done:.2f} > {j.d.area_ha})")
            if j.area_done < self._last_area.get(jid, 0.0) - 1e-9:
                self._violation(f"lost-{jid}", f"{jid} lost completed work")
            self._last_area[jid] = j.area_done
            total_done += j.area_done
        worked = sum(t.area_worked for t in self.tractors.values())
        if abs(worked - total_done) > 1e-4:
            self._violation(f"conserve-{self.now // 60}", f"area conservation broken: worked {worked:.3f} vs recorded {total_done:.3f}")

    def metrics(self) -> dict:
        done = [j for j in self.jobs.values() if j.status == "done"]
        on_time = [j for j in done if j.completed_at <= j.d.deadline]
        trs = self.tractors.values()
        return {
            "jobs_total": len(self.jobs),
            "done": len(done),
            "on_time": len(on_time),
            "late_done": len(done) - len(on_time),
            "not_done": len(self.jobs) - len(done),
            "weighted_on_time": sum(j.d.weight for j in on_time),
            "weighted_total": sum(j.d.weight for j in self.jobs.values()),
            "high_on_time": sum(1 for j in on_time if j.d.priority == "high"),
            "high_total": sum(1 for j in self.jobs.values() if j.d.priority == "high"),
            "area_done": round(sum(j.area_done for j in self.jobs.values()), 1),
            "travel_min": sum(t.travel_min for t in trs),
            "idle_min": sum(t.idle_min for t in trs),
            "work_min": sum(t.work_min for t in trs),
            "wait_min": sum(t.wait_min for t in trs),
            "replans": self.replans,
            "violations": len(self.violations),
        }
