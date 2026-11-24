"""Build the real-farm scenario near the University of Arizona Maricopa Agricultural Center (Maricopa, AZ).

  python scripts/build_maricopa.py --osm overpass.json   # (re)make scenarios/maricopa_fields.geojson from an Overpass dump
  python scripts/build_maricopa.py                       # rebuild scenarios/maricopa.json from the GeoJSON

Field boundaries are real OpenStreetMap `landuse=farmland` ways (© OpenStreetMap contributors, ODbL).
Tractors, jobs and wind are copied from the simulated demo day (scenarios/demo.json).

Overpass query used (bbox S,W,N,E):
  [out:json];(way["landuse"="farmland"](33.055,-111.995,33.085,-111.95);
              way["waterway"](33.055,-111.995,33.085,-111.95);); out geom tags;
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fleet import geo  # noqa: E402
from fleet.world import World  # noqa: E402

GEOJSON = ROOT / "scenarios" / "maricopa_fields.geojson"
OUT = ROOT / "scenarios" / "maricopa.json"
DEMO = ROOT / "scenarios" / "demo.json"

# OSM way id -> (field id, display name). Field ids keep the demo jobs (J1..J10) on fields of a sensible size.
PICK = {
    555029953: ("F1", "South Strip B"),
    230475265: ("F2", "North Strip A"),
    555029954: ("F3", "South Strip C"),
    232400463: ("F4", "North Strip D"),
    230475264: ("F5", "North Strip B"),
    1486755491: ("F8", "East Pivot S"),
    232400462: ("F7", "North Strip C"),
    1486755490: ("F6", "East Pivot N"),
    555029952: ("F9", "South Strip A"),
    142081439: ("F10", "Smith-Enke West"),
}

CELL_M = 100.0
DRAIN_LON = -111.9876  # OSM way 1020174261, intermittent drain, runs N-S along the west side of the strips
K_LON = 111320.0 * 0.83786  # m per degree lon near 33.07 N (only used to place the grid origin)
ORIGIN = [33.0712, round(DRAIN_LON - 8.5 * CELL_M / K_LON, 6)]  # SW corner; puts a cell column exactly on the drain
WIDTH, HEIGHT = 33, 20
DEPOT_LL = (33.0760, -111.9735)  # MAC campus yard, north of W Smith-Enke Rd


def geojson_from_osm(path: Path) -> dict:
    d = json.loads(path.read_text())
    feats = []
    for e in d["elements"]:
        if e.get("type") != "way" or e["id"] not in PICK:
            continue
        fid, name = PICK[e["id"]]
        ring = [[round(p["lon"], 7), round(p["lat"], 7)] for p in e["geometry"]]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        feats.append({"type": "Feature",
                      "properties": {"id": fid, "name": name, "source": "osm", "osm_id": f"way/{e['id']}"},
                      "geometry": {"type": "Polygon", "coordinates": [ring]}})
    feats.sort(key=lambda f: int(f["properties"]["id"][1:]))
    missing = {v[0] for v in PICK.values()} - {f["properties"]["id"] for f in feats}
    if missing:
        raise SystemExit(f"OSM dump is missing fields {sorted(missing)}")
    return {"type": "FeatureCollection",
            "properties": {"attribution": "© OpenStreetMap contributors (ODbL)", "area": "near the UA Maricopa Agricultural Center, Maricopa, AZ"},
            "features": feats}


def build() -> dict:
    gj = json.loads(GEOJSON.read_text())
    sc = copy.deepcopy(json.loads(DEMO.read_text()))
    g = {"origin": ORIGIN, "cell_m": CELL_M}
    sc["name"] = "Maricopa, AZ (real field boundaries)"
    scale = CELL_M / (sc["grid"].get("cell_km", 0.5) * 1000)
    sc["grid"] = {"width": WIDTH, "height": HEIGHT, "cell_km": CELL_M / 1000}
    sc["geo"] = {**g, "place": "near the University of Arizona Maricopa Agricultural Center, Maricopa, AZ",
                 "fields_source": "OpenStreetMap landuse=farmland (ODbL)"}
    dx, dy = geo.ll_to_cell(*DEPOT_LL, g, HEIGHT)
    sc["depot"] = {**sc["depot"], "x": dx, "y": dy, "name": "MAC yard"}
    river_x, _ = geo.ll_to_cell(33.08, DRAIN_LON, g, HEIGHT)
    _, south_y = geo.ll_to_cell(33.0730, DRAIN_LON, g, HEIGHT)  # W Smith-Enke Rd culvert (OSM way 890562400)
    _, north_y = geo.ll_to_cell(33.0879, DRAIN_LON, g, HEIGHT)  # field road where the drain turns west
    sc["river"] = {"x": river_x, "name": "Drain (intermittent)",
                   "bridges": [{"id": "north_bridge", "name": "North crossing", "y": north_y},
                               {"id": "south_bridge", "name": "Smith-Enke culvert", "y": south_y}]}
    fields = []
    for f in gj["features"]:
        p = f["properties"]
        ring = [[lat, lon] for lon, lat in f["geometry"]["coordinates"][0]]
        cells = geo.rasterize(ring, g, WIDTH, HEIGHT)
        cells = [c for c in cells if c[0] != river_x]
        if not cells:
            raise SystemExit(f"{p['id']} rasterizes to no cells")
        xs, ys = [c[0] for c in cells], [c[1] for c in cells]
        fields.append({"id": p["id"], "name": p["name"], "x": min(xs), "y": min(ys), "w": max(xs) - min(xs) + 1, "h": max(ys) - min(ys) + 1,
                       "area_ha": round(geo.ring_area_ha(ring, ORIGIN), 1), "source": p.get("source", "osm"), "osm_id": p.get("osm_id"),
                       "polygon": [[round(a, 6), round(b, 6)] for a, b in ring], "cells": len(cells)})
    sc["fields"] = fields
    for t in sc["tractors"]:  # same machines, same km/h: minutes per (smaller) cell scale with cell size
        t["min_per_cell"] = round(t["min_per_cell"] * scale, 3)
    w = World(sc)
    for f in w.fields.values():
        if w.dist(w.depot, f.entry) is None:
            raise SystemExit(f"{f.id} unreachable from the depot")
    return sc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm", type=Path, help="Overpass JSON dump (out geom) to rebuild the GeoJSON from")
    a = ap.parse_args()
    if a.osm:
        GEOJSON.write_text(json.dumps(geojson_from_osm(a.osm), indent=1) + "\n")
        print("wrote", GEOJSON.relative_to(ROOT))
    sc = build()
    OUT.write_text(json.dumps(sc, indent=1) + "\n")
    print("wrote", OUT.relative_to(ROOT))
    for f in sc["fields"]:
        print(f"  {f['id']:>3} {f['name']:<18} {f['area_ha']:>5} ha  cells {f['cells']:>2}  rect x{f['x']} y{f['y']} {f['w']}x{f['h']}  {f['osm_id']}")
    print("  depot", sc["depot"]["x"], sc["depot"]["y"], " drain x", sc["river"]["x"], " crossings", [(b["name"], b["y"]) for b in sc["river"]["bridges"]])


if __name__ == "__main__":
    main()
