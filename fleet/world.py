"""World model: scenario, grid navigation (Navigation Agent), weather, shared physics.

Everything that the planner, the Safety Agent and the simulator must agree on lives here,
so a plan that the planner believes is feasible is executed minute-for-minute the same way.
"""
from __future__ import annotations

import copy
import heapq
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

Cell = tuple[int, int]

IMPLEMENT_FOR_JOB = {"spray": "sprayer", "mow": "mower", "till": "tiller", "seed": "seeder"}
WORK_RATE_HA_H = {"sprayer": 14.0, "mower": 8.0, "tiller": 6.0, "seeder": 9.0}
PRIORITY_WEIGHT = {"high": 3, "medium": 2, "low": 1}
REFUEL_MIN = 15
SWAP_MIN = 20  # implement swap at depot (includes a full refuel)
FUEL_RESERVE = 8.0  # % that must remain on arrival back at the depot
WIND_MIN_MPH, WIND_MAX_MPH = 3.0, 10.0
EPS = 1e-6


def hhmm_to_min(s: str | int) -> int:
    if isinstance(s, (int, float)):
        return int(s)
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def min_to_hhmm(m: float | None) -> str:
    if m is None:
        return "--:--"
    m = int(round(m))
    return f"{m // 60:02d}:{m % 60:02d}"


def travel_minutes(cells: int, min_per_cell: float) -> int:
    return int(math.ceil(cells * min_per_cell - 1e-9)) if cells > 0 else 0


def work_minutes(area_ha: float, rate_ha_h: float) -> int:
    if area_ha <= EPS:
        return 0
    return int(math.ceil(area_ha / rate_ha_h * 60 - 1e-9))


@dataclass
class FieldDef:
    id: str
    name: str
    x: int
    y: int
    w: int
    h: int
    area_ha: float
    closed: bool = False

    @property
    def entry(self) -> Cell:
        return (self.x + self.w // 2, self.y + self.h // 2)

    def cells(self):
        for xx in range(self.x, self.x + self.w):
            for yy in range(self.y, self.y + self.h):
                yield (xx, yy)


@dataclass
class TractorSpec:
    id: str
    name: str
    model: str
    capabilities: list[str]
    implement: str | None
    fuel: float
    min_per_cell: float
    work_mult: float
    burn_work: float  # % of tank per hour of work
    burn_travel: float  # % of tank per hour of travel
    hours_since_service: float
    start: Cell = (0, 0)

    def rate(self, implement: str) -> float:
        return WORK_RATE_HA_H[implement] * self.work_mult


@dataclass
class JobDef:
    id: str
    type: str
    field: str
    priority: str
    deadline: int
    area_ha: float
    order: int
    created_at: int = 0

    @property
    def implement(self) -> str:
        return IMPLEMENT_FOR_JOB[self.type]

    @property
    def weight(self) -> int:
        return PRIORITY_WEIGHT[self.priority]


class World:
    """Static map + mutable conditions (blocked cells, closed fields, wind)."""

    def __init__(self, sc: dict):
        self.raw = copy.deepcopy(sc)
        self.name = sc.get("name", "Scenario")
        self.width = sc["grid"]["width"]
        self.height = sc["grid"]["height"]
        self.cell_km = sc["grid"].get("cell_km", 0.5)
        self.day_start = hhmm_to_min(sc["day"]["start"])
        self.day_end = hhmm_to_min(sc["day"]["end"])
        d = sc["depot"]
        self.depot: Cell = (d["x"], d["y"])
        self.depot_implements: dict[str, int] = dict(d.get("implements", {}))
        self.fields: dict[str, FieldDef] = {f["id"]: FieldDef(**{k: f[k] for k in ("id", "name", "x", "y", "w", "h", "area_ha")}) for f in sc["fields"]}
        # river: vertical wall with bridges
        self.water: set[Cell] = set()
        self.bridges: dict[str, dict] = {}
        rv = sc.get("river")
        if rv:
            bridge_cells = {}
            for b in rv.get("bridges", []):
                c = (rv["x"], b["y"])
                bridge_cells[c] = b
                self.bridges[b["id"]] = {"id": b["id"], "name": b.get("name", b["id"]), "cell": c}
            for y in range(self.height):
                c = (rv["x"], y)
                if c not in bridge_cells:
                    self.water.add(c)
        for o in sc.get("obstacles", []):
            self.water.add((o[0], o[1]))
        self.blocked: set[Cell] = set()  # operator-blocked cells (closed bridge/road)
        self.blocked_names: dict[str, Cell] = {}
        self.wind: list[float] = list(sc.get("weather", {}).get("wind_mph", []))
        self.version = 0  # bump on any change that affects routing
        self._dist_cache: dict = {}
        self._path_cache: dict = {}

    # ------------------------------------------------------------------ conditions
    def bump(self):
        self.version += 1
        self._dist_cache.clear()
        self._path_cache.clear()

    def block(self, target: str) -> Cell | None:
        cell = self.resolve_target(target)
        if cell is None:
            return None
        self.blocked.add(cell)
        self.blocked_names[target] = cell
        self.bump()
        return cell

    def unblock(self, target: str) -> Cell | None:
        cell = self.resolve_target(target)
        if cell is None:
            return None
        self.blocked.discard(cell)
        self.blocked_names.pop(target, None)
        self.bump()
        return cell

    def resolve_target(self, target: str) -> Cell | None:
        if target in self.bridges:
            return self.bridges[target]["cell"]
        if target.startswith("cell:"):
            x, y = target[5:].split(",")
            return (int(x), int(y))
        return None

    def passable(self, c: Cell) -> bool:
        x, y = c
        return 0 <= x < self.width and 0 <= y < self.height and c not in self.water and c not in self.blocked

    # ------------------------------------------------------------------ navigation (A*)
    def path(self, a: Cell, b: Cell) -> list[Cell] | None:
        """Shortest 4-connected path a->b avoiding water and blocked cells. Start cell is allowed even if blocked."""
        if a == b:
            return [a]
        key = (a, b)
        if key in self._path_cache:
            p = self._path_cache[key]
            return list(p) if p is not None else None
        if not self.passable(b):
            self._path_cache[key] = None
            return None
        openq = [(abs(a[0] - b[0]) + abs(a[1] - b[1]), 0, a)]
        came: dict[Cell, Cell] = {}
        g = {a: 0}
        while openq:
            _, gc, cur = heapq.heappop(openq)
            if cur == b:
                path = [cur]
                while cur in came:
                    cur = came[cur]
                    path.append(cur)
                path.reverse()
                self._path_cache[key] = tuple(path)
                return path
            if gc > g.get(cur, 1e18):
                continue
            x, y = cur
            for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if not self.passable(n):
                    continue
                ng = gc + 1
                if ng < g.get(n, 1e18):
                    g[n] = ng
                    came[n] = cur
                    heapq.heappush(openq, (ng + abs(n[0] - b[0]) + abs(n[1] - b[1]), ng, n))
        self._path_cache[key] = None
        return None

    def dist(self, a: Cell, b: Cell) -> int | None:
        key = (a, b)
        if key not in self._dist_cache:
            p = self.path(a, b)
            self._dist_cache[key] = None if p is None else len(p) - 1
        return self._dist_cache[key]

    # ------------------------------------------------------------------ weather
    def wind_at(self, minute: float) -> float | None:
        if not self.wind:
            return None
        idx = int((minute - self.day_start) // 60)
        if idx < 0:
            idx = 0
        if idx >= len(self.wind):
            idx = len(self.wind) - 1
        return self.wind[idx]

    def spray_ok(self, minute: float) -> bool:
        w = self.wind_at(minute)
        return w is None or (WIND_MIN_MPH <= w <= WIND_MAX_MPH)

    def earliest_spray_start(self, t: int, duration: int) -> int | None:
        """Earliest start >= t such that every minute of [s, s+duration) has sprayable wind and ends by day end."""
        if not self.wind:
            return t if t + duration <= self.day_end else None
        s = t
        while s + duration <= self.day_end:
            bad = None
            m = s
            # check hour by hour
            while m < s + duration:
                if not self.spray_ok(m):
                    bad = m
                    break
                m = (int((m - self.day_start) // 60) + 1) * 60 + self.day_start
            if bad is None:
                return s
            # jump to the next hour boundary after the bad minute
            s = (int((bad - self.day_start) // 60) + 1) * 60 + self.day_start
        return None

    def set_wind(self, start_min: int, hours: int, mph: float):
        i0 = int((start_min - self.day_start) // 60)
        for i in range(max(0, i0), min(len(self.wind), i0 + hours)):
            self.wind[i] = mph
        self.bump()

    def spray_windows(self) -> list[tuple[int, int]]:
        out = []
        cur = None
        for i, _ in enumerate(self.wind):
            m = self.day_start + i * 60
            ok = self.spray_ok(m)
            if ok and cur is None:
                cur = m
            if not ok and cur is not None:
                out.append((cur, m))
                cur = None
        if cur is not None:
            out.append((cur, min(self.day_end, self.day_start + len(self.wind) * 60)))
        return out

    def static_json(self) -> dict:
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "cell_km": self.cell_km,
            "day_start": self.day_start,
            "day_end": self.day_end,
            "depot": self.depot,
            "water": sorted(self.water),
            "bridges": list(self.bridges.values()),
            "fields": [{"id": f.id, "name": f.name, "x": f.x, "y": f.y, "w": f.w, "h": f.h, "area_ha": f.area_ha} for f in self.fields.values()],
        }


def load_scenario(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build(sc: dict) -> tuple[World, list[TractorSpec], list[JobDef]]:
    world = World(sc)
    tractors = []
    for t in sc["tractors"]:
        start = tuple(t.get("start", world.depot))
        tractors.append(TractorSpec(
            id=t["id"], name=t.get("name", t["id"]), model=t.get("model", ""), capabilities=list(t["capabilities"]),
            implement=t.get("implement"), fuel=float(t.get("fuel", 100)), min_per_cell=float(t.get("min_per_cell", 2.0)),
            work_mult=float(t.get("work_mult", 1.0)), burn_work=float(t.get("burn_work", 12)), burn_travel=float(t.get("burn_travel", 5)),
            hours_since_service=float(t.get("hours_since_service", 0)), start=start))
    jobs = []
    for i, j in enumerate(sc["jobs"]):
        f = world.fields[j["field"]]
        jobs.append(JobDef(id=j["id"], type=j["type"], field=j["field"], priority=j.get("priority", "medium"),
                           deadline=hhmm_to_min(j["deadline"]), area_ha=float(j.get("area_ha", f.area_ha)), order=i,
                           created_at=world.day_start))
    return world, tractors, jobs
