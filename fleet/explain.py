"""Explainer, turns structured plan diffs and planner decision records into plain language.

Template text is always produced instantly and is fully grounded in numbers the planner computed.
An LLM (Claude / local Ollama) may *rephrase* it later (see llm.py); it never changes the plan.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .planner import plan as run_planner, summarize
from .world import min_to_hhmm as hm

if TYPE_CHECKING:
    from .planner import Plan
    from .sim import Simulation

_seq = [0]


def _jlabel(sim: "Simulation", jid: str) -> str:
    j = sim.jobs[jid].d
    return f"{jid} {j.type} {j.field}"


def projection(sim: "Simulation", plan_: "Plan") -> dict:
    on_time = 0
    late = []
    for j in sim.jobs.values():
        if j.status == "done":
            if j.completed_at <= j.d.deadline:
                on_time += 1
            else:
                late.append(j.d.id)
    travel = sum(t.travel_min for t in sim.tractors.values())
    for tr in sim.tractors.values():
        if tr.cur is not None:
            if tr.phase == "travel":
                travel += tr.travel_total - tr.travel_elapsed
            if tr.cur.kind == "job":
                eta = sim.tractor_state(tr).t
                if eta <= sim.jobs[tr.cur.job_id].d.deadline:
                    on_time += 1
                else:
                    late.append(tr.cur.job_id)
    for jid, end in plan_.ends.items():
        if end <= sim.jobs[jid].d.deadline:
            on_time += 1
        else:
            late.append(jid)
    travel += plan_.travel
    return {"on_time": on_time, "late": late, "unassigned": list(plan_.unassigned), "travel": travel,
            "total": len(sim.jobs)}


def _reason_for(sim: "Simulation", plan_: "Plan", jid: str) -> str:
    rec = plan_.why.get(jid) or {}
    tid = rec.get("chosen")
    if not tid:
        return ""
    if rec.get("pinned"):
        return "pinned by the operator"
    alts = rec.get("alternatives", [])
    others = [a for a in alts if a["tractor"] != tid]
    feas = [a for a in others if a.get("feasible")]
    extra = ""
    tasks = plan_.tasks.get(tid, [])
    for i, t in enumerate(tasks):
        if t.job_id == jid:
            if i > 0 and tasks[i - 1].kind == "swap":
                extra = f"; picks up the spare {tasks[i - 1].implement} at the depot first"
            elif i > 0 and tasks[i - 1].kind == "refuel":
                extra = "; refuels at the depot first"
            if t.est_start > t.est_arrive and sim.jobs[jid].d.type == "spray":
                extra += f"; waits for safe wind until {hm(t.est_start)}"
            break
    if not feas:
        why_not = "; ".join(sorted({a['reason'] for a in others if a.get('reason')}))
        return f"only tractor that can do it now ({why_not})" + extra
    best = min(feas, key=lambda a: a["eta"])
    eta = rec.get("eta")
    if eta is not None and eta <= best["eta"]:
        return f"finishes earliest, {hm(eta)} vs {hm(best['eta'])} on {best['tractor']}" + extra
    return f"keeps {best['tractor']} free for more urgent work (it could finish {hm(best['eta'])}, but total lateness/travel would be higher)" + extra


def plan_change(sim: "Simulation", old: "Plan | None", new: "Plan", reason: str, cause: dict | None, report: dict) -> dict:
    _seq[0] += 1
    proj_new = projection(sim, new)
    new.proj = proj_new  # type: ignore[attr-defined]
    lines: list[str] = []
    committed = sim.committed_jobs()
    if old is None:
        title = f"Initial plan, {len(new.assign) + len(committed)} of {len(sim.jobs)} jobs scheduled"
        for tid, seq in new.routes.items():
            tr = sim.tractors[tid]
            if tr.broken:
                continue
            parts = []
            for t in new.tasks[tid]:
                if t.kind == "job":
                    late = " ⚠late" if t.est_end > sim.jobs[t.job_id].d.deadline else ""
                    parts.append(f"{_jlabel(sim, t.job_id)} ({hm(t.est_start)}–{hm(t.est_end)}){late}")
                elif t.kind == "swap":
                    parts.append(f"swap->{t.implement}")
                else:
                    parts.append("refuel")
            lines.append(f"{tid} {tr.spec.name}: " + (" -> ".join(parts) if parts else "standby"))
        waits = [t for ts in new.tasks.values() for t in ts if t.kind == "job" and t.est_start > t.est_arrive]
        for t in waits:
            w = sim.world.wind_at(t.est_arrive)
            lines.append(f"{_jlabel(sim, t.job_id)} held until {hm(t.est_start)}, wind {w:g} mph at arrival "
                         f"({'inversion risk' if w is not None and w < 3 else 'drift risk'}).")
        changed = moved = added = dropped = []
        untouched = []
    else:
        old_owner = dict(old.assign)
        for jid, tid in getattr(old, "_committed", {}).items():
            old_owner.setdefault(jid, tid)
        new_owner = {**committed, **new.assign}
        moved = [j for j in new.assign if j in old_owner and old_owner[j] != new.assign[j]]
        added = [j for j in new.assign if j not in old_owner]
        dropped = [j for j in new.unassigned if j in old_owner]
        changed = moved + added + dropped
        if cause and cause.get("kind") == "breakdown":
            tid = cause["data"]["tractor"]
            title = f"{tid} {sim.tractors[tid].spec.name} broke down, work reassigned"
        else:
            title = f"Re-planned: {reason}"
        for j in moved:
            lines.append(f"{_jlabel(sim, j)}: {old_owner[j]} -> {new.assign[j]} (ETA {hm(new.ends[j])}, due {hm(sim.jobs[j].d.deadline)}), {_reason_for(sim, new, j)}.")
        for j in added:
            rem = sim.jobs[j].remaining
            part = f", remaining {rem:.1f} ha" if sim.jobs[j].area_done > 0 else ""
            lines.append(f"{_jlabel(sim, j)}{part} -> {new.assign[j]} (ETA {hm(new.ends[j])}, due {hm(sim.jobs[j].d.deadline)}), {_reason_for(sim, new, j)}.")
        for j in dropped:
            lines.append(f"{_jlabel(sim, j)} postponed: {new.unassigned[j]}.")
        for j, e in new.ends.items():
            if j in old.ends and j not in moved and abs(e - old.ends[j]) >= 20 and len(lines) < 7:
                d = e - old.ends[j]
                lines.append(f"{_jlabel(sim, j)} stays on {new.assign[j]}, ETA {'+' if d > 0 else ''}{d} min -> {hm(e)}.")
        untouched = []
        for t in new.routes:
            if sim.tractors[t].broken:
                continue
            old_rest = [x for x in old.routes.get(t, []) if x not in committed and sim.jobs[x].status != "done"]
            if new.routes[t] == old_rest:
                untouched.append(t)
        if not lines:
            lines.append("No assignment changes needed, current plan still holds.")
    new._committed = dict(committed)  # type: ignore[attr-defined]
    proj_old = getattr(old, "proj", None) if old is not None else None
    impact = {
        "on_time_projected": proj_new["on_time"],
        "total_jobs": proj_new["total"],
        "late_projected": proj_new["late"],
        "unassigned": proj_new["unassigned"],
        "travel_projected": proj_new["travel"],
        "changes": len(changed) if old is not None else None,
        "untouched": untouched,
    }
    if proj_old:
        impact["on_time_delta"] = proj_new["on_time"] - proj_old["on_time"]
        impact["travel_delta"] = proj_new["travel"] - proj_old["travel"]
    imp_bits = [f"{proj_new['on_time']}/{proj_new['total']} jobs projected on time"]
    if proj_old:
        imp_bits.append(f"{impact['on_time_delta']:+d} on-time vs previous plan")
        imp_bits.append(f"{impact['travel_delta']:+d} min travel")
        imp_bits.append(f"{len(changed)} assignment(s) changed")
        if untouched:
            imp_bits.append(f"{', '.join(untouched)} untouched")
    safety_line = ("✅ Safety Agent: plan passed all checks" if report["status"] != "fail"
                   else "⛔ Safety Agent rejected the plan")
    if report["warnings"]:
        safety_line += f" ({len(report['warnings'])} warning(s))"
    text = title + "\n" + "\n".join(f"• {l}" for l in lines) + "\nImpact: " + " · ".join(imp_bits) + "\n" + safety_line
    return {
        "id": _seq[0], "t": sim.now, "clock": hm(sim.now), "reason": reason, "title": title, "lines": lines,
        "impact": impact, "impact_text": " · ".join(imp_bits), "safety": report, "safety_line": safety_line,
        "text": text, "source": "template", "llm_text": None, "llm_source": None,
        "cause": cause.get("kind") if cause else None,
    }


# ====================================================================== why / why-not
def why(sim: "Simulation", jid: str) -> dict:
    jid = sim.resolve_job(jid) or jid
    j = sim.jobs.get(jid)
    if j is None:
        return {"ok": False, "text": f"Unknown job {jid}."}
    d = j.d
    head = f"{jid}, {d.type} {d.field} ({sim.world.fields[d.field].name}), priority {d.priority}, due {hm(d.deadline)}."
    if j.status == "done":
        tag = "on time" if j.completed_at <= d.deadline else f"late by {j.completed_at - d.deadline} min"
        return {"ok": True, "job": jid, "text": f"{head}\nDone by {j.completed_by} at {hm(j.completed_at)} ({tag}).", "whynot": []}
    com = sim.committed_jobs()
    if jid in com:
        tid = com[jid]
        tr = sim.tractors[tid]
        eta = sim.tractor_state(tr).t
        pct = round(100 * j.area_done / d.area_ha)
        txt = (f"{head}\nIn progress on {tid} {tr.spec.name} ({tr.status()}, {pct}% done), ETA {hm(eta)}.\n"
               f"Work already under way is never reshuffled, it only moves if the tractor breaks down or the field/route closes.")
        return {"ok": True, "job": jid, "text": txt, "whynot": [t for t in sim.tractors if t != tid and not sim.tractors[t].broken]}
    p = sim.plan
    rec = p.why.get(jid) if p else None
    if not rec or not rec.get("chosen"):
        reason = (p.unassigned.get(jid) if p else None) or "not scheduled"
        return {"ok": True, "job": jid, "text": f"{head}\nNot scheduled today: {reason}.", "whynot": [t for t in sim.tractors if not sim.tractors[t].broken]}
    tid = rec["chosen"]
    lines = [head, f"Assigned to {tid} {sim.tractors[tid].spec.name}, ETA {hm(rec['eta'])}, {_reason_for(sim, p, jid)}."]
    for a in rec["alternatives"]:
        if a["tractor"] == tid:
            continue
        if a.get("feasible"):
            lines.append(f"• {a['tractor']}: could finish {hm(a['eta'])}, but the whole plan gets worse (cost +{max(0, a['delta'] - next((x['delta'] for x in rec['alternatives'] if x['tractor'] == tid and x.get('feasible')), 0)):.0f}).")
        else:
            lines.append(f"• {a['tractor']}: not possible, {a['reason']}.")
    return {"ok": True, "job": jid, "text": "\n".join(lines),
            "whynot": [a["tractor"] for a in rec["alternatives"] if a["tractor"] != tid and not sim.tractors[a["tractor"]].broken]}


def why_not(sim: "Simulation", jid: str, tid: str) -> dict:
    """Counterfactual: really re-plan with the job forced onto `tid` and compare."""
    jid = sim.resolve_job(jid) or jid
    tid = tid.upper()
    if jid not in sim.jobs or tid not in sim.tractors or sim.plan is None:
        return {"ok": False, "text": "Unknown job or tractor."}
    d = sim.jobs[jid].d
    if sim.jobs[jid].status == "done":
        j = sim.jobs[jid]
        return {"ok": True, "text": f"{jid} is already done (by {j.completed_by} at {hm(j.completed_at)}), nothing to reassign."}
    com = sim.committed_jobs()
    if jid in com:
        return {"ok": True, "text": f"{jid} is already in progress on {com[jid]}; moving work mid-field wastes travel and time, so the planner keeps it there unless that tractor fails."}
    req = sim.plan_request(pins={**sim.pins, jid: tid}, explain_=False)
    alt = run_planner(req)
    base_plan = run_planner(sim.plan_request(explain_=False))
    base = projection(sim, base_plan)
    cf = projection(sim, alt)
    if jid in alt.unassigned or jid not in alt.ends:
        return {"ok": True, "text": f"{tid} can't take {jid}: {alt.unassigned.get(jid, 'not feasible right now')}."}
    w = lambda ids: sum(sim.jobs[x].d.weight for x in ids)  # noqa: E731
    all_ids = set(sim.jobs)
    ok_b = all_ids - set(base["late"]) - set(base["unassigned"]) - {k for k, j in sim.jobs.items() if j.status == "done" and j.completed_at > j.d.deadline}
    ok_c = all_ids - set(cf["late"]) - set(cf["unassigned"]) - {k for k, j in sim.jobs.items() if j.status == "done" and j.completed_at > j.d.deadline}
    cur_eta = base_plan.ends.get(jid)
    lines = [f"Counterfactual, {jid} ({d.type} {d.field}, {d.priority}) forced onto {tid}, whole plan re-optimised:",
             f"• {jid} would finish {hm(alt.ends[jid])} (current plan {hm(cur_eta)}; due {hm(d.deadline)})",
             f"• priority-weighted on-time score: {w(ok_c)} vs {w(ok_b)} now  (jobs on time {cf['on_time']} vs {base['on_time']})",
             f"• travel: {cf['travel'] - base['travel']:+d} min"]
    newly_late = sorted(ok_b - ok_c)
    newly_ok = sorted(ok_c - ok_b)
    if newly_late:
        lines.append("• would make late / drop: " + ", ".join(f"{_jlabel(sim, x)} ({sim.jobs[x].d.priority})" for x in newly_late))
    if newly_ok:
        lines.append("• would rescue: " + ", ".join(f"{_jlabel(sim, x)} ({sim.jobs[x].d.priority})" for x in newly_ok))
    worse = alt.cost > base_plan.cost + 1
    lines.append("-> Net effect is worse for the farm, so the plan doesn't do it." if worse else
                 "-> About as good. You can pin it as an operator override, the Safety Agent still checks it.")
    return {"ok": True, "text": "\n".join(lines), "counterfactual": {"on_time": cf["on_time"], "base_on_time": base["on_time"],
                                                                     "travel_delta": cf["travel"] - base["travel"], "newly_late": newly_late,
                                                                     "worse": worse}}
