"""Multi-agent orchestration layer.

Six agents talk only through the message bus and share one memory:

  🔎 Fault Detection ──fault/event/risk──▶ 🧠 Planner ──query──▶ 🧭 Navigation
                                              │  ◀──reply──┘
                                              ├──proposal──▶ 🛡 Safety ──approved──▶ 🚀 Execution ──command──▶ 🚜 tractors
                                              │                 └──rejected──▶ Planner      ├──ack──▶ Fault Detection (watch)
                                              └── (async) LLM deliberation ──strategy──▶ Planner   └──summary──▶ 💬 Liaison ──▶ operator

Two-speed orchestration:
  ⚡ reflex     , deterministic optimizer tool, safe plan in milliseconds; tractors never wait for a language model
  🧠 deliberate , the LLM Planner reviews the incident asynchronously with tools (simulate strategies, recall memory,
                   check routes) and may submit a better strategy; Safety still has the last word
  ♻ learned    , the outcome of a deliberation is stored in shared memory as a reflex for that incident signature,
                   so the next similar incident is solved instantly without calling the LLM
"""
from __future__ import annotations

import copy
import functools
import itertools
import queue
import threading
import time
from dataclasses import dataclass, field

from .. import explain, llm, nlp
from ..world import min_to_hhmm
from .bus import Bus, Message
from .llm_tools import config as llm_config, run_tools
from .memory import SharedMemory

DEFAULT = {"objective": "on_time"}
ROUTINE = {"repair", "unblock", "open_field", "idle", "unpin", "pin", "abort", "fuel"}
ROUTE_KINDS = {"block", "unblock", "abort"}
STRATEGY_OBJECTIVES = ["on_time", "protect_high", "min_disruption", "min_travel"]

AGENT_META = {
    "monitor": ("🔎", "Fault Detection", "#ff8a65", "Watches telemetry & events every sim-minute; detects breakdowns, closures, wind, deadline risk, low fuel; writes facts to memory"),
    "planner": ("🧠", "Planner", "#b48cff", "Routes each incident: ⚡ reflex (optimizer tool) now, 🧠 LLM deliberation in the background, ♻ learned reflexes from memory"),
    "navigation": ("🧭", "Navigation", "#4aa3ff", "A* routing on the farm grid; reports detours and unreachable fields"),
    "safety": ("🛡", "Safety", "#5ad17a", "Independently re-checks every plan (implements, fuel, routes, wind, duplicates, shift end), can veto"),
    "execution": ("🚀", "Execution", "#f5b942", "Applies approved plans, dispatches commands to tractors, acknowledges to Fault Detection"),
    "telemetry": ("🛰", "GPS Twin", "#0f766e", "Bridges T1 to a real AgOpenWeb / AgOpenGPS guidance stack over its WebSocket protocol: holds control, drives its simulator, streams GPS pose into memory, reports stalls and link loss to Fault Detection"),
    "liaison": ("💬", "Operator Liaison", "#22d3ee", "Understands operator text/voice (rules ⚡ or LLM tools 🧠), answers why / why-not, writes plain-language summaries"),
}


@dataclass
class Incident:
    id: int
    kind: str
    t: int
    text: str
    data: dict
    cause: dict | None
    signature: str = ""
    path: str = ""
    t0: float = field(default_factory=time.perf_counter)
    safe_ms: float | None = None
    delib_ms: float | None = None
    provider: str | None = None
    strategy: dict = field(default_factory=lambda: dict(DEFAULT))
    llm_strategy: dict | None = None
    rationale: str = ""
    gain: float | None = None
    status: str = "open"
    msg_from: int = 0
    msg_count: int = 0
    nav: dict | None = None

    def to_json(self) -> dict:
        return {"id": self.id, "clock": min_to_hhmm(self.t), "kind": self.kind, "text": self.text, "signature": self.signature,
                "path": self.path, "safe_ms": None if self.safe_ms is None else round(self.safe_ms, 2),
                "delib_ms": None if self.delib_ms is None else round(self.delib_ms), "provider": self.provider,
                "strategy": self.strategy, "llm_strategy": self.llm_strategy, "rationale": self.rationale,
                "gain": self.gain, "status": self.status, "messages": self.msg_count}


def score(sim, plan) -> dict:
    """Projected outcome of a plan from *now* (done work + in-progress + planned)."""
    p = explain.projection(sim, plan)
    done_late = {k for k, j in sim.jobs.items() if j.status == "done" and j.completed_at > j.d.deadline}
    ok = set(sim.jobs) - set(p["late"]) - set(p["unassigned"]) - done_late
    return {"on_time": len(ok), "total": len(sim.jobs), "weighted": sum(sim.jobs[j].d.weight for j in ok),
            "high_ok": sum(1 for j in ok if sim.jobs[j].d.priority == "high"),
            "high_total": sum(1 for j in sim.jobs.values() if j.d.priority == "high"),
            "late": sorted(set(p["late"])), "unscheduled": sorted(p["unassigned"]), "travel": p["travel"]}


def better(a: dict, b: dict) -> float:
    """>0 if outcome a beats b (priority-weighted on-time first, then high-priority, then travel)."""
    if a["weighted"] != b["weighted"]:
        return a["weighted"] - b["weighted"]
    if a["high_ok"] != b["high_ok"]:
        return a["high_ok"] - b["high_ok"]
    sa, sb = a["total"] - len(a["unscheduled"]), b["total"] - len(b["unscheduled"])
    if sa != sb:  # finishing work late still beats not doing it
        return (sa - sb) * 0.5
    return (b["travel"] - a["travel"]) / 1000.0


# ====================================================================== agents
class Agent:
    name = "agent"

    def __init__(self, o: "Orchestrator"):
        self.o = o
        self.emoji, self.title, self.color, self.role = AGENT_META[self.name]

    @property
    def sim(self):
        return self.o.sim

    @property
    def memory(self) -> SharedMemory:
        return self.o.memory

    def send(self, to: str, kind: str, text: str, data: dict | None = None, reply_to: Message | None = None,
             incident: int | None = None, ms: float | None = None) -> Message:
        m = self.o.bus.post(self.name, to, kind, text, data, reply_to.id if reply_to else None, incident, ms)
        if incident in self.o.incidents:
            self.o.incidents[incident].msg_count += 1
        return m

    def tool(self, name: str, fn, args: dict | None = None, incident: int | None = None, summary=None):
        t0 = time.perf_counter()
        err = None
        try:
            out = fn()
        except Exception as e:  # noqa: BLE001
            out, err = None, f"{type(e).__name__}: {e}"
        ms = (time.perf_counter() - t0) * 1000
        txt = summary(out) if (summary and err is None) else (err or "ok")
        argtxt = ", ".join(f"{k}={v}" for k, v in (args or {}).items())
        self.o.bus.post(self.name, self.name, "tool", f"{name}({argtxt}) -> {txt}", {"args": args or {}, "error": err}, None, incident, ms)
        if err:
            raise RuntimeError(err)
        return out

    def handle(self, msg: Message):  # pragma: no cover - overridden
        pass


class FaultDetectionAgent(Agent):
    name = "monitor"

    def __init__(self, o):
        super().__init__(o)
        self.flagged_late: set = set()
        self.flagged_fuel: set = set()

    def observe(self):
        sim = self.sim
        sigs, sim.signals = sim.signals, []
        for sig in sigs:
            kind, data = self.classify(sig)
            text = (sig.get("cause") or {}).get("msg") or sig["reason"]
            inc = self.o.new_incident(kind, text, data, sig.get("cause"))
            self.write_facts(kind, data, text)
            label = "FAULT" if kind in ("breakdown", "block", "wind", "close_field", "abort") else "EVENT"
            self.send("planner", "fault" if label == "FAULT" else "event", f"{label}: {text}", {"kind": kind}, incident=inc.id)
        self.detect_deadline_risk()
        self.detect_fuel()

    def classify(self, sig) -> tuple[str, dict]:
        cause = sig.get("cause")
        if cause:
            return cause["kind"], dict(cause.get("data") or {})
        r = sig["reason"]
        if r.startswith("Initial"):
            return "initial", {}
        if "finished its queue" in r:
            return "idle", {"tractor": r.split()[0]}
        if "stopped" in r:
            return "abort", {"tractor": r.split()[0]}
        return "other", {}

    def write_facts(self, kind, data, text):
        t = self.sim.now
        m = self.memory
        if kind == "breakdown":
            n = sum(1 for e in m.episodes if e.get("kind") == "breakdown" and e.get("tractor") == data.get("tractor")) + 1
            m.set_fact(f"tractor.{data['tractor']}", f"BROKEN since {min_to_hhmm(t)} (failure #{n} today)", self.name, t)
        elif kind == "repair":
            m.set_fact(f"tractor.{data['tractor']}", f"back in service since {min_to_hhmm(t)}", self.name, t)
        elif kind in ("block", "unblock"):
            m.set_fact(f"route.{data.get('target')}", ("CLOSED" if kind == "block" else "open") + f" since {min_to_hhmm(t)}", self.name, t)
        elif kind == "wind":
            m.set_fact("weather.wind", f"{data.get('mph')} mph for {data.get('hours')} h from {min_to_hhmm(t)}", self.name, t)
        elif kind in ("close_field", "open_field"):
            m.set_fact(f"field.{data.get('field')}", ("CLOSED" if kind == "close_field" else "open") + f" since {min_to_hhmm(t)}", self.name, t)
        elif kind in ("priority", "add_job"):
            m.set_fact(f"job.{data.get('job')}", text, self.name, t)

    def detect_deadline_risk(self):
        sim = self.sim
        if sim.plan is None:
            return
        late_now = {j for j, e in sim.plan.ends.items() if e > sim.jobs[j].d.deadline and sim.jobs[j].d.priority == "high"}
        new = late_now - self.flagged_late
        self.flagged_late = late_now
        if new:
            jl = ", ".join(sorted(new))
            inc = self.o.new_incident("risk", f"High-priority {jl} projected to miss its deadline", {"jobs": sorted(new), "priority": "high"}, None)
            self.send("planner", "risk", f"RISK: high-priority {jl} trending late, can we do better?", incident=inc.id)

    def detect_fuel(self):
        for tr in self.sim.tractors.values():
            if tr.broken:
                continue
            planned_refuel = any(t.kind in ("refuel", "swap") for t in ([tr.cur] if tr.cur else []) + tr.queue)
            if tr.fuel < 15 and not planned_refuel and tr.id not in self.flagged_fuel:
                self.flagged_fuel.add(tr.id)
                inc = self.o.new_incident("fuel", f"{tr.id} fuel {tr.fuel:.0f}% with no refuel planned", {"tractor": tr.id}, None)
                self.send("planner", "risk", f"RISK: {tr.id} fuel low ({tr.fuel:.0f}%)", incident=inc.id)
            elif tr.fuel > 40:
                self.flagged_fuel.discard(tr.id)

    def handle(self, msg: Message):
        if msg.kind == "ack":
            self.memory.set_fact("plan.version", msg.text, self.name, self.sim.now)
        elif msg.sender == "telemetry" and msg.kind in ("fault", "event") and msg.data.get("event"):
            ev = dict(msg.data["event"])
            r = self.sim.apply_event(ev, "gps-telemetry")
            if self.o.on_event:
                self.o.on_event(ev, "gps-telemetry", r)


class PlannerAgent(Agent):
    name = "planner"

    def handle(self, msg: Message):
        inc = self.o.incidents.get(msg.incident)
        if inc is None:
            return
        if msg.kind in ("fault", "event", "risk"):
            if inc.kind in ROUTE_KINDS and inc.nav is None:
                self.send("navigation", "query", "Route impact? Which tractors need a detour, which fields are unreachable?", reply_to=msg, incident=inc.id)
                return
            self.plan_incident(inc)
        elif msg.kind == "reply":
            inc.nav = msg.data
            self.plan_incident(inc)
        elif msg.kind == "rejected":
            if inc.strategy != DEFAULT:
                inc.strategy = dict(DEFAULT)
                self.propose(inc, DEFAULT, "reflex-fallback")
            else:
                self.send("execution", "approved", "Safety rejected twice, hold current tasks only", {"hold": True}, incident=inc.id)
        elif msg.kind == "strategy":
            self.consider_strategy(inc, msg.data)

    # ------------------------------------------------------------------ routing
    def route(self, inc: Incident) -> tuple[str, dict, bool]:
        inc.signature = signature(inc, self.sim)
        if inc.kind in ROUTINE:
            return "reflex", dict(DEFAULT), False
        r = self.memory.lookup_reflex(inc.signature)
        if r:
            return "learned", dict(r["strategy"]), False
        return "reflex", dict(DEFAULT), self.o.deliberate_enabled

    def plan_incident(self, inc: Incident):
        path, strat, deliberate = self.route(inc)
        inc.path, inc.strategy = path, strat
        label = {"reflex": "⚡ reflex", "learned": "♻ learned reflex"}[path]
        if path == "learned":
            r = self.memory.reflexes.get(inc.signature, {})
            self.send(self.name, "thought", f"{label}: seen '{inc.signature}' before, reusing strategy {strat.get('objective')} "
                                             f"(learned by {r.get('source')} in {r.get('learned_ms')} ms, no LLM call needed)", incident=inc.id)
        if deliberate and inc.kind not in ("idle", "fuel"):
            self.o.start_deliberation(inc)
        if inc.kind in ("risk", "idle", "fuel") and not self.worth_replanning(inc, strat):
            inc.status = "resolved" if inc.id not in self.o.deliberating else "plan kept, deliberating"
            inc.safe_ms = (time.perf_counter() - inc.t0) * 1000
            self.send(self.name, "thought", "Current plan is already the best available, no change sent", incident=inc.id)
        else:
            self.propose(inc, strat, path)

    def worth_replanning(self, inc, strat) -> bool:
        sim = self.sim
        if sim.plan is None:
            return True
        plan, _ = self.tool("optimize_plan", lambda: sim.propose(strat), {"objective": strat.get("objective")}, inc.id,
                            lambda r: f"{len(r[0].assign)} assignments")
        plannable = sim.plannable_jobs()
        cur_assign = {j: t for j, t in sim.plan.assign.items() if j in plannable}
        if plan.assign == cur_assign:
            return False
        return better(score(sim, plan), score(sim, sim.plan)) >= 0

    def propose(self, inc: Incident, strat: dict, path: str):
        sim = self.sim
        plan, report = self.tool("optimize_plan", lambda: sim.propose(strat), {"objective": strat.get("objective"),
                                 **({"defer": strat["defer"]} if strat.get("defer") else {}), **({"avoid": strat["avoid"]} if strat.get("avoid") else {})},
                                 inc.id, lambda r: f"{score(sim, r[0])['on_time']}/{len(sim.jobs)} projected on time, travel {r[0].travel} min")
        sc = score(sim, plan)
        self.send("safety", "proposal", f"Plan candidate [{strat.get('objective')}] for: {inc.text[:70]}, {sc['on_time']}/{sc['total']} on time, "
                                        f"high-priority {sc['high_ok']}/{sc['high_total']}",
                  {"plan": plan, "report": report, "strategy": strat, "path": path}, incident=inc.id)

    # ------------------------------------------------------------------ deliberation result
    def consider_strategy(self, inc: Incident, data: dict):
        sim = self.sim
        self.o.deliberating.discard(inc.id)
        if not self.o.deliberating:
            self.o.busy.discard(self.name)
        inc.delib_ms = data.get("ms")
        inc.provider = data.get("provider")
        inc.rationale = data.get("rationale", "")
        strat = data.get("strategy") or dict(DEFAULT)
        inc.llm_strategy = strat
        if data.get("error") and not data.get("strategy"):
            self.send(self.name, "thought", f"Deliberation failed ({data['error']}), reflex plan stays in force", incident=inc.id)
            inc.status = "resolved"
            return
        cur_plan, _ = sim.propose(inc.strategy)
        new_plan, new_rep = sim.propose(strat)
        a, b = score(sim, new_plan), score(sim, cur_plan)
        gain = better(a, b)
        inc.gain = round(gain, 3)
        winner = strat if gain >= 0 else inc.strategy
        self.memory.learn_reflex(inc.signature, winner, data.get("provider") or "rules", gain, inc.delib_ms or 0,
                                 inc.rationale if gain >= 0 else f"deliberation tried {strat.get('objective')} but the reflex plan was better", sim.now)
        self.memory.update_episode(inc.id, llm_strategy=strat, gain=inc.gain, provider=inc.provider, delib_ms=round(inc.delib_ms or 0))
        self.send("monitor", "memory", f"Learned reflex for '{inc.signature}' -> {winner.get('objective')} (next time: instant, no LLM)", incident=inc.id)
        if gain > 0 and not new_rep["hard"]:
            self.send("safety", "proposal", f"🧠 Deliberated strategy [{strat.get('objective')}] beats reflex: {a['weighted']} vs {b['weighted']} weighted on-time"
                                            f" ({a['on_time']} vs {b['on_time']} jobs). {inc.rationale[:120]}",
                      {"plan": new_plan, "report": new_rep, "strategy": strat, "path": "deliberate"}, incident=inc.id)
        else:
            inc.status = "resolved"
            self.send(self.name, "thought", f"🧠 Deliberation ({inc.provider}, {inc.delib_ms / 1000:.1f}s) confirms the reflex plan "
                                            f"[{strat.get('objective')}: {a['weighted']} vs {b['weighted']}], no change needed", incident=inc.id)


class NavigationAgent(Agent):
    name = "navigation"

    def handle(self, msg: Message):
        if msg.kind != "query":
            return
        sim, w = self.sim, self.sim.world

        def impact():
            detours, stuck = [], []
            for tr in sim.tractors.values():
                if tr.broken or tr.cur is None:
                    continue
                d = w.dist(tr.pos, tr.cur.target)
                if d is None:
                    stuck.append(tr.id)
                else:
                    detours.append(f"{tr.id}->{tr.cur.label()} {d} cells")
            unreachable = [f.id for f in w.fields.values() if w.dist(w.depot, f.entry) is None]
            return {"detours": detours, "stuck": stuck, "unreachable": unreachable, "closed": list(w.blocked_names)}
        r = self.tool("route_impact", impact, {}, msg.incident,
                      lambda r: f"closed={r['closed'] or 'none'}, unreachable fields={r['unreachable'] or 'none'}")
        for name in r["closed"]:
            self.memory.set_fact(f"route.{name}", "CLOSED, A* avoids it", self.name, sim.now)
        txt = f"Closures: {', '.join(r['closed']) or 'none'}. Tractors en route re-pathed: {'; '.join(r['detours']) or 'none'}." + \
              (f" Unreachable fields: {', '.join(r['unreachable'])}." if r["unreachable"] else " All fields still reachable.")
        self.send("planner", "reply", txt, r, reply_to=msg, incident=msg.incident)


class SafetyAgent(Agent):
    name = "safety"

    def handle(self, msg: Message):
        if msg.kind != "proposal":
            return
        from .. import safety
        plan = msg.data["plan"]
        rep = self.tool("verify_plan", lambda: safety.check_plan(self.sim, plan), {"checks": 8}, msg.incident,
                        lambda r: f"{r['status'].upper()} ({len(r['hard'])} hard, {len(r['warnings'])} warnings)")
        data = dict(msg.data)
        data["report"] = rep
        if rep["hard"]:
            self.send("planner", "rejected", "⛔ VETO: " + "; ".join(rep["hard"][:3]), data, reply_to=msg, incident=msg.incident)
        else:
            self.send("execution", "approved", f"✅ Approved, all 8 checks passed" + (f", {len(rep['warnings'])} warning(s)" if rep["warnings"] else ""),
                      data, reply_to=msg, incident=msg.incident)


class ExecutionAgent(Agent):
    name = "execution"

    def handle(self, msg: Message):
        if msg.kind != "approved":
            return
        sim = self.sim
        inc = self.o.incidents.get(msg.incident)
        if msg.data.get("hold"):
            return
        before = {t.id: [x.label() for x in t.queue[:3]] for t in sim.tractors.values()}
        path = msg.data.get("path", "reflex")
        exp = self.tool("dispatch_plan", lambda: sim.commit(msg.data["plan"], msg.data["report"], inc.text if inc else "plan",
                                                            inc.cause if inc else None,
                                                            {"path": path, "incident": inc.id if inc else None,
                                                             "agent_strategy": msg.data.get("strategy")}),
                        {"version": sim.plan_version + 1}, msg.incident, lambda e: f"plan v{e['version']} live")
        if inc:
            if inc.safe_ms is None:
                inc.safe_ms = (time.perf_counter() - inc.t0) * 1000
            if path == "deliberate":
                inc.status = "improved"
            elif inc.status == "open":
                inc.status = "reflex plan live, deliberating" if inc.id in self.o.deliberating else "resolved"
        for tr in sim.tractors.values():
            now_q = [x.label() for x in tr.queue[:3]]
            if now_q != before[tr.id] and not tr.broken:
                eta = next((x.est_end for x in tr.queue if x.kind == "job"), None)
                self.send(f"tractor:{tr.id}", "command", f"{tr.id}: " + (" -> ".join(now_q) if now_q else "finish current task, then standby")
                          + (f" (next job ETA {min_to_hhmm(eta)})" if eta else ""), incident=msg.incident)
        self.send("monitor", "ack", f"plan v{exp['version']} executing, watch deadlines & fuel", incident=msg.incident)
        self.send("liaison", "summary", exp["title"], {"exp_id": exp["id"]}, incident=msg.incident)
        self.memory.record_episode({"incident": inc.id if inc else None, "t": min_to_hhmm(sim.now), "kind": inc.kind if inc else "plan",
                                    "tractor": (inc.data or {}).get("tractor") if inc else None, "text": inc.text if inc else "",
                                    "path": path, "strategy": msg.data.get("strategy"), "plan_version": exp["version"],
                                    "impact": exp.get("impact_text"), "safe_ms": round(inc.safe_ms, 2) if inc and inc.safe_ms else None})


class LiaisonAgent(Agent):
    name = "liaison"

    def handle(self, msg: Message):
        if msg.kind == "summary":
            self.o.polish_queue.append(msg.data.get("exp_id"))
            self.send("operator", "summary", f"📣 {msg.text}", incident=msg.incident)
        elif msg.kind == "command":
            self.command(msg.text, msg.data.get("source", "operator"))

    def command(self, text: str, source: str = "operator"):
        sim = self.sim
        t0 = time.perf_counter()
        cmds = nlp.regex_parse(text, sim)
        ms = (time.perf_counter() - t0) * 1000
        if cmds:
            self.o.bus.post(self.name, self.name, "tool", f"parse_rules(“{text[:50]}”) -> {[c['kind'] for c in cmds]}", {"args": {"text": text}}, None, None, ms)
            self.send(self.name, "thought", f"⚡ understood instantly by rules ({ms:.1f} ms), no LLM needed")
            for c in cmds:
                self.o.apply_command(c, source)
            return
        if llm_config() is None or not self.o.use_llm:
            self.send("operator", "reply", "Sorry, I didn't understand that and no LLM is configured. Try “T2 broke down”, “close north bridge”, “F8 urgent by 15:00”, “why not T3 for J5”.")
            return
        self.o.busy.add(self.name)
        self.send(self.name, "thought", "🧠 not a known phrase, asking the LLM with operator tools")
        self.o.run_bg(lambda: self._llm_command(text), lambda r: self._llm_done(r, source))

    def _llm_command(self, text):
        queued: list[dict] = []
        sim = self.sim

        def ev(kind, **kw):
            queued.append({"kind": kind, **{k: v for k, v in kw.items() if v not in (None, "")}})
            return {"queued": kind, **kw}
        S = lambda **p: {"type": "object", "properties": p, "required": [k for k in p if not k.startswith("opt_")]}  # noqa: E731
        st = {"type": "string"}
        tools = [
            {"name": "report_breakdown", "description": "A tractor broke down / is out of service", "parameters": S(tractor=st), "fn": lambda tractor: ev("breakdown", tractor=tractor)},
            {"name": "report_repair", "description": "A tractor is repaired / back in service", "parameters": S(tractor=st), "fn": lambda tractor: ev("repair", tractor=tractor)},
            {"name": "close_route", "description": "Close a bridge: north_bridge or south_bridge", "parameters": S(target=st), "fn": lambda target: ev("block", target=target)},
            {"name": "reopen_route", "description": "Reopen a bridge: north_bridge or south_bridge", "parameters": S(target=st), "fn": lambda target: ev("unblock", target=target)},
            {"name": "set_priority", "description": "Change job priority (high/medium/low), optional new deadline HH:MM. job may be a job id (J5) or field id (F7)",
             "parameters": {"type": "object", "properties": {"job": st, "priority": st, "deadline": st}, "required": ["job", "priority"]},
             "fn": lambda job, priority, deadline=None: ev("priority", job=job, priority=priority, deadline=deadline)},
            {"name": "add_job", "description": "Add a new job. type: spray|mow|till|seed; field like F3; priority; optional deadline HH:MM",
             "parameters": {"type": "object", "properties": {"type": st, "field": st, "priority": st, "deadline": st}, "required": ["type", "field"]},
             "fn": lambda type, field, priority="medium", deadline=None: ev("add_job", type=type, field=field, priority=priority, deadline=deadline)},
            {"name": "close_field", "description": "Field is closed (flooded, wet, people working)", "parameters": {"type": "object", "properties": {"field": st, "why": st}, "required": ["field"]},
             "fn": lambda field, why="operator": ev("close_field", field=field, why=why)},
            {"name": "wind_update", "description": "Wind forecast: mph for the next N hours", "parameters": {"type": "object", "properties": {"mph": {"type": "number"}, "hours": {"type": "integer"}}, "required": ["mph"]},
             "fn": lambda mph, hours=2: ev("wind", mph=mph, hours=hours)},
            {"name": "explain_job", "description": "Explain why a job is assigned the way it is", "parameters": S(job=st), "fn": lambda job: ev("why", job=job)},
            {"name": "explain_alternative", "description": "Why not give a job to another tractor (counterfactual)", "parameters": S(job=st, tractor=st), "fn": lambda job, tractor: ev("whynot", job=job, tractor=tractor)},
            {"name": "set_policy", "description": "Store a standing operator policy in shared memory (e.g. 'never use T2 for spraying near houses')", "parameters": S(policy=st),
             "fn": lambda policy: ev("policy", text=policy)},
        ]
        ctx = {"tractors": {k: v.spec.name for k, v in sim.tractors.items()}, "fields": {k: f.name for k, f in sim.world.fields.items()},
               "jobs": {k: f"{j.d.type} {j.d.field} {j.d.priority}" for k, j in sim.jobs.items()}}
        res = run_tools("You are the Operator Liaison agent of a farm fleet. Turn the operator's message into tool calls. Use only IDs from the context. "
                        "Call every tool that applies, then reply with one short confirmation sentence.",
                        f"Context: {ctx}\nOperator: {text}", tools, max_steps=4,
                        on_call=lambda n, a, out, ms: self.o.bus.post(self.name, self.name, "tool", f"{n}({a})", {"args": a}, None, None, ms),
                        on_retry=lambda c: (queued.clear(), self.o.bus.post(self.name, self.name, "thought", f"⚠ provider failed, failing over to {c['name']}")))
        res["queued"] = queued
        return res

    def _llm_done(self, r, source):
        self.o.busy.discard(self.name)
        st = self.o.bus.stats[self.name]
        st["llm_calls"] += r.get("llm_calls", 0)
        st["llm_ms"] += r.get("ms", 0)
        if not r["queued"]:
            self.send("operator", "reply", r.get("text") or f"(LLM could not map that to an action: {r.get('error')})")
            return
        self.send(self.name, "thought", f"🧠 {r['provider']} mapped it to {[q['kind'] for q in r['queued']]} in {r['ms'] / 1000:.1f}s")
        for c in r["queued"]:
            self.o.apply_command(c, source)


class TelemetryAgent(Agent):
    """Digital twin of one tractor in a real AgOpenWeb (AgOpenGPS) instance."""
    name = "telemetry"

    def __init__(self, o, bridge):
        super().__init__(o)
        self.b = bridge
        self.moving_cmd = None
        self.cmd_at = 0.0
        self.seen_moving = False
        self.slow_since = None
        self.fast_since = None
        self.gps_broke = False
        self.saw_broken = False
        self.lost_reported = False
        self.warned_observer = False
        self.last_fact = 0.0
        self.note = ""
        self.injected = False  # demo fault: the vehicle is "physically" stuck until Resume

    def desired(self) -> bool:
        tr = self.sim.tractors.get(self.b.tractor)
        return tr is not None and not tr.broken and tr.status() in ("travel", "work", "refuel", "swap")

    def drive(self, on: bool, why: str):
        if self.injected:  # a stuck vehicle ignores drive commands; telemetry must reveal it
            self.moving_cmd, self.cmd_at = on, time.time()
            self.seen_moving = self.seen_moving or on
            return
        if on:
            self.b.send("sim.stop", "sim.setSteer|7", *(["sim.speedUp"] * 16))
        else:
            self.b.send("sim.stop")
        self.moving_cmd, self.cmd_at, self.seen_moving, self.slow_since = on, time.time(), False, None
        self.send(f"tractor:{self.b.tractor}", "command",
                  f"AgOpenWeb <- {'drive ~8 kph (sim.speedUp×16, steer 7°)' if on else 'sim.stop'}, {why}")

    def inject_gps_fault(self):
        """Demo: really stop the vehicle inside AgOpenWeb; detection must come from telemetry alone."""
        self.b.send("sim.stop")
        self.injected = True
        if self.moving_cmd:
            self.seen_moving = True  # it was commanded to drive, so a stop now is a real stall
        self.note = "GPS fault injected in AgOpenWeb (sim.stop), waiting for telemetry to show it"
        self.send(f"tractor:{self.b.tractor}", "command", "AgOpenWeb <- sim.stop (fault injection, agents are NOT told; they must notice from GPS)")

    def resume(self):
        self.injected = False
        self.b.send("sim.stop", "sim.setSteer|7", *(["sim.speedUp"] * 16))
        self.note = "Resume sent to AgOpenWeb, waiting for the twin to move again"

    def observe(self):
        b, now, tid = self.b, time.time(), self.b.tractor
        if not b.connected:
            if b.ever_connected and b.disconnected_at and now - b.disconnected_at > 5 and self.desired() and not self.lost_reported:
                self.lost_reported = self.gps_broke = True
                self.send("monitor", "fault", f"GPS telemetry from {tid} lost for {now - b.disconnected_at:.0f}s (AgOpenWeb link down), safe-stop {tid}",
                          {"event": {"kind": "breakdown", "tractor": tid, "why": "GPS telemetry lost (AgOpenWeb link down)"},
                           "stalled_s": now - b.disconnected_at})
            return
        self.lost_reported = False
        if not b.has_control:
            if not self.warned_observer and b.ticks > 20:
                self.warned_observer = True
                self.note = f"observer only, control held by '{b.holder}'. Close other AgOpenWeb tabs or press 'Take control'."
            return
        self.warned_observer = False
        want = self.desired()
        if want != self.moving_cmd and not (self.gps_broke and not want):
            self.drive(want, "plan says " + ("work/travel" if want else "standby/broken"))
        elif self.gps_broke and not want and self.moving_cmd:
            self.moving_cmd = False
        sp = b.speed()
        if self.moving_cmd and sp > 1.0:
            self.seen_moving = True
            self.note = ""
        if self.moving_cmd and self.seen_moving and b.fresh() and not self.gps_broke:
            if sp < 0.3:
                self.slow_since = self.slow_since or now
                if now - self.slow_since > 4:
                    self.gps_broke = True
                    self.send("monitor", "fault", f"GPS: {tid} stopped in AgOpenWeb (speed {sp:.1f} for {now - self.slow_since:.0f}s) while the plan has it working",
                              {"event": {"kind": "breakdown", "tractor": tid, "why": "GPS shows the vehicle stopped (AgOpenWeb telemetry)"},
                               "stalled_s": now - self.slow_since})
            else:
                self.slow_since = None
        if self.gps_broke:
            tr = self.sim.tractors.get(tid)
            if tr is not None and tr.broken:
                self.saw_broken = True
            if tr is not None and not tr.broken and self.saw_broken:
                self.gps_broke = self.saw_broken = False  # repaired by someone else
            elif tr is not None and tr.broken and sp > 1.0 and b.fresh():
                self.fast_since = self.fast_since or now
                if now - self.fast_since > 3:
                    self.gps_broke, self.fast_since, self.saw_broken = False, None, False
                    self.moving_cmd = None
                    self.send("monitor", "event", f"GPS: {tid} moving again in AgOpenWeb ({sp:.1f}), back in service",
                              {"event": {"kind": "repair", "tractor": tid}, "stalled_s": now - self.fast_since if self.fast_since else 3})
            else:
                self.fast_since = None
        if b.pose and now - self.last_fact > 2:
            e, n, _, s_ = b.pose
            self.memory.set_fact(f"gps.{tid}", f"AgOpenWeb E {e:.1f} N {n:.1f} · {s_:.1f} kph · fix {b.fix} · {b.dist_m:.0f} m driven", self.name, self.sim.now)
            self.last_fact = now


# ====================================================================== helpers
def signature(inc: Incident, sim) -> str:
    k, d = inc.kind, inc.data or {}
    if k == "breakdown":
        tr = sim.tractors.get(d.get("tractor"))
        caps = "+".join(sorted(tr.spec.capabilities)) if tr else "?"
        return f"breakdown|{caps}"
    if k in ("block", "unblock"):
        return f"{k}|{d.get('target')}"
    if k == "wind":
        return f"wind|{'gust' if float(d.get('mph', 0)) > 10 else 'calm'}"
    if k == "priority":
        j = sim.jobs.get(d.get("job"))
        return f"priority|{j.d.priority if j else '?'}|{j.d.type if j else '?'}"
    if k == "add_job":
        j = sim.jobs.get(d.get("job"))
        return f"add_job|{j.d.type if j else '?'}|{j.d.priority if j else '?'}"
    if k == "risk":
        return f"risk|{d.get('priority')}"
    if k == "initial":
        return f"initial|{sim.world.name}"
    return k


def situation(sim, inc_text: str) -> dict:
    com = sim.committed_jobs()
    jobs = []
    for j in sim.jobs.values():
        tid = com.get(j.d.id) or (sim.plan.assign.get(j.d.id) if sim.plan else None)
        eta = sim.plan.ends.get(j.d.id) if sim.plan else None
        jobs.append({"id": j.d.id, "type": j.d.type, "field": j.d.field, "priority": j.d.priority, "deadline": min_to_hhmm(j.d.deadline),
                     "status": j.status if j.status == "done" else ("in_progress" if j.d.id in com else ("planned" if tid else "unscheduled")),
                     "tractor": tid, "eta": min_to_hhmm(eta) if eta else None, "pct_done": round(100 * j.area_done / j.d.area_ha)})
    return {"time": min_to_hhmm(sim.now), "incident": inc_text,
            "tractors": [{"id": t.id, "name": t.spec.name, "status": t.status(), "implement": t.implement, "can_carry": t.spec.capabilities,
                          "fuel_pct": round(t.fuel), "hours_since_service": t.spec.hours_since_service} for t in sim.tractors.values()],
            "jobs": jobs, "depot_spares": sim.pool, "closed_bridges": list(sim.world.blocked_names),
            "closed_fields": [f.id for f in sim.world.fields.values() if f.closed],
            "safe_spray_windows": [f"{min_to_hhmm(a)}-{min_to_hhmm(b)}" for a, b in sim.world.spray_windows()]}


PLANNER_SYSTEM = """You are the Planner agent in a multi-agent coordinator for autonomous tractors.
A deterministic optimizer tool already produced a SAFE reflex plan; your job is to decide whether a different STRATEGY is better for this incident.
Strategy levers: objective (on_time | protect_high | min_disruption | min_travel), defer (job ids to postpone today),
avoid (tractor ids to keep off new work, e.g. unreliable), pins ({job: tractor}).
Process: 1) recall memory about this incident type and operator policy, 2) simulate_strategy for at least 2-3 options,
3) submit_strategy with the best one and a one-sentence rationale citing the numbers. Never invent job or tractor ids.
Prefer higher priority-weighted on-time score; break ties by fewer late high-priority jobs, then less travel. Be fast: at most 6 tool calls."""


# ====================================================================== orchestrator
class Orchestrator:
    def __init__(self, sim, memory_path=None, async_llm: bool = True, deliberate: bool = True, use_llm: bool = True,
                 bridge=None):
        self.sim = sim
        self.use_llm = use_llm
        self.bus = Bus(lambda: sim.now)
        self.memory = SharedMemory(memory_path)
        self.async_llm = async_llm
        self.deliberate_enabled = deliberate
        self.monitor = FaultDetectionAgent(self)
        self.planner = PlannerAgent(self)
        self.navigation = NavigationAgent(self)
        self.safety = SafetyAgent(self)
        self.execution = ExecutionAgent(self)
        self.liaison = LiaisonAgent(self)
        self.agents = {a.name: a for a in (self.monitor, self.planner, self.navigation, self.safety, self.execution, self.liaison)}
        self.telemetry = TelemetryAgent(self, bridge) if bridge is not None else None
        if self.telemetry:
            self.agents["telemetry"] = self.telemetry
        for n in self.agents:
            self.bus.register(n)
        self.incidents: dict[int, Incident] = {}
        self._ids = itertools.count(1)
        self.results: queue.Queue = queue.Queue()
        self.busy: set = set()
        self.polish_queue: list = []
        self.answers: list = []
        self.human_interventions = 0
        self.deliberations = 0
        self.deliberating: set = set()

    # ------------------------------------------------------------------ loop
    def tick(self):
        self.collect()
        if self.telemetry:
            self.telemetry.observe()
        self.monitor.observe()
        self.drain()

    def drain(self, limit: int = 400):
        n = 0
        while self.bus.pending() and n < limit:
            for name, q in self.bus.inboxes.items():
                while q and n < limit:
                    msg = q.popleft()
                    self.agents[name].handle(msg)
                    n += 1

    def collect(self):
        while True:
            try:
                cb = self.results.get_nowait()
            except queue.Empty:
                return
            cb()

    def run_bg(self, fn, done):
        if not self.async_llm:  # deterministic mode (tests): run the work at the next tick, after the reflex plan is live
            self.results.put(lambda: done(fn()))
            return
        def work():
            try:
                r = fn()
            except Exception as e:  # noqa: BLE001
                r = {"error": f"{type(e).__name__}: {e}", "ok": False, "queued": [], "calls": [], "ms": 0}
            self.results.put(lambda: done(r))
        threading.Thread(target=work, daemon=True).start()

    def new_incident(self, kind, text, data, cause) -> Incident:
        inc = Incident(next(self._ids), kind, self.sim.now, text, data, cause, msg_from=len(self.bus.log))
        self.incidents[inc.id] = inc
        if len(self.incidents) > 300:
            for k in sorted(self.incidents)[:50]:
                self.incidents.pop(k)
        return inc

    # ------------------------------------------------------------------ operator
    def operator_command(self, text: str, source: str = "operator"):
        self.human_interventions += 1
        self.bus.post("operator", "liaison", "command", text, {"source": source})
        self.drain()
        self.monitor.observe()
        self.drain()

    def apply_command(self, c: dict, source: str):
        k = c.get("kind")
        if k == "why":
            r = explain.why(self.sim, c.get("job", ""))
            self.answers.append({"id": time.time_ns(), "q": f"why {c.get('job')}", "text": r["text"], "whynot": r.get("whynot", []), "job": r.get("job")})
            self.liaison.send("operator", "reply", r["text"])
        elif k == "whynot":
            r = explain.why_not(self.sim, c.get("job", ""), c.get("tractor", ""))
            self.answers.append({"id": time.time_ns(), "q": f"why not {c.get('tractor')} for {c.get('job')}", "text": r["text"]})
            self.liaison.send("operator", "reply", r["text"])
        elif k == "policy":
            self.memory.policies.append(c.get("text", ""))
            self.memory.save()
            self.liaison.send("operator", "reply", f"Policy stored in shared memory: “{c.get('text')}”")
        else:
            r = self.sim.apply_event(c, source)
            if self.on_event:
                self.on_event(c, source, r)
            self.liaison.send("operator", "reply", r.get("msg", ""))

    on_event = None  # server hook to mirror events into the FCFS ghost

    # ------------------------------------------------------------------ deliberation
    def start_deliberation(self, inc: Incident):
        snap = copy.deepcopy(self.sim)
        self.busy.add("planner")
        self.deliberating.add(inc.id)
        self.deliberations += 1
        cfg = llm_config() if self.use_llm else None
        who = f"{cfg['name']}:{cfg['model']}" if cfg else "rules (no LLM configured)"
        self.planner.send(self.planner.name, "thought", f"🧠 deliberating in background with {who}, reflex plan already safe & live", incident=inc.id)
        self.run_bg(lambda: self._deliberate(snap, inc, cfg), lambda r: self._deliberated(inc, r))

    def _deliberate(self, snap, inc: Incident, cfg) -> dict:
        t0 = time.perf_counter()
        sims: dict[str, dict] = {}
        final: dict = {}

        def simulate(objective="on_time", defer=None, avoid=None, pins=None):
            if objective not in STRATEGY_OBJECTIVES:
                return {"error": f"objective must be one of {STRATEGY_OBJECTIVES}"}
            strat = {"objective": objective, "defer": [d for d in (defer or []) if d in snap.jobs],
                     "avoid": [a for a in (avoid or []) if a in snap.tractors], "pins": {k: v for k, v in (pins or {}).items() if k in snap.jobs and v in snap.tractors}}
            plan, rep = snap.propose(strat)
            sc = score(snap, plan)
            changed = sum(1 for j, t in plan.assign.items() if snap.plan and snap.plan.assign.get(j) not in (None, t))
            out = {"strategy": strat, "on_time": f"{sc['on_time']}/{sc['total']}", "weighted_on_time": sc["weighted"],
                   "high_priority_on_time": f"{sc['high_ok']}/{sc['high_total']}", "late": sc["late"], "unscheduled": sc["unscheduled"],
                   "travel_min": sc["travel"], "assignments_changed": changed, "safety": rep["status"]}
            sims[objective + str(strat["defer"]) + str(strat["avoid"]) + str(strat["pins"])] = {**out, "_score": sc}
            return out

        def recall(query=""):
            return self.memory.search(f"{query} {inc.kind} {inc.signature}")

        def check_route(tractor="", field=""):
            tr, f = snap.tractors.get(str(tractor).upper()), snap.world.fields.get(str(field).upper())
            if not tr or not f:
                return {"error": "unknown tractor or field"}
            q = self.bus.post("planner", "navigation", "query", f"route {tr.id}->{f.id}?", incident=inc.id)
            d = snap.world.dist(tr.pos, f.entry)
            ans = "unreachable (route closed)" if d is None else f"{d} cells ≈ {round(d * tr.spec.min_per_cell)} min"
            self.bus.post("navigation", "planner", "reply", f"{tr.id}->{f.id}: {ans}", reply_to=q.id, incident=inc.id)
            return {"route": ans}

        def remember(note=""):
            self.memory.set_fact(f"note.{inc.id}", note[:200], "planner", snap.now)
            return {"stored": True}

        def submit(objective="on_time", defer=None, avoid=None, pins=None, rationale=""):
            final.update({"strategy": {"objective": objective if objective in STRATEGY_OBJECTIVES else "on_time",
                                       "defer": [d for d in (defer or []) if d in snap.jobs], "avoid": [a for a in (avoid or []) if a in snap.tractors],
                                       "pins": {k: v for k, v in (pins or {}).items() if k in snap.jobs and v in snap.tractors}},
                          "rationale": rationale})
            return {"submitted": True}

        def post_tool(name, args, out, ms):
            short = out.get("on_time") if isinstance(out, dict) and "on_time" in out else (str(out)[:90] if not isinstance(out, list) else f"{len(out)} memories")
            extra = f" · weighted {out.get('weighted_on_time')} · travel {out.get('travel_min')}" if isinstance(out, dict) and "weighted_on_time" in out else ""
            self.bus.post("planner", "planner", "tool", f"{name}({', '.join(f'{k}={v}' for k, v in (args or {}).items() if v)}) -> {short}{extra}",
                          {"args": args, "error": (out or {}).get("error") if isinstance(out, dict) else None}, None, inc.id, ms)

        if cfg:
            arr = {"type": "array", "items": {"type": "string"}}
            strat_props = {"objective": {"type": "string", "enum": STRATEGY_OBJECTIVES}, "defer": arr, "avoid": arr,
                           "pins": {"type": "object", "additionalProperties": {"type": "string"}}}
            tools = [
                {"name": "get_situation", "description": "Current fleet, jobs, depot, closures and wind windows", "parameters": {"type": "object", "properties": {}}, "fn": lambda: situation(snap, inc.text)},
                {"name": "recall", "description": "Search shared memory (past incidents, learned reflexes, facts, operator policy)", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}, "fn": recall},
                {"name": "simulate_strategy", "description": "Run the optimizer with a strategy on a snapshot and return projected outcome (does not apply it)",
                 "parameters": {"type": "object", "properties": strat_props, "required": ["objective"]}, "fn": simulate},
                {"name": "check_route", "description": "Ask the Navigation agent for the route from a tractor to a field", "parameters": {"type": "object", "properties": {"tractor": {"type": "string"}, "field": {"type": "string"}}, "required": ["tractor", "field"]}, "fn": check_route},
                {"name": "remember", "description": "Write a short note to shared memory for other agents", "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}, "fn": remember},
                {"name": "submit_strategy", "description": "Submit the chosen strategy (final answer)", "parameters": {"type": "object", "properties": {**strat_props, "rationale": {"type": "string"}}, "required": ["objective", "rationale"]}, "fn": submit},
            ]
            user = (f"INCIDENT at {min_to_hhmm(snap.now)}: {inc.text}\nIncident signature: {inc.signature}\n"
                    f"Operator policy: {' | '.join(self.memory.policies)}\nSituation: {situation(snap, inc.text)}")
            res = run_tools(PLANNER_SYSTEM, user, tools, max_steps=7, stop_tool="submit_strategy", on_call=post_tool,
                            on_text=lambda t: self.bus.post("planner", "planner", "thought", f"🧠 {t[:220]}", None, None, inc.id),
                            on_retry=lambda c: (final.clear(), sims.clear(), self.bus.post("planner", "planner", "thought",
                                                f"⚠ LLM provider failed, failing over to {c['name']}:{c['model']}", None, None, inc.id)))
            out = {"provider": res["provider"], "model": res["model"], "llm_calls": res["llm_calls"], "error": res["error"], "calls": len(res["calls"])}
            if final:
                out.update(final)
            elif sims:
                best = max(sims.values(), key=functools.cmp_to_key(lambda x, y: (lambda d: (d > 0) - (d < 0))(better(x["_score"], y["_score"]))))
                out.update({"strategy": best["strategy"], "rationale": "LLM did not submit, picked the best strategy it simulated"})
            else:
                out["fallback"] = True
            if out.get("strategy"):
                out["ms"] = (time.perf_counter() - t0) * 1000
                return out
            prev_err = out.get("error")
        else:
            prev_err = None
        # rules deliberation (no LLM, or the LLM failed without simulating anything)
        mem = recall("")
        post_tool("recall", {"query": inc.signature}, mem, 0.1)
        for obj in STRATEGY_OBJECTIVES:
            t1 = time.perf_counter()
            o = simulate(obj)
            post_tool("simulate_strategy", {"objective": obj}, o, (time.perf_counter() - t1) * 1000)
        best = max(sims.values(), key=functools.cmp_to_key(lambda x, y: (lambda d: (d > 0) - (d < 0))(better(x["_score"], y["_score"]))))
        return {"provider": "rules" if not cfg else f"rules (LLM error: {prev_err})", "model": None, "llm_calls": 0, "error": None,
                "strategy": best["strategy"], "ms": (time.perf_counter() - t0) * 1000,
                "rationale": f"Compared {len(sims)} strategies; '{best['strategy']['objective']}' gives {best['on_time']} on time "
                             f"(weighted {best['weighted_on_time']}, high-priority {best['high_priority_on_time']}, travel {best['travel_min']} min)."}

    def _deliberated(self, inc: Incident, r: dict):
        st = self.bus.stats["planner"]
        st["llm_calls"] += r.get("llm_calls", 0)
        st["llm_ms"] += r.get("ms", 0) if r.get("llm_calls") else 0
        self.bus.post("planner", "planner", "strategy", f"🧠 {r.get('provider')} chose [{(r.get('strategy') or {}).get('objective')}]: {r.get('rationale', '')[:200]}",
                      dict(r), None, inc.id, r.get("ms"))
        self.drain()

    # ------------------------------------------------------------------ incident story
    STAGES = {("monitor", "fault"): ("detected", "Detected"), ("monitor", "event"): ("detected", "Detected"),
              ("monitor", "risk"): ("detected", "Detected"), ("navigation", "reply"): ("route", "Route check"),
              ("planner", "proposal"): ("planned", "New plan"), ("safety", "approved"): ("verified", "Safety check passed"),
              ("safety", "rejected"): ("rejected", "Safety check failed"), ("execution", "ack"): ("executed", "Sent to tractors"),
              ("planner", "strategy"): ("second", "Second opinion"), ("planner", "memory"): ("learned", "Saved to memory")}

    def story(self, inc: Incident) -> list[dict]:
        """Timeline of one incident: what broke, how it was detected, planned, checked, executed and recovered."""
        msgs = [m for m in list(self.bus.log) if m.incident == inc.id]
        steps: list[dict] = []
        seen: set[str] = set()

        def add(key, label, who, m_wall, clock, text):
            steps.append({"key": key, "label": label, "who": who, "ms": round((m_wall - inc.t0) * 1000, 1),
                          "clock": min_to_hhmm(clock), "text": text})
        # telemetry evidence that came before the incident existed
        tid = (inc.data or {}).get("tractor")
        if tid:
            for m in reversed(list(self.bus.log)):
                if m.t_wall > inc.t0 + 0.5:
                    continue
                if inc.t0 - m.t_wall > 10:
                    break
                if m.sender == "telemetry" and m.recipient == "monitor" and ((m.data.get("event") or {}).get("tractor") == tid):
                    lag = float(m.data.get("stalled_s") or 0)
                    add("gps", "GPS shows a stop" if inc.kind != "repair" else "GPS shows it moving", "telemetry",
                        m.t_wall - lag, m.t_sim, m.text)
                    break
        first = {"breakdown": "Tractor stopped", "block": "Route closed", "wind": "Wind changed", "priority": "Priority changed",
                 "risk": "Deadline at risk", "fuel": "Low fuel", "add_job": "New job", "initial": "Start of day",
                 "close_field": "Field closed", "repair": "Tractor back"}.get(inc.kind, "Event")
        add("broke", first, "monitor", inc.t0, inc.t, inc.text)
        for m in msgs:
            st = self.STAGES.get((m.sender, m.kind))
            if not st or st[0] in seen:
                continue
            seen.add(st[0])
            add(st[0], st[1], m.sender, m.t_wall, m.t_sim, m.text)
        # recovery: a later repair / reopen for the same thing
        undo = {"breakdown": ("repair", "tractor"), "block": ("unblock", "target"), "close_field": ("open_field", "field")}.get(inc.kind)
        if undo:
            key = (inc.data or {}).get(undo[1])
            for later in self.incidents.values():
                if later.id > inc.id and later.kind == undo[0] and (later.data or {}).get(undo[1]) == key:
                    add("recovered", "Recovered", "monitor", later.t0, later.t, later.text)
                    ok = next((m for m in self.bus.log if m.incident == later.id and m.sender == "execution" and m.kind == "ack"), None)
                    if ok:
                        add("normal", "Back to normal plan", "execution", ok.t_wall, ok.t_sim, ok.text)
                    break
        steps.sort(key=lambda s: s["ms"])
        return steps

    def stories(self, n: int = 8) -> list[dict]:
        out = []
        for inc in list(self.incidents.values())[-n:]:
            if inc.kind in ROUTINE:
                continue
            out.append({**inc.to_json(), "steps": self.story(inc)})
        return out

    # ------------------------------------------------------------------ reporting
    def metrics(self) -> dict:
        incs = list(self.incidents.values())
        safe = sorted(i.safe_ms for i in incs if i.safe_ms is not None)
        delib = [i.delib_ms for i in incs if i.delib_ms]
        llm_delib = [i.delib_ms for i in incs if i.delib_ms and i.provider and not i.provider.startswith("rules")]
        msgs = [m for m in self.bus.log if m.kind not in ("tool", "thought")]
        tools = sum(s["tools"] for s in self.bus.stats.values())
        avg = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731
        cfg = llm_config() if self.use_llm else None
        return {
            "incidents": len(incs),
            "reflex": sum(1 for i in incs if i.path == "reflex"), "learned": sum(1 for i in incs if i.path == "learned"),
            "deliberations": self.deliberations, "improved": sum(1 for i in incs if (i.gain or 0) > 0),
            "avg_safe_ms": avg(safe), "p95_safe_ms": safe[int(0.95 * (len(safe) - 1))] if safe else None,
            "avg_delib_ms": avg(delib), "avg_llm_ms": avg(llm_delib),
            "speedup": (avg(llm_delib or delib) / avg(safe)) if safe and (llm_delib or delib) and avg(safe) else None,
            "messages": len(msgs), "msgs_per_incident": (len(msgs) / len(incs)) if incs else None,
            "tool_calls": tools, "tool_errors": sum(s["tool_errors"] for s in self.bus.stats.values()),
            "llm_calls": sum(s["llm_calls"] for s in self.bus.stats.values()),
            "human_interventions": self.human_interventions,
            "memory_reads": self.memory.reads, "memory_writes": self.memory.writes,
            "llm": f"{cfg['name']}:{cfg['model']}" if cfg else "none (rules)",
        }

    def agents_json(self) -> list[dict]:
        cfg = llm_config() if self.use_llm else None
        out = []
        for a in self.agents.values():
            uses_llm = a.name in ("planner", "liaison")
            eng = (f"{cfg['name']} · {cfg['model']}" if cfg else "rules (no LLM)") if uses_llm else "deterministic tools"
            if a.name == "telemetry":
                eng = "AgOpenWeb · " + ("LIVE" if a.b.fresh() else "connecting…") + ("" if a.b.has_control else " (observer)")
            out.append({"name": a.name, "title": a.title, "emoji": a.emoji, "color": a.color, "role": a.role,
                        "engine": eng,
                        "busy": a.name in self.busy, **self.bus.stats[a.name]})
        return out
