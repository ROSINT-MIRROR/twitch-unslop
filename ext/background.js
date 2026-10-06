"use strict";
/*
 * unslop — the Firefox shell.
 *
 * Everything that decides behaviour is in core.js: the pool, the PDT grid, the
 * chain, the ad tells, the counters, the event stream. This file is only the
 * Gecko-specific wiring — request interception, the response filters that read
 * and rewrite playlist bodies, the popup/dashboard message port, the log sink,
 * and the timers that start it all.
 *
 * core.js loads first (manifest.json -> background.scripts) and defines its
 * globals in the shared script scope, so everything below just refers to them.
 * A port to another browser replaces THIS file and keeps core.js as it is.
 */

/* Is this request one of ours rather than the page's?
 *
 * This used to test `details.tabId < 0`, which silently disabled the whole
 * extension: Twitch's player fetches its playlists from a worker, so the
 * page's own requests ALSO arrive with tabId -1 and every one of them was
 * skipped. Origin is the thing that actually distinguishes us — our fetches
 * come from the background page, which is a moz-extension:// document. */
const isOurs = (d) =>
  (d.originUrl || "").startsWith("moz-extension://") ||
  (d.documentUrl || "").startsWith("moz-extension://");

/* ---- interception ------------------------------------------------------ */

/* The master tells us the channel and maps each variant URL to a rendition
 * name, so that when the player polls one we know which rendition to serve.
 * Passed through untouched. */
function onMaster(details) {
  if (isOurs(details)) { stats.skippedOwn++; return {}; }
  if (!enabled) return {};
  D("usher hit:", details.url.split("?")[0], `type=${details.type}`,
    `tab=${details.tabId}`, `origin=${details.originUrl || "-"}`);
  // The web player uses /api/v2/channel/hls/<chan>.m3u8 — measured 2026-07-29.
  // The unversioned /api/channel/hls/ path that every third-party tool (and
  // our own rig) uses still works, so match either.
  const m = /\/api\/(?:v(\d+)\/)?channel\/hls\/([^./?]+)\.m3u8/.exec(details.url);
  if (!m) {
    W("usher URL is not a channel master:", details.url.slice(0, 140));
    return {};
  }
  const channel = m[2].toLowerCase();
  const fmt = m[1] ? `v${m[1]}` : "v1";     // unversioned path IS v1
  stats.sawMaster++;

  const filter = browser.webRequest.filterResponseData(details.requestId);
  const dec = new TextDecoder("utf-8");
  let body = "";
  filter.ondata = (e) => { body += dec.decode(e.data, { stream: true }); filter.write(e.data); };
  filter.onstop = () => {
    try {
      const vs = parseMaster(body);
      log(`master ${channel}: ${vs.length} variants `
        + `[${vs.map((v) => `${v.name}=${v.rend}`).join(" ")}]`);
      if (!vs.length) W("master parsed to nothing. head:", body.slice(0, 400).replace(/\n/g, " | "));
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
    } catch (e) { stats.lastError = String(e); log("master parse failed:", e); }
    filter.close();
  };
  return {};
}

/* The channel, learned independently of usher. This is the FIRST thing the
 * player does and it carries the login in the request body, so it works even
 * when the master never passes through a shape we recognise. */
function onGql(details) {
  if (isOurs(details)) { stats.skippedOwn++; return {}; }
  if (!enabled) return {};
  try {
    const raw = details.requestBody && details.requestBody.raw
      && details.requestBody.raw[0] && details.requestBody.raw[0].bytes;
    if (!raw) return {};
    const txt = new TextDecoder("utf-8").decode(raw);
    if (!txt.includes("PlaybackAccessToken")) return {};
    const m = /"login":"([^"]+)"/.exec(txt);
    if (!m || !m[1]) return {};
    const channel = m[1].toLowerCase();
    logOnce("gql:" + channel, "gql PlaybackAccessToken login=" + channel);
    poolFor(channel);
  } catch (e) { log("gql hook:", e); }
  return {};
}

/* A poll we decided not to rewrite, watched anyway.
 *
 * These two paths used to return before the body existed, which meant the one
 * question that matters — was an ad on offer while we stood aside — had no
 * answer precisely where we failed. Byte-exact write-through, the same shape
 * onMaster has used all along: every chunk goes to the player untouched and we
 * only keep a copy. Reading the body is also what teaches the segment listener
 * which URLs are ads, so skipping it here would blind the ground truth exactly
 * during a miss. */
function observePass(details, pool, rend, decision) {
  const f = {
    chan: pool ? pool.channel : null,
    rend, poolRend: pool ? pool.store.rendition : null, decision, waited: 0,
    chain: pool ? pool.store.chain().length : 0,
    store: pool ? pool.store.seg.size : 0,
    outHash: null,
  };
  let filter = null;
  try { filter = browser.webRequest.filterResponseData(details.requestId); }
  catch (e) { stats.lastError = String(e); ev("media", "warn", f); return; }
  const dec = new TextDecoder("utf-8");
  let body = "";
  filter.ondata = (e) => {
    body += dec.decode(e.data, { stream: true });
    filter.write(e.data);
  };
  filter.onstop = () => {
    try {
      const real = realFacts(body, f.chan);
      f.mseq = real.mseq;
      f.realAds = real.ads;
      f.realSrc = real.src;
      // Always evidence: we stood aside, so what we stood aside for is the
      // whole record of the miss.
      f.realHash = keepManifest(body, true);
      if (pool) {
        pool.st.exposed = real.inAd;
        if (real.inAd) {
          pool.st.leakedSegs += real.ads;
          if (!pool.st.inAdSelf) pool.st.leakedBreaks++;
          E(`LEAKED: ad reached the player on ${pool.channel} —`
            + ` ${real.ads} ad segs, ${decision}`);
        }
        pool.st.inAdSelf = real.inAd;
      }
    } catch (e) { stats.lastError = String(e); }
    ev("media", "warn", f);
    filter.close();
  };
}

/* Replace the media playlist the player sees with our chain. If the pool
 * isn't warm we write the original body back — an ad is better than a stall. */
function onMediaPlaylist(details) {
  if (isOurs(details)) { stats.skippedOwn++; T("skip own media poll"); return {}; }
  if (!enabled) return {};
  stats.sawMedia++;
  T("media poll", variantKey(details.url).slice(-42),
    `tab=${details.tabId}`, `type=${details.type}`);
  const v = variantIndex.get(variantKey(details.url));
  if (!v) {
    // No mapping means we do not know which rendition this is. Guessing one
    // fed 720p segments into a 360p decode and froze the player; passing
    // through untouched is the correct failure.
    stats.unknownVariant++;
    W("unrecognised media playlist (no master mapping) — passing through:",
      variantKey(details.url).slice(-60), `| indexed=${variantIndex.size}`);
    observePass(details, null, null, "pass_unknown");
    return {};
  }

  const pool = poolFor(v.channel);
  // The heartbeat of the whole lifecycle: this is the only thing keeping the
  // pool alive, and its recency is what decides who keeps donors under the cap.
  pool.lastPoll = Date.now();
  pool.st.sawMedia++;
  pool.setRendition(v.rend);
  /* Serve the held rendition through a collapse instead of standing aside.
   *
   * This is where the leak was. usher pulls our rendition out of the ladder,
   * the player is forced to 360, that lands here as a mismatch, and we hand
   * over Twitch's own 360 playlist — which is the one carrying the ad. Our
   * chain is clean 1080 content that continues across the break, and MEDIA-
   * SEQUENCE cannot step backwards on it (the counter only ever advances, and
   * we do not reseed from the forced variant), so serving it is safe where
   * passing through is not. */
  const held = pool.store.rendition !== v.rend && pool.inCollapse();
  if (pool.store.rendition !== v.rend && !held) {
    // The rebind debounce refused this one (the player is flapping). Never
    // splice a rendition the player did not ask for.
    pool.st.passthru++;
    D(`rendition mismatch: player wants ${v.rend}, pool on ${pool.store.rendition}`
      + " (rebind debounced) — passing through");
    observePass(details, pool, v.rend, "pass_mismatch");
    return {};
  }
  if (held) {
    D(`${pool.channel}: serving held ${pool.store.rendition} to a player forced`
      + ` onto ${v.rend} by the collapsed ladder`);
  }

  const filter = browser.webRequest.filterResponseData(details.requestId);
  const dec = new TextDecoder("utf-8");
  const enc = new TextEncoder();
  let body = "";
  filter.ondata = (e) => { body += dec.decode(e.data, { stream: true }); };
  filter.onstop = async () => {
    // Read the ad tells out of the real playlist before we discard it. This is
    // the only place the viewer's own ad state is observable: everything below
    // writes our chain over the top and the evidence is gone.
    const real = realFacts(body, pool.channel);
    // Never seed from the forced variant: its MEDIA-SEQUENCE is its own, and
    // adopting it mid-collapse is exactly the backwards step that froze the
    // player at ready=2.
    if (real.mseq !== null && !held) pool.store.seedMseq(real.mseq);
    const selfAds = real.ads;
    const selfInAd = real.inAd;
    const selfBreakEdge = selfInAd && !pool.st.inAdSelf;
    pool.st.inAdSelf = selfInAd;

    // Hold the response open until the pool can serve, rather than handing
    // over the real playlist and swapping underneath the player later. That
    // swap is what froze playback: the player had already committed to a
    // rendition and a sequence number, and our first rewrite contradicted it.
    //
    // Only on the FIRST bind, though. Holding after a rebind starves the
    // player's bandwidth estimate, which drops the quality, which triggers
    // another rebind — measured 2026-07-29 as four rebinds in 19s flapping
    // 1080<->360. While re-warming we answer immediately instead.
    let out = null;
    const t0 = Date.now();
    const deadline = t0 + (pool.everServed ? 0 : WARM_WAIT_MS);
    for (;;) {
      try { out = pool.store.playlist(); } catch (e) { pool.st.lastError = String(e); }
      if (out || Date.now() >= deadline) break;
      await sleep(250);
    }
    // Snapshot the window before anything awaits again — a concurrent poll's
    // playlist() would move it under us.
    const win = out ? pool.store.win.slice() : [];
    const waited = Date.now() - t0;
    const chainLen = pool.store.chain().length;
    T(`serve decision rend=${pool.store.rendition} store=${pool.store.seg.size}`
      + ` chain=${chainLen} waited=${waited}ms`
      + ` -> ${out ? "REWRITE" : "passthrough"}`);
    let outHash = null;
    // Bodies are only worth disk when they are evidence. An ad-bearing poll and
    // anything we did not rewrite always are; a routine clean rewrite is not,
    // and keeping every one of those cost ~18MB/hour.
    const keep = selfAds > 0 || !out || held;
    pool.st.exposed = !out && selfInAd;
    if (out) {
      outHash = keepManifest(out, keep);
      pool.st.served++;
      pool.everServed = true;
      // Only now is it honest to call it blocked: the ad segments were in the
      // body Twitch sent this session, and we replaced that body.
      pool.st.blockedSegs += selfAds;
      if (selfBreakEdge) {
        pool.st.blockedBreaks++;
        log(`BLOCKED: Twitch served THIS session an ad on ${pool.channel}`
          + ` (${selfAds} ad segs, source=${real.src || "?"})`);
      }
      // Remember what we advertised: a segment fetch is only attributable to us
      // if the URL came out of a manifest we wrote.
      for (const k of win) {
        const s = pool.store.seg.get(k);
        if (s) ourSeg.set(s.url, { dur: s.dur, chan: pool.channel });
      }
      bound(ourSeg, MAX_SEGIDX);
      const first = win.length ? pool.store.seg.get(win[0]) : null;
      const last = win.length ? pool.store.seg.get(win[win.length - 1]) : null;
      ev("serve", "info", {
        chan: pool.channel, mseq: pool.store.mseq, n: win.length,
        firstPdt: first ? first.iso : null,
        lastPdt: last ? last.iso : null,
        hash: outHash,
      });
    } else {
      pool.st.passthru++;
      // The ad went to the screen. This is the failure mode the whole design
      // exists to avoid, so it gets its own count rather than hiding in pass.
      if (selfInAd) {
        pool.st.leakedSegs += selfAds;
        if (selfBreakEdge) pool.st.leakedBreaks++;
        E(`LEAKED: ad reached the player on ${pool.channel} —`
          + ` ${selfAds} ad segs, pool was cold`
          + ` (grid=${pool.st.onGrid}/${pool.st.arms})`);
      }
      W(`${pool.channel}: pool cold (chain=${chainLen}/${MIN_SERVE},`
        + ` grid=${pool.st.onGrid}/${pool.st.arms}) — real playlist passed`
        + " through, ads included");
    }
    ev("media", out ? "info" : "warn", {
      chan: pool.channel,
      rend: v.rend, poolRend: pool.store.rendition, mseq: real.mseq,
      realAds: selfAds, realSrc: real.src,
      decision: out ? "rewrite" : "pass_cold",
      waited, chain: chainLen, store: pool.store.seg.size,
      collapsed: held || undefined,
      realHash: keepManifest(body, keep), outHash,
    });
    filter.write(enc.encode(out || body));
    filter.close();
  };
  return {};
}

// Our body is a different length than the original; a stale Content-Length
// truncates it.
function stripLength(details) {
  if (isOurs(details)) return {};
  return {
    responseHeaders: details.responseHeaders.filter(
      (h) => h.name.toLowerCase() !== "content-length"),
  };
}

/* Did an ad reach the screen?
 *
 * Nothing else in here can answer that. A manifest listing an ad proves an ad
 * was OFFERED; the player may never request those bytes, and `leaked` counts
 * offers. Only a fetch is playback.
 *
 * Registered with no extraInfoSpec, so it cannot block, redirect or delay a
 * segment — observation only, per the passive-observation rule. It also runs on
 * every rendition, not just the bound one: the player's ABR probe fetches a
 * rendition we never spliced, and an ad served there still reaches the screen. */
function onSegment(details) {
  if (isOurs(details)) return;
  const u = details.url;
  const seen = seenSeg.get(u);
  const our = ourSeg.get(u);
  const ad = !!(seen && seen.ad);
  // Attribute to the channel whose playlist carried the URL, not to whichever
  // pool polled last — with two streams open the latter is a coin toss.
  const chan = (seen && seen.chan) || (our && our.chan) || null;
  const p = chan ? pools.get(chan) : null;
  stats.segFetch++;
  if (ad) stats.segFetchAd++;
  if (p) {
    p.st.segFetch++;
    if (ad) p.st.segFetchAd++;
  }
  ev("segment", ad ? "error" : "trace", {
    url: u,
    ad,
    via: our ? "ours" : "real",
    dur: our ? our.dur : (seen ? seen.dur : undefined),
    chan: chan || undefined,
  });
  if (ad) {
    E(`AD ON SCREEN: player fetched an ad segment on ${chan || "?"}`
      + ` (dur=${seen.dur}s, via=${our ? "ours" : "real"}) ...${u.slice(-32)}`);
  }
}

/* Measured host set, not the documented one. Every segment URL in
 * data/manifests/*.media.*.m3u8 and every data/hunt session's manifests sits under
 * hls.ttvnw.net (j.cloudfront, chantenay.akamai, a.fastly — CLAUDE.md lists
 * only cloudfront), and the browser tap in data/logs/tap.*.jsonl adds
 * *.rufio.hls.live-video.net. Match the two suffixes rather than the POPs: the
 * leading label is per-session hex and the CDN behind it rotates.
 *
 * `hls.ttvnw.net` cannot collide with the playlist listener — those are on
 * `*.playlist.ttvnw.net`, a different label. */
const SEG_HOSTS = ["*://*.hls.ttvnw.net/*", "*://*.hls.live-video.net/*"];

/* Register the segment listener, and tell core.js whether it took.
 *
 * `segWatch` is declared in core.js because statsFor() is the thing that has to
 * honour it; setting it is the shell's job, since the registration is what can
 * fail. A missing host permission is silent, and `adSegsPlayed` must then read
 * as "not measured" rather than as a confident zero. */
try {
  browser.webRequest.onBeforeRequest.addListener(onSegment, { urls: SEG_HOSTS });
  segWatch = true;
} catch (e) {
  stats.lastError = String(e);
  E("segment listener did not register — ad playback cannot be measured:", e);
}

// Deliberately the whole usher host, not just /api/channel/hls/: if the player
// asks for the master by some other path we want it in the log, not silently
// dropped.
browser.webRequest.onBeforeRequest.addListener(
  onMaster, { urls: ["*://usher.ttvnw.net/*"] }, ["blocking"]);

browser.webRequest.onBeforeRequest.addListener(
  onGql, { urls: ["*://gql.twitch.tv/gql*"] }, ["requestBody"]);

browser.webRequest.onBeforeRequest.addListener(
  onMediaPlaylist, { urls: ["*://*.playlist.ttvnw.net/*"] }, ["blocking"]);

browser.webRequest.onHeadersReceived.addListener(
  stripLength, { urls: ["*://*.playlist.ttvnw.net/*"] },
  ["blocking", "responseHeaders"]);

browser.runtime.onMessage.addListener((msg) => {
  const cmd = typeof msg === "string" ? msg : (msg && msg.cmd);
  if (cmd === "stats") {
    return Promise.resolve(statsFor(msg && msg.channel));
  }
  // A ring of its own, because evbuf belongs to the sink and is emptied every
  // second. Newest last; the UI dedupes on `t`.
  if (cmd === "events") return Promise.resolve(evring.slice());

  /* Everything a bug report needs, in one file.
   *
   * On a developer box the evidence is data/ext/<session>/events.jsonl. A beta
   * tester has no sink, so without this their report is "I think I saw an ad on
   * some channel earlier" and there is nothing to check it against. The ring is
   * restored from storage first, so a browser restart between the incident and
   * the report does not lose it. */
  if (cmd === "diag") {
    return restoreRing().then(() => ({
      version: (browser.runtime.getManifest() || {}).version || "?",
      ua: navigator.userAgent,
      saved: new Date().toISOString(),
      stats: rollup(),
      // Newest last, matching events.jsonl, so ext/extreport.py can read the
      // `events` array of this file directly.
      events: evring.slice(),
      log: logbuf.slice(-400),
    }));
  }
  if (cmd === "enable") {
    setEnabled(!!msg.on);
    return Promise.resolve(statsFor(msg && msg.channel));
  }
  // Drained by the content script only — the popup must not steal these lines.
  if (cmd === "drain") return Promise.resolve(logbuf.splice(0, logbuf.length));
});

/* Ship logs straight to a local sink rather than through the content script
 * and the DOM. ext/tryout.py listens there and appends to data/ext/ext.log.
 * The DOM path depends on a content script running, page attributes surviving
 * a React app, and Selenium reading them out of a sandbox — three things that
 * can each silently fail. This one either connects or it doesn't. */
const SINK = "http://127.0.0.1:8779/log";

/* On a developer's box the sink is `ext/tryout.py` and it is always there. On a
 * beta tester's box nothing is listening, and a POST per second to a dead port
 * forever is both a waste and the kind of undisclosed background traffic an AMO
 * reviewer flags. So the sink is opportunistic: try a handful of times, then
 * stop for good and free the buffers.
 *
 * Deliberately not a build flag. One artifact behaves correctly in both places,
 * and there is no build step to set a flag in. */
const SINK_TRIES = 5;
let sinkFails = 0;
let sinkOff = false;

/* Put an unshipped batch back in front of whatever accumulated while we were
 * away, then re-apply the cap. Spread-free: `unshift(...batch)` with 20k events
 * is an argument-count gamble, and an unshift loop is quadratic. */
function rebuffer(buf, batch, max) {
  const merged = batch.concat(buf);
  buf.length = 0;
  for (let i = Math.max(0, merged.length - max); i < merged.length; i++) {
    buf.push(merged[i]);
  }
}

/* Keep the event ring across a browser restart.
 *
 * `evring` is memory-only, so without this a tester who restarts before writing
 * up what they saw has nothing left to send. Storage is the only durable place
 * an extension has, and this is small: a few hundred KB against a 10MB quota.
 * Deliberately in the shell rather than core.js — core may not name an
 * extension API, and ext/selftest.mjs asserts it. */
const RING_KEY = "unslop.ring";
let ringLoaded = false;

async function saveRing() {
  try {
    await browser.storage.local.set({ [RING_KEY]: evring.slice(-EV_RING) });
  } catch (e) { /* quota or private browsing — the live ring still works */ }
}

async function restoreRing() {
  if (ringLoaded) return;
  ringLoaded = true;
  try {
    const got = await browser.storage.local.get(RING_KEY);
    const old = (got && got[RING_KEY]) || [];
    if (!old.length) return;
    // Merge under the cap, oldest first, without duplicating what this run has
    // already recorded.
    const have = new Set(evring.map((e) => e.t + "|" + e.ev));
    const merged = old.filter((e) => e && !have.has(e.t + "|" + e.ev)).concat(evring);
    evring.length = 0;
    for (const e of merged.slice(-EV_RING)) evring.push(e);
  } catch (e) { /* nothing stored yet */ }
}

restoreRing();
setInterval(saveRing, 30000);

async function flushLogs() {
  if (sinkOff) {
    // No sink is collecting, so the unshipped buffers are dead weight — but
    // `evring` is NOT one of them, and neither is the tail of logbuf: on a
    // tester's machine those two are the entire bug report. This used to wipe
    // all of it every second.
    if (logbuf.length > 400) logbuf.splice(0, logbuf.length - 400);
    evbuf.length = 0;
    manbuf.length = 0;
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
    const r = await fetch(SINK, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lines, events, manifests, stats: rollup() }),
    });
    if (r.ok) {
      sinkFails = 0;
      const d = await r.json().catch(() => null);
      if (d && d.level) setLevel(d.level);
      // Same channel as the log level, for the same reason: the background page
      // cannot see run.sh's argv, and the canary is off by default now.
      if (d && typeof d.canary === "boolean" && d.canary !== CANARY) {
        CANARY = d.canary;
        I(`canary probe -> ${CANARY ? "on" : "off"}`);
      }
    }
  } catch (e) {
    // sink not up — keep everything, but never grow without bound
    rebuffer(logbuf, lines, LOG_MAX);
    rebuffer(evbuf, events, EV_MAX);
    rebuffer(manbuf, manifests, MAN_MAX);
    if (++sinkFails >= SINK_TRIES) {
      sinkOff = true;
      console.log("[unslop] no log sink on 127.0.0.1:8779 — logging off");
    }
  }
}
setInterval(flushLogs, 1000);

/* Heartbeat: one line per channel plus one for the budget, so the log shows
 * progress even when nothing notable happens. It used to be a single line whose
 * `channel=` was whichever pool polled last, which read as one session flapping
 * between two channels rather than as two sessions. */
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
    + `unk=${stats.unknownVariant} `
    + `canary=${stats.canaryBreaks}brk/${stats.canaryAdSegs}seg/${stats.canaryJoins}j `
    + `segwatch=${segWatch} err=${stats.lastError || "-"}`);
  if (!pools.size) ev("stat", "debug", stats);
}, 5000);

setInterval(reap, REAP_MS);
// Always looping, gated inside on CANARY: the sink can switch it on mid-run and
// the loop costs one 2s timer and no network while it is off.
new Canary().loop();

log(`unslop up: ${ARMS} arms [${TYPES.join(",")}], serving at >=${MIN_SERVE} segments,`
  + ` global donor cap ${MAX_DONORS}, idle-out ${IDLE_MS / 1000}s`);
ev("up", "info", {
  arms: ARMS, types: TYPES, minServe: MIN_SERVE, window: WINDOW, canary: CANARY,
  donorCap: MAX_DONORS, idleMs: IDLE_MS, segWatch,
});
