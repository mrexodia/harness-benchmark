// Scripted evaluator: plays one finished game the same way every time, driven
// by window.fishing.state, and writes results.json, screenshots and video.
//
//   node evaluate.mjs --root <served folder> --out <output folder> [--url http://127.0.0.1:8000/]
//
// The page must already be served at --url (entry.sh starts the server).
import { chromium } from "playwright";
import { PNG } from "pngjs";
import { execFileSync } from "node:child_process";
import { readFileSync, readdirSync, statSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { join, relative, extname } from "node:path";

const argv = Object.fromEntries(process.argv.slice(2).reduce((a, v, i, arr) => (v.startsWith("--") ? [...a, [v.slice(2), arr[i + 1]]] : a), []));
const ROOT = argv.root || "/tmp/ws";
const OUT = argv.out || "/out";
const URL_ = argv.url || "http://127.0.0.1:8000/";
// Set by entry.sh when the game was built in a single subfolder and served from there.
const SERVED_FROM = argv["served-from"] || ".";
const SHOTS = join(OUT, "screenshots");
mkdirSync(SHOTS, { recursive: true });

const RARITIES = ["Common", "Uncommon", "Rare", "Epic", "Legendary", "Ancient"];
const EXPECTED = { Common: 4500, Uncommon: 2500, Rare: 1500, Epic: 800, Legendary: 500, Ancient: 200 };
const PHASES = ["ready", "waiting", "bite", "reeling", "reveal"];
const POLL_MS = 50;
const T = { load: 10000, leaveReady: 2000, bite: 20000, reveal: 10000, ready: 8000, missReturn: 20000, settle: 30000 };

// ------------------------------------------------------------------ checks

const CHECKS = [
  ["loads", "gate", "Loads"],
  ["playable_loop", "gate", "Playable loop"],
  ["interface", "scored", "Interface present"],
  ["no_errors", "scored", "No runtime errors"],
  ["offline", "scored", "Offline"],
  ["no_audio", "scored", "No audio"],
  ["code_art", "scored", "Code-drawn art"],
  ["layout", "scored", "Layout"],
  ["odds", "scored", "Odds"],
  ["log", "scored", "Log"],
  ["miss", "scored", "Miss"],
  ["duplicate_input", "scored", "Duplicate input"],
  ["overlapping_casts", "scored", "Overlapping casts"],
  ["hook_used", "scored", "Hook is used"],
  ["fresh_random", "scored", "Fresh random input"],
  ["hidden_pause", "scored", "Hidden-tab pause"],
  // Scored only for runs whose prompt asks for it (hc passes HC_LOG_TIMING=scored).
  ["log_timing", process.env.HC_LOG_TIMING === "scored" ? "scored" : "reported", "Log waits for reveal"],
  ["cycle_time", "reported", "Cycle time"],
  ["pixel_grid", "reported", "Pixel grid"],
  ["onscreen_control", "reported", "On-screen control"],
  ["game_folder", "reported", "Game at the project root"],
];
const checks = Object.fromEntries(CHECKS.map(([k, type, label]) => [k, { label, type, status: "skipped", detail: null }]));
const set = (k, status, detail = null, extra = {}) => Object.assign(checks[k], { status, detail, ...extra });
const notes = [];
const timeline = [];
const consoleLines = [];
const pageErrors = [];
const requests = [];
let videoT0 = Date.now();
const vt = () => +((Date.now() - videoT0) / 1000).toFixed(2);
const mark = (event, extra = {}) => timeline.push({ t: vt(), event, ...extra });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ------------------------------------------------------------------ static scan

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (name === ".git") continue;
    const p = join(dir, name);
    let st;
    try { st = statSync(p); } catch { continue; }
    if (st.isDirectory()) walk(p, out);
    else out.push(p);
  }
  return out;
}

function scanAssets() {
  const ASSET_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "svg", "avif", "tif", "tiff", "psd", "ase", "aseprite",
    "ttf", "otf", "woff", "woff2", "eot", "fnt", "mp3", "wav", "ogg", "m4a", "aac", "flac", "mid", "midi", "mp4", "webm", "mov", "m4v"]);
  const SRC_EXT = new Set(["html", "htm", "js", "mjs", "cjs", "ts", "jsx", "tsx", "css", "json", "py", "md", "txt"]);
  const files = walk(ROOT).map((p) => relative(ROOT, p));
  const inDeps = (f) => f.split("/").includes("node_modules");
  const assetFiles = files.filter((f) => ASSET_EXT.has(extname(f).slice(1).toLowerCase()));
  const hits = [];
  for (const f of files) {
    if (inDeps(f) || !SRC_EXT.has(extname(f).slice(1).toLowerCase()) || /(^|\/)package-lock\.json$/.test(f)) continue;
    let text;
    try { text = readFileSync(join(ROOT, f), "utf8"); } catch { continue; }
    if (text.length > 5_000_000) { hits.push({ file: f, line: 0, kind: "large-file", text: `${text.length} bytes` }); continue; }
    text.split("\n").forEach((line, i) => {
      const found = [];
      if (/<img\b/i.test(line)) found.push("<img>");
      if (/data:image\//i.test(line)) found.push("data:image");
      for (const m of line.matchAll(/\b(?:https?|wss?):\/\/([^\s/'"`)<>:]+)/gi)) {
        const host = m[1].toLowerCase();
        if (["127.0.0.1", "localhost", "0.0.0.0", "www.w3.org"].includes(host)) continue;
        found.push(`url:${host}`);
      }
      if (found.length) hits.push({ file: f, line: i + 1, kind: found.join(","), text: line.trim().slice(0, 200) });
    });
  }
  const outside = assetFiles.filter((f) => !inDeps(f));
  const depAssets = assetFiles.filter(inDeps);
  const detail = {
    source_files_scanned: files.filter((f) => !inDeps(f)).length,
    asset_files: outside,
    asset_files_in_node_modules: depAssets.length,
    source_hits: hits.slice(0, 200),
    source_hit_count: hits.length,
  };
  codeArtPending = { detail, hits };
}

// Decided after the session: files the game never loads (an agent's own test
// screenshots, scripts and notes) do not count. An asset counts when the page
// requested it or a file the page loaded names it; a source hit counts only in
// a file the page loaded. If the page loaded nothing, everything counts.
let codeArtPending = null;
function finishCodeArt() {
  if (!codeArtPending) return;
  const { detail, hits } = codeArtPending;
  const loaded = new Set();
  for (const r of requests) {
    let u;
    try { u = new URL(r.url); } catch { continue; }
    if (u.host !== "127.0.0.1:8000") continue;
    let path = decodeURIComponent(u.pathname).replace(/^\/+/, "");
    if (path === "" || path.endsWith("/")) path += "index.html";
    loaded.add(path);
  }
  const all = !loaded.size;
  const loadedText = [...loaded].map((f) => { try { return readFileSync(join(ROOT, f), "utf8"); } catch { return ""; } }).join("\n");
  const base = (f) => f.split("/").pop();
  const used = detail.asset_files.filter((f) => all || loaded.has(f) || loadedText.includes(base(f)));
  const live = hits.filter((h) => all || loaded.has(h.file));
  Object.assign(detail, {
    asset_files: used,
    unused_asset_files: detail.asset_files.filter((f) => !used.includes(f)),
    source_hits: live.slice(0, 200),
    source_hit_count: live.length,
    hits_in_files_not_loaded: hits.length - live.length,
    files_loaded_by_page: loaded.size,
  });
  if (used.length || live.length) set("code_art", "review", detail, { note: "Hits need manual review; counted as not passed until overridden." });
  else set("code_art", "pass", detail);
}

// ------------------------------------------------------------------ browser helpers

let page;

async function ev(fn, arg, timeout = 5000) {
  let timer;
  const t = new Promise((_, rej) => { timer = setTimeout(() => rej(new Error("page.evaluate timed out (main thread blocked?)")), timeout); });
  try { return await Promise.race([page.evaluate(fn, arg), t]); } finally { clearTimeout(timer); }
}

const getState = () => ev(() => { try { return window.fishing ? window.fishing.state : undefined; } catch (e) { return "__error__"; } }).catch(() => "__timeout__");

async function waitState(pred, timeout) {
  const t0 = Date.now();
  let s;
  for (;;) {
    s = await getState();
    if (pred(s)) return { ok: true, state: s, ms: Date.now() - t0 };
    if (Date.now() - t0 >= timeout) return { ok: false, state: s, ms: Date.now() - t0 };
    await sleep(POLL_MS);
  }
}

const logInfo = () => ev(() => {
  const l = window.fishing && window.fishing.log;
  if (!Array.isArray(l)) return { len: null, first: null };
  const f = l[0];
  return { len: l.length, first: f ? { name: typeof f.name === "string" ? f.name : null, rarity: f.rarity } : null };
}).catch(() => ({ len: null, first: null }));

// Count catches added since a snapshot by finding the snapshot's newest entry
// in the current log (identity), so a log capped at some length still shows
// new catches. Falls back to the length difference when entries are copies.
const snapLog = () => ev(() => {
  const l = window.fishing && window.fishing.log;
  const t = window.__hcTrack;
  t.snaps = t.snaps || [];
  const key = (e) => JSON.stringify([e && e.name, e && e.rarity]);
  t.snaps.push({ first: Array.isArray(l) ? l[0] : undefined, len: Array.isArray(l) ? l.length : null, keys: Array.isArray(l) ? l.map(key) : [] });
  return t.snaps.length - 1;
});
const catchesSince = (id) => ev((id) => {
  const l = window.fishing && window.fishing.log;
  const snap = window.__hcTrack.snaps[id];
  if (!Array.isArray(l) || snap.len == null) return { count: null, method: "no log" };
  if (snap.first === undefined) return { count: l.length, method: "length" };
  const k = l.indexOf(snap.first);
  if (k >= 0) return { count: k, method: "identity" };
  // Entries are copies: find the smallest shift at which the old entries line up.
  const key = (e) => JSON.stringify([e && e.name, e && e.rarity]);
  const now = l.map(key), old = snap.keys;
  for (let shift = 0; shift < now.length; shift++) {
    const n = Math.min(old.length, now.length - shift);
    if (n >= 3 || n === old.length) {
      let ok = true;
      for (let i = 0; i < n && ok; i++) ok = now[shift + i] === old[i];
      if (ok) return { count: shift, method: "content" };
    }
  }
  return { count: l.length - snap.len, method: "length" };
}, id);

// In-page sampler: records every state transition at ~5 ms resolution so short
// phases (reeling) are seen even though the driver polls every 50 ms.
async function installTracker() {
  await ev(() => {
    const h = (window.__hcTrack = { hist: [], last: Symbol("none"), us: [], logs: [], lastLen: undefined });
    const sample = () => {
      let s;
      try { s = window.fishing ? window.fishing.state : undefined; } catch { s = "__error__"; }
      if (s !== h.last) { h.last = s; h.hist.push([Math.round(performance.now()), s]); }
      // Log length, sampled together with the state, so we can tell which
      // state the game was in when a catch entered the log.
      let n;
      try { n = Array.isArray(window.fishing?.log) ? window.fishing.log.length : undefined; } catch { n = undefined; }
      if (n !== h.lastLen) { h.logs.push([Math.round(performance.now()), n, s, h.lastLen]); h.lastLen = n; }
    };
    // Timers can be starved on a busy page, so also sample every frame and
    // just before any key or pointer input reaches the game's own listeners.
    setInterval(sample, 5);
    const frame = () => { sample(); requestAnimationFrame(frame); };
    requestAnimationFrame(frame);
    for (const t of ["keydown", "keyup", "pointerdown", "pointerup", "click"]) {
      window.addEventListener(t, sample, { capture: true });
      window.addEventListener(t, () => queueMicrotask(sample), { capture: false });
    }
  });
}
const histLen = () => ev(() => window.__hcTrack.hist.length);
const histSince = (i) => ev((i) => window.__hcTrack.hist.slice(i).map((x) => x[1]), i);

async function press(n = 1) {
  for (let i = 0; i < n; i++) await page.keyboard.press("Space");
}

let focusFallbackUsed = false;

async function shot(name) {
  try { await page.screenshot({ path: join(SHOTS, name), timeout: 5000 }); return name; } catch (e) { notes.push(`screenshot ${name} failed: ${e.message}`); return null; }
}

// Bring the game back to "ready" without pressing anything during a bite.
async function settle() {
  const s = await getState();
  if (s === "ready") return true;
  if (s === "reveal") {
    const w = await waitState((x) => x === "ready", T.ready);
    if (w.ok) return true;
    await press(1);
  }
  return (await waitState((x) => x === "ready", T.settle)).ok;
}

async function playCycle({ label, hookDelayMs = 0, catchIt = true, pressesAtReady = 1, pressesAtBite = 1, shotAtBite = null, shotAtReveal = null, onWaiting = null }) {
  const r = { label, ok: false };
  mark(`cycle-start:${label}`);
  const ready = await waitState((s) => s === "ready", T.ready);
  if (!ready.ok) { r.error = `not ready before cast (state ${ready.state})`; return r; }
  r.logBefore = await logInfo();
  r.uBefore = await ev(() => window.__hcTrack.us.length);
  const h0 = await histLen();
  r.h0 = h0;
  const lg0 = await ev(() => window.__hcTrack.logs.length);
  const tCast = Date.now();
  await press(pressesAtReady);
  let w = await waitState((s) => s !== "ready", T.leaveReady);
  if (!w.ok) {
    // Some games want focus or a first press to dismiss the instruction.
    r.extraPressNeeded = true;
    if (!focusFallbackUsed) { focusFallbackUsed = true; await page.focus("canvas").catch(() => {}); }
    await press(1);
    w = await waitState((s) => s !== "ready", T.leaveReady);
    if (!w.ok) { r.error = "Space at ready did not cast"; r.hist = await histSince(h0); return r; }
  }
  if (onWaiting) await onWaiting(r);
  w = await waitState((s) => s === "bite", T.bite);
  if (!w.ok) { r.error = `no bite within ${T.bite / 1000}s (state ${w.state})`; r.hist = await histSince(h0); return r; }
  r.biteAfterS = (Date.now() - tCast) / 1000;
  mark(`bite:${label}`);
  if (!catchIt) {
    if (shotAtBite) await shot(shotAtBite);
    const back = await waitState((s) => s === "ready", T.missReturn);
    r.hist = await histSince(h0);
    r.logAfter = await logInfo();
    r.ok = back.ok;
    if (!back.ok) r.error = `did not return to ready after missed bite (state ${back.state})`;
    // Leave the aftermath on screen (splash, "got away" text, etc.) before the
    // next cast, so the recording shows how the game reacts to a miss.
    if (back.ok) { mark(`miss-ready:${label}`); await sleep(2500); }
    mark(`cycle-end:${label}`);
    return r;
  }
  if (hookDelayMs) {
    // React like a person rather than instantly, so the bite cue is on screen
    // for a moment. Stops early (and presses) if the window starts closing.
    const t0 = Date.now();
    while (Date.now() - t0 < hookDelayMs && (await getState()) === "bite") await sleep(20);
    r.hookDelayS = (Date.now() - t0) / 1000;
  }
  await press(pressesAtBite);
  w = await waitState((s) => s === "reveal", T.reveal);
  if (!w.ok) { r.error = `no reveal within ${T.reveal / 1000}s (state ${w.state})`; r.hist = await histSince(h0); r.logAfter = await logInfo(); return r; }
  r.cycleS = (Date.now() - tCast) / 1000 - (r.hookDelayS || 0);
  mark(`reveal:${label}`);
  if (shotAtReveal) { await sleep(400); r.shot = await shot(shotAtReveal); r.layoutAtReveal = await measureLayout(); }
  w = await waitState((s) => s === "ready", T.ready);
  if (!w.ok && w.state === "reveal") {
    r.revealNeededDismissal = true;
    await press(1);
    w = await waitState((s) => s === "ready", T.ready);
  }
  r.hist = await histSince(h0);
  r.logAfter = await logInfo();
  r.uCalls = await ev((i) => window.__hcTrack.us.slice(i), r.uBefore);
  r.logEvents = await ev((i) => window.__hcTrack.logs.slice(i), lg0);
  r.ok = w.ok;
  if (!w.ok) r.error = `did not return to ready after reveal (state ${w.state})`;
  mark(`cycle-end:${label}`);
  return r;
}

const dedupe = (a) => a.filter((s, i) => i === 0 || s !== a[i - 1]);
function isSubsequence(seq, of) {
  let i = 0;
  for (const s of of) if (s === seq[i]) i++;
  return i === seq.length;
}

const measureLayout = () => ev(() => ({
  scrollWidth: Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0),
  scrollHeight: Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0),
  innerWidth, innerHeight,
  // The viewport scrolls with the root's overflow, or the body's when the root's is visible.
  overflow: (() => {
    const cs = (el) => (el ? getComputedStyle(el) : null);
    const h = cs(document.documentElement), b = cs(document.body);
    const pick = (ax) => (h && h[ax] !== "visible" ? h[ax] : b ? b[ax] : "visible");
    return { x: pick("overflowX"), y: pick("overflowY") };
  })(),
})).catch(() => null);

function catchLogOk(r, expectRarity = null) {
  const b = r.logBefore?.len, a = r.logAfter?.len, f = r.logAfter?.first;
  const problems = [];
  if (b == null || a == null) problems.push("log is not an array");
  else if (a !== b + 1) problems.push(`log grew by ${a - b}, expected 1`);
  if (!f) problems.push("log[0] missing");
  else {
    if (!RARITIES.includes(f.rarity)) problems.push(`log[0].rarity invalid: ${JSON.stringify(f.rarity)}`);
    if (!f.name) problems.push("log[0].name missing");
    if (expectRarity && f.rarity !== expectRarity) problems.push(`log[0].rarity ${f.rarity} != forced ${expectRarity}`);
  }
  return problems;
}

// ------------------------------------------------------------------ pixel grid

// Art drawn on a 4 px grid has its colour changes only on block borders, so at
// the right offset few 4x4 blocks contain an internal edge, while at the other
// offsets most edge blocks do. score = 1 - best/worst (1 = perfect grid, ~0 = none).
function gridScore(png, rect) {
  const x0 = Math.max(0, Math.round(rect?.x ?? 0)), y0 = Math.max(0, Math.round(rect?.y ?? 0));
  const x1 = Math.min(png.width, Math.round(rect ? rect.x + rect.width : png.width));
  const y1 = Math.min(png.height, Math.round(rect ? rect.y + rect.height : png.height));
  const d = png.data;
  const same = (a, b) => Math.abs(d[a] - d[b]) + Math.abs(d[a + 1] - d[b + 1]) + Math.abs(d[a + 2] - d[b + 2]) <= 12;
  const idx = (x, y) => (y * png.width + x) * 4;
  const counts = [];
  for (let oy = 0; oy < 4; oy++) for (let ox = 0; ox < 4; ox++) {
    let mixed = 0, total = 0;
    for (let by = y0 + oy; by + 4 <= y1; by += 4) for (let bx = x0 + ox; bx + 4 <= x1; bx += 4) {
      total++;
      const ref = idx(bx, by);
      let uniform = true;
      for (let y = by; y < by + 4 && uniform; y++) for (let x = bx; x < bx + 4; x++) if (!same(ref, idx(x, y))) { uniform = false; break; }
      if (!uniform) mixed++;
    }
    counts.push({ ox, oy, mixed, total });
  }
  const best = counts.reduce((a, b) => (b.mixed < a.mixed ? b : a));
  const worst = Math.max(...counts.map((c) => c.mixed));
  return { score: worst ? +(1 - best.mixed / worst).toFixed(3) : null, offset: [best.ox, best.oy],
    mixed_blocks_at_best: +(best.mixed / best.total).toFixed(3), mixed_blocks_at_worst: +(worst / best.total).toFixed(3) };
}

// ------------------------------------------------------------------ main session

async function main() {
  scanAssets();

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    deviceScaleFactor: 1,
    recordVideo: { dir: join(OUT, "video-tmp"), size: { width: 1280, height: 720 } },
  });
  // No audio: wrap every way a page can make sound before any page script runs.
  // Wrappers record the use and then behave exactly like the originals.
  await context.addInitScript(() => {
    const uses = (window.__hcAudio = []);
    const rec = (what) => { if (uses.length < 100) uses.push({ what, t: Math.round(performance.now()) }); };
    for (const name of ["AudioContext", "webkitAudioContext", "OfflineAudioContext", "webkitOfflineAudioContext", "Audio"]) {
      const C = window[name];
      if (typeof C !== "function") continue;
      window[name] = new Proxy(C, { construct(target, args, nt) { rec(`new ${name}`); return Reflect.construct(target, args, nt); } });
    }
    const play = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function () { rec(`${this.tagName.toLowerCase()}.play()`); return play.apply(this, arguments); };
    const create = Document.prototype.createElement;
    Document.prototype.createElement = function (tag) {
      if (typeof tag === "string" && tag.toLowerCase() === "audio") rec("createElement('audio')");
      return create.apply(this, arguments);
    };
    if (window.speechSynthesis) {
      const speak = speechSynthesis.speak.bind(speechSynthesis);
      speechSynthesis.speak = (u) => { rec("speechSynthesis.speak()"); return speak(u); };
    }
    document.addEventListener("DOMContentLoaded", () => {
      if (document.querySelector("audio")) rec("<audio> element in page");
    });
  });
  page = await context.newPage();
  videoT0 = Date.now();

  page.on("console", (m) => consoleLines.push({ t: vt(), type: m.type(), text: m.text(), url: m.location()?.url || "" }));
  page.on("pageerror", (e) => pageErrors.push({ t: vt(), message: String(e?.stack || e) }));
  page.on("request", (r) => requests.push({ t: vt(), method: r.method(), url: r.url(), type: r.resourceType() }));
  page.on("websocket", (w) => requests.push({ t: vt(), method: "WS", url: w.url(), type: "websocket" }));

  let gateFailed = false;
  let canPlay = false;
  let showcase = { start: null, end: null };
  try {
    // ---- Loads (gate)
    const t0 = Date.now();
    let loadErr = null;
    try { await page.goto(URL_, { waitUntil: "load", timeout: T.load }); } catch (e) { loadErr = e.message; }
    let root = null;
    if (!loadErr) {
      try {
        const left = Math.max(500, T.load - (Date.now() - t0));
        const el = await page.waitForSelector("canvas, #game, #app, #root, [id*=game i], [class*=game i]", { state: "visible", timeout: left });
        root = await el.evaluate((e) => e.tagName.toLowerCase() + (e.id ? `#${e.id}` : ""));
      } catch (e) { loadErr = `no visible canvas or game root: ${e.message.split("\n")[0]}`; }
    }
    mark("loaded");
    if (loadErr) { set("loads", "fail", { error: loadErr }); gateFailed = true; throw new Stop(); }
    set("loads", "pass", { root, ms: Date.now() - t0 });

    // ---- Interface
    await sleep(300);
    const iface = await ev(() => {
      const f = window.fishing;
      return {
        exists: !!f && typeof f === "object",
        state: f ? f.state : undefined,
        pickRarity: f ? typeof f.pickRarity : undefined,
        logIsArray: f ? Array.isArray(f.log) : false,
      };
    }).catch((e) => ({ exists: false, error: e.message }));
    const stateOk = PHASES.includes(iface.state);
    const problems = [];
    if (!iface.exists) problems.push("window.fishing missing");
    else {
      if (!stateOk) problems.push(`state is ${JSON.stringify(iface.state)}`);
      if (iface.pickRarity !== "function") problems.push("pickRarity is not a function");
      if (!iface.logIsArray) problems.push("log is not an array");
    }
    set("interface", problems.length ? "fail" : "pass", { ...iface, problems });
    canPlay = iface.exists && stateOk;
    await installTracker().catch(() => {});

    // ---- Odds sweep on the original pickRarity
    if (iface.pickRarity === "function") {
      const sweep = await ev(() => {
        const orig = window.fishing.pickRarity;
        window.__hcTrack.orig = orig;
        const logLen = Array.isArray(window.fishing.log) ? window.fishing.log.length : null;
        const names = [];
        let thrown = null;
        try { for (let i = 0; i < 10000; i++) names.push(orig.call(window.fishing, i / 10000)); } catch (e) { thrown = String(e); }
        let nondeterministic = 0;
        if (!thrown) for (let i = 0; i < 10000; i += 7) if (orig.call(window.fishing, i / 10000) !== names[i]) nondeterministic++;
        const counts = {}, runs = [];
        names.forEach((n, i) => { counts[n] = (counts[n] || 0) + 1; if (i === 0 || names[i - 1] !== n) runs.push(n); });
        const logAfter = Array.isArray(window.fishing.log) ? window.fishing.log.length : null;
        return { counts, runs, thrown, nondeterministic, logChanged: logLen !== logAfter };
      }, undefined, 15000).catch((e) => ({ thrown: e.message }));
      const p = [];
      if (sweep.thrown) p.push(`threw: ${sweep.thrown}`);
      else {
        const names = Object.keys(sweep.counts);
        if (names.length !== 6 || !names.every((n) => RARITIES.includes(n))) p.push(`names returned: ${JSON.stringify(names)}`);
        for (const r of RARITIES) if (Math.abs((sweep.counts[r] || 0) - EXPECTED[r]) > 1) p.push(`${r}: ${sweep.counts[r] || 0} (expected ${EXPECTED[r]})`);
        if (sweep.runs.length !== new Set(sweep.runs).size) p.push(`tiers not contiguous: ${sweep.runs.join(" > ")}`);
        if (sweep.nondeterministic) p.push(`not pure: ${sweep.nondeterministic} differing repeat calls`);
        if (sweep.logChanged) p.push("calling pickRarity changed the log");
      }
      set("odds", p.length ? "fail" : "pass", { ...sweep, problems: p });
    } else {
      set("odds", "fail", { problems: ["pickRarity is not a function"] });
    }

    if (!canPlay) {
      set("playable_loop", "unverifiable", { reason: "window.fishing.state missing or invalid; showcase must be recorded by hand" });
      for (const k of ["log", "log_timing", "miss", "duplicate_input", "overlapping_casts", "hook_used", "fresh_random", "hidden_pause"]) set(k, "unverifiable", { reason: "script cannot play without window.fishing.state" });
      notes.push("FALLBACK: record the showcase by hand in the same order, without forced tiers.");
      mark("idle-start");
      await sleep(5000);
      await shot("idle.png");
      const lay = await measureLayout();
      setLayout([lay]);
      throw new Stop();
    }

    // Wrap the original pickRarity to record each u during natural catches.
    const wrapped = await ev(() => {
      const t = window.__hcTrack;
      const orig = t.orig || window.fishing.pickRarity;
      t.wrapper = function (u) { t.us.push(u); return orig.apply(this, arguments); };
      try { window.fishing.pickRarity = t.wrapper; } catch {}
      return window.fishing.pickRarity === t.wrapper;
    }).catch(() => false);
    if (!wrapped) notes.push("could not replace window.fishing.pickRarity (read-only?)");

    // ================= Showcase segment
    showcase.start = vt();
    mark("showcase-start");
    await sleep(5000);
    await shot("idle.png");
    const layouts = [await measureLayout()];

    const natural = [];
    {
      const r = await playCycle({ label: "natural-1" });
      natural.push(r);
      const seq = dedupe(r.hist || []);
      const want = ["ready", "waiting", "bite", "reeling", "reveal", "ready"];
      const pass = r.ok && isSubsequence(want, ["ready", ...seq]);
      set("playable_loop", pass ? "pass" : "fail", { states: seq, error: r.error || null, cycle_s: r.cycleS ?? null });
      if (!pass) gateFailed = true;
      else if (!r.ok) await settle();
    }

    if (!gateFailed) {
      // Deliberate miss (also measures how long the bite window stays open)
      const miss = await playCycle({ label: "miss", catchIt: false, shotAtBite: "bite.png" });
      const missLogSame = miss.logBefore?.len === miss.logAfter?.len;
      const missWentToCatch = (miss.hist || []).some((s) => s === "reeling" || s === "reveal");
      set("miss", miss.ok && missLogSame && !missWentToCatch ? "pass" : "fail",
        { states: dedupe(miss.hist || []), log_before: miss.logBefore?.len, log_after: miss.logAfter?.len, error: miss.error || null });
      const biteWindowMs = await ev((i) => {
        const h = window.__hcTrack.hist.slice(i);
        const k = h.findIndex((x) => x[1] === "bite");
        return k >= 0 && h[k + 1] ? h[k + 1][0] - h[k][0] : null;
      }, miss.h0).catch(() => null);
      if (!miss.ok) await settle();

      // Natural catches 2 and 3 hook after a human-like pause (40% of the bite
      // window, at most 700 ms), so the bite cue shows in the catch clip.
      const hookDelayMs = Math.round(Math.min(700, 0.4 * (biteWindowMs ?? 750)));
      for (let i = 2; i <= 3; i++) {
        const r = await playCycle({ label: `natural-${i}`, hookDelayMs });
        natural.push(r);
        if (!r.ok) await settle();
      }
      notes.push(`bite window ${biteWindowMs ?? "unknown"} ms; natural-2/3 hooked after ${hookDelayMs} ms`);

      // Ambience: 6 s untouched at ready, after the first catches have dismissed
      // any start screen, so the scene itself can be judged.
      await waitState((s) => s === "ready", T.ready);
      mark("ambience-start");
      await sleep(3000);
      await shot("ambience.png");
      await sleep(3000);
      mark("ambience-end");

      // Forced tiers, Common to Ancient
      const forced = [];
      for (const tier of RARITIES) {
        await ev((tier) => { try { window.fishing.pickRarity = () => tier; } catch {} }, tier);
        const r = await playCycle({ label: `forced-${tier}`, shotAtReveal: `reveal-${tier.toLowerCase()}.png` });
        await ev(() => { try { window.fishing.pickRarity = window.__hcTrack.wrapper; } catch {} });
        r.tier = tier;
        forced.push(r);
        if (r.layoutAtReveal) layouts.push(r.layoutAtReveal);
        if (!r.ok) await settle();
      }
      showcase.end = vt() + 0.5;
      mark("showcase-end");

      // Hook is used
      const hookProblems = forced.filter((r) => r.logAfter?.first?.rarity !== r.tier || !r.ok)
        .map((r) => `${r.tier}: got ${JSON.stringify(r.logAfter?.first?.rarity)}${r.error ? ` (${r.error})` : ""}`);
      set("hook_used", hookProblems.length ? "fail" : "pass", { problems: hookProblems });
      if (hookProblems.length && checks.odds.status === "pass") {
        set("odds", "unverifiable", { ...checks.odds.detail, reason: "game does not use window.fishing.pickRarity, so the sweep says nothing about real odds" });
      }

      // Log after every catch
      const logProblems = [];
      for (const r of [...natural, ...forced]) {
        const p = catchLogOk(r, r.tier || null);
        if (p.length) logProblems.push(`${r.label}: ${p.join("; ")}`);
      }
      set("log", logProblems.length ? "fail" : "pass", { catches_checked: natural.length + forced.length, problems: logProblems });

      // Log timing: a catch must not enter the log before the reveal is shown.
      {
        const early = [], seen = [];
        for (const r of [...natural, ...forced]) {
          const grow = (r.logEvents || []).filter(([, n, , prev]) => typeof n === "number" && typeof prev === "number" && n > prev);
          if (!grow.length) continue;
          seen.push(r.label);
          const bad = grow.find(([, , st]) => ["waiting", "bite", "reeling"].includes(st));
          if (bad) early.push(`${r.label}: log grew during "${bad[2]}"`);
        }
        set("log_timing", !seen.length ? "unverifiable" : early.length ? "fail" : "pass", { catches_checked: seen.length, problems: early });
      }

      // Fresh random input
      const us = natural.map((r) => r.uCalls || []);
      const flat = us.flat();
      const fp = [];
      us.forEach((u, i) => { if (u.length !== 1) fp.push(`natural-${i + 1}: pickRarity called ${u.length} times`); });
      if (flat.some((u) => typeof u !== "number" || !(u >= 0 && u < 1))) fp.push(`u out of [0,1): ${JSON.stringify(flat)}`);
      if (new Set(flat).size !== flat.length) fp.push(`repeated u: ${JSON.stringify(flat)}`);
      set("fresh_random", fp.length ? "fail" : "pass", { u: flat, problems: fp });

      // Cycle time (reported)
      const times = natural.filter((r) => r.cycleS != null).map((r) => +r.cycleS.toFixed(2));
      const mean = times.length ? +(times.reduce((a, b) => a + b, 0) / times.length).toFixed(2) : null;
      set("cycle_time", times.length ? "measured" : "unverifiable", { seconds: times, mean, target: [5, 9], in_target: mean != null && mean >= 5 && mean <= 9 });

      const dismissals = [...natural, ...forced].filter((r) => r.revealNeededDismissal).map((r) => r.label);
      if (dismissals.length) notes.push(`reveal needed dismissal: ${dismissals.join(", ")}`);
      const extra = [...natural, ...forced, miss].filter((r) => r.extraPressNeeded).map((r) => r.label);
      if (extra.length) notes.push(`Space at ready needed a second press: ${extra.join(", ")}`);

      // ================= Robustness segment
      mark("robustness-start");
      // Duplicate input: five rapid presses in the bite window.
      {
        await settle();
        const snap = await snapLog();
        const r = await playCycle({ label: "duplicate-input", pressesAtBite: 5 });
        await settle();
        const c = await catchesSince(snap);
        set("duplicate_input", c.count === 1 ? "pass" : "fail", { new_catches: c.count, counted_by: c.method, states: dedupe(r.hist || []), error: r.error || null });
      }
      // Overlapping casts: five rapid presses at ready.
      {
        await settle();
        const snap = await snapLog();
        const h0 = await histLen();
        const r = await playCycle({ label: "overlapping-casts", pressesAtReady: 5 });
        await settle();
        const c = await catchesSince(snap);
        const hist = dedupe(await histSince(h0));
        const bites = hist.filter((s) => s === "bite").length;
        const grew = c.count ?? 0;
        set("overlapping_casts", c.count != null && grew <= bites && grew <= 1 ? "pass" : "fail", { new_catches: c.count, counted_by: c.method, bites, states: hist, error: r.error || null });
      }
      // Hidden-tab pause: 20 s simulated hidden during "waiting".
      {
        await settle();
        const snap = await snapLog();
        let stayed = null;
        const r = await playCycle({
          label: "hidden-tab",
          onWaiting: async (r) => {
            // Allow a short cast animation before "waiting"; an off-contract state
            // name is the Interface check's business, not this one's.
            const w0 = await waitState((s) => s === "waiting", 3000);
            if (!w0.ok) { stayed = { ok: false, reason: `state never reached waiting after cast (state ${w0.state})` }; return; }
            await ev(() => {
              Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
              Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
              document.dispatchEvent(new Event("visibilitychange"));
            });
            mark("hidden-start");
            const seen = new Set();
            const t0 = Date.now();
            while (Date.now() - t0 < 20000) { seen.add(await getState()); await sleep(250); }
            await ev(() => {
              delete document.hidden;
              delete document.visibilityState;
              document.dispatchEvent(new Event("visibilitychange"));
            });
            mark("hidden-end");
            stayed = { ok: seen.size === 1 && seen.has("waiting"), states_while_hidden: [...seen] };
          },
        });
        await settle();
        const c = await catchesSince(snap);
        const pass = stayed?.ok && r.ok && c.count === 1;
        set("hidden_pause", pass ? "pass" : "fail", { ...stayed, caught_after: c.count === 1, counted_by: c.method, error: r.error || null });
      }
      // On-screen control (reported).
      {
        await settle();
        const btn = await page.evaluateHandle(() => {
          const vis = (e) => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e); return r.width > 4 && r.height > 4 && cs.visibility !== "hidden" && cs.display !== "none" && +cs.opacity > 0.05; };
          const all = [...document.querySelectorAll("button, [role=button], input[type=button], input[type=submit]")].filter(vis);
          return all.find((e) => /cast|reel|fish|hook|go|action|start|throw/i.test(e.textContent + " " + (e.getAttribute("aria-label") || "") + " " + (e.value || ""))) || all[0] || null;
        });
        const el = btn.asElement();
        if (!el) set("onscreen_control", "flag", { reason: "no DOM button found; check the canvas UI by hand" });
        else {
          const text = (await el.evaluate((e) => (e.textContent || e.value || e.getAttribute("aria-label") || "").trim().slice(0, 60))) || null;
          const before = await getState();
          await el.click({ timeout: 3000 }).catch(() => {});
          const w = await waitState((s) => s === "waiting", 1500);
          set("onscreen_control", w.ok ? "pass" : "fail", { button_text: text, state_before: before, state_after: w.state });
          await settle();
        }
      }
      mark("robustness-end");
    }
    setLayout(layouts);
    // Interface: every state value seen during play must be one of the five.
    if (canPlay) {
      const extra = await ev((ph) => [...new Set(window.__hcTrack.hist.map((x) => x[1]))].filter((s) => !ph.includes(s)), PHASES).catch(() => []);
      if (extra.length) {
        const c = checks.interface;
        c.detail.problems.push(`state took values outside the five: ${extra.map((x) => JSON.stringify(x)).join(", ")}`);
        c.detail.extra_states = extra;
        c.status = "fail";
      }
    }
  } catch (e) {
    if (!(e instanceof Stop)) { notes.push(`evaluator error: ${e.stack || e}`); }
  }

  // ---- Session-wide checks
  const ignorable = (c) => /\/favicon\.ico$/.test(c.url) && /404|Failed to load resource/.test(c.text);
  const errs = consoleLines.filter((c) => c.type === "error" && !ignorable(c));
  set("no_errors", errs.length || pageErrors.length ? "fail" : "pass", { console_errors: errs.slice(0, 50), page_errors: pageErrors.slice(0, 50) });
  const external = requests.filter((r) => {
    try {
      const u = new URL(r.url);
      if (["data:", "blob:", "about:", "chrome-extension:"].includes(u.protocol)) return false;
      return !["127.0.0.1", "localhost"].includes(u.hostname);
    } catch { return true; }
  });
  set("offline", external.length ? "fail" : "pass", { external_requests: external.slice(0, 50), total_requests: requests.length });
  const audio = await ev(() => window.__hcAudio || null).catch(() => null);
  if (audio === null) set("no_audio", "unverifiable", { reason: "could not read the audio record" });
  else set("no_audio", audio.length ? "fail" : "pass", { uses: audio });

  // Pixel grid (reported). Prefer the canvas's own pixels (no DOM overlays);
  // fall back to the idle screenshot cropped to the largest canvas.
  try {
    const canvases = await ev(() => [...document.querySelectorAll("canvas")].map((c) => {
      const r = c.getBoundingClientRect();
      return { width: c.width, height: c.height, x: r.x, y: r.y, cssWidth: r.width, cssHeight: r.height, imageRendering: getComputedStyle(c).imageRendering };
    }));
    const order = canvases.map((c, i) => [i, c]).sort((a, b) => b[1].cssWidth * b[1].cssHeight - a[1].cssWidth * a[1].cssHeight);
    const [bigIdx, big] = order[0] || [null, null];
    const exact = canvases.some((c) => c.width === 320 && c.height === 180 && Math.abs(c.cssWidth - 1280) <= 1 && Math.abs(c.cssHeight - 720) <= 1);
    let grid = null, source = null;
    if (big && Math.abs(big.width - big.cssWidth) <= 1 && Math.abs(big.height - big.cssHeight) <= 1) {
      const url = await ev((i) => { try { return document.querySelectorAll("canvas")[i].toDataURL("image/png"); } catch { return null; } }, bigIdx, 10000).catch(() => null);
      if (url) { grid = gridScore(PNG.sync.read(Buffer.from(url.split(",")[1], "base64"))); source = "canvas pixels"; }
    }
    const idle = join(SHOTS, "idle.png");
    if (!grid && existsSync(idle)) {
      grid = gridScore(PNG.sync.read(readFileSync(idle)), big ? { x: big.x, y: big.y, width: big.cssWidth, height: big.cssHeight } : null);
      source = "idle screenshot";
    }
    const aligned = grid?.score != null && grid.score >= 0.6;
    set("pixel_grid", exact || aligned ? "pass" : "fail", { canvas_320x180_at_4x: exact, grid_source: exact ? null : source, grid: exact ? null : grid, canvases });
  } catch (e) { set("pixel_grid", "unverifiable", { error: e.message }); }

  // ---- Close and write outputs
  const video = page.video();
  await context.close();
  await browser.close();
  try {
    const src = await video.path();
    // Playwright records WebM; publish H.264 MP4 so clips play everywhere, including iOS.
    const h264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an"];
    execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-i", src, ...h264, join(OUT, "session.mp4")]);
    if (showcase.start != null) {
      const end = showcase.end ?? vt();
      execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-ss", String(showcase.start), "-i", src,
        "-t", String(Math.max(1, end - showcase.start)), ...h264, join(OUT, "showcase.mp4")]);
    }
  } catch (e) { notes.push(`video: ${e.message}`); }
  try { execFileSync("rm", ["-rf", join(OUT, "video-tmp")]); } catch {}

  writeFileSync(join(OUT, "console.log"), consoleLines.map((c) => `[${c.t}s] ${c.type}: ${c.text}${c.url ? `  (${c.url})` : ""}`).join("\n")
    + (pageErrors.length ? "\n\n== page errors ==\n" + pageErrors.map((e) => `[${e.t}s] ${e.message}`).join("\n") : "") + "\n");
  writeFileSync(join(OUT, "requests.json"), JSON.stringify(requests, null, 1));
  finishCodeArt();

  set("game_folder", SERVED_FROM === "." ? "pass" : "fail",
      SERVED_FROM === "." ? null : { served_from: SERVED_FROM, note: "built in a subfolder; served from there, as if at the root" });
  if (gateFailed) for (const [k, c] of Object.entries(checks)) if (c.type !== "gate" && c.status === "skipped") c.detail = { reason: "gate failed" };
  const scored = Object.values(checks).filter((c) => c.type === "scored");
  const passed = scored.filter((c) => c.status === "pass").length;
  const results = {
    evaluator_version: 9,
    served_from: SERVED_FROM,
    evaluated_at: new Date().toISOString(),
    gate_failed: gateFailed || checks.loads.status !== "pass" || checks.playable_loop.status !== "pass",
    manual_showcase_required: !canPlay && checks.loads.status === "pass",
    pass_rate: { passed, total: scored.length, rate: +(passed / scored.length).toFixed(3) },
    checks,
    showcase,
    timeline,
    notes,
  };
  writeFileSync(join(OUT, "results.json"), JSON.stringify(results, null, 2) + "\n");
  console.log(`gate_failed=${results.gate_failed} pass_rate=${passed}/${scored.length}`);
  for (const [k, c] of Object.entries(checks)) console.log(`${c.type.padEnd(8)} ${c.status.padEnd(12)} ${c.label}`);
}

class Stop extends Error {}

function setLayout(list) {
  const valid = list.filter(Boolean);
  if (!valid.length) { set("layout", "unverifiable", { reason: "could not measure" }); return; }
  // Content past the edge only counts when the viewer could scroll to it.
  const locked = (v) => v === "hidden" || v === "clip";
  const bad = valid.filter((l) =>
    (l.scrollWidth > l.innerWidth && !locked(l.overflow?.x)) || (l.scrollHeight > l.innerHeight && !locked(l.overflow?.y)));
  const clipped = valid.filter((l) => l.scrollWidth > l.innerWidth || l.scrollHeight > l.innerHeight).length - bad.length;
  set("layout", bad.length ? "fail" : "pass", { measurements: valid, clipped_overflow_seen: clipped });
}

main().catch((e) => { console.error(e); process.exit(1); });
