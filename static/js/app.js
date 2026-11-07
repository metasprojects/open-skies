/* Open Skies page: choose an area, pick airbases, build, install. Talks only to this app's local server. */
"use strict";

const TILE_M = 5120, MAX_TILES = 51, MIN_TILES = 11;
const S = { lat: null, lon: null, tiles: 51, airports: [], sel: {}, showSmall: false, setup: null,
            running: false, lastDone: null };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { data = {}; }
  if (!r.ok) { const err = new Error((data.error && data.error.title) || `Request failed (${r.status})`); err.data = data; throw err; }
  return data;
}

/* ---- the map and the box ------------------------------------------------------------------------- */
const map = L.map("map", { worldCopyJump: true, zoomControl: true }).setView([30, 10], 3);
L.tileLayer("/tiles/{z}/{x}/{y}.png", { maxZoom: 18, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a> contributors' }).addTo(map);
const box = L.polygon([], { color: "#111", weight: 3, fillColor: "#111", fillOpacity: 0.08, bubblingMouseEvents: false }).addTo(map);
const fade = L.polygon([], { color: "#111", weight: 1.5, dashArray: "5 6", fill: false, interactive: false }).addTo(map);
const corner = L.marker([0, 0], { draggable: true, icon: L.divIcon({ className: "corner-handle", iconSize: [16, 16] }), opacity: 0 }).addTo(map);
const label = L.tooltip({ permanent: true, direction: "top", className: "box-label", offset: [0, -4] });
const apLayer = L.layerGroup().addTo(map);

function approxCorners(lat, lon, tiles, inset = 0) {
  const half = tiles * TILE_M / 2 - inset;
  const dlat = half / 111320, dlon = half / (111320 * Math.cos(lat * Math.PI / 180));
  return [[lat - dlat, lon - dlon], [lat + dlat, lon - dlon], [lat + dlat, lon + dlon], [lat - dlat, lon + dlon]];
}
function sizeBad() { return S.tiles > MAX_TILES || S.tiles < MIN_TILES || Math.abs(S.lat) > 80; }
function drawBox(corners) {
  if (S.lat === null) return;
  const c = corners || approxCorners(S.lat, S.lon, S.tiles);
  const bad = sizeBad() || (S.problems && S.problems.length);
  const col = bad ? "#FF4538" : "#111";
  box.setLatLngs(c).setStyle({ color: col, fillColor: col, fillOpacity: bad ? 0.2 : 0.08, weight: bad ? 4 : 3 });
  fade.setLatLngs(approxCorners(S.lat, S.lon, S.tiles, 9000)).setStyle({ color: col });
  corner.setLatLng(c[2]).setOpacity(1);
  const km = (S.tiles * 5.12).toFixed(0);
  label.setContent(bad && S.tiles > MAX_TILES ? `${km} km: too big (max 261 km)` : `${km} x ${km} km`);
  box.bindTooltip(label).openTooltip(L.latLng(c[1][0], (c[1][1] + c[2][1]) / 2));
}

function place(lat, lon) {
  S.lat = lat; S.lon = ((lon + 540) % 360) - 180;
  drawBox();
  refreshArea();
}

map.on("click", (e) => place(e.latlng.lat, e.latlng.lng));
let drag = null;
box.on("mousedown", (e) => { drag = { start: e.latlng, lat: S.lat, lon: S.lon }; map.dragging.disable(); L.DomEvent.stop(e); });
map.on("mousemove", (e) => {
  if (!drag) return;
  S.lat = drag.lat + (e.latlng.lat - drag.start.lat);
  S.lon = drag.lon + (e.latlng.lng - drag.start.lng);
  drawBox();
});
map.on("mouseup", () => { if (drag) { drag = null; map.dragging.enable(); place(S.lat, S.lon); } });
box.on("click", (e) => L.DomEvent.stop(e));
corner.on("drag", (e) => {
  const p = e.target.getLatLng();
  const dy = Math.abs(p.lat - S.lat) * 111320, dx = Math.abs(p.lng - S.lon) * 111320 * Math.cos(S.lat * Math.PI / 180);
  S.tiles = Math.max(5, Math.min(90, Math.round(2 * Math.max(dx, dy) / TILE_M)));
  syncSizeSelect();
  drawBox();
});
corner.on("dragend", () => refreshArea());

function syncSizeSelect() {
  const sel = $("tilesSelect");
  let opt = [...sel.options].find((o) => +o.value === S.tiles);
  if (!opt) {
    [...sel.options].filter((o) => o.dataset.custom).forEach((o) => o.remove());
    opt = new Option(`${(S.tiles * 5.12).toFixed(0)} km square (${S.tiles} tiles)${S.tiles > MAX_TILES ? " - too big" : ""}`, S.tiles);
    opt.dataset.custom = "1";
    sel.add(opt);
  }
  sel.value = String(S.tiles);
}
$("tilesSelect").addEventListener("change", (e) => { S.tiles = +e.target.value; drawBox(); refreshArea(); });

/* ---- area check and airports ---------------------------------------------------------------------- */
let areaSeq = 0, areaTimer = null;
function refreshArea() {
  clearTimeout(areaTimer);
  areaTimer = setTimeout(doRefreshArea, 250);
  saveDraft();
}
async function doRefreshArea() {
  if (S.lat === null) return;
  const seq = ++areaSeq;
  const km = (S.tiles * 5.12).toFixed(0);
  $("areaInfo").innerHTML = `Centre <b>${S.lat.toFixed(3)}, ${S.lon.toFixed(3)}</b> &middot; <b>${km} x ${km} km</b>. Looking for airports...`;
  if (sizeBad() && S.tiles > MAX_TILES) {
    S.problems = [`Too big: ${km} km. Open Skies builds up to 261 km (51 tiles); the loader's hard limit is 400 km. Drag the corner in or pick a size.`];
    $("areaInfo").innerHTML = `Centre <b>${S.lat.toFixed(3)}, ${S.lon.toFixed(3)}</b> &middot; <b>${km} x ${km} km</b>.`;
    showProblems("areaProblems", S.problems); drawBox(); S.airports = []; renderAirports(); return;
  }
  try {
    const r = await api("/api/area", { lat: S.lat, lon: S.lon, tiles: S.tiles });
    if (seq !== areaSeq) return;
    S.problems = r.problems;
    showProblems("areaProblems", r.problems);
    if (r.corners.length === 4) drawBox([r.corners[0], r.corners[1], r.corners[3], r.corners[2]]);
    for (const a of r.airports) {
      const had = S.sel[a.ident];
      if (had) { a.selected = had.selected && a.ok; a.side = had.side; }
      S.sel[a.ident] = { selected: a.selected, side: a.side };
    }
    S.airports = r.airports;
    const n = r.airports.filter((a) => a.ok).length;
    $("areaInfo").innerHTML = `Centre <b>${S.lat.toFixed(3)}, ${S.lon.toFixed(3)}</b> &middot; <b>${km} x ${km} km</b> &middot; ${n} usable airport${n === 1 ? "" : "s"}. The dashed line is where the land starts sloping into the sea at the edge.`;
    renderAirports();
    saveDraft();
  } catch (e) {
    $("areaInfo").textContent = e.message;
  }
}

function shown(a) { return a.selected || S.showSmall || a.type !== "small_airport"; }
function renderAirports() {
  const ul = $("airportList");
  ul.innerHTML = "";
  apLayer.clearLayers();
  const list = S.airports.filter(shown);
  for (const a of list) {
    const li = document.createElement("li");
    if (!a.ok) li.className = "off";
    li.innerHTML = `<input type="checkbox" ${a.selected ? "checked" : ""} ${a.ok ? "" : "disabled"}>
      <span class="ap-name">${esc(a.name)}</span>
      <span class="side"><button type="button" class="boscali ${a.side === "Boscali" ? "on" : ""}">Boscali</button><button type="button" class="primeva ${a.side === "Primeva" ? "on" : ""}">Primeva</button></span>
      <span class="ap-meta">${esc(a.ident)} &middot; ${a.runwayFt.toLocaleString()} ft runway${a.runways > 1 ? ` (+${a.runways - 1})` : ""} &middot; ${esc(a.place)}</span>
      ${a.note ? `<span class="ap-note">${esc(a.note)}</span>` : ""}`;
    li.querySelector("input").addEventListener("change", (e) => setSel(a, { selected: e.target.checked }));
    li.querySelector(".boscali").addEventListener("click", () => setSel(a, { side: "Boscali" }));
    li.querySelector(".primeva").addEventListener("click", () => setSel(a, { side: "Primeva" }));
    ul.appendChild(li);
    const col = !a.ok ? "#555" : a.selected ? (a.side === "Boscali" ? "#5B9BE6" : "#FF4538") : "#8E939C";
    L.circleMarker([a.lat, a.lon], { radius: a.selected ? 7 : 4, color: "#000", weight: 1, fillColor: col, fillOpacity: 1 })
      .bindTooltip(`${a.name} (${a.ident})`).on("click", (ev) => { L.DomEvent.stop(ev); if (a.ok) setSel(a, { selected: !a.selected }); })
      .addTo(apLayer);
  }
  const sel = S.airports.filter((a) => a.selected);
  const b = sel.filter((a) => a.side === "Boscali").length;
  $("airportSummary").textContent = S.airports.length
    ? `${sel.length} ticked: ${b} Boscali, ${sel.length - b} Primeva. Boscali and Primeva are the game's two sides.`
    : (S.lat === null ? "Real airports inside the box appear here." : "No usable airports in this box. Move it over land with an airport.");
  if (!list.length && S.airports.length) ul.innerHTML = `<li class="muted">Only small airfields here: tick "show small airfields".</li>`;
}
function setSel(a, change) {
  Object.assign(a, change);
  S.sel[a.ident] = { selected: a.selected, side: a.side };
  renderAirports();
  saveDraft();
}
$("showSmall").addEventListener("change", (e) => { S.showSmall = e.target.checked; renderAirports(); });

/* ---- search ------------------------------------------------------------------------------------- */
$("searchInput").addEventListener("keydown", async (e) => {
  if (e.key !== "Enter") return;
  const q = e.target.value.trim();
  const ul = $("searchResults");
  if (!q) { ul.hidden = true; return; }
  ul.hidden = false;
  ul.innerHTML = `<li class="muted">Searching...</li>`;
  try {
    const r = await api(`/api/search?q=${encodeURIComponent(q)}`);
    ul.innerHTML = r.results.length ? "" : `<li class="muted">Nothing found.</li>`;
    for (const x of r.results) {
      const li = document.createElement("li");
      li.textContent = x.name;
      li.addEventListener("click", () => { ul.hidden = true; map.setView([x.lat, x.lon], 7); place(x.lat, x.lon); });
      ul.appendChild(li);
    }
  } catch (err) { ul.innerHTML = `<li>${esc((err.data && err.data.error) || err.message)}</li>`; }
});

/* ---- name --------------------------------------------------------------------------------------- */
function mapId(name) {
  const s = name.normalize("NFKD").replace(/[^\x20-\x7e]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32).replace(/-+$/, "");
  return s.length >= 2 ? s : "open-skies-map";
}
function nameChanged() {
  const n = $("mapName").value.trim();
  $("idNote").textContent = n ? `Map id: ${mapId(n)} (the file is ${mapId(n)}.nomap; building again under the same name replaces it)` : "";
  saveDraft();
}
$("mapName").addEventListener("input", nameChanged);
$("author").addEventListener("input", saveDraft);
$("shipLanes").addEventListener("change", saveDraft);

/* ---- remember the choices between runs ---------------------------------------------------------- */
let draftTimer = null, restoring = false;
function saveDraft() {
  if (restoring) return;
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => api("/api/draft", { lat: S.lat, lon: S.lon, tiles: S.tiles, sel: S.sel, name: $("mapName").value,
    author: $("author").value, shipLanes: $("shipLanes").checked, zoom: map.getZoom() }).catch(() => {}), 600);
}
async function restoreDraft() {
  try {
    const d = await api("/api/draft");
    if (d.lat === undefined || d.lat === null) return;
    restoring = true;
    S.tiles = d.tiles || 51; S.sel = d.sel || {};
    $("mapName").value = d.name || ""; $("author").value = d.author || ""; $("shipLanes").checked = d.shipLanes !== false;
    syncSizeSelect(); nameChanged();
    map.setView([d.lat, d.lon], d.zoom || 7);
    restoring = false;
    place(d.lat, d.lon);
  } finally { restoring = false; }
}

/* ---- build -------------------------------------------------------------------------------------- */
function showProblems(id, list) {
  const ul = $(id);
  ul.hidden = !list || !list.length;
  ul.innerHTML = (list || []).map((p) => `<li>${esc(p)}</li>`).join("");
}
$("buildBtn").addEventListener("click", async () => {
  showProblems("buildProblems", []);
  if (S.lat === null) { showProblems("buildProblems", ["Choose an area first: search for a place or click the map."]); return; }
  const body = { lat: S.lat, lon: S.lon, tiles: S.tiles, name: $("mapName").value.trim(), author: $("author").value.trim(),
    shipLanes: $("shipLanes").checked, airports: S.airports.filter((a) => a.selected).map((a) => ({ ident: a.ident, side: a.side })) };
  try {
    await api("/api/build", body);
    $("errorBox").hidden = true;
    poll(true);
  } catch (e) {
    showProblems("buildProblems", (e.data && e.data.problems) || [e.message]);
  }
});
$("stopBtn").addEventListener("click", () => api("/api/stop", {}));
$("detailsBtn").addEventListener("click", async () => {
  const pre = $("errorDetails");
  if (!pre.hidden) { pre.hidden = true; return; }
  const r = await api("/api/log");
  pre.textContent = r.lines.join("\n") + (r.path ? `\n\n(full log: ${r.path})` : "");
  pre.hidden = false;
  pre.scrollTop = pre.scrollHeight;
});
$("logsBtn").addEventListener("click", () => api("/api/open", { which: "logs" }));

const STAGES = ["Checking the area", "Elevation", "Land cover", "Roads and buildings", "Ferries", "Ship traffic", "Sea floor",
  "Height map", "Airbases", "Ground textures", "Buildings", "Roads", "Ship lanes", "Map details", "Unity: map file", "Free Flight mission"];
const KEYS = ["prepare", "elevation", "landcover", "overture", "ferries", "shipping", "seafloor", "heights", "airbases", "textures",
  "buildings", "roads", "sealanes", "manifest", "unity", "mission"];
function renderBuild(b) {
  if (!b) return;
  $("progress").hidden = false;
  S.running = b.running;
  $("buildBtn").disabled = b.running;
  $("stopBtn").hidden = !b.running;
  const same = b.mapId === mapId($("mapName").value.trim());
  $("buildBtn").textContent = b.error && !b.running && same ? "Build again (carries on)" : "Build map";
  $("stageLabel").textContent = b.done ? `Finished: ${b.name}` : `${b.name}: ${b.label}`;
  $("stageCount").textContent = b.done ? "" : `step ${b.stageIndex + 1} of ${b.stageCount}`;
  $("bar").style.width = `${b.percent}%`;
  $("stageDetail").textContent = b.detail || "";
  $("stageHint").textContent = b.running ? (b.hint || "") : "";
  const m = Math.floor(b.elapsed / 60), s = b.elapsed % 60;
  $("elapsed").textContent = `${b.running ? "Running" : "Ran"} for ${m}:${String(s).padStart(2, "0")}`;
  $("stageList").innerHTML = KEYS.map((k, i) => {
    const cls = b.skipped.includes(k) ? "skip" : (b.done || i < b.stageIndex) ? "done" : i === b.stageIndex && b.running ? "now" : "";
    return `<li class="${cls}">${STAGES[i]}</li>`;
  }).join("");
  $("notes").innerHTML = b.notes.map((n) => `<li>${esc(n)}</li>`).join("");
  const eb = $("errorBox");
  if (b.error && !b.running) {
    eb.hidden = false;
    $("errorTitle").textContent = b.error.title;
    $("errorFix").textContent = b.error.fix || "";
  } else eb.hidden = true;
  if (b.done && S.lastDone !== b.started) { S.lastDone = b.started; loadMaps(); }
}

let pollTimer = null;
async function poll(soon) {
  clearTimeout(pollTimer);
  let busy = false;
  try {
    const r = await api("/api/status");
    renderBuild(r.build);
    renderCopy(r.copy);
    busy = (r.build && r.build.running) || r.copy.running || r.unityInstall.running;
    if (r.unityInstall.running || S.unityWasRunning) { S.unityWasRunning = r.unityInstall.running; loadSetup(); }
  } catch (e) { /* the server is restarting or gone */ }
  pollTimer = setTimeout(poll, soon || busy ? 1500 : 8000);
}

/* ---- finished maps and install ------------------------------------------------------------------- */
async function loadMaps() {
  const r = await api("/api/maps");
  const el = $("mapsList");
  if (!r.maps.length) { el.innerHTML = `<span class="muted">Finished maps appear here, ready to install.</span>`; return; }
  el.innerHTML = "";
  const canInstall = S.setup ? S.setup.canInstall : true;
  for (const m of r.maps) {
    const d = document.createElement("div");
    d.className = "map-item";
    d.innerHTML = `<b>${esc(m.name)}</b> <span class="muted">&middot; ${m.sizeMB.toLocaleString()} MB &middot; ${m.airbases} airbases &middot; built ${esc(m.built)}${m.installed ? " &middot; installed" : ""}</span>
      <div class="thumbs">${m.previews.slice(0, 4).map((p) => `<img src="/preview/${m.mapId}/${p}?t=${encodeURIComponent(m.built)}" alt="${p}" title="${p}">`).join("")}</div>
      <div><code>${esc(m.prefab)}</code></div>
      <div class="muted small">Mission: ${m.missions.map(esc).join(", ") || "none"}</div>
      <div class="row"><button class="btn primary small" data-a="install" ${canInstall ? "" : "disabled title='Finish Setup first (BepInEx and NOCustomMaps)'"}>Install into the game</button>
      <button class="btn small ghost" data-a="osm">Export OSM data</button><button class="btn small ghost" data-a="folder">Open folder</button></div>`;
    d.querySelectorAll("img").forEach((img) => img.addEventListener("click", () => window.open(img.src, "_blank")));
    d.querySelector('[data-a="install"]').addEventListener("click", async () => {
      try { renderCopy(await api("/api/install", { mapId: m.mapId })); poll(true); }
      catch (e) { renderCopy({ error: (e.data && e.data.error) || { title: e.message } }); }
    });
    d.querySelector('[data-a="osm"]').addEventListener("click", async (ev) => {
      ev.target.disabled = true;
      try { const z = await api("/api/export-osm", { mapId: m.mapId }); renderCopy({ done: { text: `OSM data saved: ${z.file} (${z.sizeMB} MB)` } }); }
      catch (e) { renderCopy({ error: (e.data && e.data.error) || { title: e.message } }); }
      ev.target.disabled = false;
    });
    d.querySelector('[data-a="folder"]').addEventListener("click", () => api("/api/open", { which: "map", mapId: m.mapId }));
    el.appendChild(d);
  }
}
function renderCopy(c) {
  const box = $("copyBox");
  if (!c || (!c.running && !c.done && !c.error)) { return; }
  box.hidden = false;
  if (c.running) box.textContent = `Copying ${c.mapId} into the game: ${c.percent}%`;
  else if (c.error) box.innerHTML = `<b>${esc(c.error.title)}</b><br>${esc(c.error.fix || "")}`;
  else if (c.done && c.done.text) box.textContent = c.done.text;
  else if (c.done) {
    box.innerHTML = `<b>Installed.</b> Start Nuclear Option: the map is in the Mission Editor's New list, and the mission
      <b>${esc(c.done.missions.join(", "))}</b> is with your own missions. <span class="muted small">(${esc(c.done.map)})</span>`;
    if (!S.reloadedAfterCopy) { S.reloadedAfterCopy = true; loadMaps(); }
  }
}

/* ---- setup ---------------------------------------------------------------------------------------- */
const ACTION_LABEL = { "install-mods": "Install mods", "install-unity": "Install Unity", "get-hub": "Get Unity Hub",
  "open-hub": "Open Unity Hub", "pick-game": "Change game folder" };
async function loadSetup(force) {
  const r = await api(`/api/setup-status${force ? "?deep=1" : ""}`);
  S.setup = r;
  $("setupChip").hidden = r.ready;
  $("setupChip").textContent = "Setup incomplete";
  if (!r.ready && (force || !S.setupHidden)) $("setupCard").hidden = false;
  if (r.ready) $("setupCard").hidden = true;
  $("setupList").innerHTML = r.items.map((i) => `<li class="${i.ok ? "ok" : "bad"}"><span class="mark">${i.ok ? "&#10003;" : "&#10007;"}</span>${esc(i.label)}
    ${i.detail ? `<div class="fix">${esc(i.detail)}</div>` : ""}
    ${i.ok ? "" : `<div class="fix">${esc(i.fix)}</div>${i.action ? `<button class="btn small" data-action="${i.action}">${ACTION_LABEL[i.action]}</button>` : ""}${i.busy ? `<div class="fix">Installing... this takes a while.</div>` : ""}`}</li>`).join("");
  $("setupNotes").innerHTML = r.notes.map((n) => `<p class="muted small">${esc(n)}</p>`).join("");
  $("shipNote").textContent = r.shippingCached ? "(data already downloaded)" : `(downloads ${r.shippingMB} MB once)`;
  $("setupList").querySelectorAll("[data-action]").forEach((b) => b.addEventListener("click", () => setupAction(b.dataset.action, b)));
}
async function setupAction(action, btn) {
  const msg = $("setupMsg");
  let body = {};
  if (action === "pick-game") {
    const f = prompt("Paste the Nuclear Option folder (the one with NuclearOption.exe):");
    if (!f) return;
    body = { folder: f };
  }
  btn.disabled = true;
  msg.hidden = false;
  msg.textContent = "Working...";
  try {
    const r = await api(`/api/setup/${action}`, body);
    msg.textContent = (r.done || ["Started. Progress shows above."]).join(" ");
  } catch (e) {
    const er = (e.data && e.data.error) || { title: e.message };
    msg.innerHTML = `<b>${esc(er.title)}</b> ${esc(er.fix || "")}`;
  }
  btn.disabled = false;
  loadSetup(true); loadMaps(); poll(true);
}
$("recheckBtn").addEventListener("click", () => loadSetup(true));
$("hideSetupBtn").addEventListener("click", () => { S.setupHidden = true; $("setupCard").hidden = true; });
$("setupChip").addEventListener("click", () => { S.setupHidden = false; loadSetup(true); $("controls").scrollTop = 0; });
$("creditsBtn").addEventListener("click", () => $("credits").showModal());
$("quitBtn").addEventListener("click", async () => {
  try { await api("/api/quit", {}); document.body.innerHTML = "<p style='padding:40px'>Open Skies has closed. You can close this window.</p>"; }
  catch (e) { alert(`${e.message} ${(e.data && e.data.error && e.data.error.fix) || ""}`); }
});

loadSetup().catch(() => {});
loadMaps().catch(() => {});
restoreDraft().catch(() => {});
poll();
// #buildCard, #mapsCard, ...: open the page scrolled to that panel.
if (location.hash.length > 1) setTimeout(() => { const el = document.getElementById(location.hash.slice(1)); if (el) el.scrollIntoView(); }, 1500);
