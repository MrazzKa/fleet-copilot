"""Message bus: every agent-to-agent interaction is a message with sender, recipient, kind, sim time and wall latency."""
from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field

_ids = itertools.count(1)


@dataclass
class Message:
    id: int
    t_sim: int
    t_wall: float
    sender: str
    recipient: str
    kind: str  # fault | event | risk | query | reply | proposal | approved | rejected | command | summary | ack | tool | thought | strategy | memory
    text: str
    data: dict = field(default_factory=dict)
    reply_to: int | None = None
    incident: int | None = None
    ms: float | None = None  # duration of the work this message reports (tool call, LLM step)

    def to_json(self) -> dict:
        d = {"id": self.id, "t": self.t_sim, "sender": self.sender, "to": self.recipient, "kind": self.kind,
             "text": self.text, "reply_to": self.reply_to, "incident": self.incident}
        if self.ms is not None:
            d["ms"] = round(self.ms, 2)
        if self.data:
            d["data"] = {k: v for k, v in self.data.items() if k in ("args", "result", "path", "provider", "model", "strategy")}
        return d


class Bus:
    def __init__(self, clock):
        self.clock = clock  # callable -> sim minute
        self.log: list[Message] = []
        self.inboxes: dict[str, deque] = {}
        self.lock = threading.RLock()
        self.stats: dict[str, dict] = {}

    def register(self, name: str):
        self.inboxes[name] = deque()
        self.stats[name] = {"sent": 0, "received": 0, "tools": 0, "tool_errors": 0, "llm_calls": 0, "llm_ms": 0.0}

    def post(self, sender: str, recipient: str, kind: str, text: str, data: dict | None = None,
             reply_to: int | None = None, incident: int | None = None, ms: float | None = None, deliver: bool = True) -> Message:
        with self.lock:
            m = Message(next(_ids), self.clock(), time.perf_counter(), sender, recipient, kind, text, data or {}, reply_to, incident, ms)
            self.log.append(m)
            if len(self.log) > 4000:
                self.log = self.log[-3000:]
            if sender in self.stats:
                if kind == "tool":
                    self.stats[sender]["tools"] += 1
                    if (data or {}).get("error"):
                        self.stats[sender]["tool_errors"] += 1
                elif kind not in ("thought",):
                    self.stats[sender]["sent"] += 1
            if deliver and recipient in self.inboxes and kind not in ("tool", "thought"):
                self.inboxes[recipient].append(m)
                self.stats[recipient]["received"] += 1
            return m

    def pending(self) -> bool:
        return any(self.inboxes.values())

    def recent(self, n: int = 80) -> list[dict]:
        with self.lock:
            return [m.to_json() for m in self.log[-n:]]
