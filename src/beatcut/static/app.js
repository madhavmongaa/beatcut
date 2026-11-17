"use strict";

const $ = (sel) => document.querySelector(sel);
const MIN_GAP = 0.25;
const RULER = 22; // px of ruler above the waveform
const HIT = 7; // px either side of a marker that grabs it
const EDGE_TRIM = 0.5; // seconds skipped at clip edges when that option is on

const S = {
  clips: [],
  track: null,
  an: null, // analysis: beats, downbeats, levels, peaks
  cuts: [], // output times, relative to S.start
  segments: [],
  locks: [], // shots pinned to specific footage: {start, clip, clip_start}
  wanted: 30, // length asked for
  dur: 30, // length after clamping to the track
  start: 0, // where in the track the edit begins
  every: 2,
  meter: 4,
  aspect: "9:16",
  seed: Math.floor(Math.random() * 100000),
  pick: "motion",
  crop: "centre",
  match: false,
  trim: false,
  fast: false,
  edited: false, // cuts moved by hand
  fresh: false, // the preview matches the current edit
  busy: false,
  hover: -1,
  drag: -1,
  dragFrom: 0,
  sel: -1, // shot open in the picker
};

const audio = $("#audio");
const video = $("#video");
const canvas = $("#wave");
const ctx = canvas.getContext("2d");
const overview = $("#overview");
const octx = overview.getContext("2d");

/* ---------- server ---------- */

async function api(method, url, body) {
  const opts = { method };
  if (body instanceof Blob) opts.body = body;
  else if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers = { "Content-Type": "application/json" };
  }
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function status(text, isError = false) {
  const el = $("#status");
  el.textContent = text;
  el.classList.toggle("error", isError);
}

/* Everything the server needs to decide which footage fills the slots. */
function planBody() {
  return {
    cuts: S.cuts, duration: S.dur, start: S.start, meter: S.meter, seed: S.seed,
    pick: S.pick, crop: S.crop, match_energy: S.match, trim_edges: S.trim ? EDGE_TRIM : 0,
    locks: S.locks,
  };
}

/* ---------- saved project ---------- */

const SAVED = ["wanted", "every", "meter", "aspect", "seed", "pick", "crop", "match", "trim", "fast", "start", "cuts", "edited", "locks"];
let saveTimer = 0;
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    api("PUT", "/api/project", Object.fromEntries(SAVED.map((k) => [k, S[k]]))).catch(() => {});
  }, 400);
}

function restore(project) {
  for (const key of SAVED) if (project[key] !== undefined) S[key] = project[key];
  $("#duration").value = S.wanted;
  $("#seed").value = S.seed;
  $("#screen").dataset.aspect = S.aspect;
  for (const [id, key] of [["#every", "every"], ["#meter", "meter"], ["#aspect", "aspect"], ["#pick", "pick"], ["#crop", "crop"]]) {
    $(id).querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.v === String(S[key])));
  }
  for (const [id, key] of [["#match", "match"], ["#trim", "trim"], ["#fast", "fast"]]) {
    $(id).setAttribute("aria-pressed", String(!!S[key]));
  }
}

/* ---------- loading media ---------- */

async function addFiles(files) {
  const list = [...files];
  const tracks = list.filter((f) => f.type.startsWith("audio/") || /\.(mp3|wav|m4a|aac|flac|ogg|aiff?)$/i.test(f.name));
  const clips = list.filter((f) => !tracks.includes(f));
  let failed = 0;
  for (const [i, file] of clips.entries()) {
    status(`Adding clip ${i + 1} of ${clips.length}: ${file.name}`);
    try {
      applyState(await api("PUT", "/api/clips/" + encodeURIComponent(file.name), file));
    } catch (err) {
      failed++;
      status(err.message, true);
    }
  }
  if (tracks.length) {
    const file = tracks[0];
    status(`Finding beats in ${file.name}`);
    try {
      applyState(await api("PUT", "/api/track/" + encodeURIComponent(file.name), file));
      S.start = 0;
      await loadAnalysis();
      await regrid();
    } catch (err) {
      failed++;
      status(err.message, true);
    }
  } else if (clips.length) {
    await replan();
  }
  if (!failed) status(readyText());
}

function readyText() {
  if (!S.track && !S.clips.length) return "Drop clips and a track to start.";
  if (!S.track) return "Add a track.";
  if (!S.clips.length) return "Add some clips.";
  const locked = S.locks.length ? `, ${S.locks.length} locked` : "";
  return `${S.cuts.length} cuts across ${S.dur.toFixed(1)}s${locked}.`;
}

function applyState(state) {
  S.clips = state.clips;
  S.track = state.track;
  renderBin();
}

let audioFor = null;
async function loadAnalysis() {
  S.an = await api("GET", "/api/analysis?meter=" + S.meter);
  if (audioFor !== S.track) {
    audio.src = "/api/track?v=" + Date.now();
    audioFor = S.track;
  }
  document.body.classList.add("has-track");
  $("#bpm").textContent = S.an.tempo.toFixed(1);
  renderBin();
}

/* Ask for the default cut grid. Replaces hand-placed cuts and locks unless `keep` carries saved ones. */
async function regrid(start = S.start, keep = null) {
  if (!S.an) return;
  closePicker();
  const res = await api("POST", "/api/cuts", { duration: S.wanted, every: S.every, start, meter: S.meter });
  S.start = res.start;
  S.dur = res.duration;
  S.cuts = keep ? keep.cuts.filter((c) => c < S.dur - MIN_GAP) : res.cuts;
  S.locks = keep ? keep.locks : [];
  S.edited = !!keep;
  $("#total").textContent = "/ " + clock(S.dur);
  $("#from").textContent = clock(S.start);
  if (audio.paused) audio.currentTime = S.start;
  drawOverview();
  await replan();
}

/* Ask which clip fills each slot. Cheap: nothing is rendered. */
let planTicket = 0;
async function replan() {
  markStale();
  const ticket = ++planTicket;
  if (!S.an || !S.clips.length) {
    S.segments = [];
  } else {
    try {
      const res = await api("POST", "/api/plan", planBody());
      if (ticket !== planTicket) return;
      S.segments = res.segments;
      S.cuts = res.cuts;
      // Keep only the locks the server could honour.
      S.locks = res.segments.filter((s) => s.locked).map((s) => ({ start: s.start, clip: s.clip, clip_start: s.clip_start }));
      status(readyText());
    } catch (err) {
      if (ticket !== planTicket) return;
      S.segments = [];
      status(err.message, true);
    }
  }
  renderStrip();
  draw();
  syncControls();
  save();
}

function markStale() {
  if (S.fresh) video.pause();
  S.fresh = false;
  const screen = $("#screen");
  screen.classList.remove("fresh");
  screen.classList.toggle("stale", !!video.getAttribute("src"));
  $("#screen-note").textContent = video.getAttribute("src") ? "The edit changed. Preview again." : "Preview appears here";
}

function syncControls() {
  const ready = !!S.an && S.segments.length > 0 && !S.busy;
  $("#btn-preview").disabled = !ready;
  $("#btn-export").disabled = !ready;
  $("#play").disabled = !S.an;
  $("#loudest").disabled = !S.an;
  $("#reset-cuts").disabled = !S.edited && !S.locks.length;
}

/* ---------- bin ---------- */

const thumb = (clip, t) => `/api/thumb/${encodeURIComponent(clip)}?t=${t.toFixed(1)}`;

function renderBin() {
  $("#clips").replaceChildren(
    ...S.clips.map((c) => {
      const li = document.createElement("li");
      const img = new Image();
      img.src = thumb(c.name, c.duration / 2);
      img.alt = "";
      const name = Object.assign(document.createElement("span"), { className: "name", textContent: c.name, title: c.name });
      const time = Object.assign(document.createElement("time"), { textContent: c.duration.toFixed(1) + "s" });
      const rm = Object.assign(document.createElement("button"), { className: "rm", textContent: "×", title: "Remove clip" });
      rm.onclick = async () => {
        applyState(await api("DELETE", "/api/clips/" + encodeURIComponent(c.name)));
        await replan();
      };
      li.append(img, name, time, rm);
      return li;
    })
  );
  $("#clip-count").textContent = S.clips.length || "";
  $("#clips-empty").hidden = S.clips.length > 0;

  if (S.track) {
    const track = $("#track");
    track.className = "loaded";
    const name = Object.assign(document.createElement("span"), { textContent: S.track, title: S.track });
    const time = Object.assign(document.createElement("time"), { textContent: S.an ? clock(S.an.duration) : "" });
    track.replaceChildren(name, time);
  }
}

/* ---------- timeline drawing ---------- */

const css = getComputedStyle(document.documentElement);
const C = {
  text: css.getPropertyValue("--text").trim(),
  mid: css.getPropertyValue("--mid").trim(),
  dim: css.getPropertyValue("--dim").trim(),
  line: css.getPropertyValue("--line-strong").trim(),
  cut: css.getPropertyValue("--cut").trim(),
};

// Timeline positions are in output time: 0 is the start of the edit, S.start into the track.
const laneWidth = () => canvas.clientWidth;
const toX = (t) => (t / S.dur) * laneWidth();
const toT = (x) => (x / laneWidth()) * S.dur;

function clock(t) {
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return String(m).padStart(2, "0") + ":" + s.toFixed(2).padStart(5, "0");
}

function fit(cv, c) {
  const w = cv.clientWidth;
  const h = cv.clientHeight;
  const dpr = window.devicePixelRatio || 1;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
  }
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, w, h);
  return [w, h];
}

function peakBetween(t0, t1) {
  const { peaks, peak_rate: rate } = S.an;
  const a = Math.max(0, Math.floor(t0 * rate));
  const b = Math.max(a + 1, Math.ceil(t1 * rate));
  let peak = 0;
  for (let i = a; i < b && i < peaks.length; i++) if (peaks[i] > peak) peak = peaks[i];
  return peak / 255;
}

function draw() {
  const [w, h] = fit(canvas, ctx);
  if (!S.an) return;
  const { beats, downbeats, beat_levels: levels } = S.an;

  // Ruler: a tick per beat, taller on downbeats, seconds labelled.
  const downs = new Set(downbeats.map((t) => t.toFixed(3)));
  for (const t of beats) {
    const rel = t - S.start;
    if (rel < 0) continue;
    if (rel > S.dur) break;
    const down = downs.has(t.toFixed(3));
    ctx.fillStyle = down ? C.mid : C.line;
    ctx.fillRect(Math.round(toX(rel)), RULER - (down ? 9 : 4), 1, down ? 9 : 4);
  }
  const step = [1, 2, 5, 10, 15, 30, 60].find((s) => toX(s) >= 56) || 120;
  ctx.font = '10px ui-monospace, "SF Mono", Menlo, monospace';
  ctx.fillStyle = C.dim;
  ctx.textBaseline = "top";
  for (let t = 0; t < S.dur; t += step) ctx.fillText(t + "s", toX(t) + 3, 0);

  // Waveform: brightness shows energy. Loud sections cut faster, quiet ones slower.
  const mid = RULER + (h - RULER) / 2;
  const amp = (h - RULER) / 2 - 4;
  const shade = { "1": C.text, "0": C.mid, "-1": C.dim };
  let bi = 0;
  for (let x = 0; x < w; x += 3) {
    const t0 = S.start + toT(x);
    while (bi + 1 < beats.length && beats[bi + 1] <= t0) bi++;
    ctx.fillStyle = shade[levels[bi]] || C.mid;
    const bar = Math.max(1, peakBetween(t0, S.start + toT(x + 3)) * amp);
    ctx.fillRect(x, mid - bar, 2, bar * 2);
  }

  // Cut markers.
  S.cuts.forEach((t, i) => {
    const x = Math.round(toX(t));
    const hot = i === S.hover || i === S.drag;
    ctx.fillStyle = C.cut;
    ctx.fillRect(x, RULER - 2, hot ? 2 : 1, h - RULER + 2);
    ctx.beginPath();
    ctx.moveTo(x - 5, RULER - 12);
    ctx.lineTo(x + 6, RULER - 12);
    ctx.lineTo(x + 6, RULER - 5);
    ctx.lineTo(x + 0.5, RULER);
    ctx.lineTo(x - 5, RULER - 5);
    ctx.closePath();
    ctx.fill();
  });
}

/* The whole track, with a window marking the part the edit uses. */
function drawOverview() {
  const [w, h] = fit(overview, octx);
  if (!S.an) return;
  const total = S.an.duration;
  const x0 = (S.start / total) * w;
  const x1 = ((S.start + S.dur) / total) * w;
  for (let x = 0; x < w; x += 2) {
    const inside = x >= x0 && x < x1;
    octx.fillStyle = inside ? C.text : C.dim;
    const bar = Math.max(1, peakBetween((x / w) * total, ((x + 2) / w) * total) * (h / 2 - 2));
    octx.fillRect(x, h / 2 - bar, 1, bar * 2);
  }
  octx.strokeStyle = C.cut;
  octx.lineWidth = 1;
  octx.strokeRect(Math.round(x0) + 0.5, 0.5, Math.max(4, Math.round(x1 - x0) - 1), h - 1);
}

function renderStrip() {
  $("#strip").replaceChildren(
    ...S.segments.map((seg, i) => {
      const div = document.createElement("div");
      div.style.left = (seg.start / S.dur) * 100 + "%";
      div.style.width = ((seg.end - seg.start) / S.dur) * 100 + "%";
      div.style.backgroundImage = `url("${thumb(seg.clip, seg.clip_start)}")`;
      div.title = `${seg.clip} from ${seg.clip_start.toFixed(1)}s${seg.locked ? " (locked)" : ""}. Click to swap.`;
      div.classList.toggle("locked", seg.locked);
      div.classList.toggle("sel", i === S.sel);
      div.onclick = () => (S.sel === i ? closePicker() : openPicker(i));
      return div;
    })
  );
}

/* ---------- swapping one shot ---------- */

const sameSlot = (a, b) => Math.abs(a - b) < 0.02;

function setLock(start, clip, clipStart) {
  S.locks = S.locks.filter((l) => !sameSlot(l.start, start)).concat({ start, clip, clip_start: clipStart });
}

async function openPicker(i) {
  const seg = S.segments[i];
  if (!seg) return;
  S.sel = i;
  renderStrip();
  let options;
  try {
    options = (await api("POST", "/api/alternatives", { ...planBody(), slot: i })).alternatives;
  } catch (err) {
    status(err.message, true);
    return closePicker();
  }
  if (S.sel !== i) return;

  $("#picker-title").textContent = `Shot ${i + 1} of ${S.segments.length}: ${seg.clip}`;
  const lockBtn = $("#picker-lock");
  lockBtn.textContent = seg.locked ? "Unlock" : "Lock this shot";
  lockBtn.onclick = () => {
    if (seg.locked) S.locks = S.locks.filter((l) => !sameSlot(l.start, seg.start));
    else setLock(seg.start, seg.clip, seg.clip_start);
    closePicker();
    replan();
  };
  $("#picker-list").replaceChildren(
    ...options.map((opt) => {
      const btn = document.createElement("button");
      btn.style.backgroundImage = `url("${thumb(opt.clip, opt.clip_start)}")`;
      btn.title = `${opt.clip} from ${opt.clip_start.toFixed(1)}s`;
      btn.classList.toggle("on", opt.clip === seg.clip && Math.abs(opt.clip_start - seg.clip_start) < 0.05);
      btn.onclick = () => {
        setLock(seg.start, opt.clip, opt.clip_start);
        closePicker();
        replan();
      };
      return btn;
    })
  );
  const picker = $("#picker");
  picker.hidden = false;
  const centre = toX((seg.start + seg.end) / 2);
  picker.style.left = Math.max(0, Math.min(centre - picker.offsetWidth / 2, laneWidth() - picker.offsetWidth)) + "px";
}

function closePicker() {
  if (S.sel < 0) return;
  S.sel = -1;
  $("#picker").hidden = true;
  renderStrip();
}

document.addEventListener("pointerdown", (e) => {
  if (S.sel >= 0 && !e.target.closest("#picker, #strip")) closePicker();
});

/* ---------- marker editing ---------- */

function cutAt(x) {
  let best = -1;
  let bestDist = HIT + 1;
  S.cuts.forEach((t, i) => {
    const d = Math.abs(toX(t) - x);
    if (d < bestDist) [best, bestDist] = [i, d];
  });
  return best;
}

/* Nearest value in a sorted list of track times, returned as output time. */
function nearest(list, t) {
  let best = t;
  let bestDist = Infinity;
  for (const b of list) {
    const d = Math.abs(b - S.start - t);
    if (d < bestDist) [best, bestDist] = [b - S.start, d];
  }
  return best;
}

const localX = (e, el = canvas) => e.clientX - el.getBoundingClientRect().left;

canvas.addEventListener("pointerdown", (e) => {
  if (!S.an) return;
  const x = localX(e);
  const i = cutAt(x);
  if (i >= 0) {
    S.drag = i;
    S.dragFrom = S.cuts[i];
    canvas.setPointerCapture(e.pointerId);
  } else {
    seek(toT(x));
  }
  draw();
});

canvas.addEventListener("pointermove", (e) => {
  if (!S.an) return;
  const x = localX(e);
  if (S.drag < 0) {
    const i = cutAt(x);
    if (i !== S.hover) {
      S.hover = i;
      draw();
    }
    canvas.style.cursor = i >= 0 ? "ew-resize" : "text";
    return;
  }
  let t = toT(x);
  if (!e.shiftKey) t = nearest(S.an.beats, t);
  const lo = (S.drag > 0 ? S.cuts[S.drag - 1] : 0) + MIN_GAP;
  const hi = (S.drag < S.cuts.length - 1 ? S.cuts[S.drag + 1] : S.dur) - MIN_GAP;
  S.cuts[S.drag] = Math.min(hi, Math.max(lo, t));
  status(`Cut at ${clock(S.cuts[S.drag])}`);
  draw();
});

canvas.addEventListener("pointerup", () => {
  if (S.drag < 0) return;
  // A locked shot that starts at this cut moves with it.
  const moved = Math.round(S.cuts[S.drag] * 30) / 30;
  for (const lock of S.locks) if (sameSlot(lock.start, S.dragFrom)) lock.start = moved;
  S.drag = -1;
  S.edited = true;
  replan();
});

canvas.addEventListener("pointerleave", () => {
  if (S.hover >= 0 && S.drag < 0) {
    S.hover = -1;
    draw();
  }
});

canvas.addEventListener("dblclick", (e) => {
  if (!S.an) return;
  const x = localX(e);
  const i = cutAt(x);
  if (i >= 0) {
    S.cuts.splice(i, 1);
  } else {
    const t = e.shiftKey ? toT(x) : nearest(S.an.beats, toT(x));
    const clear = S.cuts.every((c) => Math.abs(c - t) >= MIN_GAP) && t >= MIN_GAP && t <= S.dur - MIN_GAP;
    if (!clear) return;
    S.cuts.push(t);
    S.cuts.sort((a, b) => a - b);
  }
  S.hover = -1;
  S.edited = true;
  replan();
});

/* ---------- choosing the part of the track ---------- */

let windowDrag = null; // seconds from the window's left edge to the pointer
function moveWindow(e) {
  const total = S.an.duration;
  const t = (localX(e, overview) / overview.clientWidth) * total;
  let start = Math.max(0, Math.min(t - windowDrag, total - S.dur));
  if (!e.shiftKey) start = Math.max(0, S.start + nearest(S.an.downbeats, start - S.start));
  S.start = Math.min(start, Math.max(0, total - S.dur));
  $("#from").textContent = clock(S.start);
  drawOverview();
  draw();
}

overview.addEventListener("pointerdown", (e) => {
  if (!S.an) return;
  const t = (localX(e, overview) / overview.clientWidth) * S.an.duration;
  const inside = t >= S.start && t <= S.start + S.dur;
  windowDrag = inside ? t - S.start : S.dur / 2;
  overview.setPointerCapture(e.pointerId);
  audio.pause();
  video.pause();
  moveWindow(e);
});
overview.addEventListener("pointermove", (e) => {
  if (windowDrag !== null) moveWindow(e);
});
overview.addEventListener("pointerup", () => {
  if (windowDrag === null) return;
  windowDrag = null;
  regrid(S.start);
});

$("#loudest").onclick = () => regrid("auto");

/* ---------- playback ---------- */

// The preview video is the player while it matches the edit; otherwise the bare track.
function now() {
  return S.fresh ? video.currentTime || 0 : (audio.currentTime || 0) - S.start;
}

function seek(t) {
  t = Math.max(0, Math.min(t, S.dur - 0.05));
  if (S.fresh) video.currentTime = t;
  else audio.currentTime = S.start + t;
  tick();
}

function toggle() {
  if (!S.an) return;
  const p = S.fresh ? video : audio;
  if (!p.paused) return p.pause();
  const t = now();
  if (t < 0 || t >= S.dur - 0.05) seek(0);
  p.play();
}

function tick() {
  const p = S.fresh ? video : audio;
  let t = now();
  if (t >= S.dur && !p.paused) {
    p.pause();
    seek(0);
    t = 0;
  }
  t = Math.max(0, Math.min(t, S.dur));
  $("#playhead").style.transform = `translateX(${toX(t)}px)`;
  $("#now").textContent = clock(t);
  $("#play-icon").setAttribute("d", p.paused ? "M2 1l9 5-9 5z" : "M2 1h3v10H2zM7 1h3v10H7z");
  $("#play").setAttribute("aria-label", p.paused ? "Play" : "Pause");
}

(function loop() {
  if (S.an) tick();
  requestAnimationFrame(loop);
})();

$("#play").onclick = toggle;
video.onclick = toggle;
video.addEventListener("play", () => audio.pause());
audio.addEventListener("play", () => video.pause());

document.addEventListener("keydown", (e) => {
  if (e.code === "Escape") return closePicker();
  if (e.code !== "Space" || e.target.matches("input")) return;
  e.preventDefault();
  toggle();
});

/* ---------- render ---------- */

async function render(mode) {
  if (S.busy) return;
  S.busy = true;
  closePicker();
  syncControls();
  audio.pause();
  video.pause();
  const screen = $("#screen");
  const bar = $("#progress span");
  bar.style.width = "0";
  screen.classList.add("busy");
  status(mode === "export" ? "Exporting at full resolution" : "Rendering preview");
  const poll = setInterval(async () => {
    const p = await api("GET", "/api/progress").catch(() => null);
    if (p && p.total) bar.style.width = (p.done / p.total) * 100 + "%";
  }, 300);
  try {
    const res = await api("POST", "/api/render", { ...planBody(), aspect: S.aspect, mode, fast: S.fast });
    if (mode === "export") {
      const a = document.createElement("a");
      a.href = res.url + "&download=1";
      a.download = "beatcut.mp4";
      a.click();
      status("Exported. Check your Downloads folder for beatcut.mp4.");
    } else {
      video.src = res.url;
      S.fresh = true;
      screen.classList.remove("stale");
      screen.classList.add("fresh");
      video.play().catch(() => {});
      status("Preview ready. This is a low-resolution draft; export for full quality.");
    }
  } catch (err) {
    status(err.message, true);
  } finally {
    clearInterval(poll);
    screen.classList.remove("busy");
    S.busy = false;
    syncControls();
  }
}

$("#btn-preview").onclick = () => render("preview");
$("#btn-export").onclick = () => render("export");

/* ---------- settings ---------- */

function segmented(id, onPick) {
  $(id).addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    $(id).querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
    onPick(btn.dataset.v);
  });
}

function checkbox(id, key, onChange) {
  $(id).addEventListener("click", () => {
    S[key] = !S[key];
    $(id).setAttribute("aria-pressed", String(S[key]));
    onChange();
  });
}

segmented("#every", (v) => {
  S.every = Number(v);
  regrid();
});
segmented("#meter", async (v) => {
  S.meter = Number(v);
  await loadAnalysis();
  regrid();
});
segmented("#aspect", (v) => {
  S.aspect = v;
  $("#screen").dataset.aspect = v;
  markStale();
  status(readyText());
  save();
});
segmented("#pick", (v) => {
  S.pick = v;
  replan();
});
segmented("#crop", (v) => {
  S.crop = v;
  replan();
});
checkbox("#match", "match", replan);
checkbox("#trim", "trim", replan);
checkbox("#fast", "fast", save);

$("#duration").addEventListener("change", (e) => {
  const v = Math.max(1, Number(e.target.value) || 30);
  e.target.value = v;
  S.wanted = v;
  regrid();
});

$("#seed").value = S.seed;
$("#seed").addEventListener("change", (e) => {
  S.seed = Math.max(0, Math.floor(Number(e.target.value) || 0));
  e.target.value = S.seed;
  replan();
});
// Shuffle picks new footage for every shot that is not locked.
$("#reroll").onclick = () => {
  S.seed = Math.floor(Math.random() * 100000);
  $("#seed").value = S.seed;
  replan();
};
$("#reset-cuts").onclick = () => regrid();

// Clearing the project asks for a second click instead of a dialog.
let armed = 0;
$("#new-project").onclick = async (e) => {
  if (Date.now() - armed > 4000) {
    armed = Date.now();
    e.target.textContent = "Click again to remove all clips and settings";
    setTimeout(() => (e.target.textContent = "New project"), 4000);
    return;
  }
  await api("POST", "/api/reset");
  location.reload();
};

/* ---------- drag and drop, file pickers ---------- */

let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
  e.preventDefault();
  dragDepth++;
  document.body.classList.add("dragging");
});
window.addEventListener("dragleave", () => {
  if (--dragDepth <= 0) {
    dragDepth = 0;
    document.body.classList.remove("dragging");
  }
});
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove("dragging");
  if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
});

$("#add-clips").onclick = () => $("#file-clips").click();
$("#add-track").onclick = () => $("#file-track").click();
for (const id of ["#file-clips", "#file-track"]) {
  $(id).addEventListener("change", (e) => {
    addFiles(e.target.files);
    e.target.value = "";
  });
}

window.addEventListener("resize", () => {
  draw();
  drawOverview();
});

/* ---------- start ---------- */

(async function start() {
  try {
    const state = await api("GET", "/api/state");
    const saved = state.project;
    if (saved) restore(saved);
    if (!state.hardware) {
      S.fast = false;
      $("#fast").disabled = true;
      $("#fast").title = "Hardware encoding is not available on this machine.";
    }
    applyState(state);
    if (S.track) {
      await loadAnalysis();
      const keep = saved && Array.isArray(saved.cuts) && (saved.edited || (saved.locks || []).length)
        ? { cuts: saved.cuts, locks: saved.locks || [] }
        : null;
      await regrid(S.start, keep);
      if (keep) S.edited = !!saved.edited;
      syncControls();
    } else {
      await replan();
    }
    status(readyText());
  } catch (err) {
    status(err.message, true);
  }
})();
