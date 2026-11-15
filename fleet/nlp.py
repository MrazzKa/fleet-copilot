"""Operator commands in plain language (typed or spoken) -> structured, validated events.

Deterministic regex parser first (instant, offline, EN + RU), LLM tool-style JSON parsing as fallback.
Every resulting event still goes through the planner and the Safety Agent.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from . import llm

if TYPE_CHECKING:
    from .sim import Simulation

NUM_WORDS = {"one": "1", "two": "2", "three": "3", "первый": "1", "второй": "2", "третий": "3", "первого": "1",
             "второго": "2", "третьего": "3", "1st": "1", "2nd": "2", "3rd": "3", "first": "1", "second": "2", "third": "3"}
TYPE_WORDS = {"spray": "spray", "spraying": "spray", "опрыск": "spray", "mow": "mow", "mowing": "mow", "кос": "mow",
              "till": "till", "tilling": "till", "plow": "till", "паш": "till", "культив": "till",
              "seed": "seed", "seeding": "seed", "plant": "seed", "сев": "seed", "посе": "seed", "посев": "seed"}


def _norm(text: str) -> str:
    t = " " + text.lower().replace("ё", "е") + " "
    for w, n in NUM_WORDS.items():
        t = re.sub(rf"\b{w}\b", n, t)
    t = re.sub(r"\b(?:tractor|трактор)\s*#?\s*(\d)", r"t\1", t)
    t = re.sub(r"\b(\d)\s+(?:tractor|трактор)", r"t\1", t)
    t = re.sub(r"\b(?:field|поле|поля|поле№)\s*#?\s*(\d+)", r"f\1", t)
    t = re.sub(r"\b(?:job|работа|задача)\s*#?\s*(\d+)", r"j\1", t)
    return t


def _tractor(t: str, sim: "Simulation") -> str | None:
    m = re.search(r"\bt\s?(\d)\b", t)
    if m and f"T{m.group(1)}" in sim.tractors:
        return f"T{m.group(1)}"
    for tid, tr in sim.tractors.items():
        if tr.spec.name.lower() in t:
            return tid
    return None


def _tractors(t: str, sim: "Simulation") -> list[str]:
    out = [f"T{d}" for d in re.findall(r"\bt\s?(\d)\b", t) if f"T{d}" in sim.tractors]
    for tid, tr in sim.tractors.items():
        if tr.spec.name.lower() in t and tid not in out:
            out.append(tid)
    return out


def _job(t: str, sim: "Simulation") -> str | None:
    m = re.search(r"\bj\s?(\d+)\b", t)
    if m and f"J{m.group(1)}" in sim.jobs:
        return f"J{m.group(1)}"
    m = re.search(r"\bf\s?(\d+)\b", t)
    if m:
        return sim.resolve_job(f"F{m.group(1)}")
    return None


def _field(t: str, sim: "Simulation") -> str | None:
    m = re.search(r"\bf\s?(\d+)\b", t)
    if m and f"F{m.group(1)}" in sim.world.fields:
        return f"F{m.group(1)}"
    for fid, f in sim.world.fields.items():
        if f.name.lower() in t:
            return fid
    return None


def _time(t: str) -> str | None:
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", t)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(2) == "pm" else 0)
        return f"{h:02d}:00"
    return None


def regex_parse(text: str, sim: "Simulation") -> list[dict]:
    t = _norm(text)
    has = lambda *ws: any(w in t for w in ws)  # noqa: E731
    out: list[dict] = []
    if has("why not", "почему не ", "why isn't", "why isnt"):
        j, trs = _job(t, sim), _tractors(t, sim)
        if j and trs:
            return [{"kind": "whynot", "job": j, "tractor": trs[-1]}]
    if has("why", "почему", "explain", "объясни"):
        j = _job(t, sim)
        if j:
            return [{"kind": "why", "job": j}]
    bridge = "north_bridge" if has("north", "северн", "верхн") else ("south_bridge" if has("south", "южн", "нижн") else None)
    if bridge and has("bridge", "мост", "road", "дорог", "crossing", "переезд"):
        if has("open", "reopen", "unblock", "откры", "clear"):
            return [{"kind": "unblock", "target": bridge}]
        if has("close", "block", "закры", "закро", "перекры", "перекро", "flood", "затоп", "out", "down", "washed"):
            return [{"kind": "block", "target": bridge}]
    tid = _tractor(t, sim)
    if tid and has("repair", "fixed", "back in service", "back online", "почин", "отремонт", "исправ", "вернулся", "работает снова"):
        return [{"kind": "repair", "tractor": tid}]
    if tid and has("broke", "broken", "break", "breakdown", "is down", "went down", "failed", "failure", "flat tire", "stuck",
                   "слома", "поломк", "отказ", "встал", "заглох"):
        return [{"kind": "breakdown", "tractor": tid}]
    if has("wind", "gust", "ветер", "порыв"):
        m = re.search(r"(\d{1,2})\s*(?:mph|miles|миль|м/с)?", t)
        mph = float(m.group(1)) if m else 14.0
        h = re.search(r"(\d)\s*(?:hours|hour|h|час)", t)
        return [{"kind": "wind", "mph": mph, "hours": int(h.group(1)) if h else 2}]
    fid = _field(t, sim)
    if fid and has("flood", "wet", "closed", "close", "muddy", "затоп", "мокр", "закры", "грязь") and not has("bridge", "мост"):
        if has("open", "dry", "откры", "сух"):
            return [{"kind": "open_field", "field": fid}]
        return [{"kind": "close_field", "field": fid, "why": "operator: " + text.strip()[:60]}]
    if has("add", "new job", "добав", "новая", "нужно", "need to", "please"):
        jt = next((v for k, v in TYPE_WORDS.items() if k in t), None)
        if jt and fid:
            pr = "high" if has("urgent", "asap", "срочн", "high", "rush") else ("low" if has("low", "низк") else "medium")
            return [{"kind": "add_job", "type": jt, "field": fid, "priority": pr, "deadline": _time(t)}]
    j = _job(t, sim)
    if j and has("pin", "lock", "закреп", "always", "only") and tid:
        return [{"kind": "pin", "job": j, "tractor": tid}]
    if j and has("unpin", "unlock", "откреп"):
        return [{"kind": "unpin", "job": j}]
    if j and has("urgent", "rush", "asap", "high priority", "priority high", "first", "срочн", "важн", "приоритет", "сначала", "в первую"):
        ev = {"kind": "priority", "job": j, "priority": "high"}
        dl = _time(t)
        if dl:
            ev["deadline"] = dl
        return [ev]
    if j and has("low priority", "not urgent", "later", "can wait", "не срочн", "потом", "низк"):
        return [{"kind": "priority", "job": j, "priority": "low"}]
    return out


LLM_SYSTEM = """You convert a farm operator's message into fleet-coordinator commands. Reply with ONLY JSON: {"commands": [...]}.
Allowed commands:
{"kind":"breakdown","tractor":"T2"} | {"kind":"repair","tractor":"T2"}
{"kind":"block","target":"north_bridge|south_bridge"} | {"kind":"unblock","target":"north_bridge|south_bridge"}
{"kind":"priority","job":"J5","priority":"high|medium|low","deadline":"HH:MM optional"}
{"kind":"add_job","type":"spray|mow|till|seed","field":"F3","priority":"high|medium|low","deadline":"HH:MM optional"}
{"kind":"close_field","field":"F3","why":"short reason"} | {"kind":"open_field","field":"F3"}
{"kind":"wind","mph":14,"hours":2}
{"kind":"pin","job":"J5","tractor":"T1"} | {"kind":"unpin","job":"J5"}
{"kind":"why","job":"J5"} | {"kind":"whynot","job":"J5","tractor":"T3"}
Use only IDs that exist in the context. If the message is not a command, reply {"commands": []}."""


def llm_parse(text: str, sim: "Simulation") -> tuple[list[dict], str]:
    ctx = {
        "tractors": {k: f"{v.spec.name} ({'broken' if v.broken else v.status()})" for k, v in sim.tractors.items()},
        "jobs": {k: f"{j.d.type} {j.d.field} {j.d.priority} due {j.d.deadline // 60:02d}:{j.d.deadline % 60:02d} {j.status}" for k, j in sim.jobs.items()},
        "fields": {k: f.name for k, f in sim.world.fields.items()},
        "bridges": list(sim.world.bridges),
    }
    raw, src = llm.complete(LLM_SYSTEM, f"Context: {json.dumps(ctx)}\nOperator: {text}", max_tokens=300, json_mode=True)
    if not raw:
        return [], src
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(m.group(0) if m else raw)
    except Exception:  # noqa: BLE001
        return [], src
    cmds = data.get("commands", []) if isinstance(data, dict) else []
    ok_kinds = {"breakdown", "repair", "block", "unblock", "priority", "add_job", "close_field", "open_field", "wind", "pin", "unpin", "why", "whynot"}
    return [c for c in cmds if isinstance(c, dict) and c.get("kind") in ok_kinds][:3], src


def parse(text: str, sim: "Simulation") -> tuple[list[dict], str]:
    cmds = regex_parse(text, sim)
    if cmds:
        return cmds, "rules"
    return llm_parse(text, sim)
