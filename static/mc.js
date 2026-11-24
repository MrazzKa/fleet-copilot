/* Mission Control panels (Fleet, Alerts, Agents bar, GPS chip, Details drawer). Reads globals from app.js / sat.js. */
(() => {
  if (!localStorage.getItem("mc.themed")) { setTheme("night"); localStorage.setItem("mc.themed", "1"); }  // dark theme once; the theme button still switches
  const STATUS = { work: "Working", travel: "Traveling", wait: "Waiting", refuel: "Refueling", swap: "Swapping implement", broken: "Broken", idle: "Idle" };
  const STEP_COLOR = { gps: "#ef5a4a", broke: "#ef5a4a", detected: "#e3a33b", route: "#5aa2ff", planned: "#5aa2ff", verified: "#3fb68b", rejected: "#ef5a4a",
    executed: "#3fb68b", second: "#a78bfa", learned: "#a78bfa", recovered: "#2dd4bf", normal: "#2dd4bf" };
  const ICON = {
    alert: '<svg width="13" height="13" viewBox="0 0 14 14"><path d="M7 1.5l6 11H1z" fill="none" stroke="#ef5a4a" stroke-width="1.5" stroke-linejoin="round"/><path d="M7 6v3" stroke="#ef5a4a" stroke-width="1.5"/></svg>',
    road: '<svg width="13" height="13" viewBox="0 0 14 14"><circle cx="7" cy="7" r="5.5" fill="none" stroke="#e3a33b" stroke-width="1.5"/><path d="M4 7h6" stroke="#e3a33b" stroke-width="1.8"/></svg>',
    wind: '<svg width="13" height="13" viewBox="0 0 14 14"><path d="M1 5h8a2 2 0 10-2-2M1 9h10a2 2 0 11-2 2" fill="none" stroke="#5aa2ff" stroke-width="1.4"/></svg>',
    clock: '<svg width="13" height="13" viewBox="0 0 14 14"><circle cx="7" cy="7" r="5.5" fill="none" stroke="#e3a33b" stroke-width="1.4"/><path d="M7 4v3l2 1.5" stroke="#e3a33b" stroke-width="1.4" fill="none"/></svg>',
    info: '<svg width="13" height="13" viewBox="0 0 14 14"><circle cx="7" cy="7" r="5.5" fill="none" stroke="#97a39b" stroke-width="1.4"/><path d="M7 6v4" stroke="#97a39b" stroke-width="1.5"/><circle cx="7" cy="4" r=".8" fill="#97a39b"/></svg>',
  };
  const iconFor = (k) => (["breakdown", "gps"].includes(k) ? ICON.alert : ["block", "unblock", "close_field"].includes(k) ? ICON.road : k === "wind" ? ICON.wind : ["priority", "risk", "deadline"].includes(k) ? ICON.clock : ICON.info);

  // ---------------------------------------------------------------- drawer, panel collapse, command row fold
  const drawer = $("#drawer");
  const setDrawer = (on) => { drawer.classList.toggle("open", on); if (on) setTimeout(resize, 240); };
  $("#detailsbtn").onclick = () => setDrawer(!drawer.classList.contains("open"));
  $("#drawerx").onclick = () => setDrawer(false);
  $("#sharebtn").onclick = () => $("#qrbtn").click();
  // product mode on a real-farm scenario: no hackathon-only controls on the main screen (all still in Details)
  fetch("/api/info").then((r) => r.json()).then((i) => { if (i.geo) document.body.classList.add("product"); }).catch(() => {});
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setDrawer(false); });
  $$("[data-collapse]").forEach((h) => h.onclick = () => { const p = h.parentElement; p.classList.toggle(innerWidth <= 1100 ? "open" : "collapsed"); });
  if (innerWidth < 1600 && !JUDGE) $("footer").classList.add("folded");  // fault buttons one click away; keeps the map clear
  $("#cmdfold").onclick = () => { $("footer").classList.toggle("folded"); fitFooter(); };
  function fitFooter() {
    const w = $("#agentbar").getBoundingClientRect().width, h = $("footer").getBoundingClientRect().height;
    document.body.style.setProperty("--abw", `${Math.round(w)}px`); document.body.style.setProperty("--fb", `${Math.round(Math.max(88, h) + 24)}px`);
  }
  addEventListener("resize", fitFooter);

  // ---------------------------------------------------------------- GPS chip (replaces the floating GPS twin box)
  $("#gpschipbtn").onclick = (e) => { e.stopPropagation(); $("#gpsmenu").classList.toggle("hidden"); };
  document.addEventListener("click", (e) => { if (!$("#gpschip").contains(e.target)) $("#gpsmenu").classList.add("hidden"); });
  $$("[data-agc]").forEach((b) => b.onclick = () => post("/api/agow", { action: b.dataset.agc }).then((r) => { if (!r.ok) toast(r.msg, "err"); else toast("AgOpenWeb <- " + b.textContent.trim()); $("#gpsmenu").classList.add("hidden"); }));
  function renderChip() {
    const a = S.agopenweb, chip = $("#gpschip");
    if (!a || JUDGE) { chip.classList.add("hidden"); return; }
    chip.classList.remove("hidden");
    chip.classList.toggle("live", !!a.fresh); chip.classList.toggle("fault", !!a.gps_fault_active);
    $("#gpschiptxt").textContent = a.gps_fault_active ? "AgOpenWeb · GPS fault" : a.fresh ? `AgOpenWeb LIVE · ${a.has_control ? "control" : "observer"}` : a.connected ? "AgOpenWeb · waiting" : "AgOpenWeb · offline";
    const p = a.pose;
    $("#gpsmeta").innerHTML = p ? `${a.tractor} · <b>${p.speed}</b> kph · fix ${a.fix} · ${a.dist_m} m${a.note ? `<br>${esc(a.note)}` : ""}` : "waiting for GPS ticks";
    $("[data-agc=take_control]").style.display = a.has_control ? "none" : "";
  }

  // ---------------------------------------------------------------- Fleet
  let sel = null, selUntil = 0;
  const fieldName = (fid) => { const f = W && W.fields.find((x) => x.id === fid); return f ? `${f.id} ${f.name}` : fid; };
  const jobOf = (id) => S.jobs.find((j) => j.id === id);
  function renderFleet() {
    const html = S.tractors.map((tr) => {
      const col = trColor[tr.id] || "#0e7490", st = tr.status, cur = tr.cur, j = cur && cur.job_id ? jobOf(cur.job_id) : null;
      const what = j ? `<b>${j.id}</b> ${j.type} <span class="m">· ${esc(fieldName(j.field))}</span>` : cur ? esc(cur.label) : st === "broken" ? "Out of service, jobs re-planned" : "No task";
      const pct = j ? (cur.pct ?? j.pct ?? 0) : 0;
      const nxt = (tr.queue || []).find((t) => t.kind === "job");
      const nj = nxt && jobOf(nxt.job_id);
      const fuelCol = tr.fuel > 50 ? "#3fb68b" : tr.fuel > 25 ? "#e3a33b" : "#ef5a4a";
      const wait = st === "wait" && tr.wait_until ? ` until ${hm(tr.wait_until)}` : "";
      return `<div class="tcard ${st === "broken" ? "broken" : ""} ${sel === tr.id ? "sel" : ""}" data-tr="${tr.id}">
        <div class="trow"><div class="tbadge" style="background:${st === "broken" ? "#dc2626" : col}">${tr.id}</div><div><div class="tname">${esc(tr.name)}</div><div class="tmodel">${esc(tr.model || "")}</div></div><span class="chip ${st}">${STATUS[st] || st}${wait}</span></div>
        <div class="tjob">${what}</div>
        <div class="bar"><i style="width:${pct}%;background:${col}"></i></div>
        <div class="tmeta"><span>Progress</span><span>${j ? `${pct}%` : "–"}</span><span>Fuel ${Math.round(tr.fuel)}%</span><div class="bar"><i style="width:${tr.fuel}%;background:${fuelCol}"></i></div><span>Implement</span><span>${esc(tr.implement || "none")}</span></div>
        <div class="tnext">Next: ${nj ? `<b>${nj.id}</b> ${nj.type} · ${esc(fieldName(nj.field))} · starts ${hm(nxt.start)}` : "nothing queued"}</div></div>`;
    }).join("");
    const box = $("#fleetlist"); if (box._h !== html) { box.innerHTML = html; box._h = html; }
    const working = S.tractors.filter((t) => t.status === "work").length, broken = S.tractors.filter((t) => t.status === "broken").length;
    $("#fleetsum").textContent = `${working} working${broken ? ` · ${broken} broken` : ""}`;
    if (sel && performance.now() > selUntil) { sel = null; highlight(null); }
  }
  $("#fleetlist").onclick = (e) => {
    const c = e.target.closest("[data-tr]"); if (!c) return;
    const tr = S.tractors.find((t) => t.id === c.dataset.tr); if (!tr) return;
    if (VIEW !== "sat" && $("#viewsw [data-view=sat]").style.display !== "none") setView("sat");
    sel = tr.id; selUntil = performance.now() + 8000; highlight(tr.id);
    setTimeout(() => { if (SATV.map && tr.lat) SATV.map.flyTo(SATV.pos[tr.id] || [tr.lat, tr.lon], Math.max(SATV.map.getZoom(), 16), { duration: 0.6 }); }, VIEW === "sat" ? 0 : 120);
    renderFleet();
  };
  function highlight(id) {
    for (const [tid, r] of Object.entries(SATV.routes || {})) r.setStyle(id ? { weight: tid === id ? 5 : 2, opacity: tid === id ? 1 : 0.35 } : { weight: 3, opacity: 0.95 });
  }

  // ---------------------------------------------------------------- Alerts
  const open = new Set();
  const durTxt = (ms) => fmtDur(ms).replace("+", "");
  function renderAlerts() {
    const list = (typeof STORIES !== "undefined" ? STORIES : []).filter((s) => s.kind !== "initial").slice().reverse().slice(0, 14);
    if (!list.length) { const h = `<div class="aempty">No alerts. The fleet is running on plan.<br>Break a tractor, close a crossing or press GPS fault to see the agents respond.</div>`; if ($("#alertlist")._h !== h) { $("#alertlist").innerHTML = h; $("#alertlist")._h = h; } $("#alertsum").textContent = ""; return; }
    const meta = Object.fromEntries((S.agents || []).map((a) => [a.name, a]));
    let active = 0;
    const html = list.map((s, i) => {
      const by = (k) => s.steps.find((x) => x.key === k);
      const det = by("detected"), exe = by("executed"), sec = by("second");
      const isBreak = ["breakdown", "gps"].includes(s.kind) || by("broke") || by("gps");
      const resolved = isBreak ? !!(by("recovered") || by("normal")) : !!exe;
      if (!resolved) active++;
      const state = resolved ? (isBreak ? "resolved" : "handled") : "resolving";
      const expanded = open.has(s.id) || (i === 0 && !open.size && !resolved);
      const stats = [det && exe ? `<span>Detected to safe plan <b>${durTxt(exe.ms - det.ms)}</b></span>` : "", sec ? `<span>Second opinion <b>${durTxt(sec.ms)}</b></span>` : ""].join("");
      const tl = expanded ? `<div class="tl">${s.steps.map((x) => `<div class="s" style="--sc:${STEP_COLOR[x.key] || "#97a39b"}"><b>${esc(x.label)}</b> <span class="tt">${fmtDur(x.ms)} · ${x.clock}</span><div class="tx">${esc((meta[x.who] || {}).title || x.who)}${x.text ? " · " + esc(x.text) : ""}</div></div>`).join("")}</div>` : "";
      return `<div class="acard ${resolved ? "resolved" : isBreak ? "critical" : ""}" data-st="${s.id}"><div class="ahead"><div class="aico">${iconFor(s.kind)}</div><div class="atitle">${esc(s.text)}</div></div>
        <div class="asub"><span>#${s.id} · ${s.clock}</span><span class="astate">${state}</span><span>${s.messages} msgs</span></div>${stats ? `<div class="astats">${stats}</div>` : ""}${tl}</div>`;
    }).join("");
    const box = $("#alertlist"); if (box._h !== html) { box.innerHTML = html; box._h = html; }
    $("#alertsum").textContent = active ? `${active} active` : "all resolved";
  }
  $("#alertlist").onclick = (e) => { const c = e.target.closest("[data-st]"); if (!c) return; const id = +c.dataset.st; if (open.has(id)) open.delete(id); else open.add(id); renderAlerts(); };

  // ---------------------------------------------------------------- Agents bar
  const flash = {}; let lastBus = 0;
  function renderAgents() {
    const now = performance.now();
    for (const m of S.bus || []) if (m.id > lastBus) { flash[m.sender] = now; lastBus = m.id; }
    const html = (S.agents || []).map((a) => `<div class="adot ${now - (flash[a.name] || -1e9) < 1200 ? "on" : ""} ${a.busy ? "busy" : ""}" style="--c:${a.color || "#3fb68b"}" title="${esc(a.role || "")}"><i></i>${esc(a.title)}</div>`).join("");
    const box = $("#agentdots"); if (box._h !== html) { box.innerHTML = html; box._h = html; }
    const m = S.orch || {};
    const km = `<div><b>${fmtMs(m.avg_safe_ms)}</b><span>fault to safe plan</span></div><div><b>${m.avg_llm_ms ? fmtMs(m.avg_llm_ms) : m.avg_delib_ms ? fmtMs(m.avg_delib_ms) : "–"}</b><span>${m.avg_llm_ms ? "LLM deliberation" : "deliberation (rules)"}</span></div><div><b>${m.human_interventions ?? 0}</b><span>human interventions</span></div>`;
    if ($("#keym")._h !== km) { $("#keym").innerHTML = km; $("#keym")._h = km; }
  }

  // ---------------------------------------------------------------- loop
  let lastS = null, tick = 0;
  setInterval(() => {
    if (!S || JUDGE) return;
    if (S !== lastS) { lastS = S; renderFleet(); renderChip(); }
    renderAgents();
    if (tick++ % 4 === 0) { renderAlerts(); fitFooter(); }
  }, 250);
})();
