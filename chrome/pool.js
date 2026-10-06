"use strict";
/*
 * unslop — the Chrome shell.
 *
 * Everything that decides behaviour is in core.js: the pool, the PDT grid, the
 * chain, the ad tells, the counters, the event stream. core.js is shared
 * VERBATIM with the Firefox build (chrome/core.js is a symlink to ext/core.js,
 * and ext/build.sh --target=chrome resolves it into the package). This file is
 * the Chrome-specific half — the same job ext/background.js does for Gecko —
 * and it is a different shape for one reason.
 *
 * Firefox can rewrite a response body in place: filterResponseData hands you
 * the bytes mid-flight, so the pool is consulted synchronously while the player
 * waits. Chrome has no such API. The only code that can change a playlist body
 * is code running in the page, and the page is a message hop away from here —
 * so consulting the pool per request would put a round trip on the hot path.
 *
 * Inverted instead: this file PUSHES the current serve-ready playlist to every
 * tab whenever it changes, hook.js caches it, and the decision is a synchronous
 * Map lookup in the page. The player never waits for us. The one exception is
 * the first playlist of a session, which hook.js holds open until a push
 * arrives — `fetch` is async, so it can, and that is what removes the join
 * stutter that a swap-underneath-the-player would cause.
 *
 * Where it runs: a Worker inside an offscreen document. Not the service worker
 * (MV3 kills it after ~30s idle, and a pool whose timers stop is not a pool)
 * and not the MAIN world (that would be one pool PER TAB — four donor sessions
 * times however many Twitch tabs are open, straight through CLAUDE.md's
 * ~16-poller ceiling). A worker also enforces core.js's own contract
 * mechanically: that file may name no extension API, and there is none here to
 * name.
 */

importScripts("core.js");

/* Same wire contract as the Firefox build (ext/EVENTS.md), different port:
 * 8779 belongs to ext/tryout.py and both browsers can be running at once. */
const CH_SINK = "http://127.0.0.1:8780/log";
const CH_SINK_TRIES = 5;

const CH_PUSH_MS = 400;         // how often a new chain can reach the tabs
const CH_PUSH_IDLE_MS = 5000;   // resend unchanged, so a late tab is not cold
const CH_PENDING_MAX = 200;     // in-flight polls awaiting their hook_done
const CH_GEN_MAX = 400;         // remembered generated manifests, for `serve`
const CH_BODY_MAX = 40;         // ...and how many of their bodies we still hold
const CH_FEED = 150;            // events handed to the dashboard per snapshot

/* IN CHROME THE PLAYER DOES NOT FETCH THE URL THE PLAYLIST PRINTED, AND IN
 * FIREFOX IT DOES.
 *
 * Every segment URI in a Twitch media playlist ends `....ts?dna=<~300 chars>`.
 * Measured 2026-07-29:
 *
 *   Chromium 150   0 of 46 segment requests carried the query
 *                  (data/chrome/gaules.260729-191019)
 *   LibreWolf    359 of 421 did
 *                  (data/ext/gaules.260729-182900)
 *
 * So the exact-string lookup `seenSeg.get(details.url)` that ext/background.js
 * uses — and that this shell started out copying — resolves ~85% of fetches
 * under Gecko and 0% under Chromium. It fails silently: `adSegsPlayed`, the
 * only ground truth for "did an ad reach the screen", then reads a confident
 * zero for a session nobody actually measured, which is the exact failure this
 * event stream exists to prevent. First Chrome run: 0 of 25 fetches attributed.
 *
 * Hence both maps keyed both ways. Stripping the query is the only thing this
 * shell ever does to a URL.
 *
 * The residual ~15% Gecko miss is a different, smaller hole shared by both
 * shells: the `#EXT-X-TWITCH-PREFETCH` lookahead is a TAG, so RE_PDTSEG never
 * indexes it, and an ad in the lookahead reaches the screen uncounted.
 * chIndexReal() closes it here. ext/background.js is not touched from this
 * port — reported instead. */
const chStrip = (u) => {
  const q = String(u).indexOf("?");
  return q > 0 ? String(u).slice(0, q) : String(u);
};

// stripped URL -> what a real playlist said about it. Parallel to core.js's
// seenSeg, which core.js fills with the full URL and this shell cannot rekey.
const chSeenStripped = new Map();

let chSinkFails = 0;
let chSinkOff = false;

const chPost = (m) => { try { self.postMessage(m); } catch (e) { /* */ } };

/* Events that are evidence but not story: the independent chrome.webRequest
 * stream. Straight into the shipped buffer, never into the dashboard ring — a
 * couple of network events a second would push every ad out of a 400-slot ring
 * inside a minute. */
function chRaw(e) {
  evbuf.push(e);
  if (evbuf.length > EV_MAX) evbuf.shift();
}

/* ------------------------------------------------------------- the push */

const chGen = new Map();        // manifest hash -> what was in it, for `serve`
const chBodies = new Map();     // ...and the body itself, until a poll takes it
let chPushN = 0;
let chSig = "";
let chLastPush = 0;

/* Build the map of "playlist URL the player might poll" -> "body to answer it
 * with". Everything upstream of this line is core.js; everything downstream is
 * a cache lookup in the page.
 *
 * Keyed on origin+path, never the raw URL: the player appends its own query
 * params to the variant URL it got from the master, so exact-string matching
 * misses every poll. hook.js computes the same key from the URL it is about to
 * fetch.
 *
 * A key that is not in the map is a passthrough — which is why a rendition the
 * pool is not bound to is simply absent rather than mapped to something else.
 * The player asks for a different rendition by asking a different URL, so the
 * cache physically cannot answer a 360p poll with 1080p segments. Except on
 * purpose: during a ladder collapse every variant of the channel maps to the
 * held body, because usher pulling our rendition out of the master is an ad
 * break and Twitch's own 360p playlist is the one carrying the ad. */
function chBuildPush() {
  const bodies = {};
  const map = {};
  if (enabled) {
    for (const pool of pools.values()) {
      const rend = pool.store.rendition;
      if (!rend) continue;
      let body = null;
      try { body = pool.store.playlist(); } catch (e) { pool.st.lastError = String(e); }
      if (!body) continue;
      const h = hash(body);
      if (!chGen.has(h)) {
        const win = pool.store.win.slice();
        const first = win.length ? pool.store.seg.get(win[0]) : null;
        const last = win.length ? pool.store.seg.get(win[win.length - 1]) : null;
        chGen.set(h, {
          mseq: pool.store.mseq, n: win.length,
          firstPdt: first ? first.iso : null,
          lastPdt: last ? last.iso : null,
          chan: pool.channel, rend,
        });
        bound(chGen, CH_GEN_MAX);
        // Held so that a poll which actually takes this body can hand it to
        // keepManifest with the right keep/discard judgement. Without it
        // `outHash` names a manifest that exists nowhere and "what exactly did
        // we hand the player" is unanswerable after the fact.
        chBodies.set(h, body);
        bound(chBodies, CH_BODY_MAX);
        // Remember what we advertised: a segment fetch is only attributable to
        // us if the URL came out of a manifest we generated. Keyed both ways —
        // the player drops the `?dna=` query before it requests the bytes.
        for (const k of win) {
          const s = pool.store.seg.get(k);
          if (!s) continue;
          const rec = { dur: s.dur, chan: pool.channel };
          ourSeg.set(s.url, rec);
          ourSeg.set(chStrip(s.url), rec);
        }
        bound(ourSeg, MAX_SEGIDX);
      }
      const collapsed = pool.inCollapse();
      let mapped = 0;
      for (const [key, v] of variantIndex) {
        if (v.channel !== pool.channel) continue;
        if (v.rend !== rend && !collapsed) continue;
        map[key] = { hash: h, chan: pool.channel, rend, collapsed: v.rend !== rend };
        mapped++;
      }
      if (mapped) bodies[h] = body;
    }
  }
  return { gen: ++chPushN, enabled, bodies, map };
}

/* Only actually send when something changed, or every few seconds so a tab
 * that loaded late is not left cold. The chain changes when a donor delivers a
 * new segment — roughly every four seconds — so this is a handful of KB a
 * minute, not a stream. */
function chPush(force) {
  const p = chBuildPush();
  const sig = Object.keys(p.map).sort().map((k) => k + "=" + p.map[k].hash).join("|")
    + "|" + (p.enabled ? 1 : 0);
  const now = Date.now();
  if (!force && sig === chSig && now - chLastPush < CH_PUSH_IDLE_MS) return;
  chSig = sig;
  chLastPush = now;
  chPost({ k: "push", push: p });
}

/* ------------------------------------------------- the player's requests */

/* One entry per media poll, created when hook.js reports the body Twitch sent
 * and consumed when it reports what it did with it. The split exists because
 * the pool has to see the real body BEFORE the decision — that is what lets the
 * first poll be held until the pool is warm rather than answered cold. */
const chPending = new Map();

/* The master tells us the channel and maps each variant URL to a rendition, so
 * that when the player polls one we know which rendition to serve. Passed
 * through untouched — hook.js never alters a master. */
function chMaster(e) {
  const m = /\/api\/(?:v(\d+)\/)?channel\/hls\/([^./?]+)\.m3u8/.exec(e.url || "");
  if (!m) { W("usher URL is not a channel master:", String(e.url).slice(0, 140)); return; }
  const channel = m[2].toLowerCase();
  const fmt = m[1] ? `v${m[1]}` : "v1";     // unversioned path IS v1
  stats.sawMaster++;
  try {
    const vs = parseMaster(e.body || "");
    log(`master ${channel}: ${vs.length} variants `
      + `[${vs.map((v) => `${v.name}=${v.rend}`).join(" ")}]`);
    if (!vs.length) {
      W("master parsed to nothing. head:",
        String(e.body).slice(0, 400).replace(/\n/g, " | "));
    }
    ev("master", "info", {
      chan: channel, fmt,
      variants: vs.map((v) => ({ rend: v.rend, bw: v.bw, url: v.url })),
    });
    for (const v of vs) {
      variantIndex.set(variantKey(v.url), { channel, rend: v.rend });
      T(`  variant ${v.name} rend=${v.rend} key=${variantKey(v.url)}`);
    }
    // Remember the widest ladder this channel has ever offered — that is the
    // baseline a collapse is measured against. Never shrink it: the collapsed
    // master is exactly the one we must not learn from.
    const rends = vs.map((v) => v.rend);
    const best = ladderBest.get(channel);
    if (!best || rends.length > best.length) ladderBest.set(channel, rends);
    // Only tell a pool that already exists. Creating one here would mint
    // donors off a collapsed ladder.
    const p = pools.get(channel);
    if (p) p.onLadder(rends);
    poolFor(channel);
    // A new master can add the very variant key the player is about to poll.
    chPush(true);
  } catch (err) {
    stats.lastError = String(err);
    log("master parse failed:", err);
  }
}

/* The channel, learned independently of usher. This is the FIRST thing the
 * player does and it carries the login in the request body, so it works even
 * when the master never passes through a shape we recognise. */
function chGql(e) {
  try {
    const m = /"login":"([^"]+)"/.exec(e.body || "");
    if (!m || !m[1]) return;
    const channel = m[1].toLowerCase();
    logOnce("gql:" + channel, "gql PlaybackAccessToken login=" + channel);
    poolFor(channel);
  } catch (err) { log("gql hook:", err); }
}

/* Half one of a media poll: the body Twitch served THIS session.
 *
 * This is the only place the viewer's own ad state is observable. The donors'
 * playlists cannot answer "was an ad on offer to the person watching" — they
 * are different sessions — so everything about blocked-vs-leaked is decided
 * from this body, before hook.js writes ours over the top of it. */
function chMedia(e) {
  stats.sawMedia++;
  const v = variantIndex.get(e.key);
  if (!v) {
    // No mapping means we do not know which rendition this is. Guessing one
    // fed 720p segments into a 360p decode and froze the player; passing
    // through untouched is the correct failure.
    stats.unknownVariant++;
    W("unrecognised media playlist (no master mapping) — passing through:",
      String(e.key).slice(-60), `| indexed=${variantIndex.size}`);
    chStash(e, { pool: null, rend: null, held: false, mismatch: false,
                 real: realFacts(e.body || "", null), breakEdge: false });
    return;
  }

  const pool = poolFor(v.channel);
  // The heartbeat of the whole lifecycle: this is the only thing keeping the
  // pool alive, and its recency is what decides who keeps donors under the cap.
  pool.lastPoll = Date.now();
  pool.st.sawMedia++;
  const before = pool.store.rendition;
  pool.setRendition(v.rend);
  // A rebind flushes the store, so the cache in every tab is now describing
  // segments we no longer hold. Push immediately instead of waiting out the
  // interval.
  if (pool.store.rendition !== before) chPush(true);

  /* Serve the held rendition through a collapse instead of standing aside.
   *
   * usher pulls our rendition out of the ladder, the player is forced to 360,
   * that lands here as a mismatch, and passing through hands over Twitch's own
   * 360 playlist — which is the one carrying the ad. Our chain is clean source
   * content that continues across the break, and MEDIA-SEQUENCE cannot step
   * backwards on it (the counter only ever advances, and we do not reseed from
   * the forced variant), so serving it is safe where passing through is not. */
  const held = pool.store.rendition !== v.rend && pool.inCollapse();
  const mismatch = pool.store.rendition !== v.rend && !held;
  if (mismatch) {
    // The rebind debounce refused this one (the player is flapping). Never
    // splice a rendition the player did not ask for.
    D(`rendition mismatch: player wants ${v.rend}, pool on ${pool.store.rendition}`
      + " (rebind debounced) — passing through");
  } else if (held) {
    D(`${pool.channel}: serving held ${pool.store.rendition} to a player forced`
      + ` onto ${v.rend} by the collapsed ladder`);
  }

  const real = realFacts(e.body || "", pool.channel);
  chIndexReal(e.body || "", pool.channel);
  // Never seed from the forced variant: its MEDIA-SEQUENCE is its own, and
  // adopting it mid-collapse is exactly the backwards step that froze the
  // player at ready=2.
  if (real.mseq !== null && !held && !mismatch) pool.store.seedMseq(real.mseq);
  const breakEdge = real.inAd && !pool.st.inAdSelf;
  pool.st.inAdSelf = real.inAd;

  chStash(e, { pool, rend: v.rend, held, mismatch, real, breakEdge });
}

/* The same segments core.js's realFacts() just put in seenSeg, indexed under
 * the URL the player will actually request. `#EXT-X-TWITCH-PREFETCH` lines are
 * indexed too: the low-latency lookahead is fetched like any other segment and
 * an ad in it reaches the screen the same way, but it is a tag rather than a
 * URI line so RE_PDTSEG never sees it. Ad-ness comes from seenSeg, which is
 * core.js's; this only maps the URL shape. */
function chIndexReal(body, chan) {
  for (const line of body.split("\n")) {
    const s = line.trim();
    if (!s) continue;
    let u = null;
    if (s.charAt(0) === "#") {
      if (s.startsWith("#EXT-X-TWITCH-PREFETCH:")) u = s.slice(23).trim();
    } else {
      u = s;
    }
    if (!u) continue;
    const stripped = chStrip(u);
    if (stripped !== u || !chSeenStripped.has(stripped)) {
      chSeenStripped.set(stripped, { full: u, chan });
    }
  }
  bound(chSeenStripped, MAX_SEGIDX);
}

function chStash(e, rec) {
  rec.body = e.body || "";
  rec.t = e.t || Date.now();
  chPending.set(e.id, rec);
  // A hook_done that never arrives (tab closed mid-poll) would leak an entry
  // and a 9KB body with it.
  bound(chPending, CH_PENDING_MAX);
}

/* Half two: what hook.js actually did. Every counter moves here, never at
 * generation time — we generate a chain several times a second and the player
 * consumes maybe one in ten, so counting generated manifests as `served` would
 * inflate the one number a tester reads. */
function chDone(e) {
  const p = chPending.get(e.id);
  if (!p) return;
  chPending.delete(e.id);
  const served = !!e.served;
  const pool = p.pool;

  let decision;
  if (served) decision = "rewrite";
  else if (!pool) decision = "pass_unknown";
  else if (p.mismatch) decision = "pass_mismatch";
  else decision = "pass_cold";

  if (pool) {
    pool.st.exposed = !served && p.real.inAd;
    if (served) {
      pool.st.served++;
      pool.everServed = true;
      // Only now is it honest to call it blocked: the ad segments were in the
      // body Twitch sent this session, and we replaced that body.
      pool.st.blockedSegs += p.real.ads;
      if (p.breakEdge) {
        pool.st.blockedBreaks++;
        log(`BLOCKED: Twitch served THIS session an ad on ${pool.channel}`
          + ` (${p.real.ads} ad segs, source=${p.real.src || "?"})`);
      }
    } else {
      pool.st.passthru++;
      // The ad went to the screen. This is the failure mode the whole design
      // exists to avoid, so it gets its own count rather than hiding in pass.
      if (p.real.inAd) {
        pool.st.leakedSegs += p.real.ads;
        if (p.breakEdge) pool.st.leakedBreaks++;
        E(`LEAKED: ad reached the player on ${pool.channel} —`
          + ` ${p.real.ads} ad segs, ${decision}`
          + ` (grid=${pool.st.onGrid}/${pool.st.arms})`);
      }
      if (decision === "pass_cold") {
        W(`${pool.channel}: pool cold (chain=${pool.store.chain().length}/${MIN_SERVE},`
          + ` grid=${pool.st.onGrid}/${pool.st.arms}) — real playlist passed`
          + " through, ads included");
      }
    }
  }

  // Bodies are only worth disk when they are evidence: any ad-bearing poll,
  // anything we did not rewrite, anything served through a collapse, and the
  // first few of a session regardless. Keeping every distinct body ran ~18MB/h.
  const keep = p.real.ads > 0 || !served || p.held;
  const g = e.hash ? chGen.get(e.hash) : null;
  if (served) {
    // Ours, not just Twitch's. `outHash` naming a body that exists nowhere is
    // how "what exactly did we hand the player" becomes unanswerable, which is
    // what made the last leak un-diagnosable in the Firefox build.
    const mine = chBodies.get(e.hash);
    if (mine) keepManifest(mine, keep);
    ev("serve", "info", {
      chan: pool ? pool.channel : null,
      mseq: g ? g.mseq : null, n: g ? g.n : null,
      firstPdt: g ? g.firstPdt : null, lastPdt: g ? g.lastPdt : null,
      hash: e.hash || null,
    });
  }
  ev("media", served ? "info" : "warn", {
    chan: pool ? pool.channel : null,
    rend: p.rend, poolRend: pool ? pool.store.rendition : null,
    mseq: p.real.mseq, realAds: p.real.ads, realSrc: p.real.src,
    decision, waited: e.waited,
    chain: pool ? pool.store.chain().length : 0,
    store: pool ? pool.store.seg.size : 0,
    collapsed: p.held || undefined,
    realHash: keepManifest(p.body, keep), outHash: e.hash || null,
    // Control arm only. `mode:"observe"` means the hook was installed and
    // watching but handed Twitch's body through on purpose; `would` says
    // whether the pool actually had a clean chain ready for that exact poll.
    // Absent on a normal run, so extreport.py's tables are unaffected.
    mode: e.mode === "observe" ? "observe" : undefined,
    would: e.mode === "observe" ? !!e.would : undefined,
  });
}

/* Did an ad reach the screen?
 *
 * Nothing else in here can answer that. A manifest listing an ad proves an ad
 * was OFFERED; the player may never request those bytes, and `leaked` counts
 * offers. Only a fetch is playback. Observation only — the listener in sw.js
 * has no extraInfoSpec and cannot block, redirect or delay a segment. */
function chSegment(e) {
  const u = e.url;
  // Both keys: the playlist prints `...ts?dna=<blob>`, the player requests
  // `...ts`. See chStrip.
  const alias = chSeenStripped.get(u);
  const seen = seenSeg.get(u) || (alias ? seenSeg.get(alias.full) : null);
  const our = ourSeg.get(u);
  const ad = !!(seen && seen.ad);
  // Attribute to the channel whose playlist carried the URL, not to whichever
  // pool polled last — with two streams open the latter is a coin toss.
  const chan = (seen && seen.chan) || (our && our.chan)
    || (alias && alias.chan) || null;
  const p = chan ? pools.get(chan) : null;
  stats.segFetch++;
  if (ad) stats.segFetchAd++;
  if (p) {
    p.st.segFetch++;
    if (ad) p.st.segFetchAd++;
  }
  ev("segment", ad ? "error" : "trace", {
    url: u, ad, via: our ? "ours" : "real",
    dur: our ? our.dur : (seen ? seen.dur : undefined),
    chan: chan || undefined,
  });
  if (ad) {
    E(`AD ON SCREEN: player fetched an ad segment on ${chan || "?"}`
      + ` (dur=${seen.dur}s, via=${our ? "ours" : "real"}) ...${u.slice(-32)}`);
  }
}

/* ------------------------------------------------------------- messages */

self.onmessage = (m) => {
  const msg = m && m.data;
  if (!msg) return;
  try {
    if (msg.k === "hook") {
      for (const e of msg.events || []) chHook(e);
    } else if (msg.k === "net") {
      chNet(msg.e);
    } else if (msg.k === "enable") {
      setEnabled(!!msg.on);
      chPush(true);
    } else if (msg.k === "segwatch") {
      segWatch = !!msg.on;
      if (!msg.on) {
        stats.lastError = String(msg.error || "segment listener did not register");
        E("segment listener did not register — ad playback cannot be measured:",
          msg.error);
      }
    } else if (msg.k === "ev") {
      // Something the service worker measured about itself (the badge probe).
      chRaw(Object.assign({ t: Date.now(), lvl: "info" }, msg.e));
    }
  } catch (e) {
    stats.lastError = String(e);
    E("shell:", e && e.stack || e);
  }
};

function chHook(e) {
  if (!e || !e.ev) return;
  switch (e.ev) {
    case "hook_media": if (e.body) chMedia(e); break;
    case "hook_done": chDone(e); break;
    case "hook_master": chMaster(e); break;
    case "hook_gql": chGql(e); break;
    case "hook_install":
    case "hook_worker":
      // The race and the worker shim are the two things that decide whether
      // any of this runs at all, so they are recorded, not just logged.
      chRaw(Object.assign({ t: Date.now() }, e));
      if (e.ev === "hook_install") {
        I(`hook installed in ${e.where} mode=${e.mode}`
          + ` patched=${JSON.stringify(e.patched)} err=${e.error || "-"}`);
      }
      break;
    default: chRaw(Object.assign({ t: Date.now() }, e)); break;
  }
}

function chNet(e) {
  if (!e) return;
  chRaw(e);
  if (e.kind !== "segment" || e.ours) return;
  chSegment(e);
}

/* ------------------------------------------------------------ the sink */

/* On a developer's box the sink is chrome/drive.py and it is always there. On a
 * beta tester's box nothing is listening, and a POST per second to a dead port
 * forever is both a waste and the kind of undisclosed background traffic a
 * store reviewer flags. So the sink is opportunistic: try a handful of times,
 * then stop for good and free the buffers. Deliberately not a build flag — one
 * artifact behaves correctly in both places. */
function chRebuffer(buf, batch, max) {
  const merged = batch.concat(buf);
  buf.length = 0;
  for (let i = Math.max(0, merged.length - max); i < merged.length; i++) {
    buf.push(merged[i]);
  }
}

async function chFlush() {
  if (chSinkOff) {
    logbuf.length = evbuf.length = manbuf.length = 0;
    return;
  }
  if (!logbuf.length && !evbuf.length && !manbuf.length) return;
  const lines = logbuf.splice(0, logbuf.length);
  // Events and manifests ship whatever LEVEL is: level decides what a human
  // reads, never what was recorded. An info run and a trace run have to produce
  // identical evidence or the evidence proves nothing.
  const events = evbuf.splice(0, evbuf.length);
  const manifests = manbuf.splice(0, manbuf.length);
  try {
    const r = await fetch(CH_SINK, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lines, events, manifests, stats: rollup() }),
    });
    if (r.ok) {
      chSinkFails = 0;
      const d = await r.json().catch(() => null);
      if (d && d.level) setLevel(d.level);
      // Same channel as the log level, for the same reason: this scope cannot
      // see run.sh's argv, and the canary is off by default.
      if (d && typeof d.canary === "boolean" && d.canary !== CANARY) {
        CANARY = d.canary;
        I(`canary probe -> ${CANARY ? "on" : "off"}`);
      }
    }
  } catch (e) {
    chRebuffer(logbuf, lines, LOG_MAX);
    chRebuffer(evbuf, events, EV_MAX);
    chRebuffer(manbuf, manifests, MAN_MAX);
    if (++chSinkFails >= CH_SINK_TRIES) {
      chSinkOff = true;
      console.log("[unslop] no log sink on 127.0.0.1:8780 — logging off");
    }
  }
}

/* ------------------------------------------------------------- surfaces */

/* What the popup, the dashboard and the toolbar icon read. Pushed rather than
 * asked for: the service worker answers them from this snapshot, so a UI tick
 * never waits on two message hops and a chrome.offscreen wakeup. */
function chSnapshot() {
  rollup();
  const byChan = {};
  for (const chan of pools.keys()) byChan[chan] = statsFor(chan);
  chPost({
    k: "snap", byChan, global: statsFor(null),
    // The whole ring, not a feed-sized slice: on a tester's machine this is the
    // only copy of what happened, and the `diag` bundle is built from it.
    events: evring.slice(), enabled,
    log: logbuf.slice(-400),
  });
}

/* ---------------------------------------------------------------- start */

setInterval(() => chPush(false), CH_PUSH_MS);
setInterval(chSnapshot, 1000);
setInterval(chFlush, 1000);
setInterval(reap, REAP_MS);

/* Heartbeat: one line per channel plus one for the budget, so the log shows
 * progress even when nothing notable happens. */
setInterval(() => {
  rollup();
  for (const p of pools.values()) {
    const s = p.st;
    log(`stat channel=${s.channel} rend=${s.rendition} `
      + `media=${s.sawMedia} rebind=${s.rebind}/${s.flap}flap `
      + `blocked=${s.blockedBreaks}brk/${s.blockedSegs}seg `
      + `leaked=${s.leakedBreaks}brk/${s.leakedSegs}seg `
      + `served=${s.served} pass=${s.passthru} `
      + `ads=${s.adSegsSeen} segs=${s.segs} `
      + `fetched=${s.segFetch}/${s.segFetchAd}ad `
      + `grid=${s.onGrid}/${s.arms} skew=${s.skew} regrid=${s.regrid} `
      + `exposed=${s.exposed} collapse=${p.collapsed}/${s.collapses} `
      + `idle=${Math.round((Date.now() - p.lastPoll) / 1000)}s `
      + `err=${s.lastError || "-"}`);
    ev("stat", "debug", s);
  }
  log(`stat GLOBAL enabled=${enabled} pools=${pools.size} `
    + `donors=${stats.donors}/${MAX_DONORS} idled=${stats.idled} `
    + `master=${stats.sawMaster} media=${stats.sawMedia} `
    + `unk=${stats.unknownVariant} push=${chPushN} pend=${chPending.size} `
    + `canary=${stats.canaryBreaks}brk/${stats.canaryAdSegs}seg/${stats.canaryJoins}j `
    + `segwatch=${segWatch} err=${stats.lastError || "-"}`);
  if (!pools.size) ev("stat", "debug", stats);
}, 5000);

// Always looping, gated inside on CANARY: the sink can switch it on mid-run and
// the loop costs one 2s timer and no network while it is off.
new Canary().loop();

log(`unslop up (chrome): ${ARMS} arms [${TYPES.join(",")}], serving at`
  + ` >=${MIN_SERVE} segments, global donor cap ${MAX_DONORS},`
  + ` idle-out ${IDLE_MS / 1000}s, push every ${CH_PUSH_MS}ms`);
ev("up", "info", {
  arms: ARMS, types: TYPES, minServe: MIN_SERVE, window: WINDOW, canary: CANARY,
  donorCap: MAX_DONORS, idleMs: IDLE_MS, segWatch, shell: "chrome",
});
chSnapshot();
