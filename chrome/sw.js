"use strict";

/* The event ring, kept across a browser restart. Memory-only, a tester who
 * restarts before writing up what they saw has nothing left to send. Small
 * against the 10MB quota. */
const RING_KEY = "unslop.ring";
setInterval(() => {
  try { chrome.storage.local.set({ [RING_KEY]: (SNAP.events || []).slice(-3000) }); }
  catch (e) { /* quota or incognito — the live ring still works */ }
}, 30000);
/* chrome/sw.js — MV3 service worker. The switchboard, and nothing else.
 *
 * An MV3 service worker is killed after ~30s idle, so it cannot be where the
 * donor pool lives: a pool whose timers stop every half minute is not a pool.
 * It also cannot be the MAIN world, which would mean one pool PER TAB — four
 * donor sessions times however many Twitch tabs are open, straight through
 * CLAUDE.md's ~16-poller ceiling. So the pool runs in an offscreen document
 * (see offscreen.html), which persists, and this file only routes.
 *
 * Four jobs:
 *
 *  1. Lifecycle. Create the offscreen document, and re-create it after an
 *     extension reload. Every message to the pool goes through ensureOffscreen.
 *
 *  2. Relay. Content scripts hold a Port (bridge.js); the pool talks over
 *     chrome.runtime messaging. Hook events go up, playlist pushes come down.
 *     An open Port is also what keeps this worker alive between webRequest
 *     events, which is why bridge.js reconnects rather than giving up.
 *
 *  3. Ground truth. chrome.webRequest still exists in MV3 in observe-only form
 *     — useless for changing a body, perfect for counting one. Nothing running
 *     in the page can hide a request from it, so "playlists webRequest saw" vs
 *     "playlists hook.js saw" is the honest measure of whether the MAIN-world
 *     patch won the race, and segment fetches are the only proof of what
 *     actually reached the screen. A number the hook reports about the hook is
 *     not evidence.
 *
 *  4. The toolbar icon. ext/badge.js is shared verbatim with the Firefox build
 *     and expects `enabled` and `statsFor` as globals in its own scope — in
 *     MV2 that scope was the background page, next to core.js. Here core.js is
 *     in the offscreen worker, so badgeshim.js backs those two names with the
 *     snapshot the pool pushes every second.
 */

/* ---------------------------------------------------------------- snapshot
 * Declared before importScripts, because badgeshim.js closes over it. One
 * second stale at worst; the badge ticks at two. */
var SNAP = { byChan: {}, global: null, events: [], enabled: true, t: 0 };

/* ------------------------------------------------------------- offscreen */

const OFF_URL = "offscreen.html";
let offscreenReady = null;

async function ensureOffscreen() {
  if (offscreenReady) return offscreenReady;
  offscreenReady = (async () => {
    try {
      if (await chrome.offscreen.hasDocument()) return true;
      await chrome.offscreen.createDocument({
        url: OFF_URL,
        // Honest: pool.js really is a Worker, and it is there because core.js's
        // contract is "no extension API" — a worker is a scope that can hold
        // the pool's timers and fetches and cannot accidentally reach for one.
        reasons: ["WORKERS"],
        justification:
          "Runs the donor-session pool and HLS playlist engine on a dedicated "
          + "worker. The pool polls playlists continuously and holds the "
          + "segment grid in memory; an MV3 service worker is terminated after "
          + "~30s idle and cannot hold either.",
      });
      // A fresh pool starts switched ON. Outside the promise, so `toPool` can
      // await this same ensureOffscreen without waiting on itself.
      setTimeout(replayEnabled, 0);
      return true;
    } catch (e) {
      // Two SW wakeups can race into createDocument. Losing that race is fine.
      if (/single offscreen|already/i.test(String(e))) return true;
      offscreenReady = null;
      return false;
    }
  })();
  return offscreenReady;
}

/* Off has to stay off.
 *
 * The pool lives in the offscreen document and starts enabled, so without this
 * a tester who switches Unslop off gets it back on at the next extension
 * reload, browser restart or update — with four donor sessions per open stream
 * they did not ask for. The flag is read here at startup so the popup shows the
 * right state instantly, and replayed into the pool the moment it is created. */
function replayEnabled() {
  try {
    chrome.storage.local.get("enabled", (d) => {
      if (chrome.runtime.lastError) return;
      const on = !(d && d.enabled === false);
      SNAP.enabled = on;
      if (!on) toPool({ k: "enable", on: false });
    });
  } catch (e) { /* storage unavailable; default on */ }
}
replayEnabled();

/* Messages to the pool. Queued rather than sent, because the offscreen
 * document may not exist yet and a dropped hook event is a playlist body the
 * pool never sees. Bounded: if the pool never comes up, memory is not the
 * thing to spend on saying so. */
const OUT = [];
const OUT_MAX = 2000;
let pumping = false;

function toPool(m) {
  OUT.push(m);
  if (OUT.length > OUT_MAX) OUT.splice(0, OUT.length - OUT_MAX);
  pump();
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pump() {
  if (pumping) return;
  pumping = true;
  try {
    let fails = 0;
    while (OUT.length) {
      if (!(await ensureOffscreen())) {
        if (++fails > 20) { OUT.length = 0; break; }
        await sleep(500);
        continue;
      }
      const batch = OUT.splice(0, Math.min(OUT.length, 200));
      try {
        await chrome.runtime.sendMessage({ __u: "toPool", msgs: batch });
        fails = 0;
      } catch (e) {
        // Offscreen document not listening yet (it is still parsing its own
        // scripts). Put the batch back in front and try again.
        for (let i = batch.length - 1; i >= 0; i--) OUT.unshift(batch[i]);
        if (OUT.length > OUT_MAX) OUT.length = OUT_MAX;
        if (++fails > 20) { OUT.length = 0; break; }
        await sleep(250);
      }
    }
  } finally {
    pumping = false;
    if (OUT.length) setTimeout(pump, 250);
  }
}

/* ------------------------------------------------------------ tab ports */

const PORTS = new Set();
let lastPush = null;

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "unslop") return;
  PORTS.add(port);
  port.onDisconnect.addListener(() => {
    PORTS.delete(port);
    void chrome.runtime.lastError;
  });
  port.onMessage.addListener((msg) => {
    if (!msg) return;
    if (msg.k === "hook" && Array.isArray(msg.events)) {
      toPool({ k: "hook", events: msg.events });
    } else if (msg.k === "hello") {
      // A tab just installed the hook. Wake the pool now rather than on the
      // first playlist, and hand this tab whatever we already have so it is
      // not cold for a whole push interval.
      ensureOffscreen();
      if (lastPush) { try { port.postMessage({ k: "push", push: lastPush }); } catch (e) { /* */ } }
    }
  });
  if (lastPush) { try { port.postMessage({ k: "push", push: lastPush }); } catch (e) { /* */ } }
});

function broadcast(push) {
  lastPush = push;
  for (const p of PORTS) {
    try { p.postMessage({ k: "push", push }); } catch (e) { PORTS.delete(p); }
  }
}

/* ------------------------------------------------------- messages in ---- */

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return;

  // From the pool (offscreen document).
  if (msg.__u === "fromPool") {
    if (msg.k === "push") broadcast(msg.push);
    else if (msg.k === "snap") {
      SNAP = {
        byChan: msg.byChan || {}, global: msg.global || null,
        events: msg.events || [], enabled: msg.enabled !== false,
        log: msg.log || [],
        t: Date.now(),
      };
    }
    return;   // no reply
  }
  if (msg.__u === "toPool") return;   // ours, on its way out; not for us

  // From popup.html / dash.html. Answered from the snapshot, never by asking
  // the pool: a UI that blocks on a round trip through two message hops
  // renders a spinner every second.
  const cmd = typeof msg === "string" ? msg : msg.cmd;
  if (cmd === "stats") {
    const c = msg && msg.channel;
    sendResponse((c && SNAP.byChan[c]) || SNAP.global || { enabled: SNAP.enabled });
    return;
  }
  if (cmd === "events") { sendResponse(SNAP.events || []); return; }

  /* Everything a bug report needs, in one file — the Chrome counterpart of the
   * `diag` branch in ext/background.js, same shape so ext/extreport.py reads
   * either. A tester has no log sink, so without this a report is "I think I
   * saw an ad earlier" with nothing to check it against. The ring is restored
   * from storage first so a browser restart does not lose it. */
  if (cmd === "diag") {
    (async () => {
      let stored = [];
      try {
        const got = await chrome.storage.local.get(RING_KEY);
        stored = (got && got[RING_KEY]) || [];
      } catch (e) { /* nothing stored yet */ }
      const live = SNAP.events || [];
      const have = new Set(live.map((e) => e.t + "|" + e.ev));
      sendResponse({
        version: (chrome.runtime.getManifest() || {}).version || "?",
        ua: navigator.userAgent,
        saved: new Date().toISOString(),
        stats: SNAP.global || { enabled: SNAP.enabled },
        events: stored.filter((e) => e && !have.has(e.t + "|" + e.ev)).concat(live),
        log: SNAP.log || [],
      });
    })();
    return true;              // async sendResponse
  }
  if (cmd === "enable") {
    const on = !!(msg && msg.on);
    SNAP.enabled = on;
    try { chrome.storage.local.set({ enabled: on }); } catch (e) { /* */ }
    if (SNAP.global) SNAP.global.enabled = on;
    for (const k in SNAP.byChan) SNAP.byChan[k].enabled = on;
    toPool({ k: "enable", on });
    sendResponse((msg.channel && SNAP.byChan[msg.channel]) || SNAP.global
      || { enabled: on });
    return;
  }
  // `drain` is the Firefox content script's prose channel. Chrome ships prose
  // straight to the sink from the pool, so there is nothing to drain.
  if (cmd === "drain") { sendResponse([]); return; }
});

/* -------------------------------------------------- webRequest: evidence */

const NET_URLS = [
  "*://usher.ttvnw.net/*",
  "*://*.playlist.ttvnw.net/*",
  "*://*.hls.ttvnw.net/*",
  "*://*.hls.live-video.net/*",
];

/* Measured host set, not the documented one: segments sit under
 * *.hls.ttvnw.net (cloudfront/akamai/fastly POPs) and *.hls.live-video.net.
 * The leading label is per-session hex and the CDN behind it rotates, so match
 * the suffix. `hls.ttvnw.net` cannot collide with the playlist listener —
 * those are on `*.playlist.ttvnw.net`, a different label. */
function netKind(url) {
  if (url.indexOf("usher.ttvnw.net") >= 0) return "master";
  if (/:\/\/[^/]*\.playlist\.ttvnw\.net\//.test(url)) return "media";
  if (/:\/\/[^/]*\.hls\.ttvnw\.net\//.test(url)) return "segment";
  if (/:\/\/[^/]*\.hls\.live-video\.net\//.test(url)) return "segment";
  return null;
}

const SELF_ORIGIN = "chrome-extension://" + chrome.runtime.id;

function pathOf(u) {
  try { return new URL(u).pathname; } catch (e) { return null; }
}

/* Registered with no extraInfoSpec, so it cannot block, redirect or delay
 * anything — observation only, per the passive-observation rule.
 *
 * `ours` is the donor pool's own polling. It is NOT filtered out and thrown
 * away: it is counted and labelled, because "the extension opened exactly four
 * extra playlist sessions and no more" is a claim CLAUDE.md's poller budget
 * needs evidence for, and this is the only place that evidence exists. The
 * page's own traffic is everything with a different initiator — never tabId,
 * which is -1 for the player's worker too. */
try {
  chrome.webRequest.onBeforeRequest.addListener((d) => {
    const kind = netKind(d.url);
    if (!kind) return;
    const ours = d.initiator === SELF_ORIGIN;
    toPool({
      k: "net",
      e: {
        t: Date.now(), ev: "net", lvl: "trace", kind, ours,
        url: d.url.split("?")[0], path: pathOf(d.url), type: d.type,
        tabId: d.tabId, initiator: d.initiator || null,
      },
    });
  }, { urls: NET_URLS }, []);
  toPool({ k: "segwatch", on: true });
} catch (e) {
  // A missing host permission is silent, and "no ad segments were fetched"
  // must then read as "nobody looked", never as a clean session.
  toPool({ k: "segwatch", on: false, error: String(e) });
}

/* ------------------------------------------------------------ lifecycle */

chrome.runtime.onInstalled.addListener(() => { ensureOffscreen(); });
chrome.runtime.onStartup.addListener(() => { ensureOffscreen(); });
ensureOffscreen();

/* The badge. Loaded last: badgeshim.js has to define `enabled` and `statsFor`
 * before badge.js's first tick reads them, and badge.js is shared verbatim
 * with the Firefox build. */
try {
  importScripts("badgeshim.js", "badge.js");
} catch (e) {
  console.log("[unslop] badge did not load:", e);
}
