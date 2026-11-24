/* Satellite view: the real farm on Esri World Imagery (Leaflet). Uses globals from app.js (W, S, trColor, TYPE_COLOR, hm, esc, VIEW). */
const SATV = { map: null, fields: {}, trk: {}, routes: {}, cover: {}, pos: {}, depot: null, cross: {}, drain: null, blocked: null, live: null, trail: null, fitted: false };
const M_LAT = 110574;
const kLon = () => 111320 * Math.cos((W.geo.origin[0] * Math.PI) / 180);
// grid coords (floats ok, y grows south) -> [lat, lon]; same math as fleet/geo.py
function gridLL(x, y) { const g = W.geo; return [g.origin[0] + ((W.height - y - 0.5) * g.cell_m) / M_LAT, g.origin[1] + ((x + 0.5) * g.cell_m) / kLon()]; }
// AgOpenWeb local E/N meters -> [lat, lon] around the anchor the bridge sent with sim.setCoords
function enLL(e, n, a) { return [a[0] + n / M_LAT, a[1] + e / (111320 * Math.cos((a[0] * Math.PI) / 180))]; }

function satAvailable() { return !!(window.L && W && W.geo); }

function initSat() {
  if (SATV.map || !satAvailable()) return;
  const m = L.map("sat", { zoomControl: true, attributionControl: true, zoomSnap: 0.25 });
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", { maxZoom: 19, maxNativeZoom: 18, attribution: "Imagery © Esri" }).addTo(m);
  const labels = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}", { maxZoom: 19, maxNativeZoom: 18, attribution: "Labels © Esri" });
  L.control.layers(null, { "Place labels": labels }, { position: "topleft" }).addTo(m);
  m.attributionControl.addAttribution("Field boundaries © OpenStreetMap contributors");
  L.control.scale({ imperial: true, metric: true, position: "bottomright" }).addTo(m);
  SATV.map = m;
  // modeled drain: the water cells of the river column, a thin blue line (crossings leave gaps)
  const segs = []; let run = [];
  const water = new Set(W.water.map((c) => c.join(",")));
  const rx = W.water.length ? W.water[0][0] : null;
  if (rx != null) for (let y = 0; y <= W.height; y++) { if (y < W.height && water.has(`${rx},${y}`)) run.push(gridLL(rx, y - 0.5), gridLL(rx, y + 0.5)); else if (run.length) { segs.push(run); run = []; } }
  if (segs.length) SATV.drain = L.polyline(segs, { color: "#38bdf8", weight: 3, opacity: 0.8, dashArray: "2 6", interactive: false }).addTo(m).bindTooltip(W.river_name || "Drain");
  for (const f of W.fields) {
    if (!f.polygon) continue;
    const p = L.polygon(f.polygon, { color: "#fff", weight: 2, fillOpacity: 0.15 }).addTo(m);
    p.bindTooltip("", { permanent: true, direction: "center", className: "fieldlbl", opacity: 1 }); p.bindPopup("");
    SATV.fields[f.id] = { poly: p, f, shape: shapeOf(f) };
  }
  for (const b of W.bridges) SATV.cross[b.id] = L.circleMarker(gridLL(b.cell[0], b.cell[1]), { radius: 7, color: "#fff", weight: 2, fillColor: "#a07a4a", fillOpacity: 1 }).addTo(m).bindTooltip(b.name, { permanent: true, direction: "left", className: "crosslbl", offset: [-8, 0] });
  SATV.blocked = L.layerGroup().addTo(m);
  SATV.depot = L.marker(gridLL(W.depot[0], W.depot[1]), { icon: L.divIcon({ className: "", html: `<div class="satdepot">⛽ ${esc(W.depot_name || "Depot")}</div>`, iconSize: null, iconAnchor: [0, 0] }), interactive: false, zIndexOffset: -100 }).addTo(m);
  SATV.trail = L.polyline([], { color: "#22d3ee", weight: 3, opacity: 0.9 }).addTo(m);
  satFit();
}
function satFit() {
  if (!SATV.map) return;
  const pts = W.fields.filter((f) => f.polygon).flatMap((f) => f.polygon); pts.push(gridLL(W.depot[0], W.depot[1]));
  SATV.map.invalidateSize(); SATV.map.fitBounds(L.latLngBounds(pts), { padding: [18, 18] }); SATV.fitted = true;
}

// Field geometry in local meters: pivots get concentric passes, strips get N-S AB lines (serpentine).
function shapeOf(f) {
  const a = f.polygon[0], k = 111320 * Math.cos((a[0] * Math.PI) / 180);
  const P = f.polygon.map(([la, lo]) => [(lo - a[1]) * k, (la - a[0]) * M_LAT]);
  let area = 0; for (let i = 0; i < P.length; i++) { const [x1, y1] = P[i], [x2, y2] = P[(i + 1) % P.length]; area += x1 * y2 - x2 * y1; } area = Math.abs(area) / 2;
  const xs = P.map((p) => p[0]), ys = P.map((p) => p[1]), x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const back = ([x, y]) => [a[0] + y / M_LAT, a[1] + x / k];
  const round = P.length >= 20 && area / ((x1 - x0) * (y1 - y0)) < 0.84;
  const pts = [];
  if (round) {  // center pivot: rings from the edge inwards
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, R = Math.min(x1 - x0, y1 - y0) / 2, n = 9;
    for (let i = 0; i < n; i++) { const r = R * (1 - (i + 0.5) / n); for (let s = 0; s <= 48; s++) { const t = (s / 48) * Math.PI * 2; pts.push(back([cx + r * Math.sin(t), cy + r * Math.cos(t)])); } }
  } else {  // AB lines: vertical passes clipped to the polygon
    const n = Math.max(5, Math.min(16, Math.round((x1 - x0) / 24)));
    for (let i = 0; i < n; i++) {
      const x = x0 + ((x1 - x0) * (i + 0.5)) / n, hits = [];
      for (let j = 0; j < P.length; j++) { const [ax, ay] = P[j], [bx, by] = P[(j + 1) % P.length]; if ((ax - x) * (bx - x) <= 0 && ax !== bx) hits.push(ay + ((by - ay) * (x - ax)) / (bx - ax)); }
      if (hits.length < 2) continue;
      const lo = Math.min(...hits) + 6, hi = Math.max(...hits) - 6;
      pts.push(...(i % 2 === 0 ? [back([x, lo]), back([x, hi])] : [back([x, hi]), back([x, lo])]));
    }
  }
  // cumulative length for "where is the tractor at pct%"
  const cum = [0]; for (let i = 1; i < pts.length; i++) cum.push(cum[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], (pts[i][1] - pts[i - 1][1]) * 0.84));
  return { pts, cum, round };
}
function alongPath(sh, frac) {  // -> {ll, idx}
  const L_ = sh.cum[sh.cum.length - 1] * Math.max(0, Math.min(1, frac));
  let i = 1; while (i < sh.cum.length - 1 && sh.cum[i] < L_) i++;
  const seg = sh.cum[i] - sh.cum[i - 1] || 1, u = (L_ - sh.cum[i - 1]) / seg, A = sh.pts[i - 1], B = sh.pts[i];
  return { ll: [A[0] + (B[0] - A[0]) * u, A[1] + (B[1] - A[1]) * u], idx: i };
}

function fieldJob(fid) {
  const js = S.jobs.filter((j) => j.field === fid);
  return js.find((x) => x.status === "active") || js.find((x) => x.status !== "done") || js[js.length - 1];
}

// called on every state message
function renderSat() {
  if (!SATV.map || !S) return;
  const closedF = new Set(S.closed_fields);
  for (const [fid, o] of Object.entries(SATV.fields)) {
    const j = fieldJob(fid), col = j ? TYPE_COLOR[j.type] : "#94a3b8", done = j && j.status === "done", pct = j ? j.pct : 0;
    const late = j && !done && j.late;
    o.poly.setStyle({ color: closedF.has(fid) ? "#60a5fa" : late ? "#ef4444" : done ? "#4ade80" : "#ffffff", weight: j && j.status === "active" ? 3 : 2,
      dashArray: j && j.status === "unscheduled" ? "6 5" : null, fillColor: done ? "#22c55e" : col, fillOpacity: done ? 0.42 : 0.1 + (0.45 * pct) / 100 });
    const st = j ? (done ? `✔ ${j.id}` : `${j.id}${j.priority === "high" ? "<span class=hi>!</span>" : ""} ${pct}%`) : "";
    const html = o.shape.round  // big pivots have room for the name; narrow strips get a compact label
      ? `<b>${fid}</b> ${esc(o.f.name)}<br>${j ? (done ? `✔ ${j.id} ${j.type} done ${hm(j.eta)}` : `${TYPE_ICON[j.type]} ${j.id} ${j.type}${j.priority === "high" ? " <span class=hi>HIGH</span>" : ""} · ${pct}%`) : ""}${closedF.has(fid) ? "<br>🌊 closed" : ""}`
      : `<b>${fid}</b><br>${st}${closedF.has(fid) ? "<br>🌊" : ""}`;
    if (o.html !== html) { o.poly.setTooltipContent(html); o.html = html; }
    const pop = `<b>${fid} ${esc(o.f.name)}</b> · ${o.f.area_ha} ha<br>${j ? `${TYPE_ICON[j.type]} ${j.id} ${j.type} · ${j.priority} · due ${hm(j.deadline)}<br>${done ? `done ${hm(j.eta)} by ${j.tractor}` : `${pct}% · ${j.tractor || "unassigned"} · ETA ${hm(j.eta)}`}` : ""}<br><span style="color:#64748b">Boundary: OpenStreetMap ${esc(o.f.osm_id || "")}. Job and progress are simulated.</span>`;
    if (o.pop !== pop) { o.poly.setPopupContent(pop); o.pop = pop; }
  }
  const blocked = new Set(S.blocked.map((c) => c.join(",")));
  for (const b of W.bridges) { const c = SATV.cross[b.id], closed = blocked.has(b.cell.join(",")); c.setStyle({ fillColor: closed ? "#ef4444" : "#a07a4a", radius: closed ? 9 : 7 }); c.setTooltipContent(closed ? `✖ ${b.name} CLOSED` : b.name); }
  const bridgeCells = new Set(W.bridges.map((b) => b.cell.join(",")));
  const bkey = S.blocked.map((c) => c.join(",")).join(";");
  if (SATV.bkey !== bkey) { SATV.bkey = bkey; SATV.blocked.clearLayers(); for (const [x, y] of S.blocked) if (!bridgeCells.has(`${x},${y}`)) L.rectangle([gridLL(x - 0.5, y + 0.5), gridLL(x + 0.5, y - 0.5)], { color: "#ef4444", weight: 2, fillOpacity: 0.35 }).addTo(SATV.blocked); }
  const showRoutes = $("#showPaths").checked;
  for (const tr of S.tractors) {
    const col = trColor[tr.id] || "#0e7490";
    if (!SATV.trk[tr.id]) {
      SATV.trk[tr.id] = L.marker([tr.lat, tr.lon], { icon: L.divIcon({ className: "", html: "", iconSize: [30, 30], iconAnchor: [15, 15] }), zIndexOffset: 1000 }).addTo(SATV.map);
      SATV.routes[tr.id] = L.polyline([], { color: col, weight: 3, opacity: 0.95, dashArray: "8 7", interactive: false }).addTo(SATV.map);
      SATV.cover[tr.id] = { done: L.polyline([], { color: col, weight: 3, opacity: 0.95, interactive: false }).addTo(SATV.map), todo: L.polyline([], { color: "#fff", weight: 1.2, opacity: 0.45, dashArray: "3 5", interactive: false }).addTo(SATV.map) };
      SATV.pos[tr.id] = [tr.lat, tr.lon];
    }
    // working: put the marker on the AB pattern at the job's progress, draw done/remaining passes
    let target = [tr.lat, tr.lon]; const cv = SATV.cover[tr.id];
    const fo = tr.status === "work" && tr.cur && tr.cur.job_id ? SATV.fields[(S.jobs.find((j) => j.id === tr.cur.job_id) || {}).field] : null;
    if (fo) {
      const a = alongPath(fo.shape, (tr.cur.pct || 0) / 100); target = a.ll;
      cv.done.setLatLngs([...fo.shape.pts.slice(0, a.idx), a.ll]); cv.todo.setLatLngs([a.ll, ...fo.shape.pts.slice(a.idx)]);
    } else {
      cv.done.setLatLngs([]); cv.todo.setLatLngs([]);
      if (Math.abs(tr.x - W.depot[0]) < 0.6 && Math.abs(tr.y - W.depot[1]) < 0.6) { const i = Object.keys(trColor).indexOf(tr.id); target = gridLL(W.depot[0] + (i - 1) * 0.45, W.depot[1] - 0.35); }  // parked side by side
    }
    SATV.target = SATV.target || {}; SATV.target[tr.id] = target;
    const live = S.agopenweb && S.agopenweb.tractor === tr.id;
    const cls = `trk ${tr.status === "broken" ? "broken" : ""} ${tr.status === "work" ? "working" : ""}`;
    const html = `<div class="${cls}" style="--c:${col}">${tr.status === "broken" ? "✖" : tr.id}</div><div class="trklbl">${esc(tr.name)} · ${esc(tr.implement || "-")}${tr.status !== "work" && tr.status !== "travel" ? ` · ${tr.status}` : ""}${live ? " <span class=simtag>sim</span>" : ""}</div>`;
    if (SATV.trkHtml?.[tr.id] !== html) { SATV.trkHtml = SATV.trkHtml || {}; SATV.trkHtml[tr.id] = html; SATV.trk[tr.id].setIcon(L.divIcon({ className: "", html, iconSize: [30, 30], iconAnchor: [15, 15] })); }
    const r = showRoutes && tr.status !== "broken" && tr.route ? tr.route : [];
    SATV.routes[tr.id].setLatLngs(r.length ? [SATV.pos[tr.id], ...r.map(([x, y]) => gridLL(x, y))] : []);
  }
  renderSatLive();
}

// The real AgOpenWeb vehicle: E/N from the bridge, anchored at the lat/lon we sent with sim.setCoords.
function renderSatLive() {
  const a = S.agopenweb;
  if (!a || !a.anchor || !a.pose) { if (SATV.live) { SATV.live.remove(); SATV.live = null; } SATV.trail.setLatLngs([]); return; }
  const ll = enLL(a.pose.e, a.pose.n, a.anchor);
  const html = `<div class="livegps ${a.fresh ? "" : "stale"} ${a.gps_fault_active ? "fault" : ""}"><i></i><span>🛰 LIVE GPS · AgOpenWeb · ${a.pose.speed} kph</span></div>`;
  if (!SATV.live) SATV.live = L.marker(ll, { icon: L.divIcon({ className: "", html, iconSize: [16, 16], iconAnchor: [8, 8] }), zIndexOffset: 2000 }).addTo(SATV.map);
  else { SATV.live.setLatLng(ll); if (SATV.liveHtml !== html) SATV.live.setIcon(L.divIcon({ className: "", html, iconSize: [16, 16], iconAnchor: [8, 8] })); }
  SATV.liveHtml = html;
  SATV.trail.setLatLngs((a.trail || []).slice(-200).map(([e, n]) => enLL(e, n, a.anchor)));
}

// smooth motion between 10 Hz ticks
function satFrame() {
  if (SATV.map && VIEW === "sat" && S) for (const tr of S.tractors) {
    const p = SATV.pos[tr.id], tg = SATV.target && SATV.target[tr.id], mk = SATV.trk[tr.id]; if (!p || !tg) continue;
    const far = Math.abs(tg[0] - p[0]) > 0.01 || Math.abs(tg[1] - p[1]) > 0.01;  // > ~1 km: jump (reset)
    p[0] = far ? tg[0] : p[0] + (tg[0] - p[0]) * 0.2; p[1] = far ? tg[1] : p[1] + (tg[1] - p[1]) * 0.2; mk.setLatLng(p);
    const rt = SATV.routes[tr.id]; const ll = rt.getLatLngs(); if (ll.length) { ll[0] = L.latLng(p); rt.setLatLngs(ll); }
  }
  requestAnimationFrame(satFrame);
}
requestAnimationFrame(satFrame);
