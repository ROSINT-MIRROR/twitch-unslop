/* chrome/selftest.mjs — run the shipped Chrome files for real, in node,
 * against real captured playlists. No browser, no network.
 *
 *   node chrome/selftest.mjs
 *
 * Three things are tested, and they are the three that decide whether the
 * Chrome build works at all:
 *
 *   1. core.js is shared, not forked. chrome/core.js must BE ext/core.js.
 *   2. pool.js — the Chrome shell — loads on top of core.js in a bare worker
 *      scope, produces a push map keyed the way hook.js will look it up, and
 *      moves the right counters when a poll is reported back to it.
 *   3. hook.js — the hot path — answers a media playlist from that push
 *      synchronously, holds the first one until a push arrives, passes
 *      everything through in observe mode and when switched off, and shims a
 *      Worker with a boot stub that parses and installs the push receiver.
 *
 * Both files are executed unmodified in a vm context, so this tests the shipped
 * artifacts rather than a copy of their logic. The Firefox equivalent is
 * ext/selftest.mjs; the two share core.js and nothing else.
 */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const ROOT = path.resolve(import.meta.dirname, "..");
let fails = 0;
function ok(cond, msg, extra) {
  if (cond) { console.log("  ok   " + msg); return true; }
  fails++;
  console.log("  FAIL " + msg + (extra ? "  " + extra : ""));
  return false;
}
const src = (f) => fs.readFileSync(path.join(ROOT, f), "utf8");

/* ------------------------------------------------------ fixtures on disk */

function findFiles(dir, pred, max) {
  const out = [];
  let names = [];
  try { names = fs.readdirSync(dir); } catch (e) { return out; }
  for (const n of names) {
    const p = path.join(dir, n);
    let s;
    try { s = fs.statSync(p); } catch (e) { continue; }
    if (s.isDirectory()) {
      for (const f of findFiles(p, pred, max - out.length)) out.push(f);
    } else {
      let txt = "";
      try { txt = fs.readFileSync(p, "utf8"); } catch (e) { continue; }
      if (pred(txt)) out.push({ path: p, body: txt });
    }
    if (out.length >= max) break;
  }
  return out;
}

const masters = findFiles(path.join(ROOT, "data/manifests"),
  (t) => /#EXT-X-STREAM-INF/.test(t) && /playlist\.ttvnw\.net/.test(t), 1);
const medias = findFiles(path.join(ROOT, "data/ext"),
  (t) => /#EXTINF/.test(t) && /#EXT-X-PROGRAM-DATE-TIME/.test(t), 3);

if (!masters.length || !medias.length) {
  console.log("no captured master/media playlists under data/ — cannot self-test");
  process.exit(2);
}
console.log(`[.] master  ${path.relative(ROOT, masters[0].path)}`);
console.log(`[.] media   ${medias.map((m) => path.basename(m.path)).join(" ")}`);

/* ====================================================== 1. the shared core */

console.log("\ncore.js is shared, not forked");
{
  const a = path.join(ROOT, "chrome/core.js");
  const b = path.join(ROOT, "ext/core.js");
  ok(fs.existsSync(a), "chrome/core.js exists");
  ok(fs.readFileSync(a).equals(fs.readFileSync(b)),
    "chrome/core.js is byte-identical to ext/core.js");
  let link = null;
  try { link = fs.readlinkSync(a); } catch (e) { /* a copy is allowed too */ }
  ok(link === null || link.endsWith("ext/core.js"),
    `chrome/core.js is ${link ? "a symlink to " + link : "a copy"}`);
  const bad = src("chrome/core.js").match(/browser\.|chrome\.|webRequest/g);
  ok(!bad, "core.js still names no extension API"
    + (bad ? `: ${[...new Set(bad)].join(" ")}` : ""));
}

/* ================================================= 2. pool.js, the shell */

/* A bare worker scope: exactly what core.js's contract says it may assume, plus
 * the two things a worker gives a shell — importScripts and postMessage.
 * Timers are stubbed dead on purpose: every donor loop in core.js parks on an
 * `await sleep(...)` at its first iteration, so nothing here touches the
 * network and no arm ever mints a session. */
function bootPool() {
  const posted = [];
  const timers = [];
  const sandbox = {
    console: { log: () => { } },
    fetch: () => Promise.reject(new Error("selftest: no network")),
    setTimeout: () => 0, clearTimeout: () => { },
    setInterval: (fn) => { timers.push(fn); return timers.length; },
    clearInterval: () => { },
    TextDecoder, TextEncoder, URL, URLSearchParams,
    Date, Math, Number, JSON, Promise, Array, Object, String, Error, RegExp,
    Map, Set, WeakMap, isNaN, parseInt, parseFloat,
    postMessage: (m) => posted.push(m),
  };
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.importScripts = (...fs_) => {
    for (const f of fs_) vm.runInContext(src("chrome/" + f), sandbox, { filename: f });
  };
  vm.createContext(sandbox);
  vm.runInContext(src("chrome/pool.js"), sandbox, { filename: "pool.js" });
  vm.runInContext("globalThis.__t = {chBuildPush, chPush, chMaster, chMedia,"
    + " chDone, chNet, chSnapshot, pools, variantIndex, stats, statsFor,"
    + " setEnabled, hash, evbuf, evring, parseMaster, rendKey, RE_PDTSEG,"
    + " seenSeg, ourSeg};", sandbox);
  return { sandbox, posted, timers, t: sandbox.__t };
}

console.log("\npool.js loads on top of core.js in a bare worker scope");
let P;
{
  let err = null;
  try { P = bootPool(); } catch (e) { err = e; }
  ok(!err, "pool.js + core.js load with no redeclaration collision",
    err ? String(err.message) : "");
  if (!P) { console.log("\n1 FAILED"); process.exit(1); }
  ok(P.posted.some((m) => m.k === "snap"), "posts a snapshot at start");
  const up = P.t.evbuf.find((e) => e.ev === "up");
  ok(!!up && up.shell === "chrome",
    `emits the shared \`up\` event (arms=${up && up.arms} shell=${up && up.shell})`);
  ok(P.timers.length >= 5,
    `registered ${P.timers.length} timers (push, snapshot, sink, reap, heartbeat)`);
}

console.log("\npool.js: master -> variant index -> push map");
let KEY = null, POOLCHAN = null;
{
  const body = masters[0].body;
  const url = "https://usher.ttvnw.net/api/v2/channel/hls/gaules.m3u8?allow_source=true";
  P.t.chMaster({ ev: "hook_master", url, body, t: Date.now() });
  ok(P.t.variantIndex.size > 0,
    `master indexed ${P.t.variantIndex.size} variants`);
  const first = [...P.t.variantIndex.entries()][0];
  KEY = first[0];
  POOLCHAN = first[1].channel;
  ok(POOLCHAN === "gaules", `channel taken from the usher path (${POOLCHAN})`);
  ok(/^https:\/\/[^/]+\/v1\/playlist\/[^?]+$/.test(KEY),
    "variant key is origin+path with no query", KEY);
  ok([...P.t.variantIndex.values()].some((v) => v.rend === "1920x1080@60"),
    "renditions are keyed RESOLUTION@FRAME-RATE, not by name");
  ok(P.t.pools.has("gaules"), "a pool was opened for the channel");
  const mev = P.t.evbuf.find((e) => e.ev === "master");
  ok(!!mev && mev.fmt === "v2", "master event records which of the two formats it was");
}

console.log("\npool.js: a media poll, end to end");
{
  const pool = P.t.pools.get(POOLCHAN);
  const rend = P.t.variantIndex.get(KEY).rend;
  const body = medias[0].body;

  // Half one: the body Twitch served this session.
  P.t.chMedia({ ev: "hook_media", id: "x1", key: KEY, body, t: Date.now() });
  ok(pool.store.rendition === rend,
    `pool bound to the rendition the player asked for (${rend})`);
  ok(pool.store.seeded && pool.store.mseq > 0,
    `MEDIA-SEQUENCE seeded from the player's own playlist (${pool.store.mseq})`);
  const before = pool.st.passthru;

  // Half two: what the hook did. Cold pool -> passthrough.
  P.t.chDone({ ev: "hook_done", id: "x1", served: false, waited: 8000 });
  ok(pool.st.passthru === before + 1, "a passthrough counts as a passthrough");
  const m1 = P.t.evbuf.filter((e) => e.ev === "media").pop();
  ok(m1 && m1.decision === "pass_cold",
    `cold pool -> decision=${m1 && m1.decision}`);
  ok(m1 && m1.realHash && m1.outHash === null,
    "the body Twitch sent is kept as evidence, ours is null");

  // Now warm the store from the same playlist, exactly as a donor arm would,
  // and check the push map that comes out of it.
  const re = new RegExp(P.t.RE_PDTSEG.source, "g");
  let m, n = 0;
  while ((m = re.exec(body)) !== null) {
    const ms = Date.parse(m[1]);
    pool.store.offer(ms, parseFloat(m[2]), m[4], m[1], m[3].trim() !== "live", rend);
    n++;
  }
  ok(pool.store.seg.size >= 6,
    `store holds ${pool.store.seg.size} clean segments from ${n} parsed`);

  const push = P.t.chBuildPush();
  ok(push.enabled === true, "push says the extension is on");
  ok(!!push.map[KEY], "the variant the player polls is in the push map");
  const h = push.map[KEY] && push.map[KEY].hash;
  ok(!!h && typeof push.bodies[h] === "string",
    "the map points at a body, and the body is carried once per hash");
  const out = push.bodies[h] || "";
  ok(out.startsWith("#EXTM3U"), "the pushed body is a playlist");
  ok(/#EXT-X-MEDIA-SEQUENCE:(\d+)/.test(out), "it carries a MEDIA-SEQUENCE");
  ok(!/,\s*$/m.test(out) && !/#EXTINF:[\d.]+,(?!live)/.test(out),
    "every segment in it is titled `live` — ads never reach the map");
  const mapped = Object.keys(push.map);
  const rends = new Set(mapped.map((k) => P.t.variantIndex.get(k).rend));
  ok(rends.size === 1 && rends.has(rend),
    `only the bound rendition is mapped (${[...rends].join(",")})`
    + " — a 360p poll physically cannot be answered with 1080p segments");

  // ...and a poll we DO answer.
  P.t.chMedia({ ev: "hook_media", id: "x2", key: KEY, body, t: Date.now() });
  const served0 = pool.st.served;
  P.t.chDone({ ev: "hook_done", id: "x2", served: true, hash: h, waited: 0 });
  ok(pool.st.served === served0 + 1, "a serve counts as a serve");
  const m2 = P.t.evbuf.filter((e) => e.ev === "media").pop();
  ok(m2 && m2.decision === "rewrite" && m2.outHash === h,
    "the media event names the manifest we handed over");
  const sv = P.t.evbuf.filter((e) => e.ev === "serve").pop();
  ok(!!sv && sv.n > 0 && sv.firstPdt && sv.lastPdt,
    `serve event carries the window (${sv && sv.n} segments, `
    + `${sv && sv.firstPdt} .. ${sv && sv.lastPdt})`);

  // An unknown variant must pass through, never guess.
  P.t.chMedia({ ev: "hook_media", id: "x3", key: "https://nope/v1/playlist/x", body, t: Date.now() });
  P.t.chDone({ ev: "hook_done", id: "x3", served: false, waited: 0 });
  const m3 = P.t.evbuf.filter((e) => e.ev === "media").pop();
  ok(m3 && m3.decision === "pass_unknown",
    "a playlist with no master mapping is passed through, not guessed at");

  // Switched off means off — including in the push.
  P.t.setEnabled(false);
  const offPush = P.t.chBuildPush();
  ok(offPush.enabled === false && !Object.keys(offPush.map).length,
    "disabled -> the push map is empty and every tab falls back to Twitch");
  P.t.setEnabled(true);
}

console.log("\npool.js: segment ground truth");
{
  // A segment URL the donors never advertised and a real playlist called an ad.
  const adUrl = "https://x.j.cloudfront.hls.ttvnw.net/v1/segment/AD.ts";
  P.t.seenSeg.set(adUrl, { dur: 2.0, ad: true, chan: POOLCHAN });
  const before = P.t.stats.segFetchAd;
  P.t.chNet({ ev: "net", kind: "segment", ours: false, url: adUrl, path: "/v1/segment/AD.ts" });
  ok(P.t.stats.segFetchAd === before + 1,
    "an ad segment the player fetched is counted as playback, not as a manifest");
  const se = P.t.evring.filter((e) => e.ev === "segment").pop();
  ok(se && se.ad === true && se.via === "real",
    "and it is attributed to Twitch's body, not ours");
  // Our own donor polling must never be mistaken for the player.
  const before2 = P.t.stats.segFetch;
  P.t.chNet({ ev: "net", kind: "segment", ours: true, url: adUrl, path: "/x" });
  ok(P.t.stats.segFetch === before2,
    "the pool's own requests are excluded from what reached the screen");
}
{
  /* The player does not fetch the URL the playlist printed: every segment URI
   * ends `...ts?dna=<blob>` and the request drops the query (measured
   * 2026-07-29, data/chrome/gaules.260729-185054 — 14 of 14 fetches). An
   * exact-string lookup misses every time, and `adSegsPlayed` then reads a
   * confident zero for a session nobody actually measured. */
  const chan = POOLCHAN;
  const real = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1\n"
    + "#EXT-X-PROGRAM-DATE-TIME:2026-07-29T15:00:00.000Z\n"
    + "#EXTINF:2.000,Amazon|123\n"
    + "https://cdn.j.cloudfront.hls.ttvnw.net/v1/segment/ADX.ts?dna=BLOB\n"
    + "#EXT-X-TWITCH-PREFETCH:https://cdn.j.cloudfront.hls.ttvnw.net/v1/segment/PRE.ts?dna=BLOB2\n";
  P.t.chMedia({ ev: "hook_media", id: "s1", key: KEY, body: real, t: Date.now() });
  P.t.chDone({ ev: "hook_done", id: "s1", served: false, waited: 0 });
  const before = P.t.stats.segFetchAd;
  P.t.chNet({ ev: "net", kind: "segment", ours: false,
    url: "https://cdn.j.cloudfront.hls.ttvnw.net/v1/segment/ADX.ts",
    path: "/v1/segment/ADX.ts" });
  ok(P.t.stats.segFetchAd === before + 1,
    "an ad segment fetched WITHOUT its ?dna= query is still recognised as an ad");
  const se = P.t.evring.filter((e) => e.ev === "segment").pop();
  ok(se && se.chan === chan, "and it is still attributed to the right channel");
  P.t.chNet({ ev: "net", kind: "segment", ours: false,
    url: "https://cdn.j.cloudfront.hls.ttvnw.net/v1/segment/PRE.ts",
    path: "/v1/segment/PRE.ts" });
  const pe = P.t.evring.filter((e) => e.ev === "segment").pop();
  ok(pe && pe.chan === chan,
    "the low-latency PREFETCH lookahead is indexed too — it is fetched like "
    + "any other segment but is a tag, so RE_PDTSEG never sees it");
}

/* ================================================== 3. hook.js, hot path */

/* Stand up hook.js in a fake page. Returns the sandbox so a test can call the
 * patched fetch and the push receiver directly. */
function install(hash, body) {
  const emitted = [];
  const workerSrc = [];
  const workerPosts = [];

  class SandBlob {
    constructor(parts) { this.__src = (parts || []).join(""); }
  }
  const SandURL = class extends URL { };
  SandURL.createObjectURL = (b) => {
    workerSrc.push(b.__src);
    return "blob:https://www.twitch.tv/fake-" + workerSrc.length;
  };
  SandURL.revokeObjectURL = () => { };

  class FakeWorker {
    constructor(url, opts) { this.url = url; this.opts = opts; }
    addEventListener() { }
    postMessage(m) { workerPosts.push(m); }
    terminate() { }
  }

  const listeners = [];
  const loc = {
    href: "https://www.twitch.tv/gaules",
    origin: "https://www.twitch.tv",
    hash,
  };
  const win = {
    location: loc,
    Response, Headers, URL: SandURL, Blob: SandBlob,
    Worker: FakeWorker,
    XMLHttpRequest: undefined,
    fetch: async () => new Response(body, { status: 200, statusText: "OK" }),
    postMessage: (m) => {
      if (m && m.__unslop && m.e) emitted.push(m.e);
    },
    addEventListener: (n, fn) => { if (n === "message") listeners.push(fn); },
    setTimeout, clearTimeout,
  };
  const sandbox = {
    window: win, self: win, top: win, location: loc,
    document: { readyState: "loading", body: null, getElementsByTagName: () => [] },
    performance, console: { log: () => { } },
    Response, Headers, URL: SandURL, Blob: SandBlob,
    setTimeout, clearTimeout, Promise, Date, Math, JSON, Map, Object, String,
    Error, WeakMap, Function, RegExp, Number, Array,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src("chrome/hook.js"), sandbox, { filename: "hook.js" });
  // The page-side push receiver hook.js registered on window.
  const push = (p) => {
    for (const fn of listeners) fn({ source: win, data: { __unslop: 1, push: p } });
  };
  return { sandbox, win, emitted, workerSrc, workerPosts, push };
}

const MEDIA_URL = "https://euc12.playlist.ttvnw.net/v1/playlist/abc.m3u8?sig=9&t=1";
const MEDIA_KEY = "https://euc12.playlist.ttvnw.net/v1/playlist/abc.m3u8";
const OURS = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n"
  + "#EXT-X-MEDIA-SEQUENCE:9001\n#EXT-X-PROGRAM-DATE-TIME:2026-07-29T14:30:36.312Z\n"
  + "#EXTINF:4.167,live\nhttps://donor.j.cloudfront.hls.ttvnw.net/v1/segment/OURS.ts\n";
const pushOf = (map) => ({ gen: 1, enabled: true, bodies: { h1: OURS }, map });

console.log("\nhook.js: installs and reports itself");
{
  const H = install("#unslopmode=rewrite", medias[0].body);
  const inst = H.emitted.find((e) => e.ev === "hook_install");
  ok(!!inst, "emits an install event");
  ok(inst && inst.patched && inst.patched.fetch && inst.patched.worker,
    "patched fetch and Worker", JSON.stringify(inst && inst.patched));
  ok(inst && inst.readyState === "loading" && inst.scriptsAtInstall === 0,
    "installed at document_start, before any page script existed");
  ok(inst && inst.mode === "rewrite", "defaults to rewrite with no hash present");
}

console.log("\nhook.js: the hot path is a cache lookup, not a round trip");
{
  const H = install("#unslopmode=rewrite", medias[0].body);
  H.push(pushOf({ [MEDIA_KEY]: { hash: "h1", chan: "gaules", rend: "1920x1080@60" } }));
  const t0 = Date.now();
  const res = await H.win.fetch(MEDIA_URL);
  const out = await res.text();
  const dt = Date.now() - t0;
  ok(out === OURS, "a warm cache answers the poll with the pool's body");
  ok(dt < 50, `and answers it in ${dt}ms — no message round trip`);
  const seen = H.emitted.find((e) => e.ev === "hook_media");
  ok(!!seen && seen.key === MEDIA_KEY,
    "the real body is reported under the origin+path key, query stripped");
  ok(!!seen && seen.body === medias[0].body,
    "the body Twitch sent is shipped to the pool intact — it is the only "
    + "place the viewer's own ad state is visible");
  const done = H.emitted.find((e) => e.ev === "hook_done");
  ok(!!done && done.served === true && done.hash === "h1",
    "and the decision is reported back, naming the manifest");
  ok(!!res.headers && res.headers.get("content-length") === null,
    "Content-Length is dropped — a stale one truncates our longer body");
}

console.log("\nhook.js: cold, and the hold");
{
  // Nothing pushed and nothing arriving: passthrough, bounded by warmMs.
  const H = install("#unslopmode=rewrite&warm=300", medias[0].body);
  const t0 = Date.now();
  const out = await (await H.win.fetch(MEDIA_URL)).text();
  const dt = Date.now() - t0;
  ok(out === medias[0].body, "a cold pool hands Twitch's own body through");
  ok(dt >= 250 && dt < 2000, `held the first poll for ${dt}ms, then gave up`);
  const done = H.emitted.find((e) => e.ev === "hook_done");
  ok(!!done && done.served === false && done.would === false,
    "reports that it served nothing and had nothing to serve");
}
{
  // A push that lands DURING the hold is the whole point of the hold.
  const H = install("#unslopmode=rewrite&warm=3000", medias[0].body);
  const p = H.win.fetch(MEDIA_URL);
  setTimeout(() => H.push(pushOf({
    [MEDIA_KEY]: { hash: "h1", chan: "gaules", rend: "1920x1080@60" },
  })), 250);
  const t0 = Date.now();
  const out = await (await p).text();
  const dt = Date.now() - t0;
  ok(out === OURS, "a push arriving mid-hold is served — no join stutter");
  ok(dt < 2000, `waited ${dt}ms, not the full warm window`);
}
{
  // ...and the hold must not become a permanent stall on a dead channel.
  const H = install("#unslopmode=rewrite&warm=120", medias[0].body);
  let held = 0;
  for (let i = 0; i < 7; i++) {
    const t0 = Date.now();
    await H.win.fetch(MEDIA_URL + "&n=" + i);
    if (Date.now() - t0 > 60) held++;
  }
  ok(held <= 4, `only ${held} of 7 polls were held — a pool that never warms `
    + "must not stall every poll forever; an ad beats a frozen player");
}

console.log("\nhook.js: the arms that must never serve");
{
  const H = install("#unslopmode=observe", medias[0].body);
  H.push(pushOf({ [MEDIA_KEY]: { hash: "h1", chan: "gaules", rend: "1920x1080@60" } }));
  const out = await (await H.win.fetch(MEDIA_URL)).text();
  ok(out === medias[0].body, "observe mode returns Twitch's body byte-for-byte");
  const done = H.emitted.find((e) => e.ev === "hook_done");
  ok(!!done && done.served === false && done.would === true,
    "and records that the pool WAS warm — which is what makes it a control");
}
{
  const H = install("#unslopmode=rewrite", medias[0].body);
  H.push({ gen: 2, enabled: false, bodies: {}, map: {} });
  const out = await (await H.win.fetch(MEDIA_URL)).text();
  ok(out === medias[0].body, "switched off -> every poll passes through");
}
{
  const H = install("#unslopmode=rewrite", medias[0].body);
  H.push(pushOf({ "https://other.playlist.ttvnw.net/v1/playlist/zzz.m3u8":
    { hash: "h1", chan: "gaules", rend: "640x360@30" } }));
  const out = await (await H.win.fetch(MEDIA_URL)).text();
  ok(out === medias[0].body,
    "a body mapped to a DIFFERENT variant is never served to this one");
}

console.log("\nhook.js: master and gql pass through untouched");
{
  const H = install("#unslopmode=rewrite", masters[0].body);
  const out = await (await H.win.fetch(
    "https://usher.ttvnw.net/api/v2/channel/hls/gaules.m3u8?x=1")).text();
  ok(out === masters[0].body, "the master is never altered");
  const e = H.emitted.find((x) => x.ev === "hook_master");
  ok(!!e && e.body === masters[0].body, "but it is shipped to the pool to parse");
}
{
  const H = install("#unslopmode=rewrite", "{}");
  await H.win.fetch("https://gql.twitch.tv/gql",
    { method: "POST", body: '{"operationName":"PlaybackAccessToken","variables":{"login":"gaules"}}' });
  const e = H.emitted.find((x) => x.ev === "hook_gql");
  ok(!!e && /"login":"gaules"/.test(e.body),
    "the channel is learned from the gql request body, independently of usher");
}

console.log("\nhook.js: the Worker shim — the only path to Twitch's playlists");
{
  const H = install("#unslopmode=rewrite", "#EXTM3U\n");
  new H.win.Worker("https://static.twitchcdn.net/assets/player-worker.js");
  ok(H.workerSrc.length === 1, "classic worker got a blob shim");
  const s = H.workerSrc[0];
  ok(/importScripts\("https:\/\/static\.twitchcdn\.net/.test(s),
    "the shim importScripts() the real script, after installing us");
  ok(/__unslop_push/.test(s) && s.indexOf("__unslop_push") < s.indexOf("importScripts"),
    "the push receiver is registered BEFORE the real script runs, so it is the "
    + "first message listener in the worker and can keep our traffic out of "
    + "Twitch's handlers");
  ok(/"where":"worker"/.test(s), "worker scope is tagged");
  let parsed = true;
  try { new Function(s); } catch (e) { parsed = false; console.log("      " + e.message); }
  ok(parsed, "generated worker source parses");

  new H.win.Worker("./rel.js", { type: "module" });
  const ms = H.workerSrc[1];
  ok(/import\("https:\/\/www\.twitch\.tv\/rel\.js"\)/.test(ms),
    "module worker uses dynamic import() against an absolutised URL");
  let mparsed = true;
  try { new Function(ms); } catch (e) { mparsed = false; }
  ok(mparsed, "generated module-worker source parses");

  const evs = H.emitted.filter((e) => e.ev === "hook_worker");
  ok(evs.length === 2 && evs[0].shimmed && evs[1].module === true,
    "both Worker constructions were reported");
}
{
  // Pushes have to reach the worker, because that is where the playlists are.
  const H = install("#unslopmode=rewrite", "#EXTM3U\n");
  H.push(pushOf({ [MEDIA_KEY]: { hash: "h1", chan: "gaules", rend: "1920x1080@60" } }));
  new H.win.Worker("https://static.twitchcdn.net/assets/w.js");
  ok(H.workerPosts.some((m) => m && m.__unslop_push),
    "a worker created AFTER a push is handed the current state immediately");
  H.push(pushOf({ [MEDIA_KEY]: { hash: "h1", chan: "gaules", rend: "1920x1080@60" } }));
  ok(H.workerPosts.filter((m) => m && m.__unslop_push).length >= 2,
    "and every later push is forwarded down to it");
}

console.log(fails ? `\n${fails} FAILED` : "\nALL GREEN");
process.exit(fails ? 1 : 0);
