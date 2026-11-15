"""Fleet Agent, deterministic planner.

Regret-insertion construction + relocate local search over a cost that encodes:
priority-weighted lateness (+ a step penalty for any late job), unassigned work, completion time,
travel, plan stability (don't reshuffle tractors that weren't affected) and tractor reliability.
Hard constraints are enforced by routing.evaluate_route (implements, capabilities, fuel, routes, wind, shift end)
and by a global depot-implement pool check.

The LLM never assigns work. It only explains what this planner decided.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .routing import RouteEval, Task, TractorState, evaluate_route
from .world import JobDef, World, min_to_hhmm

W_LATE_MIN = 4.0
W_LATE_STEP = 150.0
W_UNASSIGNED = 2500.0
W_COMPLETION = 0.25
W_TRAVEL = 1.0
W_STABILITY = 30.0
W_RISK = 0.02  # per priority weight per service-hour


OBJECTIVES = {
    "on_time": {},
    "protect_high": {"prio_power": 2.0},
    "min_travel": {"travel": 4.0, "completion": 0.08},
    "min_disruption": {"stability": 400.0},
}


@dataclass
class Plan:
    created_at: int
    routes: dict[str, list[str]] = field(default_factory=dict)
    tasks: dict[str, list[Task]] = field(default_factory=dict)
    ends: dict[str, int] = field(default_factory=dict)
    assign: dict[str, str] = field(default_factory=dict)
    unassigned: dict[str, str] = field(default_factory=dict)  # job -> human reason
    why: dict[str, dict] = field(default_factory=dict)
    travel: int = 0
    cost: float = 0.0
    strategy: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "created_at": self.created_at,
            "routes": self.routes,
            "tasks": {k: [t.to_json() for t in v] for k, v in self.tasks.items()},
            "ends": self.ends,
            "assign": self.assign,
            "unassigned": self.unassigned,
            "travel": self.travel,
        }


@dataclass
class PlanRequest:
    world: World
    now: int
    tractors: list[TractorState]
    jobs: dict[str, JobDef]  # plannable jobs (not done, not committed)
    remaining: dict[str, float]
    pool: dict[str, int]
    prev_assign: dict[str, str] = field(default_factory=dict)
    pins: dict[str, str] = field(default_factory=dict)
    pool_events: list = field(default_factory=list)  # (time, implement) drops already in progress
    weights: dict = field(default_factory=dict)  # objective overrides (see OBJECTIVES)
    deferred: dict = field(default_factory=dict)  # job -> reason (kept out of this plan on purpose)
    local_search: bool = True
    explain: bool = True


class _Solver:
    def __init__(self, req: PlanRequest):
        self.r = req
        w = req.weights or {}
        self.w_travel = w.get("travel", W_TRAVEL)
        self.w_completion = w.get("completion", W_COMPLETION)
        self.w_stability = w.get("stability", W_STABILITY)
        self.prio_power = w.get("prio_power", 1.0)
        self.ts = {t.id: t for t in req.tractors}
        self.routes: dict[str, list[str]] = {t.id: [] for t in req.tractors}
        self.evals: dict[str, RouteEval] = {}
        self.costs: dict[str, float] = {}
        for tid in self.routes:
            self._commit(tid, self._eval(tid, []))

    # ---------------------------------------------------------------- helpers
    def _avail(self, tid: str) -> dict[str, int]:
        # Permissive upper bound (current stock + every drop anyone will make); the exact
        # time-aware inventory check is _timeline_ok().
        av = Counter(self.r.pool)
        for t_, imp in self.r.pool_events:
            av[imp] += 1
        for k, ev in self.evals.items():
            if k != tid and ev is not None:
                for _, _, _, drop in ev.swaps:
                    if drop:
                        av[drop] += 1
        return dict(av)

    def _timeline_ok(self, tid: str | None = None, ev: RouteEval | None = None) -> str | None:
        """Depot inventory over time: implements are taken when a tractor leaves for a swap and dropped when the swap ends.
        Returns None if feasible, else the implement that would be missing."""
        events = [(t_, 0, imp, 1) for t_, imp in self.r.pool_events]
        for k, e in self.evals.items():
            if k == tid:
                e = ev
            if e is None:
                continue
            for dep, end, take, drop in e.swaps:
                events.append((dep, 1, take, -1))
                if drop:
                    events.append((end, 0, drop, 1))
        inv = Counter(self.r.pool)
        for _, _, imp, d in sorted(events):
            inv[imp] += d
            if inv[imp] < 0:
                return imp
        return None

    def _eval(self, tid: str, seq: list[str]) -> RouteEval:
        jobs = [self.r.jobs[j] for j in seq]
        return evaluate_route(self.r.world, self.ts[tid], jobs, self.r.remaining, self._avail(tid))

    def _cost(self, tid: str, ev: RouteEval) -> float:
        ts = self.ts[tid]
        c = self.w_travel * ev.travel
        for jid, end in ev.ends.items():
            j = self.r.jobs[jid]
            w = j.weight ** self.prio_power
            late = max(0, end - j.deadline)
            c += w * (W_LATE_MIN * late + (W_LATE_STEP if late > 0 else 0.0))
            c += self.w_completion * w * (end - self.r.now)
            c += W_RISK * w * ts.spec.hours_since_service
            prev = self.r.prev_assign.get(jid)
            if prev and prev != tid and prev in self.ts and self.ts[prev].functioning:
                c += self.w_stability
        return c

    def _commit(self, tid: str, ev: RouteEval):
        self.evals[tid] = ev
        self.costs[tid] = self._cost(tid, ev)

    def _allowed(self, jid: str, tid: str) -> bool:
        pin = self.r.pins.get(jid)
        return (pin is None or pin == tid) and self.ts[tid].functioning

    def _best_insert(self, jid: str, tid: str):
        """Best (delta, pos, eval) for inserting jid into tid's route, or (None, reason)."""
        base = self.costs[tid]
        route = self.routes[tid]
        best = None
        reason = None
        for p in range(len(route) + 1):
            seq = route[:p] + [jid] + route[p:]
            ev = self._eval(tid, seq)
            if not ev.feasible:
                if reason is None or ev.failed_job == jid:
                    reason = ev.reason
                continue
            short = self._timeline_ok(tid, ev)
            if short:
                reason = reason or f"no spare {short} free at the depot in time for {tid}"
                continue
            d = self._cost(tid, ev) - base
            if best is None or d < best[0]:
                best = (d, p, ev)
        return best, reason

    def total(self) -> float:
        return sum(self.costs.values())

    def why_unplaceable(self, jid: str, reasons: dict[str, str]) -> str:
        """Human reason per tractor: a hard constraint if the job is impossible even alone, otherwise 'no capacity'."""
        out = []
        for tid in self.routes:
            ts = self.ts[tid]
            if not ts.functioning:
                out.append(f"{tid} is broken down")
                continue
            if self.r.pins.get(jid) and self.r.pins[jid] != tid:
                continue
            alone = evaluate_route(self.r.world, ts, [self.r.jobs[jid]], self.r.remaining, self._avail(tid))
            if not alone.feasible:
                out.append(alone.reason)
            else:
                out.append(f"{tid}'s day is full with higher-value work")
        return "; ".join(dict.fromkeys(out)) or "no feasible tractor"

    def penalty(self, unassigned) -> float:
        return sum(W_UNASSIGNED * self.r.jobs[j].weight ** self.prio_power for j in unassigned)

    # ---------------------------------------------------------------- ejection
    def eject(self, unassigned: dict[str, str]):
        """Try to fit an unassigned job by bumping a lower-priority job out of a route."""
        for jid in sorted(list(unassigned), key=lambda j: (-self.r.jobs[j].weight, self.r.jobs[j].deadline)):
            if jid not in unassigned:
                continue
            wj = self.r.jobs[jid].weight
            base_total = self.total() + self.penalty(unassigned)
            best = None
            for tid in self.routes:
                if not self._allowed(jid, tid):
                    continue
                for k in list(self.routes[tid]):
                    if self.r.jobs[k].weight >= wj or self.r.pins.get(k):
                        continue
                    seq_wo = [x for x in self.routes[tid] if x != k]
                    ev_wo = self._eval(tid, seq_wo)
                    if not ev_wo.feasible or self._timeline_ok(tid, ev_wo):
                        continue
                    saved_route, saved_ev = list(self.routes[tid]), self.evals[tid]
                    self.routes[tid] = seq_wo
                    self._commit(tid, ev_wo)
                    b, _ = self._best_insert(jid, tid)
                    if b is not None:
                        rest = (set(unassigned) - {jid}) | {k}
                        tot = self.total() + b[0] + self.penalty(rest)
                        if tot < base_total - 1 and (best is None or tot < best[0]):
                            best = (tot, tid, k, b[1], b[2])
                    self.routes[tid] = saved_route
                    self._commit(tid, saved_ev)
            if best is None:
                continue
            _, tid, k, pos, ev = best
            self.routes[tid] = [x for x in self.routes[tid] if x != k]
            self.routes[tid].insert(pos, jid)
            self._commit(tid, ev)
            unassigned.pop(jid, None)
            unassigned[k] = f"bumped to make room for higher-priority {jid}"
            self.regret_insert([k], unassigned)
            if k in unassigned:
                unassigned[k] = f"bumped to make room for higher-priority {jid}; " + self.why_unplaceable(k, {})

    # ---------------------------------------------------------------- construction
    def regret_insert(self, pending: list[str], unassigned: dict[str, str]):
        pending = list(pending)
        while pending:
            choice = None  # (regret, weight, -deadline, jid, tid, pos, ev)
            dead = []
            for jid in pending:
                per_t = []
                reasons = {}
                for tid in self.routes:
                    if not self._allowed(jid, tid):
                        if not self.ts[tid].functioning:
                            reasons[tid] = f"{tid} is broken down"
                        else:
                            reasons[tid] = f"pinned to {self.r.pins.get(jid)}"
                        continue
                    best, reason = self._best_insert(jid, tid)
                    if best is None:
                        reasons[tid] = reason or "infeasible"
                    else:
                        per_t.append((best[0], tid, best[1], best[2]))
                if not per_t:
                    dead.append((jid, reasons))
                    continue
                per_t.sort(key=lambda x: x[0])
                regret = (per_t[1][0] - per_t[0][0]) if len(per_t) > 1 else 1e6
                j = self.r.jobs[jid]
                key = (regret, j.weight, -j.deadline)
                if choice is None or key > choice[0]:
                    choice = (key, jid, per_t[0][1], per_t[0][2], per_t[0][3])
            for jid, reasons in dead:
                pending.remove(jid)
                unassigned[jid] = self.why_unplaceable(jid, reasons)
            if choice is None:
                break
            _, jid, tid, pos, ev = choice
            self.routes[tid].insert(pos, jid)
            self._commit(tid, ev)
            pending.remove(jid)
            unassigned.pop(jid, None)

    # ---------------------------------------------------------------- improvement
    def relocate(self, max_rounds: int = 40):
        for _ in range(max_rounds):
            improved = False
            for tid in list(self.routes):
                for idx, jid in enumerate(list(self.routes[tid])):
                    seq_wo = self.routes[tid][:idx] + self.routes[tid][idx + 1:]
                    ev_wo = self._eval(tid, seq_wo)
                    if not ev_wo.feasible or self._timeline_ok(tid, ev_wo):
                        continue
                    saved_route, saved_ev = list(self.routes[tid]), self.evals[tid]
                    old_total = self.total()
                    self.routes[tid] = seq_wo
                    self._commit(tid, ev_wo)
                    best = None
                    for tid2 in self.routes:
                        if not self._allowed(jid, tid2):
                            continue
                        b, _ = self._best_insert(jid, tid2)
                        if b is None:
                            continue
                        if best is None or b[0] < best[0]:
                            best = (b[0], tid2, b[1], b[2])
                    if best is not None and self.total() + best[0] < old_total - 1e-6:
                        _, tid2, pos, ev = best
                        self.routes[tid2].insert(pos, jid)
                        self._commit(tid2, ev)
                        improved = True
                        break
                    self.routes[tid] = saved_route
                    self._commit(tid, saved_ev)
                if improved:
                    break
            if not improved:
                return


def plan(req: PlanRequest) -> Plan:
    s = _Solver(req)
    unassigned: dict[str, str] = {}
    order = sorted(req.jobs, key=lambda j: (-req.jobs[j].weight, req.jobs[j].deadline))
    s.regret_insert(order, unassigned)
    if req.local_search:
        s.relocate()
        if unassigned:
            s.regret_insert(list(unassigned), unassigned)
            s.eject(unassigned)
            s.relocate(10)
    out = Plan(created_at=req.now)
    for tid, seq in s.routes.items():
        ev = s.evals[tid]
        out.routes[tid] = list(seq)
        out.tasks[tid] = list(ev.tasks)
        out.ends.update(ev.ends)
        out.travel += ev.travel
        for j in seq:
            out.assign[j] = tid
    out.unassigned = {j: r for j, r in unassigned.items() if j not in out.assign}
    out.unassigned.update(req.deferred)
    out.cost = s.total() + s.penalty([j for j in out.unassigned if j in req.jobs])
    if req.explain:
        out.why = _why_records(s, out)
    return out


def _why_records(s: _Solver, out: Plan) -> dict[str, dict]:
    """For every job: which tractors could have taken it, at what ETA and cost, or why not."""
    recs = {}
    for jid, tid in out.assign.items():
        alts = []
        idx = s.routes[tid].index(jid)
        seq_wo = s.routes[tid][:idx] + s.routes[tid][idx + 1:]
        ev_wo = s._eval(tid, seq_wo)
        saved_route, saved_ev = list(s.routes[tid]), s.evals[tid]
        if ev_wo.feasible and not s._timeline_ok(tid, ev_wo):
            s.routes[tid] = seq_wo
            s._commit(tid, ev_wo)
        for t2 in s.routes:
            if not s.ts[t2].functioning:
                alts.append({"tractor": t2, "feasible": False, "reason": f"{t2} is broken down"})
                continue
            b, reason = s._best_insert(jid, t2)
            if b is None:
                alone = evaluate_route(s.r.world, s.ts[t2], [s.r.jobs[jid]], s.r.remaining, s._avail(t2))
                if alone.feasible:
                    reason = f"{t2}'s day is already full with higher-value work"
                else:
                    reason = alone.reason
                alts.append({"tractor": t2, "feasible": False, "reason": reason})
            else:
                alts.append({"tractor": t2, "feasible": True, "eta": b[2].ends[jid], "delta": round(b[0], 1),
                             "travel_to": next((t.travel_min for t in b[2].tasks if t.job_id == jid), None)})
        s.routes[tid] = saved_route
        s._commit(tid, saved_ev)
        recs[jid] = {"chosen": tid, "eta": out.ends.get(jid), "pinned": s.r.pins.get(jid) == tid, "alternatives": alts}
    for jid, reason in out.unassigned.items():
        recs[jid] = {"chosen": None, "eta": None, "reason": reason, "alternatives": []}
    return recs


def summarize(plan_: Plan, jobs: dict[str, JobDef]) -> dict:
    late = [j for j, e in plan_.ends.items() if e > jobs[j].deadline]
    return {
        "planned": len(plan_.assign),
        "on_time": len(plan_.assign) - len(late),
        "late": late,
        "unassigned": list(plan_.unassigned),
        "travel": plan_.travel,
        "weighted_on_time": sum(jobs[j].weight for j in plan_.assign if j not in late),
    }


def fmt_eta(m) -> str:
    return min_to_hhmm(m)
