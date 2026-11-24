/* Fleet Copilot dashboard, vanilla JS + canvas, no build step. */
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const JUDGE = new URLSearchParams(location.search).has("judge") || !["localhost", "127.0.0.1"].includes(location.hostname);  // anyone off this laptop gets the judge view
if (JUDGE) document.body.classList.add("judge");

const THEMES = {
  field: { ink: "#1e2a21", muted: "#6b7568", line: "#e3dac6", ground: "#efe7d2", grid: "rgba(120,100,60,0.08)", water1: "#a9cbe9", water2: "#7fb0dc",
    wood: "#a07a4a", red: "#c0392b", green: "#2e7d4f", amber: "#b7791f", violet: "#6d4fc2", blue: "#2563a8", paper: "#fffdf8",
    fieldBase: "#fbf7ec", nodeFill: "#ffffff", depotFill: "#fff6df", llmFill: "#f4f0fc", edge: "rgba(30,42,33,0.10)", ghost: "#8a8f86", gantGrid: "rgba(0,0,0,0.05)" },
  night: { ink: "#e6edf3", muted: "#8b98a8", line: "#1f2d3b", ground: "#121c27", grid: "rgba(140,170,200,0.06)", water1: "#17426b", water2: "#2563a0",
    wood: "#7a5a32", red: "#ef6b5a", green: "#3fb68b", amber: "#e3a33b", violet: "#a78bfa", blue: "#5aa2ff", paper: "#111a24",
    fieldBase: "#16212d", nodeFill: "#16212d", depotFill: "#2a2212", llmFill: "#1d1934", edge: "rgba(200,220,240,0.10)", ghost: "#8b98a8", gantGrid: "rgba(255,255,255,0.05)" },
};
let T = THEMES.field;
function setTheme(name) { T = THEMES[name] || THEMES.field; document.body.classList.toggle("night", name === "night"); localStorage.setItem("theme", name); }
setTheme(localStorage.getItem("theme") || "field");
const TYPE_COLOR = { spray: "#2563a8", mow: "#3f8f2f", till: "#9a5b2a", seed: "#b8860b" };
const TYPE_ICON = { spray: "💧", mow: "✂", till: "⛏", seed: "🌱" };
const TR_COLORS = ["#0e7490", "#c2410c", "#7c3aed", "#be185d", "#4d7c0f"];
const KIND_ICON = { breakdown: "💥", repair: "🔧", block: "⛔", unblock: "✅", priority: "⚡", add_job: "➕", wind: "💨", close_field: "🌊",
  open_field: "🌤", pin: "📌", unpin: "📌", plan: "🧠", replan: "🧠", job_done: "✔", job_start: "▶", refuel: "⛽", swap: "🔧",
  abort: "⏸", safety: "🛡", violation: "🚨", command: "🎤", start: "🌅", end: "🌇", reroute: "↪", dispatch: "->" };
const ROUTINE = new Set(["job_start", "refuel", "swap", "reroute", "dispatch"]);

let W = null, S = null, trColor = {}, disp = {};
let lastExpId = 0, seenToasts = new Set(), lastAnswerId = 0, ttsOn = false, lastBusId = 0;
const hm = (m) => m == null ? "--:--" : `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(Math.round(m % 60)).padStart(2, "0")}`;
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtMs = (x) => x == null ? "–" : x < 1000 ? `${x.toFixed(x < 10 ? 1 : 0)} ms` : `${(x / 1000).toFixed(1)} s`;
const hexA = (hex, a) => { const n = parseInt(hex.slice(1), 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`; };

// ------------------------------------------------------------------ websocket
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws${JUDGE ? "?lite=1" : ""}`);
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    if (d.type === "static") { W = d.world; $("#scn").textContent = W.name; Object.keys(d.tractor_specs).forEach((t, i) => trColor[t] = TR_COLORS[i % TR_COLORS.length]); buildQuick(d.tractor_specs); resize(); }
    else if (d.type === "state") { S = d; onState(); }
  };
  ws.onclose = () => { $("#scn").textContent = "reconnecting…"; setTimeout(connect, 1000); };
}
connect();
async function post(url, body) { const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body || {}) }); return r.json(); }
const ev = (e) => post("/api/event", { ...e, source: JUDGE ? `judge${$("#judgeName").value ? ": " + $("#judgeName").value : ""}` : "operator" }).then((r) => { if (!r.ok) toast(r.msg, "err"); });

// ------------------------------------------------------------------ controls
$("#play").onclick = () => post("/api/control", { action: "toggle" });
$("#reset").onclick = () => { if (confirm("Reset the day?")) { lastExpId = 0; lastBusId = 0; post("/api/control", { action: "reset" }); } };
$("#skip").onclick = () => S && post("/api/control", { action: "jump", value: S.now + 30 });
$$("#speeds button").forEach((b) => b.onclick = () => post("/api/control", { action: "speed", value: +b.dataset.s }));
$(".agowhead").onclick = () => {  // satellite view: header only -> buttons -> full details -> header only
  const b = $("#agow");
  if (b.classList.contains("mini")) b.classList.remove("mini");
  else if (VIEW === "sat" && b.classList.contains("open")) { b.classList.remove("open"); b.classList.add("mini"); }
  else b.classList.toggle("open");
};
$("#theme").onclick = () => setTheme(document.body.classList.contains("night") ? "field" : "night");
$$(".agowbtns [data-ag]").forEach((b) => b.onclick = () => post("/api/agow", { action: b.dataset.ag }).then((r) => { if (!r.ok) toast(r.msg, "err"); else toast("AgOpenWeb <- " + b.textContent.trim()); }));
$("#tts").onclick = () => { ttsOn = !ttsOn; $("#tts").textContent = ttsOn ? "🔊" : "🔈"; };
$("#llm").onclick = () => S && post("/api/control", { action: "offline", value: !S.llm.offline_mode });
$$(".tabs button").forEach((b) => b.onclick = () => showTab(b.dataset.tab));
function showTab(t) { $$(".tabs button").forEach((x) => x.classList.toggle("on", x.dataset.tab === t)); $$(".tab").forEach((x) => x.classList.toggle("on", x.id === "tab-" + t)); if (t === "proof") loadProof(); if (t === "memory") loadMemory(); if (t === "tests") loadTests(); }
document.addEventListener("keydown", (e) => { if (e.target.tagName === "INPUT") return; if (e.code === "Space") { e.preventDefault(); $("#play").click(); } });
$("#saveRun").onclick = () => post("/api/export").then((r) => toast("Saved: " + r.files.join(", ")));
$("#modalx").onclick = () => $("#modal").classList.add("hidden");
$("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); };
$("#judgeName").value = localStorage.getItem("judgeName") || "";
$("#judgeName").oninput = () => localStorage.setItem("judgeName", $("#judgeName").value);
$("#feedTools").onchange = () => S && renderFeed();

function buildQuick(specs) {
  const q = $("#quick"); q.innerHTML = "";
  const add = (h) => q.insertAdjacentHTML("beforeend", h);
  add(`<span class="lbl">Simulate:</span>`);
  Object.keys(specs).forEach((t) => add(`<button data-ev='{"kind":"breakdown","tractor":"${t}"}' style="border-color:${trColor[t]};color:${trColor[t]}">💥 Break ${t}</button>`));
  if (!JUDGE) Object.keys(specs).forEach((t) => add(`<button data-ev='{"kind":"repair","tractor":"${t}"}' title="Repair ${t}">✚ Fix ${t}</button>`));
  add(`<button data-bridge="north_bridge">⛔ N bridge</button><button data-bridge="south_bridge">⛔ S bridge</button><button data-ev='{"kind":"wind","mph":14,"hours":2}'>💨 Gust</button><button id="rushBtn">⚡ Rush</button>`);
  if (!JUDGE) add(`<select id="addType"><option value="spray">💧 spray</option><option value="mow">✂ mow</option><option value="till">⛏ till</option><option value="seed">🌱 seed</option></select><select id="addField"></select><button id="addBtn">+ Job</button>`);
  $$("#quick [data-ev]").forEach((b) => b.onclick = () => ev(JSON.parse(b.dataset.ev)));
  $$("#quick [data-bridge]").forEach((b) => b.onclick = () => ev({ kind: S && S.blocked_names.includes(b.dataset.bridge) ? "unblock" : "block", target: b.dataset.bridge }));
  $("#rushBtn").onclick = () => {
    if (!S) return;
    const open = S.jobs.filter((j) => j.status !== "done" && j.priority !== "high");
    if (!open.length) return toast("Nothing left to rush");
    const j = JUDGE ? open[Math.floor(Math.random() * open.length)] : open.sort((a, b) => a.deadline - b.deadline)[0];
    ev({ kind: "priority", job: j.id, priority: "high", deadline: hm(Math.min(W.day_end - 60, Math.max(S.now + 150, j.deadline - 120))) });
  };
  if (!JUDGE) {
    $("#addField").innerHTML = W.fields.map((f) => `<option value="${f.id}">${f.id} ${esc(f.name)}</option>`).join("");
    $("#addBtn").onclick = () => ev({ kind: "add_job", type: $("#addType").value, field: $("#addField").value, priority: "high", deadline: hm(Math.min(W.day_end - 30, S.now + 300)) });
  }
}

// ------------------------------------------------------------------ command bar + voice
$("#cmd").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("#cmdtext").value.trim(); if (!text) return;
  $("#cmdtext").value = ""; toast(`🎤 “${text}”`); showTab("feed");
  const r = await post("/api/command", { text });
  if (!r.ok) return toast(r.msg, "err");
  for (const x of r.results || []) showAnswer(text, x.text, x.whynot || [], x.job);
};
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
if (SR) {
  const rec = new SR(); rec.lang = "en-US"; rec.interimResults = true; rec.continuous = false; let on = false, heard = false;
  rec.onstart = () => { heard = false; toast("Listening... speak now", "info"); };
  rec.onresult = (e) => { heard = true; const t = [...e.results].map((r) => r[0].transcript).join(""); $("#cmdtext").value = t; if (e.results[e.results.length - 1].isFinal) { rec.stop(); setTimeout(() => $("#cmd").requestSubmit(), 150); } };
  rec.onerror = (e) => { const why = { "not-allowed": "microphone blocked: click the lock icon in the address bar and allow the microphone", "no-speech": "no speech heard, try again closer to the mic", "audio-capture": "no microphone found, check Windows sound settings", "network": "speech service needs internet" }[e.error] || e.error; toast("Voice: " + why, "err"); };
  rec.onend = () => { on = false; $("#mic").classList.remove("rec"); };
  $("#mic").onclick = () => { if (on) { rec.stop(); return; } on = true; $("#mic").classList.add("rec"); try { rec.start(); } catch (err) { on = false; $("#mic").classList.remove("rec"); toast("Voice: " + err.message, "err"); } };
} else { $("#mic").onclick = () => toast("Voice input needs Chrome (localhost or https)", "err"); }

function showAnswer(q, text, whynot, job) {
  showTab("plan");
  const btns = (whynot || []).map((t) => `<button data-wn="${t}" data-job="${job}">Why not ${t}?</button>`).join("");
  $("#answer").innerHTML = `<div class="answer"><div class="q">❓ ${esc(q)}</div>${esc(text)}<div class="btns">${btns}</div><div id="wnout"></div></div>`;
  $$("#answer [data-wn]").forEach((b) => b.onclick = async () => { b.disabled = true; const r = await fetch(`/api/whynot?job=${b.dataset.job}&tractor=${b.dataset.wn}`).then((x) => x.json()); $("#wnout").insertAdjacentHTML("beforeend", `<div class="wn">${esc(r.text)}</div>`); });
}
async function whyModal(job) {
  const r = await fetch(`/api/why?job=${job}`).then((x) => x.json());
  $("#modalbody").innerHTML = `<b>Why?</b>\n${esc(r.text)}\n\n${(r.whynot || []).map((t) => `<button data-wn="${t}">Why not ${t}?</button>`).join(" ")}<div id="mwn"></div>`;
  $("#modal").classList.remove("hidden");
  $$("#modalbody [data-wn]").forEach((b) => b.onclick = async () => { b.disabled = true; const x = await fetch(`/api/whynot?job=${job}&tractor=${b.dataset.wn}`).then((y) => y.json()); $("#mwn").insertAdjacentHTML("beforeend", `<div class="wn">${esc(x.text)}</div>`); });
}
$("#qrbtn").onclick = async () => {
  const info = await fetch("/api/info").then((r) => r.json());
  const base = info.public_url || (["localhost", "127.0.0.1"].includes(location.hostname) ? info.lan_url : location.origin);
  $("#modalbody").innerHTML = `<div class="qr"><img id="qrimg"><div><h2 style="margin:0 0 6px;font-family:var(--serif)">Break it yourself 📱</h2>
    <div style="color:var(--muted)">Scan, then break a tractor, close a bridge or call a wind gust. Watch the agents negotiate a new plan in the network graph.</div>
    <div style="margin-top:10px;font-size:12px;color:var(--muted)">Public URL (paste your tunnel URL):</div><input id="qrbase" value="${esc(base)}"><div id="qrurl" style="font-size:12px;color:var(--blue);word-break:break-all"></div></div></div>`;
  $("#modal").classList.remove("hidden");
  const upd = () => { const u = $("#qrbase").value.trim().replace(/\/+$/, "") + "/?judge=1"; $("#qrimg").src = `/api/qr.svg?url=${encodeURIComponent(u)}`; $("#qrurl").textContent = u; };
  $("#qrbase").oninput = upd; upd();
};

// ------------------------------------------------------------------ state -> DOM
let domTick = 0;
function onState() {
  for (const t of S.tractors) if (!disp[t.id]) disp[t.id] = { x: t.x, y: t.y };
  for (const t of S.ghost) if (!disp["g" + t.id]) disp["g" + t.id] = { x: t.x, y: t.y };
  $("#clock").textContent = S.clock;
  if (W) $("#dayfill").style.width = `${(100 * (S.now - W.day_start)) / (W.day_end - W.day_start)}%`;
  $("#play").textContent = S.running ? "⏸ Pause" : (S.finished ? "✔ Day done" : (W && S.now > W.day_start ? "▶ Resume" : "▶ Start day"));
  $$("#speeds button").forEach((b) => b.classList.toggle("on", +b.dataset.s === S.speed));
  const w = $("#wind"); w.textContent = `💨 ${S.wind_now ?? "–"} mph · ${S.spray_ok ? "spray OK" : (S.wind_now < 3 ? "no spray (inversion)" : "no spray (drift)")}`; w.className = "pill " + (S.spray_ok ? "ok" : "bad");
  const llmName = S.orch ? S.orch.llm : "none";
  $("#llm").textContent = S.llm.offline_mode ? (llmName.startsWith("none") ? "📵 No signal -> rules" : `📵 No signal -> ${llmName}`) : (llmName.startsWith("none") ? "🧠 Agents: rules only" : `🧠 ${llmName}`);
  $("#llm").className = "pill clickable " + (llmName.startsWith("none") ? "" : "ok");
  renderScore();
  for (const t of S.toasts) if (!seenToasts.has(t.id)) { seenToasts.add(t.id); toast(t.msg, t.level); }
  spawnParticles();
  const latest = S.explanations[S.explanations.length - 1];
  if (latest && latest.id !== lastExpId) { const isNew = lastExpId !== 0; lastExpId = latest.id; renderExplain(isNew); if (isNew && ttsOn && "speechSynthesis" in window) speechSynthesis.speak(new SpeechSynthesisUtterance(latest.title)); }
  else if (domTick % 6 === 0) renderExplain(false);
  const ans = S.answers && S.answers[S.answers.length - 1];
  if (ans && ans.id !== lastAnswerId) { const first = lastAnswerId === 0; lastAnswerId = ans.id; if (!first) showAnswer(ans.q, ans.text, ans.whynot, ans.job); }
  const busy = (S.agents || []).filter((a) => a.busy);
  $("#thinking").style.display = busy.length ? "block" : "none";
  if (busy.length) $("#thinking").textContent = `🧠 ${busy.map((a) => a.title).join(" & ")} deliberating with ${busy[0].engine}, tractors keep working on the safe ⚡ reflex plan`;
  if (VIEW === "sat" && typeof renderSat === "function") renderSat();
  if (domTick % 2 === 0) { renderOrchMetrics(); if (!JUDGE) renderFeed(); }
  if (domTick++ % 3 === 0) { renderTractors(); renderResil(); if (!JUDGE) { renderJobs(); renderLog(); } }
}

function renderScore() {
  const c = S.metrics.copilot, f = S.metrics.fcfs;
  const cell = (a, b, lowerBetter, suffix = "") => { const win = lowerBetter ? a < b : a > b, lose = lowerBetter ? a > b : a < b; return [`<div class="v ${win ? "win" : ""}">${a}${suffix}</div>`, `<div class="v ${lose ? "" : "lose"}">${b}${suffix}</div>`]; };
  const cols = [["On time", cell(c.on_time, f.on_time, false, `<small>/${c.jobs_total}</small>`)], ["High-prio", cell(c.high_on_time, f.high_on_time, false, `<small>/${c.high_total}</small>`)],
    ["Hectares", cell(Math.round(c.area_done), Math.round(f.area_done), false)], ["Travel", cell(c.travel_min, f.travel_min, true)], ["Idle+wait", cell(c.idle_min + c.wait_min, f.idle_min + f.wait_min, true)]];
  $("#score").innerHTML = `<div class="scoreboard"><div></div>${cols.map((x) => `<div class="h">${x[0]}</div>`).join("")}<div class="who co">Agents</div>${cols.map((x) => x[1][0]).join("")}<div class="who fc" title="Baseline: the same day and events, planned first-come, first-served">Baseline (FCFS)</div>${cols.map((x) => x[1][1]).join("")}</div>`;
}

function renderOrchMetrics() {
  const m = S.orch; if (!m) return;
  const cell = (v, l, cls = "") => `<div class="${cls}"><b>${v}</b><span>${l}</span></div>`;
  $("#orchm").innerHTML = cell(m.incidents, "incidents") + cell(fmtMs(m.avg_safe_ms), "⚡ fault -> safe plan", "hl")
    + cell(m.avg_llm_ms ? fmtMs(m.avg_llm_ms) : (m.avg_delib_ms ? fmtMs(m.avg_delib_ms) : "–"), m.avg_llm_ms ? "🧠 LLM deliberation" : "🧠 deliberation (rules)", "vi")
    + cell(m.speedup ? `${Math.round(m.speedup)}×` : "–", "faster than waiting for LLM", "hl") + cell(m.learned, "♻ learned-reflex hits")
    + cell(m.msgs_per_incident ? m.msgs_per_incident.toFixed(1) : "–", "msgs / incident") + cell(`${m.tool_calls}`, `tool calls · ${m.llm_calls} LLM`)
    + cell(m.human_interventions, "human interventions");
}

function pathBadge(p) { if (!p) return ""; const m = { reflex: "⚡ reflex", learned: "♻ learned reflex", deliberate: "🧠 LLM-deliberated", "reflex-fallback": "⚡ fallback" }; return `<span class="path ${p.startsWith("reflex") ? "reflex" : p}">${m[p] || p}</span>`; }
function agentLabel(n) {
  const a = (S.agents || []).find((x) => x.name === n);
  if (a) return `<span style="color:${NODE_COLOR[n] || a.color}">${a.emoji} ${a.title}</span>`;
  if (n.startsWith("tractor:")) { const t = n.slice(8); return `<span style="color:${trColor[t]}">🚜 ${t}</span>`; }
  if (n === "operator") return "👩‍🌾 Operator";
  return esc(n);
}
function renderFeed() {
  $("#agentchips").innerHTML = (S.agents || []).map((a) => `<span class="achip ${a.busy ? "busy" : ""}" title="${esc(a.role)}">${a.emoji} ${a.title}: ${esc(a.engine)}</span>`).filter((_, i) => [1, 5].includes(i)).join("");
  const showTools = $("#feedTools").checked;
  $("#feed").innerHTML = S.bus.slice().reverse().filter((x) => showTools || (x.kind !== "tool" && x.kind !== "thought")).map((x) => {
    const self = x.sender === x.to;
    return `<div class="msg ${x.kind === "tool" ? "tool" : x.kind === "thought" ? "thought" : ""} k-${x.kind}"><span class="tm">${hm(x.t)}${x.incident ? `<br>#${x.incident}` : ""}</span>
      <div><span class="hd">${self ? agentLabel(x.sender) : `${agentLabel(x.sender)} -> ${agentLabel(x.to)}`}</span><span class="k">${x.kind === "tool" ? "🔧 tool" : x.kind}</span>${x.ms != null ? `<span class="ms">${fmtMs(x.ms)}</span>` : ""}
      <div class="body">${esc(x.text)}</div></div></div>`;
  }).join("");
}

function renderExplain(flash) {
  const ex = S.explanations.slice().reverse();
  if (!ex.length) { $("#explain").innerHTML = `<div class="exp">Press <b>▶ Start day</b>.</div>`; return; }
  const card = (e, latest) => {
    const imp = e.impact || {}, chips = [];
    chips.push(`<span class="chip ${imp.late_projected && imp.late_projected.length ? "warn" : "good"}">${imp.on_time_projected}/${imp.total_jobs} projected on time</span>`);
    if (imp.on_time_delta !== undefined) chips.push(`<span class="chip ${imp.on_time_delta < 0 ? "bad" : "good"}">${imp.on_time_delta >= 0 ? "+" : ""}${imp.on_time_delta} on-time</span>`);
    if (imp.travel_delta !== undefined) chips.push(`<span class="chip ${imp.travel_delta > 20 ? "warn" : ""}">${imp.travel_delta >= 0 ? "+" : ""}${imp.travel_delta} min travel</span>`);
    if (imp.changes != null) chips.push(`<span class="chip">${imp.changes} change(s)</span>`);
    if (imp.untouched && imp.untouched.length) chips.push(`<span class="chip good">${imp.untouched.join(", ")} untouched</span>`);
    if (imp.unassigned && imp.unassigned.length) chips.push(`<span class="chip bad">not today: ${imp.unassigned.join(", ")}</span>`);
    if (e.agent_strategy && e.agent_strategy.objective) chips.push(`<span class="chip">strategy: ${esc(e.agent_strategy.objective)}</span>`);
    const s = e.safety || {};
    const llmBlock = e.llm_text ? `<div class="llm"><small>🧠 ${esc(e.llm_source)}, plain-language summary</small>${esc(e.llm_text)}</div>` : "";
    return `<div class="exp ${latest ? "latest" : ""} ${latest && flash ? "flash" : ""}"><div class="t"><b>${esc(e.title)}${pathBadge(e.path)}</b><span>${e.clock}${e.version ? " · plan v" + e.version : ""}</span></div>${llmBlock}
      <ul>${e.lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul><div class="chips">${chips.join("")}</div>
      <div class="safety"><details><summary>${esc(e.safety_line)}</summary>${(s.checks || []).map((c) => `✓ ${esc(c)}`).join("<br>")}${(s.warnings || []).length ? "<br><br>⚠ " + s.warnings.map(esc).join("<br>⚠ ") : ""}</details></div></div>`;
  };
  $("#explain").innerHTML = card(ex[0], true) + (ex.length > 1 ? `<details class="older"><summary>Earlier plan versions (${ex.length - 1})</summary>${ex.slice(1).map((e) => card(e, false)).join("")}</details>` : "");
}
function renderResil() {
  const R = S.resilience || {}, ids = Object.keys(R);
  $("#resil").innerHTML = ids.length ? `<div class="sect">🛟 Plan B, precomputed for every tractor (N-1)</div><div class="resil">${ids.map((t) => { const r = R[t]; const cls = r.at_risk === 0 ? "good" : r.at_risk <= 2 ? "warn" : "bad"; return `<div class="row"><span class="chip ${cls}">If ${t} fails now</span>${r.at_risk === 0 ? "nothing slips" : `${r.at_risk} job(s) at risk: ${r.jobs.join(", ")}`}</div>`; }).join("")}</div>` : "";
}
function taskText(t) {
  if (!t) return "standby";
  if (t.kind === "job") { const j = S.jobs.find((x) => x.id === t.job_id); return `${t.job_id} ${j ? TYPE_ICON[j.type] + " " + j.type + " " + j.field : ""}${t.pct != null ? " · " + t.pct + "%" : ""} · ETA ${hm(t.end)}`; }
  return t.kind === "refuel" ? `⛽ refuel -> ${hm(t.end)}` : `🔧 swap to ${t.implement} -> ${hm(t.end)}`;
}
function renderTractors() {
  if (JUDGE) return;
  $("#tractors").innerHTML = `<div class="sect">Fleet</div><div class="trs">${S.tractors.map((t) => {
    const fc = t.fuel > 50 ? T.green : t.fuel > 25 ? T.amber : T.red;
    const next = (t.queue || []).slice(0, 3).map((q) => q.kind === "job" ? q.job_id : q.kind === "refuel" ? "⛽" : "🔧").join(" -> ");
    return `<div class="tr"><div class="dot" style="background:${trColor[t.id]}"></div><div><div class="name">${t.id} ${esc(t.name)} <span class="sub">· ${esc(t.implement || "-")} · ${t.hours} h since service</span></div>
      <div class="sub">${t.status === "broken" ? "💥 broken down" : esc(taskText(t.cur))}${next ? " &nbsp;then " + next : ""}</div><div class="fuel"><i style="width:${t.fuel}%;background:${fc}"></i></div></div><span class="status ${t.status}">${t.status}</span></div>`;
  }).join("")}</div>`;
}
function renderJobs() {
  const ord = { active: 0, planned: 1, unscheduled: 2, done: 3 };
  $("#jobs").innerHTML = `<tr><th>Job</th><th>Work</th><th>Prio</th><th>Due</th><th>Tractor</th><th>ETA</th><th>Status</th><th></th></tr>` + S.jobs.slice().sort((a, b) => ord[a.status] - ord[b.status] || a.deadline - b.deadline).map((j) => `
    <tr><td><b>${j.id}</b>${j.pinned ? " 📌" : ""}</td><td>${TYPE_ICON[j.type]} ${j.type} ${j.field}<br><span style="color:var(--muted);font-size:11px">${j.done_ha}/${j.area} ha</span></td>
    <td class="p-${j.priority}">${j.priority}</td><td>${hm(j.deadline)}</td><td>${j.tractor ? `<span style="color:${trColor[j.tractor]}">●</span> ${j.tractor}` : "-"}</td>
    <td class="${j.eta ? (j.late ? "late" : "ok") : ""}">${hm(j.eta)}</td><td><span class="st ${j.status}" title="${esc(j.reason || "")}">${j.status === "active" ? j.pct + "%" : j.status}</span></td>
    <td><button onclick="whyModal('${j.id}')">Why?</button></td></tr>`).join("");
}
function renderLog() {
  const all = $("#logAll").checked;
  $("#log").innerHTML = (S.log || []).slice().reverse().filter((e) => all || !ROUTINE.has(e.kind)).map((e) => `<div class="le k-${e.kind}"><span class="tm">${e.clock}</span><span>${KIND_ICON[e.kind] || "·"}</span><span>${esc(e.msg)}${e.source !== "system" ? `<span class="src">${esc(e.source)}</span>` : ""}</span></div>`).join("");
}
async function loadMemory() {
  const M = await fetch("/api/memory").then((r) => r.json());
  const refl = Object.entries(M.reflexes);
  $("#memory").innerHTML = `<div class="sect">Operator policies, read by the LLM Planner</div>${M.policies.map((p) => `<div class="chip" style="margin:3px 0;display:block">📜 ${esc(p)}</div>`).join("")}
    <form id="polform" style="display:flex;gap:6px;margin-top:6px"><input id="poltext" style="flex:1" placeholder="Add a policy, e.g. “Keep T2 off high-priority work, it broke twice this week”"><button>Add</button></form>
    <div class="sect">♻ Learned reflexes, incident signature -> strategy (persist across runs) <button id="forget" style="float:right;font-size:11px;padding:1px 8px">forget all</button></div>
    ${refl.length ? `<table class="memtbl"><tr><th>Signature</th><th>Strategy</th><th>Learned by</th><th>Took</th><th>Uses</th></tr>${refl.map(([k, r]) => `<tr><td>${esc(k)}</td><td>${esc(r.strategy.objective)}${r.strategy.avoid && r.strategy.avoid.length ? " avoid " + r.strategy.avoid : ""}${r.strategy.defer && r.strategy.defer.length ? " defer " + r.strategy.defer : ""}</td><td>${esc(r.source)} @${r.learned_at}</td><td>${fmtMs(r.learned_ms)}</td><td>${r.uses}</td></tr>`).join("")}</table>` : "<p style='color:var(--muted)'>None yet, break a tractor and watch the Planner learn one.</p>"}
    <div class="sect">Blackboard facts</div><table class="memtbl"><tr><th>Key</th><th>Value</th><th>By</th><th>At</th></tr>${Object.entries(M.facts).map(([k, f]) => `<tr><td>${esc(k)}</td><td>${esc(f.value)}</td><td>${esc(f.by)}</td><td>${f.clock}</td></tr>`).join("")}</table>
    <div class="sect">Episodes</div>${M.episodes.slice().reverse().map((e) => `<div class="le"><span class="tm">${e.t}</span><span>${e.path === "learned" ? "♻" : e.path === "deliberate" ? "🧠" : "⚡"}</span><span>${esc(e.text || e.kind)} -> plan v${e.plan_version} [${esc((e.strategy || {}).objective || "")}] ${e.safe_ms != null ? fmtMs(e.safe_ms) : ""}${e.provider ? " · deliberated by " + esc(e.provider) : ""}</span></div>`).join("")}
    <p style="color:var(--muted);font-size:11px">memory reads ${M.reads} · writes ${M.writes}</p>`;
  $("#polform").onsubmit = async (e) => { e.preventDefault(); await post("/api/policy", { text: $("#poltext").value }); loadMemory(); };
  $("#forget").onclick = async () => { await post("/api/control", { action: "forget" }); loadMemory(); };
}
async function loadTests(run = false) {
  if (run) $("#tests").innerHTML = "Running every fault-injection case through the full agent loop…";
  const L = await fetch("/api/tests").then((r) => r.json());
  const last = run ? (await post("/api/tests/run")).results : L.last;
  const by = Object.fromEntries((last || []).map((r) => [r.id, r]));
  const passed = (last || []).filter((r) => r.passed).length;
  $("#tests").innerHTML = `<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px"><button id="runAll" class="primary">▶ Run all ${L.cases.length} cases</button>${last ? `<b class="${passed === last.length ? "pass" : "fail"}">${passed} / ${last.length} passed</b>` : ""}</div>` +
    L.cases.map((c) => { const r = by[c.id]; return `<div class="case"><div class="top"><b>${r ? (r.passed ? "✅" : "❌") : "◻"} ${esc(c.title)}</b><button data-play="${c.id}">▶ Play live</button></div><div class="story">${esc(c.story)}</div>
      ${r ? `<ul>${r.checks.map((k) => `<li class="${k.ok ? "" : "fail"}">${k.ok ? "✓" : "✗"} ${esc(k.check)}${k.detail ? ` <span style="color:var(--muted)">- ${esc(k.detail)}</span>` : ""}</li>`).join("")}</ul>
      <div class="story">${r.agents.incidents} incidents · ⚡ ${fmtMs(r.agents.avg_safe_ms)} avg · ${(r.agents.msgs_per_incident || 0).toFixed(1)} msgs/incident · ${r.agents.tool_calls} tool calls · on-time ${r.copilot.on_time} vs FCFS ${r.fcfs.on_time}</div>` : ""}</div>`; }).join("");
  $("#runAll").onclick = () => loadTests(true);
  $$("#tests [data-play]").forEach((b) => b.onclick = async () => { lastBusId = 0; await post("/api/tests/play", { id: b.dataset.play }); showTab("feed"); });
}
async function loadProof() {
  const p = await fetch("/api/proof").then((r) => r.json()); const ch = p.chaos;
  let h = `<div class="proof">`;
  if (ch) h += `<h2>Chaos test, full agent loop, every invariant checked every minute</h2><div class="big"><div><b>${ch.runs}</b><span>random farms · ${ch.events_injected} faults</span></div><div><b>${ch.agent_incidents || "–"}</b><span>incidents · ${ch.agent_messages || "–"} agent msgs</span></div><div><b>${ch.copilot_violations} / ${ch.crashes} / ${ch.unhandled_incidents ?? 0}</b><span>violations / crashes / unhandled</span></div></div>`;
  h += `<button id="liveChaos" class="primary">▶ Run live chaos test (200 farms)</button><div id="liveOut"></div>` + (p["comparison.md"] ? md(p["comparison.md"]) : "");
  $("#proof").innerHTML = h + "</div>";
  $("#liveChaos").onclick = async () => { $("#liveChaos").disabled = true; $("#liveOut").innerHTML = "Running 200 random farms through the agent loop…"; const r = await post("/api/chaos", { n: 200 });
    $("#liveOut").innerHTML = `<div class="big"><div><b>${r.runs}</b><span>farms in ${r.seconds}s</span></div><div><b>${r.events}</b><span>faults injected</span></div><div><b>${r.violations} / ${r.crashes}</b><span>violations / crashes</span></div></div><p>Priority-weighted on-time work: <b>${r.copilot_on_time}</b> vs ${r.fcfs_on_time} FCFS, <b>${Math.round(100 * (r.copilot_on_time - r.fcfs_on_time) / Math.max(1, r.fcfs_on_time))}% more</b></p>`; $("#liveChaos").disabled = false; };
}
function md(src) { const lines = src.split("\n"); let out = "", tbl = []; const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  const flush = () => { if (!tbl.length) return; const rows = tbl.filter((r) => !/^\|\s*-/.test(r)); out += "<table>" + rows.map((r, i) => "<tr>" + r.split("|").slice(1, -1).map((c) => i ? `<td>${inline(c.trim())}</td>` : `<th>${inline(c.trim())}</th>`).join("") + "</tr>").join("") + "</table>"; tbl = []; };
  for (const l of lines) { if (l.startsWith("|")) { tbl.push(l); continue; } flush(); if (l.startsWith("# ")) out += `<h1>${inline(l.slice(2))}</h1>`; else if (l.startsWith("## ")) out += `<h2>${inline(l.slice(3))}</h2>`; else if (l.trim()) out += `<p>${inline(l)}</p>`; } flush(); return out; }
function toast(msg, level = "info") { const t = document.createElement("div"); t.className = "toast " + level; t.textContent = msg; $("#toasts").appendChild(t); setTimeout(() => t.remove(), level === "event" ? 5000 : 3600); while ($("#toasts").children.length > 3) $("#toasts").firstChild.remove(); }

// ------------------------------------------------------------------ agent network graph
const net = $("#net"), nctx = net.getContext("2d");
const NODES = {
  operator: { x: 0.03, y: 0.90, e: "👩‍🌾", t: "Operator" }, liaison: { x: 0.20, y: 0.90, e: "💬", t: "Liaison" },
  monitor: { x: 0.03, y: 0.47, e: "🔎", t: "Fault Detection" }, planner: { x: 0.30, y: 0.47, e: "🧠", t: "Planner" },
  llm: { x: 0.30, y: 0.06, e: "☁", t: "LLM" }, memory: { x: 0.46, y: 0.90, e: "💾", t: "Shared memory" },
  navigation: { x: 0.57, y: 0.06, e: "🧭", t: "Navigation" }, safety: { x: 0.57, y: 0.47, e: "🛡", t: "Safety" },
  execution: { x: 0.84, y: 0.47, e: "🚀", t: "Execution" }, tractors: { x: 0.97, y: 0.90, e: "🚜", t: "Tractors" },
  telemetry: { x: 0.80, y: 0.06, e: "🛰", t: "GPS Twin" },
};
const NODE_COLOR = { telemetry: "#0f766e", monitor: "#c2410c", planner: "#6d4fc2", navigation: "#2563a8", safety: "#2e7d4f", execution: "#b7791f", liaison: "#0e7490", llm: "#6d4fc2", memory: "#6b7568", operator: null, tractors: null };
const EDGES = [["monitor", "planner"], ["planner", "navigation"], ["planner", "safety"], ["safety", "execution"], ["execution", "tractors"], ["execution", "monitor"],
  ["execution", "liaison"], ["liaison", "operator"], ["planner", "llm"], ["liaison", "llm"], ["planner", "memory"], ["monitor", "memory"], ["execution", "memory"], ["liaison", "memory"],
  ["telemetry", "tractors"], ["telemetry", "monitor"], ["telemetry", "memory"]];
let particles = [], pulses = {};
function nodeOf(n) { if (n.startsWith("tractor:")) return "tractors"; return NODES[n] ? n : null; }
function spawnParticles() {
  if (!S.bus) return;
  const fresh = S.bus.filter((m) => m.id > lastBusId);
  if (!fresh.length) return;
  const first = lastBusId === 0; lastBusId = Math.max(...S.bus.map((m) => m.id));
  if (first) return;
  let delay = 0; const now = performance.now();
  for (const m of fresh.slice(-30)) {
    let a = nodeOf(m.sender), b = nodeOf(m.to), col = NODE_COLOR[m.sender] || T.ink;
    if (m.kind === "tool") {
      const txt = m.text || "";
      if (/^(recall|remember)/.test(txt)) b = "memory";
      else if (m.sender === "planner" && /simulate_strategy|submit_strategy|get_situation|check_route/.test(txt) && S.agents.find((x) => x.name === "planner" && x.busy)) b = "llm";
      else { pulses[a] = now + delay; delay += 40; continue; }
      [a, b] = b === "llm" ? ["llm", "planner"] : [a, b];
    } else if (m.kind === "thought") { if (/🧠/.test(m.text) && a === "planner") { b = "llm"; col = NODE_COLOR.llm; } else { pulses[a] = now + delay; continue; } }
    else if (m.kind === "strategy") { a = "llm"; b = "planner"; col = NODE_COLOR.llm; }
    else if (m.kind === "memory") { b = "memory"; }
    if (!a || !b || a === b) { if (a) pulses[a] = now + delay; continue; }
    particles.push({ a, b, t0: now + delay, dur: 650, col, kind: m.kind });
    delay += 140;
  }
}
function drawNet(t) {
  const r = net.getBoundingClientRect(); if (r.width < 10) return;
  const ctx = nctx; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, r.width, r.height);
  const P = (n) => ({ x: 40 + NODES[n].x * (r.width - 80), y: 20 + NODES[n].y * (r.height - 50) });
  const roomy = r.height > 210;
  const busy = new Set((S && S.agents || []).filter((a) => a.busy).map((a) => a.name));
  const hasTel = (S && S.agents || []).some((a) => a.name === "telemetry");
  for (const [a, b] of EDGES) { if ((a === "telemetry" || b === "telemetry") && !hasTel) continue; const p = P(a), q = P(b); ctx.strokeStyle = (a === "planner" && b === "llm" && busy.has("planner")) ? hexA(T.violet, 0.6) : T.edge; ctx.lineWidth = b === "memory" ? 1 : 1.6; ctx.setLineDash(b === "memory" || b === "llm" ? [3, 4] : []); ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(q.x, q.y); ctx.stroke(); }
  ctx.setLineDash([]);
  particles = particles.filter((p) => t < p.t0 + p.dur + 200);
  for (const p of particles) {
    if (t < p.t0) continue;
    const k = Math.min(1, (t - p.t0) / p.dur), e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
    const A = P(p.a), B = P(p.b), x = A.x + (B.x - A.x) * e, y = A.y + (B.y - A.y) * e;
    ctx.strokeStyle = hexA(p.col, 0.35); ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(A.x + (B.x - A.x) * Math.max(0, e - 0.18), A.y + (B.y - A.y) * Math.max(0, e - 0.18)); ctx.lineTo(x, y); ctx.stroke();
    ctx.fillStyle = p.col; ctx.beginPath(); ctx.arc(x, y, p.kind === "fault" || p.kind === "risk" ? 6 : 4.5, 0, Math.PI * 2); ctx.fill();
    if (k >= 1) pulses[p.b] = Math.max(pulses[p.b] || 0, p.t0 + p.dur);
  }
  const stats = Object.fromEntries((S && S.agents || []).map((a) => [a.name, a]));
  for (const [n, nd] of Object.entries(NODES)) {
    if (n === "telemetry" && !hasTel) continue;
    const p = P(n), col = NODE_COLOR[n] || T.ink, R = n === "planner" ? (roomy ? 20 : 16) : (roomy ? 16 : 13);
    const since = t - (pulses[n] || -1e9);
    if (since >= 0 && since < 600) { ctx.strokeStyle = hexA(col, 0.5 * (1 - since / 600)); ctx.lineWidth = 3; ctx.beginPath(); ctx.arc(p.x, p.y, R + 4 + since / 40, 0, Math.PI * 2); ctx.stroke(); }
    if (busy.has(n) || (n === "llm" && busy.size)) { ctx.strokeStyle = T.violet; ctx.lineWidth = 2.5; ctx.beginPath(); ctx.arc(p.x, p.y, R + 5, t / 180, t / 180 + 4); ctx.stroke(); }
    if (n === "memory") { ctx.fillStyle = T.nodeFill; ctx.strokeStyle = col; ctx.lineWidth = 1.5; rr(ctx, p.x - 18, p.y - 14, 36, 28, 6); ctx.fill(); ctx.stroke(); }
    else if (n === "llm") { ctx.fillStyle = T.llmFill; ctx.strokeStyle = col; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.ellipse(p.x, p.y, 62, 13, 0, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); }
    else { ctx.fillStyle = T.nodeFill; ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(p.x, p.y, R, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); }
    ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.font = `${n === "planner" ? 18 : 15}px sans-serif`; ctx.fillStyle = T.ink;
    ctx.fillText(n === "llm" ? "" : nd.e, p.x, p.y + 1);
    ctx.textBaseline = "alphabetic";
    ctx.font = "700 10.5px Inter, sans-serif"; ctx.fillStyle = col;
    const llmOn = S && S.orch && !S.orch.llm.startsWith("none");
    const label = n === "llm" ? (llmOn ? "☁ " + S.orch.llm.split(":").slice(1).join(":").slice(0, 20) : "☁ no LLM -> rules") : nd.t;
    if (n === "llm") { ctx.fillText(label, p.x, p.y + 4); }
    else if (["monitor", "planner", "safety", "execution", "navigation", "telemetry"].includes(n) && !roomy) { ctx.textAlign = "left"; ctx.fillText(label, p.x + R + 5, p.y - 2); }
    else ctx.fillText(label, p.x, p.y + R + 12);
    const st = stats[n];
    if (st && !roomy && ["monitor", "planner", "safety", "execution", "navigation", "telemetry"].includes(n)) { ctx.textAlign = "left"; ctx.font = "600 9.5px Inter, sans-serif"; ctx.fillStyle = T.muted; ctx.fillText(`↑${st.sent} ↓${st.received} 🔧${st.tools}${st.llm_calls ? " 🧠" + st.llm_calls : ""}`, p.x + R + 5, p.y + 10); }
    else if (st) { ctx.font = "600 9.5px Inter, sans-serif"; ctx.fillStyle = T.muted; ctx.fillText(`↑${st.sent} ↓${st.received} · 🔧${st.tools}${st.llm_calls ? " · 🧠" + st.llm_calls : ""}`, p.x, p.y + R + 24); }
  }
}

// ------------------------------------------------------------------ map
const map = $("#map"), mctx = map.getContext("2d"), gantt = $("#gantt"), gctx = gantt.getContext("2d");
let cs = 20, ox = 0, oy = 0, dpr = 1;
function resize() {
  if (!W) return; dpr = window.devicePixelRatio || 1;
  if (map.getBoundingClientRect().width < 20) { const r = net.getBoundingClientRect(); net.width = Math.max(10, r.width * dpr); net.height = Math.max(10, r.height * dpr); const g = gantt.getBoundingClientRect(); if (g.width > 10) { gantt.width = g.width * dpr; gantt.height = g.height * dpr; } return; }
  for (const c of [map, gantt, net]) { const r = c.getBoundingClientRect(); c.width = Math.max(10, r.width * dpr); c.height = Math.max(10, r.height * dpr); }
  const r = map.getBoundingClientRect(); cs = Math.min((r.width - 16) / W.width, (r.height - 12) / W.height); ox = (r.width - cs * W.width) / 2; oy = (r.height - cs * W.height) / 2;
}
window.addEventListener("resize", resize);
new ResizeObserver(resize).observe(map); new ResizeObserver(resize).observe(net); new ResizeObserver(resize).observe(gantt);

// ------------------------------------------------------------------ main view: AgOpenWeb or farm map
let VIEW = null, agowUrl = null, SCN = "demo", INFO = null;
function setView(v) {
  VIEW = v;
  $$("#viewsw button").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
  $("#agowview").classList.toggle("hidden", v !== "agow");
  map.classList.toggle("hidden", v !== "farm");
  $("#sat").classList.toggle("hidden", v !== "sat");
  $("#farmlegend").style.display = v === "agow" ? "none" : "";
  $("#showGhost").parentElement.style.display = v === "sat" ? "none" : "";
  if (v === "agow") loadAgowFrame();
  $("#agow").classList.toggle("mini", v === "sat"); if (v === "sat") $("#agow").classList.remove("open");
  if (v === "sat") { initSat(); setTimeout(() => { if (SATV.map) { SATV.map.invalidateSize(); if (!SATV.fitted) satFit(); } }, 40); }
  try { localStorage.setItem("fleet.view." + SCN, v); } catch (e) { /* ignore */ }
  setTimeout(resize, 30);
}
$$("#viewsw button").forEach((b) => b.onclick = () => setView(b.dataset.view));
// Load the AgOpenWeb page only after our GPS Twin holds control, so the embedded page joins as an observer.
let agowWait = 0;
function loadAgowFrame() {
  const f = $("#agowframe"); if (!agowUrl || f.src) return;
  const a = S && S.agopenweb;
  if ((a && a.has_control) || agowWait > 16) { f.src = agowUrl; return; }
  agowWait++; setTimeout(loadAgowFrame, 500);
}
fetch("/api/info").then((r) => r.json()).then((info) => {
  const local = ["localhost", "127.0.0.1"].includes(location.hostname);
  agowUrl = info.agopenweb_view ? info.agopenweb_view.replace("localhost", local ? "localhost" : location.hostname) : null;
  if (agowUrl) $("#agowopen").href = agowUrl;
  INFO = info; SCN = info.scenario_id || "demo";
  const sat = !!(info.geo && window.L);  // real-farm scenario and Leaflet loaded (needs internet)
  if (sat) $("#viewsw [data-view=sat]").style.display = "";
  let saved = null; try { saved = localStorage.getItem("fleet.view." + SCN); } catch (e) { /* ignore */ }
  if ((saved === "sat" && !sat) || (saved === "agow" && !(agowUrl && local))) saved = null;
  const whenReady = (fn) => (W ? fn() : setTimeout(() => whenReady(fn), 100));  // the satellite view needs the static world first
  whenReady(() => setView(saved || (sat ? "sat" : agowUrl && local && !JUDGE ? "agow" : "farm")));
  if (!agowUrl) $("#viewsw [data-view=agow]").style.display = "none";
}).catch(() => setView("farm"));

// ------------------------------------------------------------------ incident story
let BOTTOM = "story", STORIES = [], storyPick = null;
$$(".bottomsw [data-bottom]").forEach((b) => b.onclick = () => {
  BOTTOM = b.dataset.bottom; $$(".bottomsw [data-bottom]").forEach((x) => x.classList.toggle("on", x === b));
  $("#story").classList.toggle("hidden", BOTTOM !== "story"); gantt.classList.toggle("hidden", BOTTOM !== "gantt"); $("#storypick").style.display = BOTTOM === "story" ? "" : "none";
  setTimeout(resize, 30);
});
$("#storypick").onchange = (e) => { storyPick = +e.target.value || null; renderStory(); };
function fmtDur(ms) {
  const a = Math.abs(ms), neg = ms < 0;
  const s = a < 1000 ? `${a < 10 ? a.toFixed(1) : Math.round(a)} ms` : a < 60000 ? `${(a / 1000).toFixed(1)} s` : `${Math.floor(a / 60000)} min ${Math.round((a % 60000) / 1000)} s`;
  return neg ? `${s} before` : (ms === 0 ? "0 ms" : `+${s}`);
}
async function loadStories() {
  try { STORIES = (await fetch("/api/stories?n=12").then((r) => r.json())).stories || []; } catch (e) { return; }
  const list = STORIES.filter((s) => s.kind !== "initial").reverse();
  const sel = $("#storypick"), want = list.map((s) => `${s.id}|${s.clock} #${s.id} ${s.kind}`).join(";");
  if (sel.dataset.k !== want) { sel.dataset.k = want; sel.innerHTML = `<option value="">Latest incident</option>` + list.map((s) => `<option value="${s.id}">${s.clock} · #${s.id} ${esc(s.kind)}</option>`).join(""); if (storyPick) sel.value = storyPick; }
  renderStory();
}
function renderStory() {
  const box = $("#story");
  const list = STORIES.filter((s) => s.kind !== "initial");
  const s = (storyPick && STORIES.find((x) => x.id === storyPick)) || list[list.length - 1];
  if (!s) { box.innerHTML = `<div class="empty">No incidents yet. Start the day, then break a tractor (or press GPS fault in AgOpenWeb). Every step will show up here with its time.</div>`; return; }
  const meta = Object.fromEntries((S && S.agents || []).map((a) => [a.name, a]));
  const by = (k) => s.steps.find((x) => x.key === k);
  const det = by("detected"), exe = by("executed"), sec = by("second"), rec = by("recovered"), brk = by("broke"), gps = by("gps");
  const toMin = (c) => { const [h, m] = c.split(":").map(Number); return h * 60 + m; };
  const sum = [
    gps ? `<span>GPS to detection: <b>${fmtDur(-gps.ms).replace("+", "")}</b></span>` : "",
    det && exe ? `<span>Detection to safe plan on tractors: <b>${fmtDur(exe.ms - det.ms).replace("+", "")}</b></span>` : "",
    sec ? `<span>Second opinion (${esc(s.provider || "rules")}): <b>${fmtDur(sec.ms).replace("+", "")}</b></span>` : "",
    rec && brk ? `<span>Out of service: <b>${toMin(rec.clock) - toMin(brk.clock)} sim min</b></span>` : "",
    `<span>Messages: <b>${s.messages}</b></span>`,
  ].join("");
  box.innerHTML = `<div class="head"><b>#${s.id} ${esc(s.kind)}</b> at ${s.clock} · ${esc(s.text)}</div><div class="sum">${sum}</div>
    <div class="steps">${s.steps.map((x, i) => { const a = meta[x.who] || {}; return `<div class="step k-${x.key}" title="${esc(x.text)}"><div class="n">${i + 1}. ${esc(x.label)}</div><div class="t">${fmtDur(x.ms)} · ${x.clock}</div><div class="w">${esc(a.title || x.who)}</div></div>`; }).join("")}</div>`;
}
setInterval(() => { if (!JUDGE && BOTTOM === "story") loadStories(); }, 1000);
function rr(ctx, x, y, w, h, r) { ctx.beginPath(); ctx.roundRect(x, y, w, h, r); }

function drawMap(t) {
  const ctx = mctx; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); const r = map.getBoundingClientRect(); ctx.clearRect(0, 0, r.width, r.height);
  if (!W || !S) return;
  ctx.save(); ctx.translate(ox, oy);
  ctx.fillStyle = T.ground; rr(ctx, 0, 0, W.width * cs, W.height * cs, 10); ctx.fill();
  ctx.strokeStyle = T.grid; ctx.lineWidth = 1;
  for (let x = 0; x <= W.width; x++) { ctx.beginPath(); ctx.moveTo(x * cs, 0); ctx.lineTo(x * cs, W.height * cs); ctx.stroke(); }
  for (let y = 0; y <= W.height; y++) { ctx.beginPath(); ctx.moveTo(0, y * cs); ctx.lineTo(W.width * cs, y * cs); ctx.stroke(); }
  for (const [x, y] of W.water) { const g = ctx.createLinearGradient(x * cs, 0, (x + 1) * cs, 0); g.addColorStop(0, T.water1); g.addColorStop(1, T.water2); ctx.fillStyle = g; ctx.fillRect(x * cs, y * cs, cs + 0.5, cs + 0.5); }
  ctx.strokeStyle = "rgba(255,255,255,0.6)"; for (const [x, y] of W.water) { const ph = (t / 1100 + y * 0.37) % 1; ctx.beginPath(); ctx.moveTo(x * cs + cs * 0.25, (y + ph) * cs); ctx.lineTo(x * cs + cs * 0.75, (y + ph) * cs); ctx.stroke(); }
  const blocked = new Set(S.blocked.map((c) => c.join(",")));
  for (const b of W.bridges) {
    const [x, y] = b.cell, closed = blocked.has(`${x},${y}`);
    ctx.fillStyle = closed ? "#e9b1a8" : T.wood; ctx.fillRect(x * cs - 3, y * cs + 2, cs + 6, cs - 4);
    ctx.strokeStyle = closed ? T.red : "#7a5a32"; ctx.lineWidth = 1; for (let i = 1; i < 4; i++) { ctx.beginPath(); ctx.moveTo(x * cs + (i * cs) / 4, y * cs + 3); ctx.lineTo(x * cs + (i * cs) / 4, y * cs + cs - 3); ctx.stroke(); }
    ctx.fillStyle = closed ? T.red : T.muted; ctx.font = `700 ${Math.max(9, cs * 0.32)}px Inter, sans-serif`; ctx.textAlign = "center"; ctx.fillText(closed ? "✖ CLOSED" : b.name, x * cs + cs / 2, y * cs - 4);
    if (closed) { ctx.strokeStyle = T.red; ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(x * cs + 3, y * cs + 3); ctx.lineTo(x * cs + cs - 3, y * cs + cs - 3); ctx.moveTo(x * cs + cs - 3, y * cs + 3); ctx.lineTo(x * cs + 3, y * cs + cs - 3); ctx.stroke(); }
  }
  const closedF = new Set(S.closed_fields);
  for (const f of W.fields) {
    const js = S.jobs.filter((j) => j.field === f.id);
    const j = js.find((x) => x.status === "active") || js.find((x) => x.status !== "done") || js[js.length - 1];
    const col = j ? TYPE_COLOR[j.type] : T.muted;
    const X = f.x * cs + 2, Y = f.y * cs + 2, Wd = f.w * cs - 4, Ht = f.h * cs - 4;
    ctx.save(); rr(ctx, X, Y, Wd, Ht, 7); ctx.clip();
    ctx.fillStyle = T.fieldBase; ctx.fillRect(X, Y, Wd, Ht); ctx.fillStyle = hexA(col, 0.12); ctx.fillRect(X, Y, Wd, Ht);
    if (j && j.pct > 0) { ctx.fillStyle = j.status === "done" ? "rgba(46,125,79,0.28)" : hexA(col, 0.38); ctx.fillRect(X, Y, (Wd * j.pct) / 100, Ht); }
    ctx.strokeStyle = hexA(col, 0.18); ctx.lineWidth = 1; for (let yy = Y + 3; yy < Y + Ht; yy += Math.max(4, cs / 4)) { ctx.beginPath(); ctx.moveTo(X, yy); ctx.lineTo(X + Wd, yy); ctx.stroke(); }
    if (closedF.has(f.id)) { ctx.fillStyle = "rgba(37,99,168,0.25)"; ctx.fillRect(X, Y, Wd, Ht); }
    ctx.restore();
    const late = j && j.status !== "done" && j.late;
    ctx.lineWidth = j && j.status === "active" ? 2.5 : 1.5; ctx.setLineDash(j && j.status === "unscheduled" ? [5, 4] : []);
    ctx.strokeStyle = closedF.has(f.id) ? T.blue : late ? T.red : j && j.status === "done" ? T.green : hexA(col, 0.85); rr(ctx, X, Y, Wd, Ht, 7); ctx.stroke(); ctx.setLineDash([]);
    const fs = Math.max(9, Math.min(13, cs * 0.36)); ctx.textAlign = "left"; ctx.fillStyle = T.ink; ctx.font = `700 ${fs}px Inter, sans-serif`; ctx.fillText(`${f.id} ${f.name}`, X + 5, Y + fs + 2, Wd - 8);
    if (j) {
      ctx.font = `600 ${fs * 0.9}px Inter, sans-serif`; ctx.fillStyle = j.priority === "high" ? T.red : T.muted;
      ctx.fillText(j.status === "done" ? `✔ ${j.id} done ${hm(j.eta)}` : `${TYPE_ICON[j.type]} ${j.id} ${j.priority === "high" ? "HIGH " : ""}due ${hm(j.deadline)}`, X + 5, Y + fs * 2.25 + 2, Wd - 8);
      if (j.status !== "done" && Ht > fs * 3.4) { ctx.fillStyle = j.status === "unscheduled" ? T.red : late ? T.red : T.green; ctx.fillText(closedF.has(f.id) ? "🌊 field closed" : j.status === "unscheduled" ? "not scheduled today" : `${j.tractor || ""} ETA ${hm(j.eta)}${j.status === "active" ? " · " + j.pct + "%" : ""}`, X + 5, Y + fs * 3.45 + 2, Wd - 8); }
    }
  }
  const [dx, dy] = W.depot;
  ctx.fillStyle = T.depotFill; ctx.strokeStyle = T.amber; ctx.lineWidth = 2; rr(ctx, dx * cs - cs * 0.3, dy * cs - cs * 0.3, cs * 1.6, cs * 1.6, 7); ctx.fill(); ctx.stroke();
  ctx.fillStyle = T.amber; ctx.textAlign = "center"; ctx.font = `800 ${Math.max(9, cs * 0.33)}px Inter, sans-serif`; ctx.fillText("⛽ DEPOT", dx * cs + cs / 2, dy * cs + cs * 1.65 + 8);
  ctx.font = `600 ${Math.max(8, cs * 0.27)}px Inter, sans-serif`; ctx.fillStyle = T.amber; ctx.fillText(Object.entries(S.pool).filter(([, n]) => n > 0).map(([k, n]) => `${k}×${n}`).join(" ") || "no spares", dx * cs + cs / 2, dy * cs + cs * 1.65 + 8 + cs * 0.38);
  if ($("#showPaths").checked) for (const tr of S.tractors) {
    if (!tr.path || tr.path.length < 2) continue;
    ctx.strokeStyle = hexA(trColor[tr.id], 0.8); ctx.lineWidth = 2.5; ctx.setLineDash([6, 5]); ctx.lineDashOffset = -t / 40;
    ctx.beginPath(); const d = disp[tr.id]; ctx.moveTo((d.x + 0.5) * cs, (d.y + 0.5) * cs); for (const [x, y] of tr.path.slice(1)) ctx.lineTo((x + 0.5) * cs, (y + 0.5) * cs); ctx.stroke(); ctx.setLineDash([]);
  }
  if ($("#showGhost").checked) for (const g of S.ghost) drawTractor(ctx, g, true, t);
  for (const tr of S.tractors) drawTractor(ctx, tr, false, t);
  ctx.restore();
}
function drawTractor(ctx, tr, ghost, t) {
  const d = disp[ghost ? "g" + tr.id : tr.id]; d.x += (tr.x - d.x) * 0.25; d.y += (tr.y - d.y) * 0.25;
  let cx = (d.x + 0.5) * cs, cy = (d.y + 0.5) * cs; const idx = Object.keys(trColor).indexOf(tr.id);
  if (Math.abs(tr.x - W.depot[0]) < 0.6 && Math.abs(tr.y - W.depot[1]) < 0.6) { cx += (idx - 1) * cs * 0.45; cy += (ghost ? 0.35 : -0.1) * cs; }
  const R = cs * (ghost ? 0.3 : 0.38), col = trColor[tr.id];
  if (ghost) { ctx.globalAlpha = 0.6; ctx.setLineDash([3, 3]); ctx.strokeStyle = tr.status === "broken" ? T.red : T.ghost; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = T.ghost; ctx.font = `700 ${Math.max(8, cs * 0.25)}px Inter, sans-serif`; ctx.textAlign = "center"; ctx.fillText(`${tr.id}·baseline`, cx, cy + R + 10); ctx.globalAlpha = 1; return; }
  if (tr.status === "work") { ctx.strokeStyle = hexA(col, 0.45); ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(cx, cy, R + 6 + 3 * Math.sin(t / 200), 0, Math.PI * 2); ctx.stroke(); }
  if (tr.status === "broken") for (let i = 0; i < 4; i++) { const ph = (t / 1400 + i / 4) % 1; ctx.fillStyle = `rgba(90,90,90,${0.35 * (1 - ph)})`; ctx.beginPath(); ctx.arc(cx + Math.sin(i * 2 + t / 500) * 4, cy - R - ph * cs * 0.9, 3 + ph * 7, 0, Math.PI * 2); ctx.fill(); }
  ctx.fillStyle = tr.status === "broken" ? T.red : col; ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
  const fc = tr.fuel > 50 ? T.green : tr.fuel > 25 ? T.amber : T.red;
  ctx.strokeStyle = "rgba(0,0,0,0.12)"; ctx.lineWidth = 3; ctx.beginPath(); ctx.arc(cx, cy, R + 3.5, 0, Math.PI * 2); ctx.stroke();
  ctx.strokeStyle = fc; ctx.beginPath(); ctx.arc(cx, cy, R + 3.5, -Math.PI / 2, -Math.PI / 2 + (Math.PI * 2 * tr.fuel) / 100); ctx.stroke();
  ctx.fillStyle = "#fff"; ctx.font = `900 ${Math.max(10, cs * 0.34)}px Inter, sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(tr.status === "broken" ? "✖" : tr.id, cx, cy + 1); ctx.textBaseline = "alphabetic";
  const badge = { wait: "⏳", refuel: "⛽", swap: "🔧", broken: "💥" }[tr.status]; if (badge) { ctx.font = `${Math.max(11, cs * 0.4)}px sans-serif`; ctx.fillText(badge, cx + R, cy - R); }
  ctx.font = `700 ${Math.max(8, cs * 0.26)}px Inter, sans-serif`; ctx.fillStyle = T.ink; ctx.fillText(`${tr.name} · ${tr.implement || "-"}`, cx, cy + R + 13);
  const ag = S.agopenweb;
  if (ag && ag.tractor === tr.id) { const live = ag.fresh; ctx.fillStyle = live ? "#0f766e" : T.muted; rr(ctx, cx - 26, cy - R - 22, 52, 14, 7); ctx.fill(); ctx.fillStyle = "#fff"; ctx.font = "800 9px Inter, sans-serif"; ctx.fillText(live ? "🛰 LIVE GPS" : "🛰 offline", cx, cy - R - 12); }
}
function drawGantt() {
  if (JUDGE || !W || !S) return;
  const ctx = gctx; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); const r = gantt.getBoundingClientRect(), Wd = r.width, Ht = r.height; ctx.clearRect(0, 0, Wd, Ht);
  const L = 84, top = 16, rows = [...S.tractors.map((x) => ({ tr: x, ghost: false })), ...S.ghost.map((x) => ({ tr: x, ghost: true }))];
  const sep = 8, rh = Math.min(20, (Ht - top - sep - 4) / rows.length);
  const X = (m) => L + ((m - W.day_start) / (W.day_end - W.day_start)) * (Wd - L - 6), rowY = (i) => top + i * rh + (i >= S.tractors.length ? sep : 0);
  S.wind.forEach((w, i) => { if (w < 3 || w > 10) { ctx.fillStyle = "rgba(192,57,43,0.07)"; ctx.fillRect(X(W.day_start + i * 60), top - 3, X(W.day_start + 60) - X(W.day_start), rowY(rows.length - 1) + rh - top + 3); } });
  ctx.fillStyle = T.muted; ctx.font = "600 10px Inter, sans-serif"; ctx.textAlign = "center";
  for (let m = W.day_start; m <= W.day_end; m += 60) { ctx.fillText(hm(m).slice(0, 2), X(m), 10); ctx.strokeStyle = T.gantGrid; ctx.beginPath(); ctx.moveTo(X(m), top - 3); ctx.lineTo(X(m), Ht); ctx.stroke(); }
  const jt = Object.fromEntries(S.jobs.map((j) => [j.id, j.type]));
  rows.forEach(({ tr, ghost }, i) => {
    const y = rowY(i), h = rh - 5;
    ctx.fillStyle = ghost ? T.muted : trColor[tr.id]; ctx.font = `700 ${ghost ? 9.5 : 11}px Inter`; ctx.textAlign = "left"; ctx.fillText(ghost ? `${tr.id} baseline` : `${tr.id} ${tr.name}`, 2, y + h - 2);
    for (const [s, e, kind, job] of tr.history || []) { let c = { travel: "#b9b29f", wait: "#e0b45a", refuel: "#9a9a9a", swap: "#8b74d6", broken: "#d36b5d" }[kind]; if (kind === "work") c = TYPE_COLOR[jt[job]] || T.green; if (!c) continue; ctx.fillStyle = ghost ? hexA(c, 0.45) : c; ctx.fillRect(X(s), y, Math.max(1, X(e) - X(s)), h); }
    if (!ghost) {
      for (const tk of [...(tr.cur ? [tr.cur] : []), ...(tr.queue || [])]) {
        const s0 = Math.max(S.now, tk.depart), a = Math.max(S.now, tk.arrive), st = Math.max(S.now, tk.start);
        ctx.fillStyle = "rgba(120,110,90,0.25)"; ctx.fillRect(X(s0), y + h * 0.35, Math.max(0, X(a) - X(s0)), h * 0.3);
        if (st > a) { ctx.fillStyle = "rgba(224,180,90,0.35)"; ctx.fillRect(X(a), y, X(st) - X(a), h); }
        const c = tk.kind === "job" ? TYPE_COLOR[jt[tk.job_id]] : tk.kind === "swap" ? "#8b74d6" : "#9a9a9a";
        ctx.fillStyle = hexA(c, 0.22); ctx.strokeStyle = hexA(c, 0.9); ctx.setLineDash([3, 2]); ctx.fillRect(X(st), y, Math.max(1, X(tk.end) - X(st)), h); ctx.strokeRect(X(st) + 0.5, y + 0.5, Math.max(1, X(tk.end) - X(st)) - 1, h - 1); ctx.setLineDash([]);
        if (tk.kind === "job" && X(tk.end) - X(st) > 22) { ctx.fillStyle = T.ink; ctx.font = "700 9.5px Inter"; ctx.fillText(tk.job_id, X(st) + 3, y + h - 3); }
      }
      for (const j of S.jobs) { if (j.tractor !== tr.id || j.status === "done") continue; const x = X(j.deadline); ctx.fillStyle = j.late ? T.red : T.ink; ctx.beginPath(); ctx.moveTo(x, y - 2); ctx.lineTo(x + 4, y + 3); ctx.lineTo(x, y + 8); ctx.lineTo(x - 4, y + 3); ctx.fill(); }
    }
    if (i === S.tractors.length - 1) { ctx.strokeStyle = "rgba(0,0,0,0.12)"; ctx.beginPath(); ctx.moveTo(2, y + rh + sep / 2); ctx.lineTo(Wd, y + rh + sep / 2); ctx.stroke(); }
  });
  ctx.strokeStyle = T.ink; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.moveTo(X(S.now), top - 6); ctx.lineTo(X(S.now), Ht); ctx.stroke();
}
function renderAgow() {
  const a = S.agopenweb, box = $("#agow");
  $("#agowview").classList.toggle("down", !a || !a.connected);
  if (!a || JUDGE) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const st = $("#agowstat");
  st.textContent = a.fresh ? (a.has_control ? "LIVE · control" : "LIVE · observer") : (a.connected ? "connected" : "connecting…");
  st.className = "pill " + (a.fresh ? "ok" : "bad");
  const p = a.pose;
  $("#agowinfo").innerHTML = (p ? `${a.tractor} · <b>${p.speed}</b> kph · fix ${a.fix} · ${a.dist_m} m<span class="more"><br>E <b>${p.e}</b> N <b>${p.n}</b> · hdg ${p.heading_deg}° · ${a.ticks} ticks<br><code>${esc(a.url)}</code></span>` : `waiting for Tick frames… <span class="more"><code>${esc(a.url)}</code></span>`)
    + (a.gps_fault_active ? `<div class="note">⚠ GPS fault active, agents re-planned without ${a.tractor}</div>` : "") + (a.note ? `<div class="note">${esc(a.note)}</div>` : "") + (a.error && !a.connected ? `<div class="note">${esc(a.error)}</div>` : "");
  const cv = $("#agowcv"), r = cv.getBoundingClientRect(); if (r.width < 10) return;
  cv.width = r.width * dpr; cv.height = r.height * dpr; const c = cv.getContext("2d"); c.setTransform(dpr, 0, 0, dpr, 0, 0);
  const tr = a.trail || []; if (!tr.length) return;
  let x0 = Math.min(...tr.map((q) => q[0])), x1 = Math.max(...tr.map((q) => q[0])), y0 = Math.min(...tr.map((q) => q[1])), y1 = Math.max(...tr.map((q) => q[1]));
  const span = Math.max(20, x1 - x0, y1 - y0), cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, k = (Math.min(r.width, r.height) - 16) / span;
  const X = (e) => r.width / 2 + (e - cx) * k, Y = (n) => r.height / 2 - (n - cy) * k;
  c.strokeStyle = "#0f766e"; c.lineWidth = 2; c.beginPath(); tr.forEach((q, i) => i ? c.lineTo(X(q[0]), Y(q[1])) : c.moveTo(X(q[0]), Y(q[1]))); c.stroke();
  if (p) { c.fillStyle = a.gps_fault_active ? T.red : "#0f766e"; c.beginPath(); c.arc(X(p.e), Y(p.n), 5, 0, Math.PI * 2); c.fill(); }
  c.fillStyle = T.muted; c.font = "600 9px Inter"; c.fillText(`${Math.round(span)} m`, 6, r.height - 6);
}
function frame(t) { if (VIEW === "farm") drawMap(t); if (BOTTOM === "gantt") drawGantt(); if (S) { drawNet(t); if (Math.floor(t / 250) !== frame.k) { frame.k = Math.floor(t / 250); renderAgow(); } } requestAnimationFrame(frame); }
requestAnimationFrame(frame);
