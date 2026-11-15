"""Random farm + random event generator for the benchmark and the chaos test."""
from __future__ import annotations

import random

from .world import min_to_hhmm

JOB_TYPES = ["spray", "mow", "till", "seed"]
IMPL = {"spray": "sprayer", "mow": "mower", "till": "tiller", "seed": "seeder"}


def random_scenario(seed: int, n_tractors: int | None = None, n_fields: int | None = None, n_jobs: int | None = None) -> dict:
    rng = random.Random(seed)
    W, H = 24, 16
    rx = rng.randint(9, 14)
    bridges = sorted(rng.sample(range(1, H - 1), 2))
    occupied = {(rx, y) for y in range(H)}
    depot = (rng.randint(2, rx - 3), rng.randint(5, H - 6))
    occupied.add(depot)
    fields = []
    nf = n_fields or rng.randint(5, 10)
    tries = 0
    while len(fields) < nf and tries < 500:
        tries += 1
        w, h = rng.randint(2, 4), rng.randint(2, 4)
        x, y = rng.randint(0, W - w), rng.randint(0, H - h)
        cells = {(xx, yy) for xx in range(x - 1, x + w + 1) for yy in range(y - 1, y + h + 1)}
        if cells & occupied:
            continue
        occupied |= {(xx, yy) for xx in range(x, x + w) for yy in range(y, y + h)}
        fid = f"F{len(fields) + 1}"
        fields.append({"id": fid, "name": f"Field {len(fields) + 1}", "x": x, "y": y, "w": w, "h": h,
                       "area_ha": rng.randint(8, 22)})
    nt = n_tractors or rng.randint(2, 3)
    tractors = []
    for i in range(nt):
        caps = rng.sample(["sprayer", "mower", "tiller", "seeder"], rng.randint(2, 4))
        tractors.append({"id": f"T{i + 1}", "name": ["Atlas", "Bison", "Colt"][i], "model": "sim",
                         "capabilities": caps, "implement": rng.choice(caps), "fuel": rng.randint(45, 100),
                         "min_per_cell": round(rng.uniform(1.5, 2.2), 1), "work_mult": round(rng.uniform(0.8, 1.1), 2),
                         "burn_work": rng.randint(9, 13), "burn_travel": rng.randint(4, 6),
                         "hours_since_service": rng.randint(10, 400)})
    possible = sorted({t for tr in tractors for t in JOB_TYPES if IMPL[t] in tr["capabilities"]})
    jobs = []
    nj = n_jobs or rng.randint(6, 10)
    for i in range(nj):
        f = rng.choice(fields)
        jobs.append({"id": f"J{i + 1}", "type": rng.choice(possible), "field": f["id"],
                     "priority": rng.choice(["high", "medium", "medium", "low"]),
                     "deadline": min_to_hhmm(rng.randint(9, 19) * 60 + rng.choice([0, 30]))})
    spares = {k: rng.randint(0, 1) for k in ["sprayer", "mower", "tiller", "seeder"]}
    wind = [rng.choice([2, 3, 5, 6, 7, 8, 9, 10, 11, 13]) for _ in range(15)]
    return {"name": f"random-{seed}", "grid": {"width": W, "height": H, "cell_km": 0.5},
            "day": {"start": "06:00", "end": "20:00"},
            "depot": {"x": depot[0], "y": depot[1], "implements": spares},
            "river": {"x": rx, "bridges": [{"id": "north_bridge", "name": "North Bridge", "y": bridges[0]},
                                           {"id": "south_bridge", "name": "South Bridge", "y": bridges[1]}]},
            "weather": {"wind_mph": wind}, "fields": fields, "tractors": tractors, "jobs": jobs}


def random_events(seed: int, sc: dict, n: int | None = None, chaos: bool = False) -> list[tuple[int, dict]]:
    """List of (sim minute, event). Chaos mode = many more and nastier events."""
    rng = random.Random(seed * 7919 + 13)
    n = n if n is not None else (rng.randint(4, 9) if chaos else rng.randint(1, 3))
    tids = [t["id"] for t in sc["tractors"]]
    fids = [f["id"] for f in sc["fields"]]
    jids = [j["id"] for j in sc["jobs"]]
    out = []
    for _ in range(n):
        t = rng.randint(6 * 60 + 20, 17 * 60)
        k = rng.choices(["breakdown", "repair", "block", "unblock", "priority", "add_job", "wind", "close_field", "open_field", "pin"],
                        weights=[5, 2, 3, 1, 3, 2, 2, 1, 1, 1 if chaos else 0])[0]
        if k in ("breakdown", "repair"):
            ev = {"kind": k, "tractor": rng.choice(tids)}
        elif k in ("block", "unblock"):
            ev = {"kind": k, "target": rng.choice(["north_bridge", "south_bridge"])}
        elif k == "priority":
            ev = {"kind": k, "job": rng.choice(jids), "priority": rng.choice(["high", "medium", "low"])}
        elif k == "add_job":
            ev = {"kind": k, "type": rng.choice(["spray", "mow", "till", "seed"]), "field": rng.choice(fids),
                  "priority": rng.choice(["high", "medium", "low"]), "deadline": min_to_hhmm(min(19 * 60, t + rng.randint(120, 480)))}
        elif k == "wind":
            ev = {"kind": k, "mph": rng.choice([2, 12, 14, 16]), "hours": rng.randint(1, 3)}
        elif k in ("close_field", "open_field"):
            ev = {"kind": k, "field": rng.choice(fids)}
        else:
            ev = {"kind": "pin", "job": rng.choice(jids), "tractor": rng.choice(tids)}
        out.append((t, ev))
    out.sort(key=lambda x: x[0])
    return out
