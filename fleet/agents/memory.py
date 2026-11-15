"""Shared memory (blackboard) used by all agents.

- facts:     current world knowledge with provenance ("bridge.north_bridge" = "closed since 10:00", by Navigation)
- episodes:  incident history: what happened, which path handled it, what strategy, the outcome
- reflexes:  learned incident-signature -> strategy (written after a deliberation, reused instantly next time)
- policies:  operator policies in plain language (read by the LLM Planner)
Reflexes and policies persist to disk between runs.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from ..world import min_to_hhmm


class SharedMemory:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.facts: dict[str, dict] = {}
        self.episodes: list[dict] = []
        self.reflexes: dict[str, dict] = {}
        self.policies: list[str] = ["Protect high-priority jobs first; then minimize late jobs; avoid reshuffling tractors that were not affected."]
        self.reads = 0
        self.writes = 0
        self.load()

    # ---------------------------------------------------------------- persistence
    def load(self):
        if self.path and self.path.exists():
            try:
                d = json.loads(self.path.read_text())
                self.reflexes = d.get("reflexes", {})
                self.policies = d.get("policies", self.policies)
            except Exception:  # noqa: BLE001
                pass

    def save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.path.write_text(json.dumps({"reflexes": self.reflexes, "policies": self.policies}, indent=2))

    def forget_reflexes(self):
        with self.lock:
            self.reflexes = {}
            self.save()

    # ---------------------------------------------------------------- facts
    def set_fact(self, key: str, value: str, by: str, t: int):
        with self.lock:
            self.facts[key] = {"value": value, "by": by, "t": t, "clock": min_to_hhmm(t)}
            self.writes += 1

    def get(self, key: str):
        with self.lock:
            self.reads += 1
            return self.facts.get(key)

    # ---------------------------------------------------------------- episodes
    def record_episode(self, ep: dict):
        with self.lock:
            self.episodes.append(ep)
            self.writes += 1

    def update_episode(self, inc_id: int, **kw):
        with self.lock:
            for ep in reversed(self.episodes):
                if ep.get("incident") == inc_id:
                    ep.update(kw)
                    self.writes += 1
                    return

    # ---------------------------------------------------------------- reflexes
    def lookup_reflex(self, signature: str) -> dict | None:
        with self.lock:
            self.reads += 1
            r = self.reflexes.get(signature)
            if r:
                r["uses"] = r.get("uses", 0) + 1
            return r

    def learn_reflex(self, signature: str, strategy: dict, source: str, gain: float, ms: float, rationale: str, t: int):
        with self.lock:
            self.reflexes[signature] = {"strategy": strategy, "source": source, "gain": gain, "learned_ms": round(ms),
                                        "rationale": rationale[:240], "learned_at": min_to_hhmm(t), "uses": 0}
            self.writes += 1
            self.save()

    # ---------------------------------------------------------------- search (for LLM recall tool)
    def search(self, query: str, k: int = 6) -> list[dict]:
        q = [w for w in query.lower().replace(",", " ").split() if len(w) > 1]
        with self.lock:
            self.reads += 1
            items = [{"type": "fact", "key": key, **v} for key, v in self.facts.items()]
            items += [{"type": "episode", **e} for e in self.episodes[-30:]]
            items += [{"type": "reflex", "signature": s, **r} for s, r in self.reflexes.items()]
            items += [{"type": "policy", "text": p} for p in self.policies]

            def score(it):
                txt = json.dumps(it).lower()
                return sum(txt.count(w) for w in q) + (0.5 if it["type"] == "policy" else 0)
            ranked = sorted(items, key=score, reverse=True)
            return [it for it in ranked[:k] if score(it) > 0] or ranked[:2]

    def to_json(self) -> dict:
        with self.lock:
            return {"facts": self.facts, "episodes": self.episodes[-25:], "reflexes": self.reflexes,
                    "policies": self.policies, "reads": self.reads, "writes": self.writes}
