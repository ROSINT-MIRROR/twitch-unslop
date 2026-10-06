"use strict";
/*
 * unslop core — every part of the extension that has no extension API in it.
 *
 * Split out of background.js on 2026-07-29 so the Chrome port can share this
 * file verbatim instead of reimplementing it: the m3u8 parsing, the PDT grid,
 * the donor pool, the chain walk, ladder-collapse detection, the ad tells, the
 * counters and the event stream all live here. That is the part that took all
 * day to get right and it is not browser-specific in any way.
 *
 * The rule that keeps the split honest, asserted by ext/selftest.mjs: nothing
 * in this file may name an extension API. fetch, timers, URL, TextDecoder only
 * — things a page, a worker and node's vm all have.
 *
 * Loaded FIRST in manifest.json's background.scripts array, ahead of the
 * Firefox shell in background.js. Plain script concatenation, no modules and no
 * build step, so these globals are simply visible to whatever loads next.
 *
 * unslop — strip Twitch SSAI ads without blanking the player.
 *
 * This is a port of unslop.py. It is NOT a filter. Deleting ad segments from
 * the viewer's own manifest does not work: once a Twitch session takes an ad,
 * the origin re-cuts its content timeline from wherever the pod ended and that
 * session is permanently off the segment grid its ad-free peers share
 * (measured 2026-07-29: 1431ms off, 0 of 39 post-ad segments re-converged).
 * Its post-break content would overlap whatever we spliced in.
 *
 * So we run our own pool of parallel sessions, keep them on the canonical grid
 * by retiring any that takes an ad, and replace the media playlist body the
 * player sees with a chain built from the pool. The player fetches segment
 * bytes straight from Twitch's CDN, so this costs no video bandwidth.
 *
 * Splice key is #EXT-X-PROGRAM-DATE-TIME. Sessions that have not taken an ad
 * emit byte-identical PDTs for the same broadcast moment, so clean segments
 * dedupe on the exact PDT with no tolerance. There is no fixed cadence to
 * quantize to (content 4.166/4.167s, stitched ads exactly 2.000s), so every
 * alignment decision uses the segment's own declared EXTINF duration.
 */

const CID = "kimne78kx3ncx6brgo4mv6wki5h1ko";
const PQ = "0828119ded1c13477966434e15800ff57ddacf13ba1911c129dc2200705b0712";

const ARMS = 4;               // parallel donor sessions per channel
// Donors only — never `embed`. Measured over 1979 joins: embed draws 46.3 ad
// sessions per 100 joins against 12.5 for frontpage and 12.7 for site, so an
// embed arm is the one most likely to be dark exactly when we need it. (The
// measurement rig runs embed on purpose, to GENERATE breaks. Different job.)
const TYPES = ["site", "frontpage"];
const WINDOW = 12;            // segments advertised to the player
const MIN_SERVE = 6;          // below this we pass the real playlist through
const WARM_WAIT_MS = 8000;      // how long to hold a playlist waiting for donors
const POLL_MS = 2000;
const GAP_MS = 500;           // slack before a PDT gap counts as a real hole
const REGRID_MS = 30000;      // min between an arm's post-ad rejoins
const REGRID_FAST_MS = 8000;  // ...when the whole pool is dark and we're desperate
const REBIND_MS = 5000;       // min between following the player's quality changes
const FLAP_MS = 15000;        // a rebind undone inside this is flapping, not tuning
/* A non-donating `embed` session whose only job is to answer "was there an ad
 * to block?". Donor types draw ~12.5 ad sessions per 100 joins, embed 46.3, so
 * the pool going quiet tells us nothing on its own. Rejoins often because
 * prerolls fire on join.
 *
 * Off by default, and ONE probe globally rather than one per channel. It was a
 * 5th session per pool, so three open streams meant three extra `embed` joins
 * on a 45s rejoin loop — the join pattern that draws the most ads of any we
 * measured. A diagnostic that manufactures the thing it measures, times the
 * number of tabs, is not one a beta tester should be running. `run.sh` flips it
 * on for measurement runs. */
let CANARY = false;
const CANARY_REJOIN_MS = 45000;
const MAX_SEG = 400;
const MAX_SEGIDX = 2000;      // segment URLs remembered for the `segment` event

/* Total donor sessions across EVERY channel, not per channel.
 *
 * CLAUDE.md caps the whole rig at ~16 concurrent playlist pollers and says this
 * must never resemble a load test. Pools used to be immortal, so the count was
 * cumulative over every channel ever *visited*: browse five streams in an hour
 * and 20 donors were still polling. 12 leaves headroom for the canary and for
 * hunt.py running alongside. */
const MAX_DONORS = 12;
const IDLE_MS = 30000;        // no player poll for this long -> tear the pool down
const REAP_MS = 5000;
/* Ceiling on a ladder collapse. A pod measured 15.2s and the collapse clears
 * when the full ladder returns, so this only exists so a master that never
 * comes back cannot pin the pool to a rendition forever. */
const COLLAPSE_MAX_MS = 90000;

// PDT immediately precedes its EXTINF/url pair in both clean and ad playlists
const RE_PDTSEG =
  /#EXT-X-PROGRAM-DATE-TIME:(\S+)\s*\n(?:#EXT-X-[^\n]*\n)*?#EXTINF:([\d.]+),([^\r\n]*)\r?\n(\S+)/g;
const RE_TARGET = /#EXT-X-TARGETDURATION:(\d+)/;
const RE_SOURCE = /X-TV-TWITCH-STREAM-SOURCE="([^"]*)"/;
const RE_MSEQ = /#EXT-X-MEDIA-SEQUENCE:(\d+)/;

/* One of these per Pool. It used to be a single global, which meant two open
 * streams overwrote each other's `channel` and `rendition` every second and
 * silently summed each other's `served`/`blockedBreaks` with no attribution —
 * so no number in the UI belonged to the channel it was displayed next to. */
function newStats(channel) {
  return {
    channel, rendition: null, arms: 0, onGrid: 0,
    segs: 0, adSegsSeen: 0, skew: 0, served: 0, passthru: 0, regrid: 0,
    sawMedia: 0, rebind: 0, flap: 0,
    // Ads found in the VIEWER'S OWN playlist, i.e. ones Twitch actually served
    // to this session and we overwrote. Without this, `ads` only counts what the
    // donors saw and `ads=0` is unreadable — it cannot distinguish "the splice
    // worked" from "nothing was on offer". The canary can't answer it either:
    // it is an `embed` session (46 ads/100 joins vs `site`'s 12), so it proves
    // ads existed on the channel, not that this viewer was targeted.
    blockedBreaks: 0, blockedSegs: 0, leakedBreaks: 0, leakedSegs: 0,
    // Segment fetches the PLAYER made. `segFetchAd` is the only number here that
    // means "an ad reached the screen" — every other ad counter describes a
    // manifest, and a manifest that lists an ad proves nothing about playback.
    segFetch: 0, segFetchAd: 0,
    inAdSelf: false,
    collapses: 0,
    // Right now, not ever: we are handing this channel a playlist that lists an
    // ad. Recomputed on every poll, so it clears itself.
    exposed: false,
    lastError: null,
  };
}

/* The roll-up. Budget, the heartbeat line, and the sink's `stats` field — that
 * is all. Nothing may read "the current channel" from here; per-channel answers
 * come from a Pool. `channel`/`channels` are kept only so ext/tryout.py's log
 * header still resolves, and `channel` explicitly means "most recently polled",
 * not "the one the user is watching" — we deliberately never guess that. */
const stats = {
  channel: null, channels: [], enabled: true, pools: 0,
  donors: 0, donorCap: MAX_DONORS, idled: 0,
  rendition: null, arms: 0, onGrid: 0,
  segs: 0, adSegsSeen: 0, skew: 0, served: 0, passthru: 0, regrid: 0,
  sawMaster: 0, sawMedia: 0, skippedOwn: 0, unknownVariant: 0, rebind: 0,
  flap: 0, canaryJoins: 0, canaryBreaks: 0, canaryAdSegs: 0,
  blockedBreaks: 0, blockedSegs: 0, leakedBreaks: 0, leakedSegs: 0,
  segFetch: 0, segFetchAd: 0,
  lastError: null,
};

// Per-pool counters that roll up by addition. Anything not listed is either
// global-only or meaningless as a sum.
const SUMMED = [
  "segs", "adSegsSeen", "skew", "served", "passthru", "regrid", "sawMedia",
  "rebind", "flap", "blockedBreaks", "blockedSegs", "leakedBreaks",
  "leakedSegs", "segFetch", "segFetchAd", "arms", "onGrid", "collapses",
];

let enabled = true;

const pools = new Map();       // channel -> Pool
const variantIndex = new Map();  // variant key -> {channel, rend}
const ladderBest = new Map();  // channel -> biggest ladder seen, as [rend, ...]

// Key on origin+path, never the raw URL: the player appends its own query
// params to the variant URL it got from the master, so exact-string matching
// misses every poll.
function variantKey(u) {
  try { const x = new URL(u); return x.origin + x.pathname; }
  catch (e) { return u; }
}

/* Leveled logging, shipped to the local sink and appended to data/ext/ext.log.
 *
 * Every bug in this thing so far has been invisible rather than hard: the
 * whole extension was disabled by a tabId check, the master was fetched from a
 * path we did not match, rendition names did not survive across two master
 * formats. None of them threw. So trace everything that decides behaviour —
 * every intercepted request, every parse result, every serve/passthrough
 * decision — and pick the level at run time instead of adding a log line after
 * each surprise.
 *
 * Level comes back in the sink's response to our own POST, so `run.sh <chan>
 * <secs> trace` reaches the extension with no extra permission or plumbing. */
const LEVELS = { error: 0, warn: 1, info: 2, debug: 3, trace: 4 };
let LEVEL = LEVELS.debug;

const LOG_MAX = 2000;
const EV_MAX = 20000;
const MAN_MAX = 200;       // unshipped bodies; these are kilobytes each
const MAN_SEEN_MAX = 20000;
/* The dashboard's live feed needs a few dozen entries. A beta tester reporting
 * "I saw an ad at some point" needs hours. This is the only evidence that
 * exists on a machine with no log sink, so it is sized for the report, not for
 * the feed: ~3000 events is roughly an hour of watching at a few hundred KB. */
const EV_RING = 3000;
const MAN_KEEP_FIRST = 20;   // bodies kept unconditionally, for sanity checking
const logbuf = [];
const evbuf = [];
const evring = [];      // for the dashboard; never drained
const manbuf = [];      // manifest bodies not yet shipped
const manSent = new Set();
const logged = new Set();
let manKept = 0;

function emit(lvl, ...a) {
  if (LEVELS[lvl] > LEVEL) return;
  const line = `${lvl.toUpperCase().padEnd(5)} ${a.join(" ")}`;
  logbuf.push(line);
  if (logbuf.length > LOG_MAX) logbuf.shift();
  console.log("[unslop]", line);
}

/* The structured stream. `ext.log` is a rendering; THIS is the source of
 * truth, and ext/EVENTS.md is its contract.
 *
 * Prose lines were costing a bespoke awk incantation per question, and the two
 * questions that actually matter — "did an ad reach the screen" and "when we
 * missed, was it our fault or the donors'" — were not answerable from them at
 * all, because the facts needed were never recorded. One object per fact,
 * queryable with jq, analysed by ext/extreport.py.
 *
 * Unlike `emit`, events are NOT dropped by level: level filters what a human
 * reads, never what gets recorded. A trace-level run and an info-level run must
 * produce the same evidence or the evidence is worthless. */
function ev(name, lvl, fields) {
  const e = { t: Date.now(), ev: name, lvl };
  for (const k in fields) if (fields[k] !== undefined) e[k] = fields[k];
  evbuf.push(e);
  if (evbuf.length > EV_MAX) evbuf.shift();
  // A second, undrained ring for the dashboard. It cannot read evbuf: the sink
  // empties that every second, so a 1s-polling UI would see a random slice of
  // the stream. `stat` and clean `segment` events are left out — one burst of
  // ordinary segment fetches would push every ad out of a bounded ring.
  if (name !== "stat" && !(name === "segment" && !e.ad)) {
    evring.push(e);
    if (evring.length > EV_RING) evring.shift();
  }
  return e;
}

// Map and Set both iterate in insertion order, so the front is always the
// oldest entry. Used for every unbounded-by-nature collection in here.
function bound(m, max) {
  while (m.size > max) m.delete(m.keys().next().value);
}

/* Cheap non-cryptographic content hash. Only has to distinguish playlist
 * bodies within one session, so FNV-1a is plenty and avoids dragging in
 * SubtleCrypto's async API on a hot path. */
function hash(s) {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, "0") + "-" + s.length.toString(36);
}

/* Hash every body, keep only the ones that are evidence.
 *
 * "What exactly did we hand the player" is unanswerable after the fact without
 * the body, and that is what made the last leak un-diagnosable — but keeping
 * every distinct playlist is ~18MB an hour, which is not a thing to leave on a
 * beta tester's disk. A poll with no ad in it that we rewrote normally proves
 * nothing; the hash in the event still pins its identity if it ever matters.
 *
 * `keep` is the caller's judgement: any ad-bearing body, anything we did not
 * rewrite, and the first few of a session regardless. */
function keepManifest(body, keep) {
  const h = hash(body);
  if (keep === false && manKept >= MAN_KEEP_FIRST) return h;
  if (!manSent.has(h)) {
    manSent.add(h);
    manKept++;
    manbuf.push({ hash: h, body });
    if (manbuf.length > MAN_MAX) manbuf.shift();
    // Forgetting a hash only costs one redundant re-send; the sink keys on the
    // hash, so an overnight run must not pay for perfect memory.
    bound(manSent, MAN_SEEN_MAX);
  }
  return h;
}

/* Segment URL -> what the playlist that carried it said about it.
 *
 * `seenSeg` is filled only from the PLAYER'S OWN real playlist bodies, so
 * `ad` means "Twitch offered this session an ad segment at this URL" — the
 * question the donors' playlists cannot answer. `ourSeg` is what we advertised,
 * which is how a later fetch is attributed to us rather than to Twitch. */
const seenSeg = new Map();     // url -> {dur, ad, chan}
const ourSeg = new Map();      // url -> {dur, chan}

/* Ad tells out of a real playlist body, plus the segment URLs behind them.
 * Recording the URL is the whole point: a manifest listing an ad proves only
 * that one was offered, never that the player fetched it. The channel rides
 * along so a fetch counts against the right pool — with several streams open,
 * an unattributed ad segment is worse than none. */
function realFacts(body, chan) {
  let ads = 0;
  const re = new RegExp(RE_PDTSEG.source, "g");
  let m;
  while ((m = re.exec(body)) !== null) {
    const ad = m[3].trim() !== "live";
    if (ad) ads++;
    seenSeg.set(m[4], { dur: parseFloat(m[2]), ad, chan });
  }
  bound(seenSeg, MAX_SEGIDX);
  const src = RE_SOURCE.exec(body);
  const mseq = RE_MSEQ.exec(body);
  return {
    ads,
    src: src ? src[1] : null,
    mseq: mseq ? +mseq[1] : null,
    inAd: ads > 0 || !!(src && src[1] !== "live"),
  };
}

const E = (...a) => emit("error", ...a);
const W = (...a) => emit("warn", ...a);
const I = (...a) => emit("info", ...a);
const D = (...a) => emit("debug", ...a);
const T = (...a) => emit("trace", ...a);

// kept for the call sites that want info level
const log = I;

function logOnce(key, ...a) {
  if (logged.has(key)) return;
  logged.add(key);
  I(...a);
}

function setLevel(name) {
  if (!(name in LEVELS) || LEVELS[name] === LEVEL) return;
  LEVEL = LEVELS[name];
  I(`log level -> ${name}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const randHex = (n) =>
  Array.from({ length: n }, () => "0123456789abcdef"[(Math.random() * 16) | 0]).join("");

async function mint(channel, deviceId, playerType) {
  const r = await fetch("https://gql.twitch.tv/gql", {
    method: "POST",
    headers: {
      "Client-ID": CID,
      "Content-Type": "application/json",
      "X-Device-Id": deviceId,
      "Device-ID": deviceId,
    },
    body: JSON.stringify({
      operationName: "PlaybackAccessToken",
      variables: { isLive: true, login: channel, isVod: false, vodID: "", playerType },
      extensions: { persistedQuery: { version: 1, sha256Hash: PQ } },
    }),
  });
  const d = await r.json();
  const node = d && d.data && d.data.streamPlaybackAccessToken;
  if (!node) throw new Error(`${channel}: no token (offline?)`);
  return node;
}

/* Identify a rendition by what actually has to match for a splice —
 * resolution and frame rate — never by its name.
 *
 * Twitch serves two structurally different masters. `/api/channel/hls/` (what
 * our rig and every third-party tool uses) carries #EXT-X-MEDIA lines and
 * calls the source rendition VIDEO="chunked". The web player's
 * `/api/v2/channel/hls/` has no #EXT-X-MEDIA lines at all and calls that same
 * rendition STABLE-VARIANT-ID="1080p60". Measured 2026-07-29: keying on the
 * name meant the player's master and our donors' master never agreed on a
 * single rendition, so the pool never bound one and never donated a segment. */
function rendKey(streamInf) {
  const r = /RESOLUTION=(\d+x\d+)/.exec(streamInf);
  if (!r) return "audio";
  const f = /FRAME-RATE=([\d.]+)/.exec(streamInf);
  return `${r[1]}@${f ? Math.round(parseFloat(f[1])) : 30}`;
}

/* [{rend, name, url}] — usher also randomises the ORDER of this list between
 * mints and hands each player_type a different ladder, so never take "the
 * first variant" either. */
function parseMaster(txt) {
  const lines = txt.split("\n");
  const out = [];
  let mediaName = null;
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (l.startsWith("#EXT-X-MEDIA:") && l.includes("TYPE=VIDEO")) {
      const g = /GROUP-ID="([^"]*)"/.exec(l);
      mediaName = g ? g[1] : null;
    } else if (l.startsWith("#EXT-X-STREAM-INF") && i + 1 < lines.length) {
      const u = lines[i + 1].trim();
      if (u.startsWith("http")) {
        const v = /VIDEO="([^"]*)"/.exec(l);
        const s = /STABLE-VARIANT-ID="([^"]*)"/.exec(l);
        const b = /[^-]BANDWIDTH=(\d+)/.exec(l);
        const rend = rendKey(l);
        out.push({
          rend,
          name: (v && v[1]) || (s && s[1]) || mediaName || rend,
          bw: b ? +b[1] : null,
          url: u,
        });
      }
      mediaName = null;
    }
  }
  return out;
}

async function ladder(channel, node) {
  const q = new URLSearchParams({
    client_id: CID, token: node.value, sig: node.signature,
    allow_source: "true", allow_audio_only: "true", fast_bread: "true",
    player_backend: "mediaplayer", supported_codecs: "h264",
    p: String(1 + ((Math.random() * 9999999) | 0)),
  });
  const r = await fetch(`https://usher.ttvnw.net/api/channel/hls/${channel}.m3u8?${q}`);
  if (!r.ok) throw new Error(`usher ${r.status}`);
  return parseMaster(await r.text());
}

class Store {
  constructor(st) {
    this.st = st || null;      // the owning Pool's counters; absent in tests
    this.seg = new Map();      // pdtMs -> {dur, url, iso}, clean only
    this.skewed = new Set();
    this.win = [];
    this.mseq = 0;
    this.target = 6;
    this.rendition = null;     // set from whatever the player asked for
    this.seeded = false;
  }

  /* Continue the player's own MEDIA-SEQUENCE rather than restarting at 0.
   * Measured 2026-07-29: Twitch was at 2295, we served 0, and the player
   * froze at ready=2 the instant we took over — a sequence that steps
   * backwards reads as a playlist reset and the engine rebuffers forever. */
  seedMseq(n) {
    if (!this.seeded && Number.isFinite(n)) {
      this.mseq = n;
      this.seeded = true;
      log(`MEDIA-SEQUENCE seeded from the player's own playlist at ${n}`);
    }
  }

  offer(ms, dur, url, iso, isAd, rend) {
    if (isAd) { if (this.st) this.st.adSegsSeen++; return; }
    // Two renditions have identical PDTs, so they splice together silently and
    // play as a resolution flip mid-stream. Only ever store the one the player
    // is actually asking for.
    if (this.rendition && rend !== this.rendition) return;
    if (!this.seg.has(ms)) {
      this.seg.set(ms, { dur, url, iso });
      if (this.st) this.st.segs++;
    }
    if (this.seg.size > MAX_SEG) {
      const ks = [...this.seg.keys()].sort((a, b) => a - b);
      for (const k of ks.slice(0, -MAX_SEG / 2)) this.seg.delete(k);
      const cut = ks[ks.length - MAX_SEG / 2];
      for (const k of this.skewed) if (k < cut) this.skewed.delete(k);
    }
  }

  /* Walk BACK from the newest segment, emitting a non-overlapping timeline.
   * Newest-first because the window we serve is the tail: walking forward from
   * the oldest key let one off-phase segment minutes back pick the phase for
   * everything after it. Anchored on the tail, a bad phase costs at most the
   * current window. */
  chain() {
    const keys = [...this.seg.keys()].sort((a, b) => b - a);
    const out = [];
    let nxt = null;
    for (const k of keys) {
      const s = this.seg.get(k);
      if (nxt !== null && k + Math.round(s.dur * 1000) > nxt + GAP_MS) {
        this.skewed.add(k);
        continue;
      }
      out.push(k);
      nxt = k;
      if (out.length >= WINDOW) break;
    }
    if (this.st) this.st.skew = this.skewed.size;
    return out.reverse();
  }

  playlist() {
    // Serve as soon as there is enough to start on. Demanding a full WINDOW
    // means one arm mid-ad can hold the whole pool below the bar and we pass
    // the real (ad-bearing) playlist through instead.
    const keys = this.chain();
    if (keys.length < MIN_SERVE) return null;
    // HLS semantics: MEDIA-SEQUENCE is the index of the first segment
    // advertised, so it advances by however many slid out the front.
    this.mseq += this.win.filter((k) => k < keys[0]).length;
    this.win = keys;
    const out = [
      "#EXTM3U", "#EXT-X-VERSION:3",
      `#EXT-X-TARGETDURATION:${this.target}`,
      `#EXT-X-MEDIA-SEQUENCE:${this.mseq}`,
    ];
    let end = null;
    for (const k of keys) {
      const s = this.seg.get(k);
      // A real hole: this segment doesn't start where the last one ended.
      // Declared durations are exact, so only a genuine gap gets a
      // discontinuity — not every segment boundary.
      if (end !== null && k - end > GAP_MS) out.push("#EXT-X-DISCONTINUITY");
      out.push(`#EXT-X-PROGRAM-DATE-TIME:${s.iso}`);
      out.push(`#EXTINF:${s.dur.toFixed(3)},live`);
      out.push(s.url);
      end = k + Math.round(s.dur * 1000);
    }
    return out.join("\n") + "\n";
  }
}

class Arm {
  constructor(pool, i) {
    this.pool = pool;
    this.n = i;
    this.name = `arm${i}`;
    this.playerType = TYPES[i % TYPES.length];
    this.url = null;
    this.rend = null;
    this.dirty = false;        // this session has taken an ad
    this.lad = null;           // this session's full ladder, for retune()
    this.lastRegrid = 0;
    this.fails = 0;
    this.onGrid = false;
    // Donors above the global cap are parked, not destroyed: the quota moves
    // with which channel is still polling, and a parked arm rejoins for free.
    this.active = true;
  }

  setActive(on) {
    if (this.active === on) return;
    this.active = on;
    if (!on) { this.url = null; this.onGrid = false; }
  }

  async join() {
    const dev = randHex(32);
    const node = await mint(this.pool.channel, dev, this.playerType);
    const lad = await ladder(this.pool.channel, node);
    const want = this.pool.store.rendition;
    const v = lad.find((x) => x.rend === want);
    if (!v) throw new Error(`${this.playerType}: no ${want} in ladder`);
    this.lad = lad;          // keep it: a quality change is then just a retune
    this.url = v.url;
    this.rend = v.rend;
  }

  /* Switch renditions inside the session we already have.
   *
   * A quality change used to null the URL, forcing a full re-mint: GraphQL,
   * then usher, then a first poll, ~4s during which the pool served nothing
   * and the player rebuffered. The ladder we fetched at join already carries
   * every rendition, so this is a pointer move. It also avoids a fresh mint,
   * and fresh mints are exactly what draws prerolls. */
  retune(rend) {
    if (!this.lad) return false;
    const v = this.lad.find((x) => x.rend === rend);
    if (!v) return false;
    this.url = v.url;
    this.rend = rend;
    return true;
  }

  /* Drop a session that has taken an ad — it is off-grid for good, and is
   * dead weight as a donor no matter how clean its video is. A fresh mint
   * lands back on the canonical grid. Prerolls fire on join, so a rejoin can
   * draw a new ad: hence the cooldown, or this becomes a mint loop. */
  retire() {
    const now = Date.now();
    // If nothing in the pool is on grid we are already handing the viewer an
    // ad, so rejoin aggressively. Otherwise back off — prerolls fire on join,
    // and an unthrottled retire loop just mints fresh ads for itself.
    const cool = this.pool.arms.some((a) => a.onGrid) ? REGRID_MS : REGRID_FAST_MS;
    if (now - this.lastRegrid < cool) return;
    this.lastRegrid = now;
    this.url = null;
    this.dirty = false;
    this.onGrid = false;
    this.pool.st.regrid++;
    ev("regrid", "info", { n: this.n, ptype: this.playerType,
                           chan: this.pool.channel });
  }

  async loop() {
    while (this.pool.alive) {
      try {
        if (!this.active) { await sleep(1000); continue; }
        if (!this.pool.store.rendition) { await sleep(300); continue; }
        if (!this.url) await this.join();

        const r = await fetch(this.url);
        if (!r.ok) {                    // dead session — rejoin
          this.url = null;
          await sleep(2000);
          continue;
        }
        const body = await r.text();
        this.fails = 0;

        const t = RE_TARGET.exec(body);
        if (t) this.pool.store.target = Math.max(this.pool.store.target, +t[1]);

        const src = RE_SOURCE.exec(body);
        let sawAd = !!(src && src[1] !== "live");

        RE_PDTSEG.lastIndex = 0;
        let m;
        let added = 0;
        while ((m = RE_PDTSEG.exec(body)) !== null) {
          const [, iso, dur, title, url] = m;
          const isAd = title.trim() !== "live";
          sawAd = sawAd || isAd;
          const ms = Date.parse(iso);
          if (!Number.isFinite(ms)) continue;
          // Ask the store, don't diff its size: it evicts, and an eviction poll
          // would otherwise report a large negative donation.
          const had = this.pool.store.seg.has(ms);
          this.pool.store.offer(ms, parseFloat(dur), url, iso, isAd, this.rend);
          if (!had && this.pool.store.seg.has(ms)) added++;
        }

        this.onGrid = !sawAd && !this.dirty;
        T(`${this.name}(${this.playerType}) poll ad=${sawAd} dirty=${this.dirty}`
          + ` onGrid=${this.onGrid} store=${this.pool.store.seg.size}`);
        ev("arm", sawAd ? "info" : "trace", {
          n: this.n, ptype: this.playerType, rend: this.rend, ad: sawAd,
          onGrid: this.onGrid, new: added, hash: hash(body),
          chan: this.pool.channel,
        });
        if (sawAd) {
          if (!this.dirty) I(`${this.name}(${this.playerType}) took an ad`);
          this.dirty = true;
        } else if (this.dirty) this.retire();

        await sleep(POLL_MS + Math.random() * 300);
      } catch (e) {
        this.fails++;
        this.pool.st.lastError = String(e);
        this.url = null;
        this.onGrid = false;
        // Back off — the usual cause is the channel going offline, and every
        // arm would otherwise re-mint every couple of seconds for as long as
        // the stream is down.
        await sleep(Math.min(60000, 3000 * 2 ** Math.min(this.fails - 1, 5)));
      }
    }
  }
}

/* Ground truth for "did the channel serve an ad at all". It never donates a
 * segment; it only reports. Without it, ads=0 is unreadable — it could mean
 * the splice worked or it could mean nothing was on offer.
 *
 * ONE of these for the whole extension, following whichever channel polled
 * most recently. It used to be a fifth session per pool, which with three
 * streams open meant three `embed` sessions rejoining every 45s — and `embed`
 * on a rejoin loop is the single ad-drawing-est pattern we measured. It reads
 * whatever channel it is pointed at; it does not try to work out which one the
 * user cares about. */
class Canary {
  constructor() {
    this.url = null;
    this.chan = null;
    this.inAd = false;
    this.joinAt = 0;
  }

  async loop() {
    for (;;) {
      try {
        const want = enabled && CANARY ? recentChannel() : null;
        if (!want) { this.url = null; await sleep(2000); continue; }
        if (!this.url || this.chan !== want
            || Date.now() - this.joinAt > CANARY_REJOIN_MS) {
          const node = await mint(want, randHex(32), "embed");
          const lad = await ladder(want, node);
          // cheapest rendition — we only read tags, never the bytes
          const v = lad.find((x) => x.rend === "284x160@30") || lad[lad.length - 1];
          this.url = v.url;
          this.chan = want;
          this.joinAt = Date.now();
          this.inAd = false;
          stats.canaryJoins++;
          ev("canary", "debug", { kind: "join", chan: want, adSegs: 0,
                                  onGrid: stats.onGrid });
        }
        const body = await (await fetch(this.url)).text();
        let ads = 0;
        const re = new RegExp(RE_PDTSEG.source, "g");
        let m;
        while ((m = re.exec(body)) !== null) if (m[3].trim() !== "live") ads++;
        const src = RE_SOURCE.exec(body);
        const inAd = ads > 0 || !!(src && src[1] !== "live");
        if (inAd && !this.inAd) {
          stats.canaryBreaks++;
          log(`CANARY: ad break on ${this.chan} — the pool had to cover `
            + `this one (donors on grid: ${stats.onGrid}/${stats.arms})`);
          ev("canary", "warn", { kind: "break", chan: this.chan, adSegs: ads,
                                 onGrid: stats.onGrid });
        }
        this.inAd = inAd;
        stats.canaryAdSegs += ads;
        await sleep(POLL_MS + Math.random() * 300);
      } catch (e) {
        this.url = null;
        await sleep(5000);
      }
    }
  }
}

class Pool {
  constructor(channel) {
    this.channel = channel;
    this.st = newStats(channel);
    this.store = new Store(this.st);
    this.alive = true;
    this.lastRebind = 0;
    this.rebinds = [];         // recent {from, to, t}, for the flap count
    this.everServed = false;
    // The only liveness signal we use. Not mute, not focus, not audible: those
    // guess at intent and get PiP, a second monitor and audio-only listeners
    // wrong. A player that stopped asking for playlists is a player nobody is
    // being served by, which is the thing we actually need to know.
    this.lastPoll = Date.now();
    this.collapsed = false;    // usher has taken our rendition out of the ladder
    this.collapseAt = 0;
    this.ladderN = 0;
    this.quota = ARMS;
    this.arms = Array.from({ length: ARMS }, (_, i) => new Arm(this, i));
    for (const a of this.arms) a.loop();
    this.st.arms = ARMS;
  }

  /* Park or wake donors to match the global budget. Parked arms stay in the
   * array — the quota follows whichever channel is still polling, and a pool
   * that drops to two donors still covers breaks, just less of them. Degrade,
   * never exceed the cap. */
  setQuota(n) {
    if (n === this.quota) return;
    const from = this.quota;
    this.quota = n;
    for (let i = 0; i < this.arms.length; i++) this.arms[i].setActive(i < n);
    this.st.arms = n;
    I(`${this.channel}: donors ${from} -> ${n} (global cap ${MAX_DONORS})`);
    ev("pool", n < from ? "warn" : "info", {
      kind: "quota", chan: this.channel, from, to: n, cap: MAX_DONORS,
    });
  }

  stop() {
    this.alive = false;
    for (const a of this.arms) a.setActive(false);
  }

  /* A rendition vanishing from the master is an ad break, NOT a quality change.
   *
   * Measured on gaules 2026-07-29, 4/4 breaks with no false positives: usher
   * drops the ladder to 640x360@30 + 284x160@30 four to twelve seconds BEFORE
   * the first ad segment exists. The source rendition is simply absent, so the
   * player is forced down — it did not choose to move, and treating it as a
   * quality change is what produced 46 rebinds in 36 minutes. Every rebind
   * flushes the store, the store goes cold, and the real ad-bearing playlist
   * goes through: 21 of 91 breaks leaked with regrid=0 the whole time. The
   * donors were never the problem, the rebind was.
   *
   * Membership, never a variant count. A user picking 720p leaves the ladder
   * intact; a collapse removes the bound rendition from it entirely. */
  onLadder(rends) {
    const bound = this.store.rendition;
    const prev = this.ladderN;
    this.ladderN = rends.length;
    if (!bound) return;
    const has = rends.indexOf(bound) >= 0;
    const full = ladderBest.get(this.channel);
    if (!has && !this.collapsed) {
      this.collapsed = true;
      this.collapseAt = Date.now();
      this.st.collapses++;
      W(`${this.channel}: ladder collapsed, ${full ? full.length : prev} -> `
        + `${rends.length} variants and ${bound} is gone — ad break incoming.`
        + " Holding the pool; NOT rebinding");
      ev("ladder_collapse", "warn", {
        chan: this.channel, had: full ? full.length : prev,
        now: rends.length, bound,
      });
    } else if (has && this.collapsed) {
      this.endCollapse("ladder restored", rends.length);
    }
  }

  endCollapse(why, n) {
    if (!this.collapsed) return;
    const held = Date.now() - this.collapseAt;
    this.collapsed = false;
    I(`${this.channel}: ${why} after ${Math.round(held / 1000)}s`
      + ` — following the player's rendition again`);
    ev("ladder_restore", "info", {
      chan: this.channel, now: n === undefined ? this.ladderN : n,
      bound: this.store.rendition, heldMs: held,
    });
  }

  /* The collapse, with its ceiling applied. Also the definition of
   * `adIncoming`: it is true exactly while usher has taken our rendition away,
   * which preceded the ad by 4-12s in every break measured. It is not a
   * prediction of anything else. */
  inCollapse() {
    if (!this.collapsed) return false;
    if (Date.now() - this.collapseAt > COLLAPSE_MAX_MS) {
      this.endCollapse("collapse ceiling hit, assuming a real ladder change");
      return false;
    }
    return true;
  }

  /* Follow the player's rendition instead of locking onto the first one seen.
   *
   * Twitch's ABR probes low and climbs: measured 2026-07-29, the player's very
   * first media request was 640x360@30 and one second later it was on
   * 1920x1080@60 for the rest of the session. Binding to that first request
   * meant every subsequent playlist was a rendition mismatch and the pool
   * passed all 65 of them straight through. A manual quality change has the
   * same shape, which is why switching to 1080 by hand did nothing. */
  setRendition(rend) {
    if (!rend) return;
    const st = this.store;
    if (st.rendition === rend) return;

    if (st.rendition === null) {
      st.rendition = rend;
      this.st.rendition = rend;
      log(`${this.channel}: pool bound to ${rend}`);
      // Its own event, not a rebind with from=null. As a rebind it rendered in
      // the dashboard as "quality change: null to 1080p60" and it inflated
      // extreport's rebind/flap tables with a transition that never happened.
      ev("bind", "info", { chan: this.channel, to: rend });
      return;
    }

    // The player was forced off our rendition by usher, not by bandwidth and
    // not by the user. Rebinding here is the bug: it flushes a store full of
    // clean content at exactly the moment the ad needs covering.
    if (this.inCollapse()) {
      D(`${this.channel}: player forced to ${rend} by a collapsed ladder`
        + ` — holding ${st.rendition}, store kept (${st.seg.size} segments)`);
      return;
    }

    // ABR can flap; rebinding flushes the store, so rate-limit it or a
    // flapping player keeps the pool permanently cold.
    const now = Date.now();
    if (now - this.lastRebind < REBIND_MS) return;
    this.lastRebind = now;

    const from = st.rendition;
    log(`${this.channel}: player moved ${from} -> ${rend};`
      + ` rebinding ${this.quota} donors`);
    st.rendition = rend;
    this.st.rendition = rend;
    this.st.rebind++;
    // A rebind that undoes a recent one. Same rule as extreport.py's flaps():
    // the pool chasing Twitch's ABR probe empties its own store twice in a few
    // seconds, and the misses that follow are ours, not a donor shortage.
    this.rebinds = this.rebinds.filter((r) => now - r.t <= FLAP_MS);
    if (this.rebinds.some((r) => r.from === rend && r.to === from)) this.st.flap++;
    this.rebinds.push({ from, to: rend, t: now });
    // old segments are the wrong resolution — splicing them in now would be
    // worse than the ad we are trying to remove
    st.seg.clear();
    st.skewed.clear();
    st.win = [];
    st.seeded = false;         // reseed MEDIA-SEQUENCE from the new variant
    let retuned = 0;
    for (const a of this.arms) {
      a.onGrid = false;
      if (a.retune(rend)) {
        retuned++;            // same session, keeps its ad history
      } else {
        a.url = null;
        a.rend = null;
        a.dirty = false;
      }
    }
    D(`rebind: ${retuned}/${this.arms.length} donors retuned in-session,`
      + ` ${this.arms.length - retuned} need a fresh mint`);
    // The store was just flushed. If an ad leaks in the next few seconds this
    // event is the reason, and reconstructing that from prose cost an afternoon.
    ev("rebind", "warn", {
      chan: this.channel, from, to: rend, retuned,
      minted: this.arms.length - retuned,
    });
  }
}

/* Counters of pools that have been torn down.
 *
 * `rollup()` re-sums from live pools only, so without this every global total
 * dropped to zero 30s after you stopped watching — the popup, the badge and the
 * heartbeat all read 0 for a session that had really blocked things. Keeping
 * the last snapshot makes the totals lifetime-per-channel instead of
 * lifetime-per-pool-instance, which is what a viewer means by "blocked". */
const retired = new Map();     // channel -> the st it died with

function retire(p) {
  retired.set(p.channel, p.st);
}

function poolFor(channel) {
  let p = pools.get(channel);
  if (!p) {
    p = new Pool(channel);
    // Reopening a channel resumes its count rather than restarting it. The
    // snapshot is consumed here so the same numbers can never be counted twice
    // — once carried forward they live on the pool again.
    const old = retired.get(channel);
    if (old) {
      for (const k of SUMMED) p.st[k] = old[k] || 0;
      retired.delete(channel);
    }
    pools.set(channel, p);
    ev("pool", "info", { kind: "open", chan: channel, pools: pools.size });
    rebalance();
  }
  return p;
}

// Most recently polled, never "most likely to be watched". Only used to pick a
// default for the UI and a channel for the canary.
function recentChannel() {
  let best = null;
  for (const p of pools.values()) if (!best || p.lastPoll > best.lastPoll) best = p;
  return best ? best.channel : null;
}

/* Hand out the global donor budget, most-recently-polled channel first.
 *
 * The ordering is the whole point: when we are over the cap the donors come off
 * the channel whose player stopped asking for playlists, which is a fact, not
 * the muted one, which would be a guess. */
function rebalance() {
  const live = [...pools.values()].sort((a, b) => b.lastPoll - a.lastPoll);
  let left = MAX_DONORS;
  for (const p of live) {
    const want = Math.max(0, Math.min(ARMS, left));
    left -= want;
    p.setQuota(want);
  }
}

/* Tear down a pool whose player has gone quiet. This is what makes the cap
 * hold: pools used to be immortal, so closing a tab left four donors and a
 * canary polling for the rest of the browser session and the count grew with
 * every channel ever visited. No tabs permission needed — a closed tab stops
 * polling, and so does a stopped one. */
function reap() {
  const now = Date.now();
  let gone = 0;
  for (const [chan, p] of [...pools.entries()]) {
    if (now - p.lastPoll < IDLE_MS) continue;
    p.stop();
    retire(p);
    pools.delete(chan);
    gone++;
    stats.idled++;
    I(`${chan}: no player poll for ${Math.round((now - p.lastPoll) / 1000)}s`
      + ` — pool retired, ${p.quota} donors released`);
    ev("pool", "info", {
      kind: "idle", chan, arms: p.quota, idleMs: now - p.lastPoll,
      pools: pools.size,
    });
  }
  if (gone) rebalance();
}

function stopAll(reason) {
  for (const [chan, p] of [...pools.entries()]) {
    p.stop();
    retire(p);
    pools.delete(chan);
    ev("pool", "info", { kind: "stop", chan, reason, pools: pools.size });
  }
}

/* Whether we are actually watching segments. If this listener never registered
 * — a missing host permission is silent — `adSegsPlayed` must read as "not
 * measured" and never as a confident zero. Claiming zero for something nobody
 * looked at is the exact failure this whole event stream exists to stop. */
let segWatch = false;

function refreshOnGrid() {
  for (const p of pools.values()) {
    p.st.onGrid = p.arms.filter((a) => a.active && a.onGrid).length;
    p.st.arms = p.quota;
  }
}

/* Recompute the global view. Sums where a sum means something, and the rest
 * flagged as global: `unknownVariant` has no channel by definition (it is a
 * playlist we could not map to one) and the canary is one probe for the whole
 * extension, so both are reported as-is against every pool. */
function rollup() {
  refreshOnGrid();
  for (const k of SUMMED) stats[k] = 0;
  const names = [];
  let err = null;
  for (const p of pools.values()) {
    names.push(p.channel);
    for (const k of SUMMED) stats[k] += p.st[k] || 0;
    if (p.st.lastError) err = p.st.lastError;
  }
  // Torn-down pools still count. `names` deliberately excludes them: they are
  // no longer streams you have open, only totals you earned.
  for (const st of retired.values()) {
    for (const k of SUMMED) stats[k] += st[k] || 0;
  }
  stats.channels = names;
  stats.channel = recentChannel();
  const rp = stats.channel ? pools.get(stats.channel) : null;
  stats.rendition = rp ? rp.st.rendition : null;
  stats.pools = pools.size;
  stats.donors = stats.arms;
  stats.enabled = enabled;
  if (err) stats.lastError = err;
  return stats;
}

/* What the popup and dashboard read. One channel's numbers, never a blend.
 *
 * `adSegsPlayed` is omitted, not zeroed, when the segment listener is not
 * watching or unslop is switched off: the UI renders a missing value as "not
 * measured" and a zero as proof of a clean session, and only one of those is
 * honest when nobody was looking. */
function statsFor(channel) {
  rollup();
  const key = channel ? String(channel).toLowerCase() : stats.channel;
  const p = key ? pools.get(key) : null;
  // A reaped channel keeps its numbers until it is reopened, so a viewer who
  // pauses does not watch the count fall to zero.
  const s = p ? p.st : (key ? retired.get(key) || null : null);
  const out = {
    channel: s ? s.channel : null,
    rendition: s ? s.rendition : null,
    enabled,
    exposed: !!(s && s.exposed),
    // usher has dropped our rendition from the ladder. In every break measured
    // that ran 4-12s ahead of the first ad segment, so it is a warning, not a
    // report — and it is only ever set from that one observation.
    adIncoming: !!(p && p.inCollapse()),
    served: s ? s.served : 0,
    passthru: s ? s.passthru : 0,
    blockedBreaks: s ? s.blockedBreaks : 0,
    leakedBreaks: s ? s.leakedBreaks : 0,
    onGrid: s ? s.onGrid : 0,
    arms: s ? s.arms : 0,
    segs: s ? s.segs : 0,
    regrid: s ? s.regrid : 0,
    rebind: s ? s.rebind : 0,
    flap: s ? s.flap : 0,
    // No channel of their own: an unmappable variant has none, and the canary
    // is a single global probe.
    unknownVariant: stats.unknownVariant,
    canaryBreaks: stats.canaryBreaks,
    canaryJoins: stats.canaryJoins,
    lastError: (s && s.lastError) || stats.lastError,
    // context, ignored by the UI
    blockedSegs: s ? s.blockedSegs : 0,
    leakedSegs: s ? s.leakedSegs : 0,
    adSegsSeen: s ? s.adSegsSeen : 0,
    skew: s ? s.skew : 0,
    channels: stats.channels,
    pools: pools.size,
    donors: stats.donors,
    donorCap: MAX_DONORS,
  };
  if (segWatch && enabled && s) out.adSegsPlayed = s.segFetchAd;
  return out;
}

function setEnabled(on) {
  if (enabled === on) return;
  enabled = on;
  stats.enabled = on;
  I(`unslop ${on ? "enabled" : "disabled"} by the user`);
  ev("enable", on ? "info" : "warn", { on });
  // Off means off: leaving donors polling for a pool nobody will be served from
  // is exactly the load the poller cap exists to prevent.
  if (!on) stopAll("disabled");
}
