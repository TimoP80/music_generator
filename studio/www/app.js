/* ==========================================================================
   TIMBOR Album Studio — frontend application
   Data comes exclusively from the TIMBOR backend (album.json, project.json,
   sequence/loudness/qc/manifest documents, WAV streams). No invented values.
   ========================================================================== */
"use strict";

/* ------------------------------------------------------------------ utils */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(c));
  }
  return el;
}

const fmtTime = (s) => {
  if (s === undefined || s === null || isNaN(s)) return "—";
  s = Math.max(0, s);
  const m = Math.floor(s / 60), r = s - m * 60;
  return `${String(m).padStart(2, "0")}:${r < 10 ? "0" : ""}${r.toFixed(1)}`;
};
const fmtClock = (s) => {
  if (s === undefined || s === null || isNaN(s)) return "00:00";
  const m = Math.floor(s / 60), r = Math.floor(s % 60);
  return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
};
const fmtBytes = (b) => {
  if (b === undefined || b === null) return "";
  if (b > 1 << 20) return (b / (1 << 20)).toFixed(1) + " MB";
  if (b > 1 << 10) return (b / (1 << 10)).toFixed(1) + " KB";
  return b + " B";
};
const fmtDb = (v, digits = 2) =>
  (v === undefined || v === null) ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(digits)} dB`;
const stateClass = (s) =>
  ({ READY: "", VERIFIED: "", RENDERING: "amber", VERIFYING: "cyan",
     DIRTY: "amber", WARNING: "amber", ERROR: "red" }[s] ?? "");

function toast(text, kind = "") {
  const box = $("toast");
  const el = h("div", { class: `toast-msg ${kind}` }, text);
  box.append(el);
  setTimeout(() => el.remove(), 5200);
}

/* ------------------------------------------------------------- API layer */
const albumApi = {
  health: () => api.get("/api/health"),
  albums: () => api.get("/api/albums"),
  get: () => api.get("/api/album"),
  manifest: () => api.get("/api/album/manifest").catch(() => null),
  motifs: () => api.get("/api/motifs"),
  palette: () => api.get("/api/palette"),
  saveSequencing: (body) => api.post("/api/album/config/sequencing", body),
  saveLoudness: (body) => api.post("/api/album/config/loudness", body),
};
const libraryApi = {
  roots: () => api.get("/api/sample-libraries"),
  add: (path) => api.post("/api/sample-libraries", { path }),
  remove: (id) => api.delete(`/api/sample-libraries/${id}`),
  scan: (root_id) => api.post("/api/sample-libraries/scan", { root_id }),
  scanStatus: () => api.get("/api/sample-libraries/scan/status"),
};
const sampleApi = {
  search: (params = {}) => {
    const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null));
    return api.get(`/api/samples?${q}`);
  },
  detail: (id) => api.get(`/api/samples/${encodeURIComponent(id)}`),
  waveform: (id, bins = 1200) => api.get(`/api/samples/${encodeURIComponent(id)}/waveform?bins=${bins}`),
  usage: (id) => api.get(`/api/samples/${encodeURIComponent(id)}/usage`),
  addToPalette: (sample_id, role) => api.post("/api/album/palette/samples", { sample_id, role }),
  mediaUrl: (id) => `/media/sample/${encodeURIComponent(id)}`,
};
const trackApi = {
  all: () => api.get("/api/tracks"),
  one: (id) => api.get(`/api/tracks/${id}`),
  timeline: (id) => api.get(`/api/tracks/${id}/timeline`),
  stems: (id) => api.get(`/api/tracks/${id}/stems`),
  peaks: (id, bins = 1400) => api.get(`/api/tracks/${id}/peaks?bins=${bins}`),
};
const renderApi = {
  submit: (kind) => api.post("/api/jobs", { kind }),
  jobs: () => api.get("/api/jobs"),
};
const qcApi = {
  doc: () => api.get("/api/qc").catch(() => null),
};
const audioApi = {
  sequence: () => api.get("/api/sequence").catch(() => null),
  loudness: () => api.get("/api/loudness").catch(() => null),
  albumPeaks: (bins = 1600) => api.get(`/api/album/peaks?bins=${bins}`),
  url: (rel) => `/media/${rel.split("?")[0]}`,
};
const releaseApi = {
  doc: () => api.get("/api/release").catch(() => null),
  tree: () => api.get("/api/release/tree"),
};

const api = {
  async get(url) {
    const r = await fetch(url);
    if (!r.ok) throw await api.err(r);
    return r.json();
  },
  async post(url, body) {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    if (!r.ok) throw await api.err(r);
    return r.json();
  },
  async delete(url) {
    const r = await fetch(url, { method: "DELETE" });
    if (!r.ok) throw await api.err(r);
    return r.json();
  },
  async err(r) {
    let detail = `HTTP ${r.status}`;
    try { const j = await r.json(); if (j.error) detail = j.error.detail || j.error.title; } catch {}
    return new Error(detail);
  },
};

/* --------------------------------------------------------- app state */
const app = {
  screen: "overview",
  health: null,
  album: null,
  tracks: null,
  selectedTrack: null,
  selectedMotif: null,
  seqEdits: null,          // pending sequencing edits {mode, gap, xfade, quantize, curve, order}
  playingItem: null,
  selectedSample: null,
  libraryResults: null,
  libraryQuery: "",
  libraryFilters: { category: "", bpm_min: "", bpm_max: "", key: "", duration_min: "", duration_max: "", library: "", status: "", tag: "" },
  libraryPage: 1,
  librarySort: "name",
  libraryScan: null,
  jobWasRunning: false,
  palettes: { category: "ALL" },
  qcFilter: null,
};

/* ------------------------------------------------------- waveform drawing */
function drawPeaks(canvas, peaks, opts = {}) {
  if (!canvas || !peaks) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 600, ht = canvas.clientHeight || 64;
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(ht * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.fillStyle = "#0b0e11";
  ctx.fillRect(0, 0, w, ht);
  ctx.strokeStyle = "rgba(63,198,224,.07)";
  ctx.lineWidth = 1;
  for (let gx = 0; gx < w; gx += 32) {
    ctx.beginPath(); ctx.moveTo(gx + .5, 0); ctx.lineTo(gx + .5, ht); ctx.stroke();
  }
  const mid = ht / 2;
  ctx.strokeStyle = "rgba(125,139,150,.35)";
  ctx.beginPath(); ctx.moveTo(0, mid + .5); ctx.lineTo(w, mid + .5); ctx.stroke();
  const n = peaks.bins, color = opts.color || "#2aa97b";
  const rmsColor = opts.rmsColor || "#37e0a0";
  const regions = opts.regions || [];
  for (const rg of regions) {
    ctx.fillStyle = rg.color || "rgba(63,198,224,.10)";
    ctx.fillRect(rg.x0 * w, 0, (rg.x1 - rg.x0) * w, ht);
  }
  for (let i = 0; i < n; i++) {
    const x0 = (i / n) * w, bw = Math.max(1, w / n - 0.4);
    const lo = Math.max(-1, peaks.min[i]), hi = Math.min(1, peaks.max[i]);
    ctx.fillStyle = color;
    ctx.fillRect(x0, mid - hi * mid * .96, bw, Math.max(1, (hi - lo) * mid * .96));
    const rms = Math.min(1, peaks.rms[i] || 0);
    ctx.fillStyle = rmsColor;
    ctx.fillRect(x0, mid - rms * mid * .96, bw, Math.max(1, rms * mid * 1.92));
  }
}

/* screens assemble panels asynchronously, so canvases can be detached
   while their peaks request is in flight — wait for connection */
async function drawWhenConnected(canvas, peaksPromise, opts) {
  let p;
  try { p = await peaksPromise; } catch { return; }
  for (let i = 0; i < 60 && (!canvas || !canvas.isConnected); i++)
    await new Promise((r) => setTimeout(r, 100));
  if (canvas && canvas.isConnected) drawPeaks(canvas, p, opts);
}

/* ============================================================ player === */
const player = {
  audio: new Audio(),
  queue: [],
  index: -1,
  loop: false,
  wasPlaying: false,

  /* playhead plan: album-time positions of track-master playback, from the
     real engine sequence document — no projection, no invented positions.
     Rebuilt on "player-changed" (load/stop) and after timeline redraws. */
  playheadPlan: null,     // {map, total} | null
  playheadLast: null,     // last applied x-position (avoids repaint spam)

  buildPlayheadPlan() {
    this.playheadLast = null;
    const item = this.playingItem || app.playingItem;
    if (!item || !item.id) return this.playheadPlan = null;
    const seq = app.seqDoc;
    if (!seq || seq.mode !== "gap" || !Array.isArray(seq.entries))
      return this.playheadPlan = null;
    const map = new Map();
    for (const e of seq.entries)
      if (e.track_id !== undefined && e.start_seconds !== undefined)
        map.set(String(e.track_id), e.start_seconds);
    if (!map.size) return this.playheadPlan = null;
    this.playheadPlan = { map, total: seq.duration_seconds };
  },

  playheadAlbumTime() {
    const plan = this.playheadPlan;
    if (!plan || !this.playingItem?.id) return null;
    const start = plan.map.get(String(this.playingItem.id));
    if (start === undefined) return null;
    const c = this.audio.currentTime || 0;
    // clamp within the entry; gaps are dead air and would show a stale marker
    const entry = [...plan.map.entries()].find(([, s]) => s === start);
    const all = [...plan.map.values()].sort((a, b) => a - b);
    const idx = all.indexOf(start);
    const end = (idx + 1 < all.length)
      ? Math.min(start + (this.playingItem.dur || 0), all[idx + 1])
      : start + (this.playingItem.dur || 0);
    void entry;
    return Math.min(Math.max(start + c, start), end);
  },
  init() {
    this.audio.volume = ($("pl-vol").value / 100);
    $("pl-play").onclick = () => (this.audio.paused ? this.resume() : this.pause());
    $("pl-stop").onclick = () => this.stop();
    $("pl-next").onclick = () => this.next();
    $("pl-prev").onclick = () => this.prev();
    $("pl-mute").onclick = () => {
      this.audio.muted = !this.audio.muted;
      $("pl-mute").textContent = this.audio.muted ? "🔇" : "🔊";
    };
    $("pl-loop").onclick = () => {
      this.loop = !this.loop;
      $("pl-loop").classList.toggle("on", this.loop);
    };
    $("pl-vol").oninput = (e) => { this.audio.volume = e.target.value / 100; };
    $("pl-bar").onclick = (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      const f = (e.clientX - r.left) / r.width;
      if (this.audio.duration) this.audio.currentTime = f * this.audio.duration;
    };
    this.audio.addEventListener("timeupdate", () => this.uiTime());
    this.audio.addEventListener("ended", () => {
      if (this.loop) { this.audio.currentTime = 0; this.audio.play(); }
      else this.next(true);
    });
    this.audio.addEventListener("play", () => { $("pl-play").textContent = "⏸"; });
    this.audio.addEventListener("pause", () => { $("pl-play").textContent = "▶"; });
    this.audio.addEventListener("error", () => {
      if (this.playingItem) toast(`audio playback failed: ${this.playingItem.name}`, "err");
    });
  },

  playQueue(items, startIdx, autoPlay = true) {
    this.queue = items;
    this.index = startIdx;
    this.load(this.queue[this.index], autoPlay);
  },

  /** Queue the album's track masters in engine sequence order, carrying
     each item's album-relative start offset (gap mode = exact starts). */
  playQueueAlbum(orderIds) {
    const seq = app.seqDoc;
    const useSeq = seq && seq.mode === "gap" && Array.isArray(seq.entries)
      && seq.entries.every((e) => e.track_id !== undefined
        && e.start_seconds !== undefined);
    const byId = Object.fromEntries((app.tracks || [])
      .map((t) => [t.track_id, t]));
    const items = [];
    if (useSeq) {
      for (const e of seq.entries) {
        const t = byId[e.track_id];
        if (!t) return false;
        items.push({ id: String(e.track_id),
          rel: `${t.directory}/audio/master.wav`,
          name: `${e.track_id} ${e.title || t.title || ""}`,
          dur: e.duration_seconds ?? t.duration_seconds,
          albumStart: e.start_seconds });
      }
    } else {
      for (const id of orderIds) {
        const t = byId[id];
        if (!t) return false;
        items.push({ id: String(id),
          rel: `${t.directory}/audio/master.wav`,
          name: `${id} ${t.title || ""}`,
          dur: t.duration_seconds });
      }
    }
    this.playQueue(items, 0, true);
    return true;
  },

  playSingle(item) { this.playQueue([item], 0, true); },

  load(item, autoPlay = true) {
    if (!item) return this.stop();
    this.playingItem = item;
    this.audio.src = item.mediaUrl || audioApi.url(item.rel);
    $("pl-name").textContent = item.name;
    if (autoPlay) this.audio.play().catch((e) => toast(`playback failed: ${e.message}`, "err"));
    this.uiTime();
    app.playingItem = item;
    document.dispatchEvent(new CustomEvent("player-changed"));
  },
  resume() { this.audio.play().catch(() => {}); },
  pause() { this.audio.pause(); },
  stop() {
    this.audio.pause(); this.audio.currentTime = 0;
    this.playingItem = null;
    this.queue = []; this.index = -1;
    $("pl-name").textContent = "—"; $("pl-fill").style.width = "0%";
    $("pl-time").textContent = "00:00 / 00:00";
    app.playingItem = null;
    document.dispatchEvent(new CustomEvent("player-changed"));
  },
  next(fromEnded = false) {
    if (!this.queue.length) return;
    this.index = (this.index + 1) % this.queue.length;
    this.load(this.queue[this.index]);
  },
  prev() {
    if (!this.queue.length) return;
    this.index = (this.index - 1 + this.queue.length) % this.queue.length;
    this.load(this.queue[this.index]);
  },
  seek(delta) {
    if (this.audio.duration) {
      this.audio.currentTime = Math.min(this.audio.duration,
        Math.max(0, this.audio.currentTime + delta));
    }
  },
  uiTime() {
    const c = this.audio.currentTime || 0;
    // streamed 32-bit-float WAVs often report no duration until fully
    // buffered — fall back to the known duration from project metadata
    const d = (isFinite(this.audio.duration) && this.audio.duration > 0)
      ? this.audio.duration : (this.playingItem?.dur || 0);
    $("pl-time").textContent = `${fmtClock(c)} / ${fmtClock(d)}`;
    $("pl-fill").style.width = d ? `${Math.min(100, (c / d) * 100)}%` : "0%";
  },
};

/* =========================================================== console === */
const con = {
  last: 0, count: 0,
  async poll() {
    try {
      const r = await api.get(`/api/logs?since=${this.last}`);
      this.last = r.next;
      const body = $("console-body");
      const nearBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 40;
      for (const l of r.lines) {
        this.count++;
        body.append(h("div", { class: `cline ${l.src.endsWith(":error") ? "err" : ""}` },
          h("span", { class: "ts" }, `${l.ts} `),
          h("span", { class: "src" }, `${l.src.padEnd(13)} `),
          h("span", { class: "txt" }, l.text)));
      }
      if (r.lines.length) {
        $("console-stats").textContent = `${this.count} lines`;
        if (nearBottom) body.scrollTop = body.scrollHeight;
      }
    } catch {}
  },
  toggle(force) {
    const el = $("console");
    const open = force !== undefined ? force : el.classList.contains("collapsed");
    el.classList.toggle("expanded", open);
    el.classList.toggle("collapsed", !open);
    $("console-chev").textContent = open ? "▾" : "▸";
  },
  clear() { $("console-body").innerHTML = ""; $("console-stats").textContent = ""; },
};

/* ============================================================= shell === */
const SCREENS = {
  overview: { title: "OVERVIEW", render: screenOverview },
  dna: { title: "DNA", render: screenDna },
  tracks: { title: "TRACKS", render: screenTracks },
  motifs: { title: "MOTIFS", render: screenMotifs },
  palette: { title: "SAMPLE PALETTE", render: screenPalette },
  sampleLibrary: { title: "SAMPLE LIBRARY", render: screenSampleLibrary },
  sequence: { title: "SEQUENCE", render: screenSequence },
  loudness: { title: "LOUDNESS", render: screenLoudness },
  qc: { title: "QC", render: screenQc },
  release: { title: "RELEASE", render: screenRelease },
};

async function showScreen(name, param) {
  if (!SCREENS[name]) return;
  app.screen = name;
  if (name === "tracks" && param !== undefined) app.selectedTrack = param;
  if (name === "motifs" && param !== undefined) app.selectedMotif = param;
  if (name === "sampleLibrary" && param !== undefined) app.selectedSample = param;
  document.querySelectorAll(".nav-item").forEach((b) =>
    b.classList.toggle("active", b.dataset.screen === name));
  const host = $("screen");
  host.scrollTop = 0;
  host.innerHTML = "";
  host.append(h("div", { class: "dim mono", style: "padding:30px" }, `loading ${SCREENS[name].title.toLowerCase()} …`));
  try {
    const el = await SCREENS[name].render(param);
    host.innerHTML = "";
    host.append(el);
  } catch (e) {
    host.innerHTML = "";
    host.append(h("div", { class: "panel" },
      h("div", { class: "panel-h" }, `SCREEN ERROR — ${SCREENS[name].title}`),
      h("div", { class: "panel-b mono red" }, String(e.message || e))));
  }
}

async function refreshHealth() {
  try {
    const before = app.health?.job;
    app.health = await albumApi.health();
    updateShell();
    const running = app.health.job && app.health.job.status === "running";
    if (before && before.status === "running" && (!app.health.job || app.health.job.status !== "running")) {
      const done = before;
      if (done.status && done.error) toast(`${done.label} FAILED — see console`, "err");
      else toast(`${done.label} complete`);
      invalidateAndRefresh();
    }
    app.jobWasRunning = !!running;
  } catch {}
}

function updateShell() {
  const al = app.health;
  if (!al) return;
  const led = $("top-led");
  led.className = `led ${stateClass(al.state)} ${al.state === "RENDERING" || al.state === "VERIFYING" ? "pulse" : ""}`;
  $("top-state-text").textContent = `● ${al.state}`;
  $("top-state").title = al.message || "";
  $("sb-state").innerHTML = `<span class="led ${stateClass(al.state)}"></span> ${al.state} — ${esc(al.message || "")}`;
  if (al.album_dir) $("sb-album").textContent = al.album_dir.toUpperCase();
  if (app.album && !app.album._shellDone) {
    app.album._shellDone = true;
    const g = app.album.generator, cfg = app.album.config, dna = app.album.dna;
    $("top-album-name").textContent = app.album.name;
    $("top-album-meta").textContent =
      `${cfg.tracks} TRACKS · ${Math.round(cfg.bpm_center)} BPM CENTER · ${dna.root_key} ${dna.mode.toUpperCase()} · SEED ${g.seed}`;
    $("nav-seed").textContent = g.seed;
    $("nav-engine").textContent = `v${g.version}`;
    $("sb-seed").textContent = `SEED ${g.seed}`;
    $("sb-engine").textContent = `GENERATOR v${g.version}`;
    $("sb-tracks").textContent = `${cfg.tracks} TRACKS`;
  }
  const f = app.health.format;
  if (f) {
    const t = `${(f.sample_rate / 1000).toFixed(1)} kHz / ${f.bits === 32 ? "32-bit FLOAT" : f.bits + "-bit"}`;
    $("nav-audio").textContent = t;
    $("sb-format").textContent = t;
  }
}

function invalidateAndRefresh() {
  app.album = null;
  app.tracks = null;
  refreshHealth().then(() => showScreen(app.screen, app.selectedTrack));
}

/* ==================================================== shared fragments = */
function panel(title, ...body) {
  return h("div", { class: "panel" },
    h("div", { class: "panel-h" }, title), h("div", { class: "panel-b" }, ...body));
}
function openInAlbumLibrary(sampleId) {
  app.selectedSample = sampleId;
  showScreen("sampleLibrary", sampleId);
}
function kv(pairs) {
  const g = h("div", { class: "kv" });
  for (const [k, v] of pairs) {
    g.append(h("div", { class: "k" }, k), h("div", { class: "v" }, v));
  }
  return g;
}
function badge(text, kind = "") { return h("span", { class: `badge ${kind}` }, text); }
function statusBadgeForHealth() {
  const s = app.health?.state || "—";
  const kind = { READY: "ok", VERIFIED: "ok", DIRTY: "warn", WARNING: "warn",
                 ERROR: "err", RENDERING: "info", VERIFYING: "info" }[s] || "";
  return badge(`● ${s}`, kind);
}

async function ensureCore() {
  if (!app.album) app.album = await albumApi.get();
  if (!app.tracks) app.tracks = (await trackApi.all()).tracks;
}

function trackRow(t, selected, onOpen) {
  const loud = t.loudness;
  const lu = loud ? `${loud.integrated_lufs_approx.toFixed(2)} LUFS` : "not measured";
  const row = h("div", {
    class: `track-row ${selected ? "sel" : ""}`,
    onclick: onOpen,
  },
    h("div", { class: "track-num" }, t.track_id),
    h("div", { class: "track-main" },
      h("div", { class: "track-title" }, `${t.track_id} ${t.title || ""}`),
      h("div", { class: "track-meta" },
        h("span", {}, (t.genre || "").toUpperCase()),
        h("span", {}, `${Math.round(t.bpm)} BPM`),
        h("span", {}, (t.key || "").toUpperCase()),
        h("span", {}, fmtTime(t.duration_seconds)),
        h("span", {}, `MOTIF ${t.lineage.variant}`),
        h("span", {}, `${t.samples_used} palette samples`),
        h("span", { class: loud ? "acc" : "faint" }, lu))),
    h("div", { class: "" },
      h("canvas", { class: "wave mini", "data-track": t.track_id })),
    h("div", { class: "track-actions" },
      badge(t.master_exists ? "● READY" : "○ NO MASTER", t.master_exists ? "ok" : "err"),
      h("button", { class: "btn tiny", onclick: (e) => { e.stopPropagation();
        player.playQueue(app.tracks.map((x) => ({ rel: `${x.directory}/audio/master.wav`, name: `${x.track_id} ${x.title}`, id: x.track_id, dur: x.duration_seconds })),
          app.tracks.indexOf(t)); } }, "PLAY"),
      h("button", { class: "btn tiny", onclick: (e) => { e.stopPropagation(); onOpen(); } }, "OPEN")));
  const rowCanvas = row.querySelector("canvas");
  drawWhenConnected(rowCanvas, trackApi.peaks(t.track_id, 240),
    { color: "#1d6b50" });
  return row;
}

/* ------------------------------------------------- sequence projection */
/* shared by the sequence editor preview, the timeline playhead and the
   scrubber: one source of truth for projected block positions */
function projectTimelineShared(order, mode, gap, xfade) {
  const byId = Object.fromEntries(app.tracks.map((t) => [t.track_id, t]));
  let cursor = 0;
  const blocks = [];
  order.forEach((id, i) => {
    const t = byId[id];
    const dur = t ? (t.duration_seconds || 0) : 0;
    let overlap = 0;
    if (i > 0) {
      if (mode === "gap") cursor += gap;
      else if (mode === "crossfade") {
        overlap = Math.min(xfade, 0.4 * Math.min(blocks[blocks.length - 1].dur, dur));
      }
    }
    blocks.push({ id, t, start: cursor - overlap, dur, overlap });
    cursor = cursor - overlap + dur;
  });
  return { blocks, total: Math.max(1, cursor) };
}

/* ====================================================== 01 OVERVIEW ==== */
async function screenOverview() {
  await ensureCore();
  const art = app.health.artifacts;
  const artifactList = h("div", { class: "checklist" },
    ["sequence", "loudness", "qc", "cue", "tracklist", "release_manifest"]
      .map((k) => h("div", { class: `ck ${art[k] ? "" : "todo"}` },
        h("span", { class: "mark" }, art[k] ? "✓" : "○"),
        h("span", {}, k.replace("_", " ")),
        h("span", { class: "faint" }, art[k] ? "written" : "missing"))));

  const waveCanvas = h("canvas", { class: "wave tall" });
  const waveBox = h("div", {}, waveCanvas,
    h("div", { class: "loud-scale mono faint", id: "ov-wave-scale" }, ""));

  const host = h("div", {},
    h("div", { class: "screen-title" }, app.album.name),
    h("div", { class: "screen-sub" },
      `ALBUM DIRECTORY ${app.health.album_dir} · ${app.album.format} v${app.album.version} · GENERATED ${app.album.generator.generated_at}`),

    panel("PROJECT STATUS", h("div", { class: "row" },
      statusBadgeForHealth(),
      h("span", { class: "mono dim", style: "flex:1" }, app.health.message || ""),
      h("button", { class: "btn", onclick: () => submitJob("resequence") }, "SEQUENCE"),
      h("button", { class: "btn primary", onclick: () => submitJob("render") }, "RENDER ALBUM"),
      h("button", { class: "btn", onclick: () => submitJob("verify") }, "VERIFY"),
      h("button", { class: "btn", onclick: () => submitJob("package") }, "PACKAGE"))),

    h("div", { class: "grid2" },
      panel("MUSICAL DNA", kv([
        ["KEY CENTER", `${app.album.dna.root_key} ${app.album.dna.mode}`],
        ["MODE", app.album.dna.mode.toUpperCase()],
        ["BPM CENTER", app.album.dna.bpm_center],
        ["BPM RANGE", `${app.album.dna.bpm_center - app.album.dna.bpm_range / 2}–${app.album.dna.bpm_center + app.album.dna.bpm_range / 2}`],
        ["MOTIF FAMILY", `A (${app.album.dna.motif_contour})`],
        ["HARMONIC FAMILY", app.album.dna.harmonic_family],
        ["PALETTE", app.album.dna.palette_mode.toUpperCase()],
        ["ERA", app.album.dna.era],
        ["DNA HASH", app.album.dna_hash],
      ])),
      panel("RELEASE ARTIFACTS", artifactList)),

    panel("ALBUM MASTER — WAVEFORM", waveBox),
    panel(`TRACKS (${app.tracks.length})`,
      ...app.tracks.map((t) => trackRow(t, t.track_id === app.selectedTrack,
        () => showScreen("tracks", t.track_id)))));

  // album waveform + region overlay from real sequence
  (async () => {
    try {
      const peaks = await audioApi.albumPeaks(1600);
      const seq = await audioApi.sequence();
      const regions = [];
      if (seq) {
        const total = seq.total_samples;
        for (const b of seq.boundaries || []) {
          regions.push({ x0: Math.max(0, (b.start_sample -
            (seq.entries[b.destination_position]?.overlap_samples || 0)) / total),
            x1: b.start_sample / total, color: "rgba(224,177,63,.16)" });
        }
        for (const e of seq.entries) {
          if (e.gap_before_samples > 0) {
            regions.push({ x0: (e.start_sample - e.gap_before_samples) / total,
              x1: e.start_sample / total, color: "rgba(125,139,150,.10)" });
          }
        }
        $("ov-wave-scale").textContent =
          `MODE ${seq.mode.toUpperCase()} · ${fmtTime(seq.duration_seconds)} TOTAL · ${seq.entries.length} TRACKS`;
      }
      drawPeaks(waveCanvas, peaks, { regions });
      waveCanvas.onclick = () => {
        player.playSingle({
          rel: `${app.health.album_dir}/album/album.wav`,
          name: `${app.album.name} — ALBUM MASTER` });
      };
    } catch (e) {
      waveCanvas.replaceWith(h("div", { class: "mono faint", style: "padding:8px" },
        "album.wav not rendered yet — run RENDER ALBUM"));
    }
  })();

  return host;
}

/* ========================================================== 02 DNA ===== */
async function screenDna() {
  await ensureCore();
  const d = app.album.dna;
  const perTrack = app.tracks.map((t) =>
    ({ id: t.track_id, bpm: t.bpm, dev: t.bpm - d.bpm_center }));

  const fp = h("div", { class: "fp" },
    d.rhythm_fingerprint.map((b) => h("i", { class: b ? "" : "off",
      style: `height:${b ? 100 : 35}%` })));

  const nodes = h("div", { class: "dna-center" },
    h("div", { class: "dna-node" },
      h("h4", {}, "HARMONIC"),
      kv([["KEY", `${d.root_key} ${d.mode}`],
          ["FAMILY", d.harmonic_family],
          ["PROGRESSION", app.tracks[0] ? (await trackApi.one(app.tracks[0].track_id)).song.progression : "—"]])),
    h("div", { class: "dna-core" },
      h("div", { class: "big" }, `${d.root_key}${d.mode === "minor" ? "m" : ""}`),
      h("div", { class: "lbl" }, "ALBUM DNA"),
      h("div", { class: "dna-hash" }, `hash ${app.album.dna_hash}`)),
    h("div", { class: "dna-node" },
      h("h4", {}, "RHYTHM"),
      fp,
      h("div", { class: "mono faint", style: "margin-top:6px" },
        `rhythm fingerprint · ${d.rhythm_fingerprint.filter(Boolean).length}/${d.rhythm_fingerprint.length} active`)));

  const bpmRows = perTrack.map((p) => {
    const meter = h("div", { class: `meter ${Math.abs(p.dev) > 6 ? "warn" : ""}` },
      h("div", { class: "fill", style: `transform:scaleX(${Math.min(1, Math.abs(p.dev) / 12)})` }));
    return h("div", { class: "loud-row", style: "grid-template-columns:150px 1fr 130px 90px" },
      h("div", { class: "nm" }, `TRACK ${p.id}`),
      meter,
      h("div", { class: "val" }, `${p.bpm.toFixed(1)} BPM`),
      h("div", { class: "val" }, `${p.dev > 0 ? "+" : ""}${p.dev.toFixed(1)}`));
  });

  const sig = Object.entries(d.signature_instruments || {})
    .map(([k, v]) => [k.toUpperCase(), v]);
  sig.push(["ERA", d.era], ["MOOD", d.mood],
           ["AUTHENTICITY", d.authenticity], ["PALETTE", d.palette_mode]);

  return h("div", {},
    h("div", { class: "screen-title" }, "ALBUM DNA"),
    h("div", { class: "screen-sub" },
      `MUSICAL IDENTITY DERIVED FROM SEED ${app.album.generator.seed} · GENERATOR v${app.album.generator.version}`),
    nodes,
    h("div", { class: "grid2", style: "margin-top:14px" },
      panel("SONIC DNA", kv(sig)),
      panel("BPM DNA — PER-TRACK DEVIATION FROM CENTER",
        h("div", { style: "padding:2px 0" }, ...bpmRows),
        h("div", { class: "loud-scale" },
          h("span", {}, "0"), h("span", {}, "+6"), h("span", {}, "+12")))));
}

/* ======================================================== 03 TRACKS ==== */
async function screenTracks(sel) {
  await ensureCore();
  if (sel) app.selectedTrack = sel;
  const t = app.tracks.find((x) => x.track_id === app.selectedTrack) || app.tracks[0];
  app.selectedTrack = t.track_id;

  const list = h("div", {}, ...app.tracks.map((x) =>
    trackRow(x, x.track_id === t.track_id, () => showScreen("tracks", x.track_id))));

  const detail = await buildTrackDetail(t);
  return h("div", {},
    h("div", { class: "screen-title" }, "TRACKS"),
    h("div", { class: "screen-sub" }, `${app.tracks.length} INDEPENDENT PROJECTS · CLICK A ROW TO INSPECT`),
    list, detail);
}

async function buildTrackDetail(t) {
  let doc = null, stems = [], tPeaks = null;
  try { doc = await trackApi.one(t.track_id); } catch {}
  try { stems = (await trackApi.stems(t.track_id)).stems; } catch {}
  try { tPeaks = await trackApi.peaks(t.track_id, 900); } catch {}

  const wave = h("canvas", { class: "wave tall" });
  if (tPeaks) drawPeaks(wave, tPeaks);
  else wave.replaceWith(h("div", { class: "mono faint", style: "padding:8px" }, "master.wav missing — RE-RENDER the project"));

  // section ruler from project sections
  let ruler = null;
  if (doc?.sections?.length && doc.seconds_per_beat) {
    const dur = doc.track.duration_seconds || 1;
    ruler = h("div", { class: "row", style: "margin-top:8px; flex-wrap:wrap; gap:0" },
      doc.sections.map((s) => h("div", {
        class: "mono faint",
        style: `width:${Math.max(2, ((s.end_beat - s.start_beat) * doc.seconds_per_beat) / dur * 100)}%;` +
               `border-left:1px solid var(--line2);padding:2px 4px;font-size:9px;text-transform:uppercase;overflow:hidden;white-space:nowrap` },
        s.name)));
  }

  // stem mixer rows
  const mixer = h("div", {});
  const stemStates = {};
  for (const s of stems) {
    stemStates[s.name] = { muted: false, solo: false };
    const nm = h("div", { class: "mono", style: "width:90px;text-transform:uppercase" }, s.name);
    const mBut = h("button", { class: "btn tiny", title: `Mute ${s.name}`, onclick: () => {
      const st = stemStates[s.name]; st.muted = !st.muted;
      mBut.classList.toggle("warn", st.muted); applyMixer();
    } }, "M");
    const sBut = h("button", { class: "btn tiny", title: `Solo ${s.name}`, onclick: () => {
      const st = stemStates[s.name]; st.solo = !st.solo;
      sBut.classList.toggle("info", st.solo); applyMixer();
    } }, "S");
    const playBut = h("button", { class: "btn tiny", onclick: () =>
      player.playSingle({ rel: `${s.rel}`, dur: s.duration_seconds,
        name: `${t.track_id} ${t.title} — ${s.name}` }) }, "▶");
    mixer.append(h("div", { class: "row", style: "padding:4px 0" },
      playBut, nm, mBut, sBut,
      h("span", { class: "mono faint" }, `${s.rate} Hz · ${s.bits === 32 ? "f32" : s.bits + "b"} · ${fmtTime(s.duration_seconds)}`)));
  }
  function applyMixer() {
    const anySolo = Object.values(stemStates).some((s) => s.solo);
    for (const [name, st] of Object.entries(stemStates)) {
      const active = anySolo ? st.solo : !st.muted;
      if (!active && app.playingItem?.rel?.includes(`${t.directory}/audio/${name}`)) player.stop();
    }
    toast(anySolo ? "solo applied — soloed stems remain playable" : "mixer state updated");
  }
  if (!stems.length) mixer.append(h("div", { class: "faint mono" }, "no stem files found"));

  const lu = doc?.track && t.loudness;
  const exports = Object.entries(t.exports || {}).map(([k, v]) =>
    h("div", { class: "row", style: "padding:2px 0" },
      h("span", { class: "mono faint", style: "width:80px" }, k.toUpperCase()),
      h("span", { class: "mono dim" }, v)));

  return h("div", { class: "panel tr-editor", style: "margin-top:14px" },
    h("div", { class: "panel-h" },
      `TRACK WORKSPACE — ${t.track_id} ${t.title || ""}`,
      h("span", { class: "spacer" }),
      h("button", { class: "btn tiny primary", onclick: () =>
        player.playQueue(app.tracks.map((x) => ({ rel: `${x.directory}/audio/master.wav`, name: `${x.track_id} ${x.title}`, id: x.track_id, dur: x.duration_seconds })),
          app.tracks.indexOf(t)) }, "▶ PLAY"),
      h("button", { class: "btn tiny", onclick: () => player.stop() }, "■ STOP"),
      h("button", { class: "btn tiny", onclick: () => submitJob("render") }, "RENDER ALBUM"),
      h("button", { class: "btn tiny", onclick: () => submitJob("verify") }, "VERIFY")),
    h("div", { class: "panel-b" },
      h("div", { class: "track-meta", style: "margin-bottom:10px" },
        h("span", {}, (t.genre || "").toUpperCase()),
        h("span", {}, `${Math.round(t.bpm)} BPM`),
        h("span", {}, (t.key || "").toUpperCase()),
        h("span", {}, `${t.bars} BARS`),
        h("span", {}, fmtTime(t.duration_seconds)),
        h("span", {}, `SEED ${t.seed}`)),
      wave, ruler,
      h("div", { class: "grid2", style: "margin-top:14px" },
        h("div", {},
          h("div", { class: "mono faint", style: "letter-spacing:2px;margin-bottom:6px" }, "STEMS"),
          mixer),
        h("div", {},
          h("div", { class: "mono faint", style: "letter-spacing:2px;margin-bottom:6px" }, "MEASUREMENTS"),
          kv([
            ["INTEGRATED", lu ? `${t.loudness.integrated_lufs_approx.toFixed(2)} LUFS*` : "—"],
            ["PEAK", lu ? fmtDb(t.loudness.peak_dbfs) : "—"],
            ["TRUE PEAK*", lu ? fmtDb(t.loudness.true_peak_dbfs_approx) : "—"],
            ["RMS", lu ? fmtDb(t.loudness.rms_dbfs) : "—"],
            ["RANGE", lu ? `${t.loudness.loudness_range_lu.toFixed(2)} LU` : "—"],
          ]),
          h("div", { class: "mono faint", style: "letter-spacing:2px;margin:12px 0 6px" }, "EXPORTS"),
          ...exports))));
}

/* ========================================================= 04 MOTIFS === */
async function screenMotifs(sel) {
  await ensureCore();
  const m = await albumApi.motifs();
  if (sel) app.selectedMotif = sel;
  const selId = app.selectedMotif || m.nodes[0].track_id;
  const node = m.nodes.find((n) => n.track_id === selId) || m.nodes[0];

  const tree = h("div", { class: "mtree" },
    m.nodes.map((n, i) => {
      const el = h("div", {
        class: `mnode ${n.track_id === selId ? "sel" : ""}`,
        onclick: () => showScreen("motifs", n.track_id),
      },
        h("div", { class: "var" }, n.variant || "?"),
        h("div", { class: "trk" }, `TRACK ${n.track_id} · ${n.title}`),
        h("div", { class: "ident" },
          `identity ${(n.identity_score * 100).toFixed(0)}%`,
          h("div", { class: "bar" }, h("b", { style: `width:${n.identity_score * 100}%` }))));
      if (i > 0) el.prepend(h("div", { class: "medge" }));
      return el;
    }));

  const rootKey = m.root_key, mode = m.mode;
  const noteName = (deg) => {
    if (deg === null || deg === undefined) return null;
    const steps = mode === "minor" ? [0, 2, 3, 5, 7, 8, 10] : [0, 2, 4, 5, 7, 9, 11];
    const names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
    const base = names.indexOf(rootKey);
    const octv = Math.floor(deg / 7), dg = ((deg % 7) + 7) % 7;
    const semi = base + steps[dg] + 12 * octv;
    return `${names[semi % 12]}${4 + Math.floor(semi / 12)}`;
  };

  function pianoRoll(motif, label) {
    const degs = [...new Set(motif.filter((x) => x !== null && x !== undefined))];
    const lo = Math.min(...degs), hi = Math.max(...degs);
    const span = Math.max(1, hi - lo);
    const rows = motif.map((d, i) => {
      const nn = noteName(d);
      const row = h("div", { class: "rrow" },
        h("span", { class: "faint" }, nn || "·"));
      const note = h("div", { class: "note" });
      if (d !== null && d !== undefined) {
        const y = 1 - (d - lo) / span;
        note.append(h("i", { style: `left:${(i / motif.length) * 100}%;width:${100 / motif.length - 3}%;top:${y * 100}%` }));
      }
      row.append(note);
      return row;
    });
    return h("div", {},
      h("div", { class: "mono faint", style: "letter-spacing:2px;margin-bottom:6px" }, label),
      h("div", { class: "roll" }, ...rows));
  }

  const variantInfo = m.plan_variants[selId] || {};
  const tags = h("div", { class: "tags" },
    (node.transformations || []).map((tr) => h("span", { class: "tag" }, tr.toUpperCase())));

  return h("div", {},
    h("div", { class: "screen-title" }, "MOTIF LINEAGE"),
    h("div", { class: "screen-sub" },
      `FAMILY ${rootKey} ${mode.toUpperCase()} · CONTOUR ${m.motif_contour} · BASE A = DNA MOTIF · CLICK A NODE`),
    panel("LINEAGE TREE", tree),
    h("div", { class: "grid2" },
      panel("MOTIF COMPARISON", h("div", { class: "grid2" },
        pianoRoll(m.motif_family, `BASE MOTIF A (DNA)`),
        pianoRoll(node.motif, `TRACK ${node.track_id} — ${node.variant}`))),
      panel(`TRACK ${node.track_id} — ${node.variant}`,
        kv([
          ["IDENTITY SCORE", `${(node.identity_score * 100).toFixed(1)}%`],
          ["PARENT", node.parent],
          ["FINGERPRINT", node.fingerprint],
          ["SONG MOTIF", (variantInfo.song_motif || []).map((x) => x ?? "·").join(" ")],
        ]),
        h("div", { class: "mono faint", style: "letter-spacing:2px;margin:12px 0 4px" }, "TRANSFORMATIONS"),
        tags,
        h("div", { class: "mono faint", style: "letter-spacing:2px;margin:12px 0 4px" }, "PLAN VARIANTS"),
        h("div", { class: "mono dim" },
          Object.entries(variantInfo.plan_variants || {}).map(([k, v]) =>
            `${k}: ${v.map((x) => x ?? "·").join(" ")}`).join("   ·   ")))),
    h("div", { class: "mono faint", style: "margin-top:4px" },
      "identity floor 35% enforced by the engine — every variant stays musically related to Motif A"));
}

/* =================================================== 05 SAMPLE LIBRARY == */
async function screenSampleLibrary() {
  const [libraryDoc, rolesDoc] = await Promise.all([
    libraryApi.roots(), api.get("/api/album/palette/roles"),
  ]);
  let searchTimer = null;
  let searchRequest = 0;
  const scanTimer = setInterval(async () => {
    if (app.screen !== "sampleLibrary") { clearInterval(scanTimer); return; }
    try {
      const status = await libraryApi.scanStatus();
      app.libraryScan = status;
      drawScan();
      if (["COMPLETE", "ERROR"].includes(status.state)) {
        clearInterval(scanTimer);
        refreshRoots();
        runSearch();
      }
    } catch {}
  }, 800);

  const searchIn = h("input", { type: "text", id: "sample-library-search", class: "lib-search",
    placeholder: "Search filename · path · tag · BPM · key", value: app.libraryQuery,
    oninput: (e) => {
      app.libraryQuery = e.target.value;
      clearTimeout(searchTimer);
      app.libraryPage = 1;
      searchTimer = setTimeout(runSearch, 180);
    }});
  const categoryIn = h("select", { class: "lib-select", onchange: (e) => {
    app.libraryFilters.category = e.target.value; app.libraryPage = 1; runSearch();
  }}, h("option", { value: "" }, "ALL TYPES"),
    ...["breakbeat","drum_loop","kick","snare","clap","hat","ride","perc","tom",
      "bass","acid","hoover","lead","pad","stab","chord","piano","vocal",
      "fx","riser","impact","texture","ambience"].map(v => h("option", {
      value: v, selected: app.libraryFilters.category === v }, v.toUpperCase())));
  const libraryIn = h("select", { class: "lib-select", onchange: (e) => {
    app.libraryFilters.library = e.target.value; app.libraryPage = 1; runSearch();
  }}, h("option", { value: "" }, "ALL LIBRARIES"),
    ...libraryDoc.roots.map(r => h("option", { value: r.id,
      selected: app.libraryFilters.library === r.id }, r.name)));
  const statusIn = h("select", { class: "lib-select", onchange: (e) => {
    app.libraryFilters.status = e.target.value; app.libraryPage = 1; runSearch();
  }}, ...[["", "ALL STATUS"], ["analyzed", "ANALYZED"], ["pending", "PENDING"],
    ["error", "ERROR"], ["decoder", "DECODER REQUIRED"]].map(([v,t]) =>
      h("option", { value: v, selected: app.libraryFilters.status === v }, t)));
  const sortIn = h("select", { class: "lib-select", onchange: (e) => {
    app.librarySort = e.target.value; app.libraryPage = 1; runSearch();
  }}, ...[["name","NAME"],["bpm","BPM"],["key","KEY"],["duration","DURATION"],
    ["type","TYPE"],["match","CLASSIFICATION CONFIDENCE"],["indexed","RECENTLY INDEXED"],
    ["path","PATH"]].map(([v,t]) => h("option", {
      value: v, selected: app.librarySort === v }, t)));
  const bpmLo = h("input", { type: "number", placeholder: "BPM ≥", value: app.libraryFilters.bpm_min,
    onchange: e => { app.libraryFilters.bpm_min=e.target.value; app.libraryPage=1; runSearch(); }});
  const bpmHi = h("input", { type: "number", placeholder: "BPM ≤", value: app.libraryFilters.bpm_max,
    onchange: e => { app.libraryFilters.bpm_max=e.target.value; app.libraryPage=1; runSearch(); }});
  const durLo = h("input", { type: "number", step: "0.1", placeholder: "SEC ≥", value: app.libraryFilters.duration_min,
    onchange: e => { app.libraryFilters.duration_min=e.target.value; app.libraryPage=1; runSearch(); }});
  const durHi = h("input", { type: "number", step: "0.1", placeholder: "SEC ≤", value: app.libraryFilters.duration_max,
    onchange: e => { app.libraryFilters.duration_max=e.target.value; app.libraryPage=1; runSearch(); }});
  const keyIn = h("input", { type: "text", placeholder: "KEY e.g. F#:min", value: app.libraryFilters.key,
    onchange: e => { app.libraryFilters.key=e.target.value; app.libraryPage=1; runSearch(); }});
  const tagIn = h("input", { type: "text", placeholder: "TAG", value: app.libraryFilters.tag,
    onchange: e => { app.libraryFilters.tag=e.target.value; app.libraryPage=1; runSearch(); }});
  const rootsBox = h("div", { class: "lib-roots" });
  const rootPath = h("input", { type: "text", placeholder: "D:\\Samples\\Rave Packs" });
  const rootAdd = h("button", { class: "btn tiny", onclick: async () => {
    try { await libraryApi.add(rootPath.value); rootPath.value=""; await refreshRoots();
      toast("sample library registered; press SCAN to index new files"); }
    catch(e) { toast(`library add failed: ${e.message}`, "err"); }
  }}, "+ ADD LIBRARY");
  const statsBox = h("div", { class: "lib-stats" });
  const scanBox = h("div", { class: "lib-scan" });
  const resultBody = h("tbody", {});
  const resultCount = h("span", { class: "mono faint" }, "loading index…");
  const pageLabel = h("span", { class: "mono faint" }, "");
  const prev = h("button", { class: "btn tiny", onclick: () => { app.libraryPage=Math.max(1,app.libraryPage-1); runSearch(); } }, "◀");
  const next = h("button", { class: "btn tiny", onclick: () => { app.libraryPage++; runSearch(); } }, "▶");
  const inspectBox = h("div", { class: "lib-inspector" });

  function drawStats(doc) {
    const s = doc.stats || {};
    statsBox.innerHTML = "";
    const top = [["FILES",s.files],["ANALYZED",s.analyzed],["PENDING",s.pending],
      ["DECODER REQUIRED",s.decoder_required],["ERRORS",s.errors],["DUPLICATE GROUPS",s.duplicate_groups]];
    for (const [k,v] of top) statsBox.append(h("div", { class: "lib-stat" },
      h("span", { class: "mono faint" }, k), h("b", { class: "mono" }, String(v ?? 0))));
    const formats = Object.entries(s.formats || {}).map(([k,v]) => `${k.toUpperCase()} ${v}`).join(" · ") || "no indexed formats";
    const cats = Object.entries(s.categories || {}).sort((a,b) => b[1]-a[1])
      .map(([k,v]) => `${k.toUpperCase()} ${v}`).join(" · ");
    statsBox.append(h("div", { class: "mono faint lib-statline" }, formats),
      h("div", { class: "mono dim lib-statline" }, cats));
  }
  async function refreshRoots() {
    const doc = await libraryApi.roots();
    drawStats(doc);
    libraryIn.innerHTML = "";
    libraryIn.append(h("option", { value: "" }, "ALL LIBRARIES"));
    for (const r of doc.roots) libraryIn.append(h("option", { value: r.id,
      selected: app.libraryFilters.library === r.id }, r.name));
    rootsBox.innerHTML = "";
    if (!doc.roots.length) rootsBox.append(h("div", { class: "mono faint" },
      "No library roots registered. Add a directory above; files remain in place."));
    for (const r of doc.roots) rootsBox.append(h("div", { class: "lib-root" },
      h("div", { class: "lib-root-main" },
        h("div", { class: "mono acc" }, r.path),
        h("div", { class: "mono faint" },
          `${r.stats.files} files · ${r.stats.analyzed} analyzed · ${r.stats.pending} pending · ${r.stats.decoder_required || 0} decoder required · ${r.stats.errors} errors`)),
      h("button", { class: "btn tiny primary", onclick: async () => {          try { app.libraryScan = await libraryApi.scan(r.id); drawScan(); }
          catch(e) { toast(`scan failed: ${e.message}`, "err"); }
      }}, "SCAN / RESCAN"),
      h("button", { class: "btn tiny", onclick: async () => {
        if (!confirm("Remove this library from Studio? Indexed rows and physical files are retained.")) return;
        try { await libraryApi.remove(r.id); await refreshRoots(); runSearch(); }
        catch(e) { toast(`remove failed: ${e.message}`, "err"); }
      }}, "REMOVE")));
  }
  function drawScan() {
    const s = app.libraryScan;
    scanBox.innerHTML = "";
    if (!s || !["SCANNING","ANALYZING","COMPLETE","ERROR"].includes(s.state)) return;
    const pct = Math.round((s.progress || 0) * 100);
    scanBox.append(h("div", { class: `mono ${s.state === "ERROR" ? "red" : "cyan"}` },
      `${s.state} · ${pct}% · ${s.analyzed || 0} analyzed · ${s.failed || 0} failed`),
      h("div", { class: "lib-progress" }, h("i", { style: `width:${pct}%` })),
      h("div", { class: "mono faint" },
        `${s.discovered || 0} discovered · ${s.changed || 0} changed · ${s.unchanged || 0} unchanged · ${s.deleted || 0} deleted`),
      s.current_file ? h("div", { class: "mono faint lib-current" }, s.current_file) : null,
      s.error ? h("div", { class: "mono red" }, s.error) : null,
      ["SCANNING","ANALYZING"].includes(s.state)
        ? h("div", { class: "mono faint" }, "Analysis runs in a background worker; cancellation is not available") : null);
  }
  async function runSearch() {
    const req = ++searchRequest;
    const params = { ...app.libraryFilters, q: app.libraryQuery, page: app.libraryPage,
      page_size: 50, sort: app.librarySort };
    Object.keys(params).forEach(k => { if (params[k] === "") delete params[k]; });
    try {
      const res = await sampleApi.search(params);
      if (req !== searchRequest || app.screen !== "sampleLibrary") return;
      app.libraryResults = res;
      if (app.libraryScan && ["SCANNING","ANALYZING"].includes(app.libraryScan.state)) drawScan();
      if (res.pages && app.libraryPage > res.pages) {
        app.libraryPage = res.pages;
        return runSearch();
      }
      resultCount.textContent = `${res.total.toLocaleString()} indexed samples`;
      pageLabel.textContent = `PAGE ${res.page} / ${Math.max(1,res.pages)}`;
      prev.disabled = res.page <= 1; next.disabled = res.page >= res.pages;
      resultBody.innerHTML = "";
      if (!res.results.length) resultBody.append(h("tr", {},
        h("td", { colspan: "7", class: "faint mono" }, "No indexed samples match this query.")));
      for (const s of res.results) resultBody.append(h("tr", {
        class: `lib-result ${app.selectedSample === s.id ? "sel" : ""}`,
        onclick: () => { app.selectedSample=s.id; inspect(s.id); }
      }, h("td", { class: "mono" }, s.category?.toUpperCase() || "—"),
         h("td", {}, h("span", { class: "lib-filename" }, s.filename),
           h("span", { class: "lib-path mono faint" }, s.path)),
         h("td", { class: "num" }, s.bpm == null ? "—" : Number(s.bpm).toFixed(1)),
         h("td", {}, s.key || "—"), h("td", { class: "num" }, s.duration ? `${Number(s.duration).toFixed(2)}s` : "—"),
         h("td", { class: "mono faint" }, (s.tags || []).slice(0,5).join(" · ")),
         h("td", {}, badge(s.status, s.status === "ANALYZED" ? "ok" : s.status === "ERROR" ? "err" : "warn"))));
      if (app.selectedSample && res.results.some(s => s.id === app.selectedSample)) inspect(app.selectedSample);
    } catch(e) { resultCount.textContent = `SEARCH ERROR · ${e.message}`; }
  }
  async function inspect(id) {
    inspectBox.innerHTML = "";
    inspectBox.append(h("div", { class: "mono faint" }, "loading sample inspector…"));
    try {
      const [s, usage, peaks] = await Promise.all([
        sampleApi.detail(id), sampleApi.usage(id).catch(() => null),
        sampleApi.waveform(id, 1000).catch(() => null),
      ]);        if (app.screen !== "sampleLibrary" || app.selectedSample !== id) return;
        inspectBox.innerHTML = "";
        const canvas = h("canvas", { class: "wave lib-wave" });
      const playMarker = h("i", { class: "lib-wave-playhead" });
      const waveformView = peaks ? canvas : h("div", { class: "mono faint lib-wave-note" },
        `No waveform decoder for ${s.format.toUpperCase()} (${s.error || "decoder required"}).`);
      if (peaks) canvas.addEventListener("click", (e) => {
        const box = canvas.getBoundingClientRect();
        const position = Math.max(0, Math.min(1, (e.clientX - box.left) / box.width));
        if (player.playingItem?.id !== s.id)
          player.playSingle({ id: s.id, mediaUrl: sampleApi.mediaUrl(s.id), name: s.filename, dur: s.duration });
        player.audio.currentTime = position * s.duration;
        if (player.audio.paused) player.resume();
      });
      const markerTimer = setInterval(() => {
        if (app.screen !== "sampleLibrary" || app.selectedSample !== id) {
          clearInterval(markerTimer); return;
        }
        if (player.playingItem?.id === s.id && s.duration > 0) {
          playMarker.style.display = "block";
          playMarker.style.left = `${(player.audio.currentTime / s.duration) * 100}%`;
        } else playMarker.style.display = "none";
      }, 100);
      const addRole = h("select", { class: "lib-select lib-add-role" }, ...rolesDoc.roles.map(r =>
        h("option", { value: r.id }, `${r.id.toUpperCase()} · ${r.categories.join("/")}`)));
      const details = h("div", { class: "lib-detail-grid" },
        kv([["PATH",s.path],["FORMAT",s.format.toUpperCase()],["RATE",`${s.sample_rate} Hz`],
          ["CHANNELS",s.channels],["DURATION",`${Number(s.duration).toFixed(3)} s`],
          ["STATUS",s.status],["TYPE",s.category],["SUBTYPE",s.subcategory || "—"],
          ["BPM",s.bpm == null ? "—" : `${s.bpm} · confidence ${Number(s.bpm_confidence).toFixed(2)}`],
          ["KEY",s.key || "—"],["TAGS",(s.tags||[]).join(" · ") || "—"],
          ["ENERGY",Number(s.energy).toFixed(3)],["CENTROID",`${Number(s.spectral_centroid).toFixed(1)} Hz`],
          ["CLASSIFICATION",Number(s.classification_confidence).toFixed(2)],
          ["KEY CONFIDENCE",Number(s.key_confidence).toFixed(2)],
          ["SPECTRAL WIDTH",`${Number(s.spectral_bandwidth).toFixed(1)} Hz`],
          ["ROLLOFF",`${Number(s.spectral_rolloff).toFixed(1)} Hz`],
          ["TONALNESS",Number(s.tonalness).toFixed(3)],
          ["TRANSIENT DENSITY",`${Number(s.transient_density).toFixed(2)} /s`],
          ["SIZE",fmtBytes(s.size)],
          ["DUPLICATE GROUP",s.duplicate_group || "—"]]));
      const usageBox = h("div", { class: "mono faint" }, "USAGE / SELECTION PROVENANCE");
      if (usage) {
        usageBox.innerHTML = "";
        usageBox.append(h("div", { class: "mono dim" },
          `${usage.total_placements} placements across ${usage.tracks.length} tracks`));
        if (usage.selection) usageBox.append(h("div", { class: "mono acc" },
          usage.selection.map(x => `${x.role} · score ${x.selection_score ?? "not stored"} · ${x.scored_for_genre || "manual"}`).join(" | ")));
        for (const t of usage.tracks) usageBox.append(h("div", { class: "lib-usage-track" },
          h("button", { class: "btn tiny", onclick: () => showScreen("tracks",t.track_id) }, `${t.track_id} ${t.title}`),
          h("span", { class: "mono" }, `${t.placements} placements · ${t.genre} · ${t.bpm} BPM · ${(t.roles || []).join("/")}`),
          h("span", { class: "mono faint" }, [...new Set(t.transformations.flatMap(x=>x.labels))].join(" · "))));
        const actualReasons = usage.tracks.flatMap(t => t.selection || []);
        if (actualReasons.length) usageBox.append(h("div", { class: "mono acc" },
          `SELECTOR PROVENANCE · ${actualReasons.map(x => `${x.role || "role"}: ${x.score ?? "score unavailable"}`).join(" · ")}`));
        usageBox.append(h("div", { class: "mono faint" }, usage.selector_note));
      }
      inspectBox.append(panel(`INSPECT · ${s.filename.toUpperCase()}`,
        h("div", { class: "lib-wave-wrap" }, waveformView, peaks ? playMarker : null,
          peaks ? h("div", { class: "mono faint" }, `0:00 ───────────────── ${fmtTime(s.duration)}`) : null),
        h("div", { class: "row lib-actions" },
          h("button", { class: "btn primary", disabled: !s.preview_supported,
            onclick: () => player.playSingle({ id: s.id,
              mediaUrl: sampleApi.mediaUrl(s.id), name: s.filename, dur: s.duration }) }, "▶ PREVIEW IN TRANSPORT"),
          addRole,
      h("button", { class: "btn", onclick: async () => {
        try {
          await sampleApi.addToPalette(s.id, addRole.value);
          app.album = await albumApi.get(); app.health = await albumApi.health(); updateShell();
          const assignedRole = addRole.value;
          toast(`ADDED TO ALBUM PALETTE · ${s.filename} · ${assignedRole.toUpperCase()} · ${rolesDoc.mode.toUpperCase()}`);
          const updatedRoles = await api.get("/api/album/palette/roles");
          addRole.innerHTML = "";
          for (const item of updatedRoles.roles)
            addRole.append(h("option", { value: item.id }, `${item.id.toUpperCase()} · ${item.categories.join("/")}`));
          addRole.value = assignedRole;
          app.health = await albumApi.health(); updateShell();
        } catch(e) { toast(`palette add failed: ${e.message}`, "err"); }
      }}, "ADD TO PALETTE"),
          h("button", { class: "btn tiny", onclick: () => { app.palettes.category="ALL"; showScreen("palette"); } }, "OPEN PALETTE")),
        details, usageBox));
      if (peaks) drawPeaks(canvas, peaks, { color: "#6d4790", rmsColor: "#e078a8" });
    } catch(e) { inspectBox.textContent = `INSPECT ERROR · ${e.message}`; }
  }
  await refreshRoots();
  try {
    const existingScan = await libraryApi.scanStatus();
    app.libraryScan = existingScan;
    if (["SCANNING", "ANALYZING"].includes(existingScan.state)) drawScan();
  } catch {}
  if (app.selectedSample) {
    try {
      const deepLinked = await sampleApi.detail(app.selectedSample);
      if (!app.libraryQuery) app.libraryQuery = deepLinked.filename;
    } catch { app.selectedSample = null; }
  }
  searchIn.value = app.libraryQuery;
  await runSearch();
  const formatLine = Object.entries(libraryDoc.formats || {}).map(([f,v]) =>
    `${f.toUpperCase()} ${v.analyzed ? "ANALYZED" : v.indexed ? (v.note || "INDEXED / DECODER REQUIRED") : "UNSUPPORTED"}`).join(" · ");
  return h("div", {},
    h("div", { class: "screen-title" }, "SAMPLE LIBRARY"),
    h("div", { class: "screen-sub" }, "REAL INDEX · TIMBOR SCANNER / ANALYZER / CLASSIFIER · PAGED SQL SEARCH"),
    panel("LIBRARY ROOTS — FILES REMAIN IN PLACE",
      rootsBox, h("div", { class: "row lib-add" }, rootPath, rootAdd)),
    panel("INDEX STATISTICS", statsBox,
      h("div", { class: "mono faint", style: "margin-top:7px" }, formatLine)),
    scanBox,
    panel("SEARCH / FILTER / SORT",
      h("div", { class: "lib-search-row" }, searchIn, categoryIn, libraryIn, statusIn, sortIn),
      h("div", { class: "lib-filter-row" }, bpmLo, bpmHi, keyIn, durLo, durHi, tagIn)),
    panel("INDEX RESULTS", resultCount, h("div", { class: "lib-table-wrap" },
      h("table", { class: "tech lib-table" },
        h("thead", {}, h("tr", {}, ...["TYPE","NAME / PATH","BPM","KEY","LENGTH","TAGS","STATUS"].map(x=>h("th",{},x)))),
        resultBody),
      h("div", { class: "row lib-pages" }, prev, pageLabel, next))),
    inspectBox);
}

/* ========================================================= 06 PALETTE == */
async function screenPalette() {
  await ensureCore();
  const pal = await albumApi.palette();
  const pathKey = (p) => String(p || "").replace(/\\/g, "/").toLowerCase();
  const cats = ["ALL", ...new Set(pal.entries.map((e) => e.category).filter(Boolean).map((c) => c.toUpperCase()))];
  const byPath = Object.fromEntries(pal.entries.map((e) => [pathKey(e.path), e]));
  const usageByPath = Object.fromEntries(pal.usage.map((r) => [pathKey(r.path), r]));
  const tracksById = Object.fromEntries(app.tracks.map(t => [t.track_id, t]));
  const usageRows = pal.entries.map((e) => ({ ...e,
    usage: usageByPath[pathKey(e.path)] || e.usage || { by_track: {}, tracks_count: 0, uses: 0 } }))
    .filter((r) => app.palettes.category === "ALL" ||
      (r.category || "").toUpperCase() === app.palettes.category);
  const paletteCounts = {};
  for (const entry of pal.entries)
    for (const role of entry.roles || [entry.role])
      if (role) paletteCounts[role] = (paletteCounts[role] || 0) + 1;

  const filters = h("div", { class: "filters" },
    cats.map((c) => h("button", {
      class: `fbtn ${app.palettes.category === c ? "active" : ""}`,
      onclick: () => { app.palettes.category = c; showScreen("palette"); },
    }, c)));

  const tbody = h("tbody", {});
  const trackIds = app.tracks.map((t) => t.track_id);
  for (const r of usageRows) {
    const meta = byPath[pathKey(r.path)] || r;
    r.sample_id = meta.sample_id || r.sample_id;
    r.by_track = r.usage.by_track || {};
    const roleNames = meta.roles || (meta.role ? [meta.role] : []);
    const roleControls = h("div", { class: "lib-role-controls" }, ...roleNames.map(role =>
      h("button", { class: "btn tiny", title: `Remove ${role} from this album palette`,
        onclick: async (e) => {
          e.stopPropagation();
          try { await api.delete(`/api/album/palette/roles/${encodeURIComponent(role)}`);
            app.album = await albumApi.get(); app.health = await albumApi.health(); updateShell();
            showScreen("palette"); toast(`REMOVED ${role.toUpperCase()} FROM ALBUM PALETTE`);
          } catch(err) { toast(`palette remove failed: ${err.message}`, "err"); }
        } }, `− ${role.toUpperCase()}`)));
    tbody.append(h("tr", { style: "cursor:pointer", onclick: () => showSampleDetail(r, meta) },
      h("td", { class: "dim" }, (meta.category || "—").toUpperCase()),
      h("td", {}, r.filename),
      h("td", { class: "num" }, meta.bpm == null ? "—" : Number(meta.bpm).toFixed(1)),
      h("td", {}, meta.key || "—"),
      h("td", { class: "mono faint" }, roleNames.join(" · ") || "—"),
      h("td", { class: "num" }, r.usage.tracks_count),
      h("td", { class: "num" }, r.usage.uses),
      h("td", {}, roleControls)));
  }

  const detailBox = h("div", { id: "sample-detail" });
  function showSampleDetail(r, meta) {
    detailBox.innerHTML = "";
    const barRows = app.tracks.map((t) => {
      const n = r.by_track?.[t.track_id] || 0;
      if (!n) return null;
      return h("div", { class: "usage-bar" },
        h("button", { class: "btn tiny", onclick: () => showScreen("tracks", t.track_id) }, `${t.track_id} ${tracksById[t.track_id]?.title || ""}`),
        h("div", { class: "cells" },
          h("i", { class: "on", style: `width:${Math.min(300, 30 + n * 12)}px` })),
        h("span", { class: "val" }, `${n} placements`));
    }).filter(Boolean);
    const wave = h("canvas", { class: "wave mini", style: "max-width:480px" });
    detailBox.append(panel(`USAGE MAP — ${r.filename}`,
      h("div", { class: "grid2" },
        h("div", {},
          kv([
            ["CATEGORY", (meta.category || "—").toUpperCase()],
            ["BPM", meta.bpm ? meta.bpm.toFixed(1) : "—"],
            ["KEY", meta.key || "—"],
            ["DURATION", meta.duration ? `${meta.duration.toFixed(2)} s` : "—"],
            ["ROLES", (meta.roles || []).join(", ") || "—"],
            ["LIBRARY", meta.library || "—"],
            ["SELECTION SCORE", meta.selection_score == null ? "not persisted" : Number(meta.selection_score).toFixed(2)],
          ]),
          meta.sample_id ? h("button", { class: "btn tiny", style: "margin-top:10px", onclick: () => openInAlbumLibrary(meta.sample_id) },
            "OPEN IN LIBRARY") : null,
          meta.path ? h("div", { class: "mono faint", style: "margin-top:6px;font-size:9.5px" }, meta.path) : null),
        h("div", {}, ...barRows))));
    if (meta.sample_id) {
      sampleApi.waveform(meta.sample_id, 400).then((p) => drawPeaks(wave, p,
        { color: "#6d4790", rmsColor: "#e078a8" })).catch(() => {});
      wave.style.maxWidth = "none";
      detailBox.lastChild.querySelector(".panel-b").prepend(wave);
    }
  }

  return h("div", {},
    h("div", { class: "screen-title" }, "SHARED SAMPLE PALETTE"),
    h("div", { class: "screen-sub" },
      `MODE ${pal.mode.toUpperCase()} · ${pal.entries.length} PALETTE SAMPLES · ${pal.shared_count} USED BY MULTIPLE TRACKS`),
    h("div", { class: "row", style: "margin-bottom:10px" },
      h("button", { class: "btn primary", onclick: () => showScreen("sampleLibrary") }, "OPEN SAMPLE LIBRARY")),
    panel("FILTER", filters),
    panel("ROLE COUNTS", h("div", { class: "filters" },
      ...Object.entries(paletteCounts).map(([role, count]) =>
        h("span", { class: "badge" }, `${role.toUpperCase()} ${count}`)))),
    panel(`ALBUM PALETTE · ${pal.entries.length} SAMPLES`, h("table", { class: "tech" },
      h("thead", {}, h("tr", {},
        h("th", {}, "TYPE"), h("th", {}, "SAMPLE"), h("th", {}, "BPM"),
        h("th", {}, "KEY"), h("th", {}, "ROLE"), h("th", {}, "TRACKS"),
        h("th", {}, "USES"), h("th", {}, "ACTIONS"))), tbody)),
    detailBox);
}

/* ======================================================== 06 SEQUENCE == */
async function screenSequence() {
  await ensureCore();
  const seq = await audioApi.sequence();
  app.seqDoc = seq;
  player.buildPlayheadPlan();
  const seqCfg = app.album.sequencing || {};
  const trCfg = app.album.transition || {};
  const ed = app.seqEdits || {
    mode: seqCfg.transition_mode || "gap",
    gap: seqCfg.gap_seconds ?? 2.0,
    xfade: trCfg.crossfade_seconds ?? seqCfg.crossfade_seconds ?? 4.0,
    quantize: trCfg.quantize || "none",
    curve: trCfg.curve || "equal_power",
    order: app.tracks.map((t) => t.track_id),
  };
  app.seqEdits = ed;
  const dirty = app.health.state === "DIRTY";

  // ---- controls
  const modeSeg = h("div", { class: "seg" },
    ["gap", "crossfade", "continuous"].map((m) => h("button", {
      class: ed.mode === m ? "active" : "",
      onclick: () => { ed.mode = m; app.seqEdits = ed; showScreen("sequence"); },
    }, m.toUpperCase())));
  const quantSeg = h("div", { class: "seg" },
    ["none", "beat", "bar", "phrase"].map((q) => h("button", {
      class: ed.quantize === q ? "active" : "",
      onclick: () => { ed.quantize = q; app.seqEdits = ed; showScreen("sequence"); },
    }, q.toUpperCase())));
  const curveSeg = h("div", { class: "seg" },
    ["equal_power", "linear"].map((c) => h("button", {
      class: ed.curve === c ? "active" : "",
      onclick: () => { ed.curve = c; app.seqEdits = ed; showScreen("sequence"); },
    }, c.replace("_", "-").toUpperCase())));
  const gapIn = h("input", { type: "number", step: "0.25", min: "0", value: ed.gap,
    disabled: ed.mode !== "gap",
    onchange: (e) => { ed.gap = Math.max(0, parseFloat(e.target.value) || 0); } });
  const xinIn = h("input", { type: "number", step: "0.25", min: "0", value: ed.xfade,
    disabled: ed.mode !== "crossfade",
    onchange: (e) => { ed.xfade = Math.max(0, parseFloat(e.target.value) || 0); } });

  const saveBtn = h("button", { class: "btn", onclick: async () => {
    try {
      await albumApi.saveSequencing({
        transition_mode: ed.mode, gap_seconds: ed.gap,
        crossfade_seconds: ed.xfade, quantize: ed.quantize,
        curve: ed.curve, order: ed.order,
      });
      toast("sequencing configuration written to album.json — re-sequence + render to apply");
      app.seqEdits = null;
      refreshHealth().then(() => showScreen("sequence"));
    } catch (e) { toast(`config rejected: ${e.message}`, "err"); }
  } }, "SAVE CONFIG");
  const seqBtn = h("button", { class: "btn primary", onclick: () => submitJob("resequence") }, "RE-SEQUENCE");
  const renderBtn = h("button", { class: "btn", onclick: () => submitJob("render") }, "RENDER");

  // ---- order editor
  const orderBox = h("div", {});
  function renderOrder() {
    orderBox.innerHTML = "";
    ed.order.forEach((id, i) => {
      const t = app.tracks.find((x) => x.track_id === id);
      const up = h("button", { class: "btn tiny", onclick: () => {
        if (i === 0) return;
        [ed.order[i - 1], ed.order[i]] = [ed.order[i], ed.order[i - 1]];
        app.seqEdits = ed; renderOrder(); drawTimeline();
      } }, "▲");
      const dn = h("button", { class: "btn tiny", onclick: () => {
        if (i === ed.order.length - 1) return;
        [ed.order[i + 1], ed.order[i]] = [ed.order[i], ed.order[i + 1]];
        app.seqEdits = ed; renderOrder(); drawTimeline();
      } }, "▼");
      orderBox.append(h("div", { class: "row", style: "padding:4px 0" },
        h("span", { class: "mono acc", style: "width:30px" }, String(i + 1).padStart(2, "0")),
        h("span", { class: "mono", style: "flex:1" }, `${id} ${t.title}`),
        h("span", { class: "mono dim" }, fmtTime(t.duration_seconds)),
        up, dn));
    });
  }
  renderOrder();  // ---- timeline preview (client-side projection of engine math)
  function projectTimeline() {
    return projectTimelineShared(ed.order, ed.mode, ed.gap, ed.xfade);
  }

  const canvas = h("canvas", { class: "seq-canvas" });
  const playhead = h("div", { class: "seq-playhead" });
  const playheadLabel = h("div", { class: "seq-playhead-label mono" }, "");
  let selBoundary = -1;
  async function drawTimeline() {
    const proj = projectTimeline();
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 900, ht = 300;
    canvas.width = w * dpr; canvas.height = ht * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.fillStyle = "#0b0e11"; ctx.fillRect(0, 0, w, ht);
    // ruler
    ctx.font = "9px Consolas, monospace";
    ctx.fillStyle = "#55626c"; ctx.strokeStyle = "rgba(125,139,150,.2)";
    const step = proj.total > 400 ? 60 : proj.total > 200 ? 30 : 10;
    for (let s = 0; s <= proj.total; s += step) {
      const x = (s / proj.total) * w;
      ctx.beginPath(); ctx.moveTo(x + .5, 18); ctx.lineTo(x + .5, ht); ctx.stroke();
      ctx.fillText(fmtClock(s), x + 3, 12);
    }
    const rowH = (ht - 26) / ed.order.length;
    const peaksCache = {};
    for (const [ri, id] of ed.order.entries()) {
      const b = proj.blocks.find((x) => x.id === id);
      const t = b.t;
      const x0 = (b.start / proj.total) * w;
      const bw = Math.max(4, (b.dur / proj.total) * w);
      const y = 26 + ri * rowH;
      ctx.fillStyle = "rgba(55,224,160,.14)";
      ctx.fillRect(x0, y, bw, rowH - 8);
      ctx.strokeStyle = "rgba(55,224,160,.5)";
      ctx.strokeRect(x0 + .5, y + .5, bw - 1, rowH - 9);
      // waveform
      try {
        if (!peaksCache[id]) peaksCache[id] = await trackApi.peaks(id, 300);
        const p = peaksCache[id];
        const clip = document.createElement("canvas");
        clip.width = Math.max(2, bw - 2); clip.height = rowH - 10;
        const cctx = clip.getContext("2d");
        const mid = clip.height / 2;
        cctx.fillStyle = "rgba(55,224,160,.75)";
        for (let i = 0; i < p.bins; i++) {
          const cx = (i / p.bins) * clip.width;
          const amp = Math.max(Math.abs(p.min[i]), Math.abs(p.max[i]));
          cctx.fillRect(cx, mid - amp * mid, Math.max(1, clip.width / p.bins - .3), amp * mid * 2);
        }
        ctx.drawImage(clip, x0 + 1, y + 1);
      } catch {}
      ctx.fillStyle = "#c8d3dc"; ctx.font = "10px Consolas, monospace";
      ctx.fillText(`${id} ${t.title}`, x0 + 5, y + 12);
      ctx.fillStyle = "#7d8b96";
      ctx.fillText(`${Math.round(t.bpm)} BPM`, x0 + 5, y + rowH - 14);
      // overlap / gap annotations
      if (b.overlap > 0.01) {
        ctx.fillStyle = "rgba(224,177,63,.85)";
        ctx.fillText(`◂ xfade ${b.overlap.toFixed(1)}s`, x0 + 5, y - 3);
      }
    }
    // gap regions
    for (const b of proj.blocks) {
      if (b.start > 0 && ed.mode === "gap") {
        const gx0 = ((b.start - ed.gap) / proj.total) * w;
        const gx1 = (b.start / proj.total) * w;
        ctx.fillStyle = "rgba(125,139,150,.12)";
        ctx.fillRect(gx0, 26, gx1 - gx0, ht - 26);
        ctx.fillStyle = "#7d8b96";
        ctx.fillText(`GAP ${ed.gap.toFixed(1)}s`, gx0 + 4, 24);
      }
    }
    player.buildPlayheadPlan();
    updatePlayhead(true);
  }

  /* live playhead: gap-mode albums map track-master playback onto exact
     album-time positions from the engine sequence document */
  function updatePlayhead(force = false) {
    const plan = player.playheadPlan;
    if (!plan || !canvas.isConnected) {
      playhead.style.display = "none";
      playheadLabel.textContent = "";
      return;
    }
    const at = player.playheadAlbumTime();
    if (at === null || at > plan.total) {
      if (playhead.style.display !== "none" || playheadLabel.textContent) {
        playhead.style.display = "none";
        playheadLabel.textContent = "";
      }
      return;
    }
    const x = (at / plan.total) * 100;
    if (!force && x === player.playheadLast) return;
    player.playheadLast = x;
    playhead.style.display = "";
    playhead.style.left = `${x}%`;
    const item = player.playingItem;
    playheadLabel.textContent = `${fmtClock(at)} · ${item ? item.name : ""}`;
  }
  const activeSequenceCanvas = canvas;
  function onSequencePlayerChanged() {
    if (app.screen !== "sequence" || !activeSequenceCanvas.isConnected) {
      document.removeEventListener("player-changed", onSequencePlayerChanged);
      return;
    }
    player.buildPlayheadPlan();
    updatePlayhead(true);
  }
  document.addEventListener("player-changed", onSequencePlayerChanged);
  const sequenceTimer = setInterval(() => {
    if (app.screen !== "sequence" || !activeSequenceCanvas.isConnected) {
      clearInterval(sequenceTimer);
      document.removeEventListener("player-changed", onSequencePlayerChanged);
      return;
    }
    updatePlayhead();
  }, 250);

  /* scrub: click the preview timeline to seek within the playing track
     (gap mode only — positions come from the engine sequence document) */
  canvas.onclick = (e) => {
    const plan = player.playheadPlan;
    const item = player.playingItem;
    if (plan && item) {
      const at = player.playheadAlbumTime();
      if (at !== null) {
        const r = canvas.getBoundingClientRect();
        const target = ((e.clientX - r.left) / r.width) * plan.total;
        const start = plan.map.get(String(item.id)) || 0;
        player.audio.currentTime = Math.max(0,
          Math.min(target - start, (player.audio.duration || Infinity) - 0.05));
        setTimeout(() => updatePlayhead(true), 60);
        return;
      }
    }
    const r = canvas.getBoundingClientRect();
    const frac = (e.clientX - r.left) / r.width;
    const proj = projectTimeline();
    const sec = frac * proj.total;
    const hit = proj.blocks.findIndex((b) => sec >= b.start && sec <= b.start + b.dur + 0.2);
    if (hit >= 0) showScreen("tracks", proj.blocks[hit].id);
  };

  // ---- stored sequence table (engine output when present)
  let storedTable = h("div", { class: "mono faint" },
    seq ? "" : "sequence.json not written yet — configure, save, then RE-SEQUENCE");
  if (seq) {
    storedTable = h("table", { class: "tech" },
      h("thead", {}, h("tr", {},
        h("th", {}, "#"), h("th", {}, "TRACK"), h("th", {}, "START"),
        h("th", {}, "END"), h("th", {}, "GAP"), h("th", {}, "OVERLAP"),
        h("th", {}, "BPM"))),
      h("tbody", {}, seq.entries.map((e, i) => h("tr", { class: "tr-row",
        onclick: () => showTransitionEditor(i) },
        h("td", {}, e.position),
        h("td", {}, `${e.track_id} ${e.title}`),
        h("td", { class: "num" }, `${fmtClock(e.start_seconds)}.${String(Math.round((e.start_seconds % 1) * 100)).padStart(2, "0")}`),
        h("td", { class: "num" }, fmtTime(e.end_seconds)),
        h("td", { class: "num" }, `${e.gap_before.toFixed(2)} s`),
        h("td", { class: "num" }, e.overlap_samples ? `${(e.overlap_samples / seq.sample_rate).toFixed(2)} s` : "—"),
        h("td", { class: "num" }, e.bpm.toFixed(1))))));
  }

  function showTransitionEditor(i) {
    if (!seq || !seq.transitions || !seq.transitions[i]) return;
    const tr = seq.transitions[i];
    const a = seq.entries[tr.source_position], b = seq.entries[tr.destination_position];
    openModal(`TRANSITION — TRACK ${a.track_id} → TRACK ${b.track_id}`, h("div", {},
      kv([
        ["MODE", tr.type.replace(/_/g, " ").toUpperCase()],
        ["DURATION", `${tr.duration_seconds.toFixed(3)} s`],
        ["QUANTIZE", (tr.quantize || "none").toUpperCase()],
        ["SOURCE BPM", `${tr.source_bpm.toFixed(1)} (track ${a.track_id})`],
        ["DESTINATION BPM", `${tr.destination_bpm.toFixed(1)} (track ${b.track_id})`],
        ["OVERLAP", tr.overlap_samples ? `${tr.overlap_samples} samples (${(tr.overlap_samples / seq.sample_rate).toFixed(3)} s)` : "—"],
        ["GAP", tr.gap_samples ? `${tr.gap_samples} samples (${(tr.gap_samples / seq.sample_rate).toFixed(3)} s)` : "—"],
        ["CURVE", (tr.curve || "—").replace("_", "-").toUpperCase()],
        ["BPM MISMATCH", tr.bpm_mismatch ? "YES — flagged by engine" : "no"],
        ["BOUNDARY (ALBUM)", `${fmtClock(b.start_seconds)} (${b.start_sample} samples)`],
      ]),
      h("div", { class: "mini-wave-pair", style: "margin-top:12px" },
        miniTransitionWave(a), miniTransitionWave(b)),
      h("div", { class: "mono faint", style: "margin-top:10px" },
        "boundary click diagnostics for this seam are on the QC screen (boundary_diagnostics)")));
  }
  function miniTransitionWave(entry) {
    const c = h("canvas", { class: "wave mini" });
    drawWhenConnected(c, trackApi.peaks(entry.track_id, 320),
      { color: "#1d6b50" });
    return h("div", {},
      h("div", { class: "mono faint", style: "margin-bottom:4px" },
        `TRACK ${entry.track_id} · ${fmtClock(entry.duration_seconds)}`), c);
  }

  const host = h("div", {},
    h("div", { class: "screen-title" }, "SEQUENCE EDITOR"),
    h("div", { class: "screen-sub" },
      `STORED ${seq ? `${seq.mode.toUpperCase()} · ${fmtTime(seq.duration_seconds)} · HASH ${seq.config_hash}` : "—"} · EDITS WRITE album.json`),
    dirty ? h("div", { class: "panel", style: "border-color:rgba(224,177,63,.4)" },
      h("div", { class: "panel-b mono amber" },
        "● DIRTY — configuration differs from the rendered sequence. SAVE + RE-SEQUENCE + RENDER to apply.")) : null,
    panel("SEQUENCE CONTROLS",
      h("div", { class: "seq-tools" },
        h("div", { class: "ctl-group" }, h("span", { class: "lbl" }, "MODE"), modeSeg),
        h("div", { class: "ctl-group" }, h("span", { class: "lbl" }, "QUANTIZE"), quantSeg),
        h("div", { class: "ctl-group" }, h("span", { class: "lbl" }, "CURVE"), curveSeg),
        h("div", { class: "ctl-group" }, h("span", { class: "lbl" }, "GAP S"), gapIn),
        h("div", { class: "ctl-group" }, h("span", { class: "lbl" }, "CROSSFADE S"), xinIn),
        h("div", { class: "ctl-group" }, saveBtn, seqBtn, renderBtn)),
      h("div", { class: "mono faint", style: "margin-top:8px" },
        "quantize snaps transition durations UP to the beat/bar/phrase grid of the source BPM (engine math)")),
    panel("ALBUM TIMELINE — PREVIEW",
      h("div", { class: "seq-wrap" }, canvas, playhead, playheadLabel),
      h("div", { class: "seq-hint" },
        "blocks are projected from real durations + current controls · click a block to open its track · when a track is playing, click the timeline to scrub (gap mode)")),
    h("div", { class: "grid2" },
      panel("TRACK ORDER (explicit — never filesystem)", orderBox),
      panel("STORED SEQUENCE (engine output)", storedTable)));

  queueMicrotask(drawTimeline);
  window.addEventListener("resize", () => {
    if (app.screen === "sequence") drawTimeline();
  });
  return host;
}

/* ------------------------------------------------- album picker ===== */
let pickerOpen = false;
async function openAlbumPicker() {
  if (pickerOpen) { closeAlbumPicker(); return; }
  pickerOpen = true;
  let doc;    try { doc = await albumApi.albums(); }
  catch (e) { pickerOpen = false; toast(`album library unavailable: ${e.message}`, "err"); return; }
  const items = doc.albums.map((a) => h("div", {
    class: `picker-item ${a.active ? "active" : ""}`,
    onclick: () => switchAlbum(a.dir),
  },
    h("div", { class: "pi-main" },
      h("div", { class: "pi-name" }, a.name,
        a.active ? h("span", { class: "pi-flag" }, "● ACTIVE") : null),
      h("div", { class: "pi-meta mono" },
        `${a.dir} · ${a.track_count} tracks · ${a.format || "—"} v${a.version || "—"}`
        + (a.bpm_center ? ` · ${Math.round(a.bpm_center)} bpm` : "")
        + (a.master_rendered ? ` · ${fmtTime(a.duration_seconds)} master` : " · no album master yet"))),
    h("div", { class: "pi-state mono faint" },
      a.sequence_mode.toUpperCase())));
  openModal("ALBUM LIBRARY", h("div", { class: "album-picker" },
    h("div", { class: "mono faint", style: "margin-bottom:10px" },
      "jobs and the audio API target the active album — switching takes effect immediately"),
    ...items));
}
function closeAlbumPicker() { pickerOpen = false; closeModal(); }
async function switchAlbum(dir) {
  try {
    const r = await api.post("/api/albums/switch", { dir });
    pickerOpen = false;
    closeModal();
    toast(`album switched → ${r.album_dir} — reloading`);
    player.stop();
    app.album = null; app.tracks = null; app.seqEdits = null; app.seqDoc = null;
    app.health = r.health;
    updateShell();
    showScreen("overview");
  } catch (e) {
    toast(`album switch failed: ${e.message}`, "err");
  }
}

/* ======================================================== 07 LOUDNESS == */
async function screenLoudness() {
  await ensureCore();
  const loud = await audioApi.loudness();
  const pol = app.album.loudness || { mode: "preserve", limiter: false,
    target_lufs: -14.0, limiter_ceiling_db: -1.0 };
  const ed = { ...pol };

  const modeSeg = h("div", { class: "seg" },
    ["preserve", "match", "target"].map((m) => h("button", {
      class: ed.mode === m ? "active" : "",
      onclick: () => { ed.mode = m;
        targetIn.disabled = m !== "target"; saveBtn.classList.add("primary");
        [...modeSeg.children].forEach((b) => b.classList.toggle("active", b.textContent === m.toUpperCase()));
      },
    }, m.toUpperCase())));
  const targetIn = h("input", { type: "number", step: "0.5", value: ed.target_lufs,
    disabled: ed.mode !== "target",
    onchange: (e) => { ed.target_lufs = parseFloat(e.target.value) || -14; } });
  const limChk = h("input", { type: "checkbox", checked: !!ed.limiter,
    onchange: (e) => { ed.limiter = e.target.checked; } });
  const ceilIn = h("input", { type: "number", step: "0.1", value: ed.limiter_ceiling_db,
    onchange: (e) => { ed.limiter_ceiling_db = parseFloat(e.target.value) || -1; } });
  const saveBtn = h("button", { class: "btn primary", onclick: async () => {
    try {
      await albumApi.saveLoudness({ mode: ed.mode, target_lufs: ed.target_lufs,
        limiter: ed.limiter, limiter_ceiling_db: ed.limiter_ceiling_db });
      toast(`loudness policy written: ${ed.mode.toUpperCase()} — render to apply`);
      refreshHealth().then(() => showScreen("loudness"));
    } catch (e) { toast(`policy rejected: ${e.message}`, "err"); }
  } }, "SAVE POLICY");
  const analyzeBtn = h("button", { class: "btn", onclick: () => submitJob("loudness") }, "ANALYZE");

  const method = loud?.method || "—";
  const note = loud?.method_note || "";

  // per-track measurement rows (bar scale -24..-6 LU)
  const LU_LO = -24, LU_HI = -6;
  const rows = h("div", {});
  if (loud?.tracks) {
    for (const t of loud.tracks) {
      const frac = Math.min(1, Math.max(0, (t.integrated_lufs_approx - LU_LO) / (LU_HI - LU_LO)));
      rows.append(h("div", { class: "loud-row" },
        h("div", { class: "nm" }, `${t.position} ${t.title}`),
        h("div", { class: "meter" }, h("div", { class: "fill", style: `transform:scaleX(${frac})` })),
        h("div", { class: "val" }, `${t.integrated_lufs_approx.toFixed(2)} LUFS*`),
        h("div", { class: "val" }, `${fmtDb(t.peak_dbfs, 1)} pk`)));
    }
  }

  // album short-term graph
  const stCanvas = h("canvas", { class: "wave tall" });
  if (loud?.album?.short_term_lufs) {
    queueMicrotask(() => {
      const dpr = window.devicePixelRatio || 1;
      const w = stCanvas.clientWidth || 900, ht = 110;
      stCanvas.width = w * dpr; stCanvas.height = ht * dpr;
      const ctx = stCanvas.getContext("2d");
      ctx.scale(dpr, dpr);
      ctx.fillStyle = "#0b0e11"; ctx.fillRect(0, 0, w, ht);
      const series = loud.album.short_term_lufs;
      const lo = -30, hi = -5;
      const X = (i) => (i / (series.length - 1)) * w;
      const Y = (v) => ht - ((v - lo) / (hi - lo)) * ht;
      ctx.strokeStyle = "rgba(125,139,150,.2)";
      for (const g of [-25, -20, -15, -10]) {
        ctx.beginPath(); ctx.moveTo(0, Y(g)); ctx.lineTo(w, Y(g)); ctx.stroke();
        ctx.fillStyle = "#55626c"; ctx.font = "9px Consolas";
        ctx.fillText(`${g}`, 4, Y(g) - 3);
      }
      ctx.strokeStyle = "#37e0a0"; ctx.lineWidth = 1.4;
      ctx.shadowColor = "rgba(55,224,160,.5)"; ctx.shadowBlur = 6;
      ctx.beginPath();
      series.forEach((v, i) => i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(0), Y(v)));
      ctx.stroke();
      ctx.shadowBlur = 0;
      if (loud.album.integrated_lufs_approx !== undefined) {
        ctx.strokeStyle = "rgba(224,177,63,.8)"; ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(0, Y(loud.album.integrated_lufs_approx));
        ctx.lineTo(w, Y(loud.album.integrated_lufs_approx)); ctx.stroke();
        ctx.setLineDash([]);
      }
    });
  }

  const noData = !loud;
  return h("div", {},
    h("div", { class: "screen-title" }, "ALBUM LOUDNESS"),
    h("div", { class: "screen-sub" },
      `POLICY ${pol.mode.toUpperCase()} · MEASUREMENT ${method}`),
    noData ? panel("NO MEASUREMENTS",
      h("div", { class: "mono dim" },
        "loudness.json not written yet — run ANALYZE to measure track masters + assembled album."),
      h("div", { style: "margin-top:10px" }, analyzeBtn)) :
    h("div", {},
      h("div", { class: "grid3" },
        panel("ALBUM — INTEGRATED", h("div", { class: "big-num" },
          loud.album.integrated_lufs_approx.toFixed(2), h("span", { class: "unit" }, " LUFS*"))),
        panel("ALBUM — TRUE PEAK*", h("div", { class: "big-num" },
          loud.album.true_peak_dbfs_approx.toFixed(2), h("span", { class: "unit" }, " dBTP"))),
        panel("ALBUM — DYNAMIC RANGE", h("div", { class: "big-num" },
          loud.album.loudness_range_lu.toFixed(2), h("span", { class: "unit" }, " LU")))),
      panel("MEASUREMENT METHOD",
        kv([["METHOD", method], ["NOTE", note]])),
      panel("PER-TRACK MEASUREMENT (not a ranking)",
        rows,
        h("div", { class: "loud-scale" },
          h("span", {}, `${LU_LO} LU`), h("span", {}, `${LU_HI} LU`))),
      panel("ALBUM SHORT-TERM LOUDNESS (3 s window · dashed = integrated)",
        stCanvas),
      h("div", { class: "grid2" },
        panel("LOUDNESS POLICY (album-level, non-destructive)",
          h("div", { class: "seq-tools" },
            h("div", { class: "ctl-group" }, h("span", { class: "lbl" }, "MODE"), modeSeg),
            h("div", { class: "ctl-group" }, h("span", { class: "lbl" }, "TARGET LUFS"), targetIn),
            h("div", { class: "ctl-group" }, h("span", { class: "lbl" }, "LIMITER"), limChk),
            h("div", { class: "ctl-group" }, h("span", { class: "lbl" }, "CEILING"), ceilIn),
            h("div", { class: "ctl-group" }, saveBtn, analyzeBtn)),
          h("div", { class: "mono faint", style: "margin-top:8px" },
            "preserve = masters unchanged · match = lift quieter tracks toward the loudest (boost-only, capped) · target = fixed offset toward the target. Gains are applied during assembly; source WAVs are never modified.")),
        panel("GAIN PROVENANCE (release manifest)",
          h("div", { id: "gain-prov", class: "mono dim" }, "loading…")))));
  (async () => {
    const rel = await releaseApi.doc();
    const box = $("gain-prov");
    if (!box) return;
    if (!rel) { box.textContent = "release manifest not written yet"; return; }
    box.innerHTML = "";
    for (const t of rel.tracks || []) {
      box.append(h("div", { class: "row", style: "padding:3px 0" },
        h("span", { style: "width:110px" }, `TRACK ${t.order} ${t.title}`),
        h("span", { class: t.album_gain_db ? "amber" : "acc" },
          `album_gain ${t.album_gain_db >= 0 ? "+" : ""}${t.album_gain_db.toFixed(2)} dB`)));
    }
    box.append(h("div", { class: "faint", style: "margin-top:8px" },
      `loudness_mode=${rel.provenance?.loudness_mode} · limiter=${rel.provenance?.limiter_applied}`));
  })();
}

/* ============================================================= 08 QC === */
async function screenQc() {
  await ensureCore();
  const qc = await qcApi.doc();
  const valWarnings = app.album.validation || [];
  app.qcFilter = null;

  if (!qc) {
    return h("div", {},
      h("div", { class: "screen-title" }, "ALBUM QC"),
      h("div", { class: "screen-sub" }, "qc.json not written yet"),
      panel("QC", h("div", { class: "mono dim" },
        "Run RENDER ALBUM — QC is produced during assembly."),
        h("div", { style: "margin-top:10px" },
          h("button", { class: "btn primary", onclick: () => submitJob("render") }, "RENDER ALBUM"))));
  }

  const catDefs = [
    ["STRUCTURE", ["sequence_structure", "expected_duration"]],
    ["AUDIO", ["sample_rate", "channels", "finite_samples", "no_clipping"]],
    ["LOUDNESS", ["loudness_policy_recorded", "gains_recorded"]],
    ["TRANSITIONS", qc.boundary_diagnostics?.map((b) => `boundary_${b.from}_${b.to}`) || []],
    ["SOURCE MASTERS", Object.keys(qc).filter((k) => k.startsWith("master_unchanged")).map(() => "master_unchanged")],
    ["REPRODUCIBILITY", ["verify"]],
  ];
  const checks = qc.checks || [];
  function catState(def) {
    let pass = 0, warn = 0, err = 0;
    for (const name of def[1]) {
      const c = checks.find((x) => x.check === name || x.check.startsWith(name));
      if (!c) continue;
      if (c.ok) pass++; else err++;
    }
    return { pass, warn, err, seen: pass + err };
  }
  const catRows = catDefs.map(([name, keys]) => {
    const st = catState([name, keys]);
    const word = st.err ? "FAIL" : st.seen ? "PASS" : "—";
    const kind = st.err ? "err" : st.seen ? "ok" : "";
    return h("div", { class: "qc-cat", onclick: () => {
      app.qcFilter = app.qcFilter === name ? null : name;
      diagBox.render(app.qcFilter);
    } },
      h("span", { class: "dim" }, name),
      badge(`● ${word}`, kind),
      h("span", { class: "mono faint" }, `${st.pass} pass / ${st.err} fail`));
  });

  const diags = [];
  for (const c of checks) {
    diags.push({ st: c.ok ? "PASS" : "ERROR", msg: `${c.check}${c.detail ? " — " + c.detail : ""}`,
      cat: catDefs.find(([, keys]) => keys.some((k) => c.check.startsWith(k)))?.[0] });
  }
  for (const b of qc.boundary_diagnostics || []) {
    diags.push({ st: b.status === "PASS" ? "PASS" : b.status === "WARN" ? "WARN" : "ERROR",
      msg: `boundary ${b.from}→${b.to} · ${b.mode} · seam disc ${b.discontinuity.toFixed(4)} vs context ${b.context_discontinuity.toFixed(4)} · peak ${b.peak_around} dBFS`,
      cat: "TRANSITIONS" });
  }
  for (const v of valWarnings) {
    diags.push({ st: v.severity === "error" ? "ERROR" : "WARN",
      msg: `album validation: ${v.message}`, cat: "STRUCTURE" });
  }

  const list = h("div", {});
  function renderDiags(filterCat) {
    list.innerHTML = "";
    for (const d of diags) {
      if (filterCat && d.cat !== filterCat) continue;
      list.append(h("div", { class: "diag" },
        h("span", { class: `st ${d.st}` }, d.st),
        h("span", { class: "msg" }, d.msg)));
    }
    if (!list.children.length) {
      list.append(h("div", { class: "diag faint mono" }, "no diagnostics in this category"));
    }
  }
  const diagBox = { render: renderDiags };
  renderDiags(null);

  const pass = qc.ok && !diags.some((d) => d.st === "ERROR");
  return h("div", {},
    h("div", { class: "screen-title" }, "ALBUM QC"),
    h("div", { class: "screen-sub" }, `FORMAT ${qc.format} v${qc.version} · ${qc.errors} ERRORS · WARNINGS NEVER HIDDEN`),
    panel("ALBUM QC STATUS", h("div", { class: "qc-status" },
      h("span", { class: `led ${pass ? "" : "red"}`, style: "width:14px;height:14px" }),
      h("span", { class: `word ${pass ? "pass" : "fail"}` }, pass ? "PASS" : "FAIL"),
      h("span", { class: "mono dim" },
        `${checks.length} checks · ${diags.filter((d) => d.st === "WARN").length} warnings · ${diags.filter((d) => d.st === "ERROR").length} errors`))),
    panel("CATEGORIES", ...catRows),
    panel(app.qcFilter ? `DIAGNOSTICS — ${app.qcFilter}` : "DIAGNOSTICS", list));
}

/* ====================================================== 09 RELEASE ===== */
async function screenRelease() {
  await ensureCore();
  const rel = await releaseApi.doc();
  const tree = await releaseApi.tree();
  const art = app.health.artifacts;

  const checks = [
    ["TRACKS VALID", app.tracks.every((t) => t.master_exists)],
    ["SOURCE MASTERS VERIFIED", art.qc],
    ["SEQUENCE VALID", art.sequence],
    ["LOUDNESS ANALYZED", art.loudness],
    ["ALBUM MASTER RENDERED", art.album_wav],
    ["CUE SHEET GENERATED", art.cue],
    ["MANIFEST GENERATED", art.release_manifest],
    ["RELEASE README GENERATED", art.release_readme],
  ];
  const checklist = h("div", { class: "checklist" },
    checks.map(([label, ok]) => h("div", { class: `ck ${ok ? "" : "todo"}` },
      h("span", { class: "mark" }, ok ? "✓" : "○"), h("span", {}, label),
      h("span", { class: "faint" }, ok ? "present" : "missing"))));

  const jobPanel = h("div", { id: "job-panel" });
  function renderJobs() {
    jobPanel.innerHTML = "";
    const snap = app.health.job;
    const defs = { render: "RENDER ALBUM", verify: "VERIFY ALBUM",
      loudness: "LOUDNESS", package: "PACKAGE", resequence: "SEQUENCE" };
    for (const [kind, label] of Object.entries(defs)) {
      const active = snap && snap.kind === kind && snap.status === "running";
      const stages = snap && snap.kind === kind ? snap.stages : null;
      jobPanel.append(h("div", { class: "jobline" },
        h("span", {}, label),
        h("span", { class: "row" }, stages ? stages.map((s) =>
          h("span", { class: `st ${s.state}`, style: "margin-right:10px" },
            `${s.name} ${s.state === "done" ? "✓" : s.state === "running" ? "…" : s.state === "failed" ? "✗" : ""}`)) :
          h("span", { class: "faint" }, "idle")),
        h("span", { class: `st ${active ? "running" : "pending"}` },
          active ? "RUNNING" : "—")));
    }
  }
  renderJobs();

  function renderTree(nodes, depth) {
    return nodes.map((n) => h("div", { style: `padding-left:${depth * 16}px` },
      n.bytes === null
        ? h("div", {}, h("span", { class: "dir" }, "▸ " + n.path))
        : h("div", {}, h("span", { class: "dim" }, n.path),
            h("span", { class: "bytes" }, fmtBytes(n.bytes))),
      n.children ? renderTree(n.children, depth + 1) : null));
  }

  const manifestBox = h("div", {});
  if (rel) {
    manifestBox.append(kv([
      ["ALBUM MASTER MD5", rel.audio?.album_master_hash],
      ["FORMAT", `${rel.audio?.channels} ch · ${(rel.audio?.sample_rate / 1000).toFixed(1)} kHz · ${rel.audio?.bit_depth}-bit`],
      ["SEQUENCE MODE", `${rel.sequence?.mode} · ${fmtTime(rel.sequence?.duration_seconds)}`],
      ["CONFIG HASH", rel.provenance?.configuration_hash],
      ["LOUDNESS MODE", rel.provenance?.loudness_mode],
      ["LIMITER", rel.provenance?.limiter_applied ? "applied" : "not applied"],
      ["GENERATOR", `v${rel.provenance?.generator_version}`],
    ]));
  } else {
    manifestBox.append(h("div", { class: "mono faint" },
      "release manifest not written yet — RENDER ALBUM produces it"));
  }

  const hashState = app.health.album_master_hash && app.health.release_manifest_hash
    ? (app.health.album_master_hash === app.health.release_manifest_hash
      ? badge("● MASTER MATCHES MANIFEST", "ok")
      : badge("● MASTER ≠ MANIFEST — RENDER REQUIRED", "warn"))
    : badge("○ UNVERIFIED", "");

  return h("div", {},
    h("div", { class: "screen-title" }, "RELEASE ASSEMBLY"),
    h("div", { class: "screen-sub" },
      `${app.album.name} · ${app.tracks.length} TRACKS · ${app.health.state}`),
    panel("OPERATIONS", h("div", { class: "row", style: "flex-wrap:wrap" },
      h("button", { class: "btn", onclick: () => submitJob("resequence") }, "SEQUENCE ALBUM"),
      h("button", { class: "btn primary", onclick: () => submitJob("render") }, "RENDER ALBUM"),
      h("button", { class: "btn", onclick: () => submitJob("verify") }, "VERIFY ALBUM"),
      h("button", { class: "btn", onclick: () => submitJob("loudness") }, "ANALYZE LOUDNESS"),
      h("button", { class: "btn", onclick: () => submitJob("package") }, "PACKAGE RELEASE"),
      h("span", { class: "spacer" }), hashState), jobPanel),
    panel("CHECKLIST", checklist),
    h("div", { class: "grid2" },
      panel("RELEASE MANIFEST", manifestBox),
      panel("PACKAGE TREE", h("div", { class: "tree" },
        renderTree(tree.album, 0), renderTree(tree.release, 0)))));
}

/* =========================================================== modal ===== */
function openModal(title, body) {
  let back = $("modal-back");
  if (!back) {
    back = h("div", { id: "modal-back" });
    back.append(h("div", { id: "modal" }));
    document.body.append(back);
    back.addEventListener("click", (e) => { if (e.target === back) closeModal(); });
  }
  const modal = back.firstChild;
  modal.innerHTML = "";
  modal.append(
    h("div", { class: "panel-h" }, title, h("span", { class: "spacer" }),
      h("span", { class: "x", onclick: closeModal }, "✕ CLOSE")),
    h("div", { class: "panel-b" }, body));
  back.classList.add("open");
}
function closeModal() {
  pickerOpen = false;
  $("modal-back")?.classList.remove("open");
}

/* ============================================================= jobs ==== */
async function submitJob(kind) {
  try {
    const r = await renderApi.submit(kind);
    if (!r.submitted) {
      toast(`a job is already running (${r.job?.label || "?"}) — wait for it to finish`, "warn");
      return;
    }
    toast(`${r.job.label} queued`);
    con.toggle(true);
    refreshHealth();
    setTimeout(pollJobStatus, 400);
  } catch (e) { toast(`job submit failed: ${e.message}`, "err"); }
}

async function pollJobStatus() {
  for (let i = 0; i < 600; i++) {
    await new Promise((r) => setTimeout(r, 900));
    try {
      const jobs = await renderApi.jobs();
      const cur = jobs.current;
      if (!cur) return;
      updateShell();
    } catch { return; }
  }
}

/* ======================================================== shortcuts ==== */
document.addEventListener("keydown", (e) => {
  const tag = (e.target.tagName || "").toLowerCase();
  if (["input", "textarea", "select"].includes(tag)) return;
  if (e.key === "`") { con.toggle(); e.preventDefault(); return; }
  if (e.key === "Escape") { closeModal(); return; }

  const seqLike = app.screen === "sequence";
  if (e.code === "Space") { e.preventDefault();
    player.audio.paused ? player.resume() : player.pause(); return; }
  if (e.key === "Enter") { player.stop(); return; }
  if (e.key === "ArrowRight") { player.seek(e.shiftKey ? 15 : 5); e.preventDefault(); return; }
  if (e.key === "ArrowLeft") { player.seek(e.shiftKey ? -15 : -5); e.preventDefault(); return; }
  if (app.screen === "sampleLibrary") {
    if (e.key === "f" || e.key === "F") {
      $("sample-library-search")?.focus(); e.preventDefault(); return;
    }
    if ((e.key === "a" || e.key === "A") && app.selectedSample) {
      const role = document.querySelector(".lib-add-role")?.value;
      if (role)          sampleApi.addToPalette(app.selectedSample, role)
        .then(async () => { app.album=await albumApi.get(); app.health=await albumApi.health(); updateShell(); toast(`ADDED TO ALBUM PALETTE · ${role.toUpperCase()}`); })
        .catch(err => toast(`palette add failed: ${err.message}`, "err"));
      return;
    }
    if (["ArrowDown", "ArrowUp"].includes(e.key)) {
      const rows = [...document.querySelectorAll(".lib-result")];
      const i = rows.findIndex(r => r.classList.contains("sel"));
      const nextRow = rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))];
      if (nextRow) { nextRow.click(); nextRow.scrollIntoView({block:"nearest"}); e.preventDefault(); }
      return;
    }
  }
  if (/^[1-9]$/.test(e.key)) {
    const t = app.tracks?.[+e.key - 1];
    if (t) showScreen("tracks", t.track_id);
    return;
  }
  if (e.key === "r" || e.key === "R") { submitJob("render"); return; }
  if (e.key === "v" || e.key === "V") { submitJob("verify"); return; }
  if (e.key === "q" || e.key === "Q") { showScreen("qc"); return; }
});

/* ============================================================= boot ==== */
async function boot() {
  player.init();
  document.querySelectorAll(".nav-item").forEach((b) =>
    b.addEventListener("click", () => showScreen(b.dataset.screen)));
  $("console-head").addEventListener("click", (e) => {
    if (e.target.id !== "console-clear") con.toggle();
  });
  $("console-clear").addEventListener("click", (e) => { e.stopPropagation(); con.clear(); });
  $("album-picker").addEventListener("click", openAlbumPicker);

  try {
    app.health = await albumApi.health();
    app.album = await albumApi.get();
  } catch (e) {
    $("screen").append(h("div", { class: "panel", style: "margin:30px" },
      h("div", { class: "panel-h" }, "BACKEND UNAVAILABLE"),
      h("div", { class: "panel-b mono red" }, String(e.message || e))));
    return;
  }
  updateShell();
  setInterval(refreshHealth, 2500);
  setInterval(() => con.poll(), 1200);
  con.poll();
  showScreen("overview");
}

boot();
