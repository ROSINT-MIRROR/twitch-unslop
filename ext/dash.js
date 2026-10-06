"use strict";

/* The advanced surface. Same data as the popup, but every number is shown with
 * the sentence that makes it readable and with an honest note about how far it
 * is from ground truth.
 *
 * Three tiers of confidence, and the page must never blur them:
 *   measured  the player fetched an ad segment           -> it played
 *   observed  Twitch put an ad in our playlist           -> it was offered
 *   inferred  a probe session on a rejoin loop saw ads   -> ads exist here
 * The probe overstates badly (it rejoins, and joining draws ads), which is why
 * it gets its own section and a warning rather than a slot next to the rest.
 *
 * Everything except the last two sections is scoped to ONE stream. Several can
 * be open, each with its own pool and its own counters, and a sum across them
 * would describe nobody's viewing. */

const api = typeof chrome !== "undefined" ? chrome : browser;
const $ = (id) => document.getElementById(id);

/* One call shape for both browsers: callbacks. Firefox's chrome namespace takes
 * them, Chrome takes them under MV2 and MV3, and the promise form is the one
 * that is missing somewhere (Chrome MV2). */
function call(fn, arg) {
  return new Promise((resolve, reject) => {
    try {
      fn(arg, (v) => {
        const err = api.runtime && api.runtime.lastError;
        if (err) reject(new Error(err.message || String(err)));
        else resolve(v);
      });
    } catch (e) { reject(e); }
  });
}
const send = (m) => call(api.runtime.sendMessage.bind(api.runtime), m);

const FEED_MAX = 120;
const feed = [];
let lastEvT = 0;
let lastEvKeys = new Set();

/* A stream is live while its player is still asking for playlists. That is the
 * same signal the background reaps pools on, and the only one that is a fact
 * rather than a guess about intent — mute, focus and visibility all get PiP, a
 * second monitor and audio-only listeners wrong.
 *
 * We cannot read the pool's own lastPoll, so we watch its poll counter move.
 * The background gives up after 30s; flagging at 12 shows the stall while the
 * pool is still there to explain it. */
const STALE_MS = 12000;
const seen = new Map();          // channel -> {polls, moved}
const warned = new Map();        // channel -> ladder collapses seen on this page

let selected = null;             // the stream every scoped number describes
let busy = false;

// Default note text, so a conditional override ("not measured") can be undone
// when the condition clears instead of sticking for the life of the page.
const defNote = new Map();
for (const el of document.querySelectorAll(".stat")) {
  defNote.set(el.id, el.querySelector(".stat-note").textContent);
}
$("s-reached").dataset.truth = "1";

const esc = (s) => String(s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const secs = (ms) => `${Math.max(0, Math.round((ms || 0) / 1000))}s`;

/* Absent is not zero. Any counter that can be missing renders as a dash and
 * says why in its note; printing 0 for something nobody measured is the lie
 * this page exists to not tell. */
const NOT_MEASURED = "<small>not measured</small>";
const num = (v) => (v === undefined || v === null ? "<small>—</small>" : v);

function setStat(id, value, tone, note) {
  const el = $(id);
  if (!el) return;
  el.querySelector(".stat-v").innerHTML = value;
  el.className = "card stat" + (tone ? " " + tone : "")
    + (el.dataset.truth === "1" ? " truth" : "");
  el.querySelector(".stat-note").textContent = note || defNote.get(id) || "";
}

/* The states, ranked. Same ladder as the popup so the two surfaces never
 * disagree about what is happening:
 *
 *   off / exposed   loud, in that order
 *   holding         usher pulled our rendition out of the ladder, which ran
 *                   4-12s ahead of the ad in every break measured. Predictive,
 *                   and not a failure
 *   covered / warming / idle
 */
function readState(s, enabled) {
  if (enabled === false) return { k: "bad", label: "off", short: "off" };
  if (!s) return { k: "idle", label: "no stream", short: "idle" };
  if (s.exposed) return { k: "bad", label: "exposed", short: "exposed" };
  if (s.adIncoming) {
    return (s.onGrid || 0) > 0
      ? { k: "hold", label: "holding through a break", short: "holding" }
      : { k: "warn", label: "break coming, not ready", short: "no cover" };
  }
  if (s.served > 0) return { k: "ok", label: "covered", short: "covered" };
  return { k: "warn", label: "warming up", short: "warming" };
}

/* The headline sentence. Written to be true at a glance rather than
 * flattering: if we have blocked nothing because nothing was offered, it says
 * exactly that instead of implying a win. */
function verdict(s, enabled, chan) {
  if (enabled === false) {
    return { k: "bad", text: "Unslop is switched off. Twitch is serving you ads normally." };
  }
  if (!s) {
    return { k: "idle", text: "No Twitch stream is playing. Open one and its numbers appear here." };
  }
  const reached = s.adSegsPlayed;
  const blocked = s.blockedBreaks || 0;
  const exposed = s.leakedBreaks || 0;

  if (reached > 0) {
    return { k: "bad", text: `${reached} ad segment${reached === 1 ? "" : "s"} `
      + `reached your player on ${chan}. Check the misses below — a reset in the `
      + "seconds before a break is our fault, an empty pool is a shortage." };
  }
  if (s.adIncoming) {
    return (s.onGrid || 0) > 0
      ? { k: "ok", text: `Twitch is starting a break on ${chan} in the next few `
          + "seconds. We saw it in the quality list before any ad existed and we "
          + "are holding the clean feed through it." }
      : { k: "warn", text: `Twitch is starting a break on ${chan} in the next few `
          + "seconds and there is no clean video buffered yet. This one is likely "
          + "to get through." };
  }
  if (blocked === 0 && exposed === 0) {
    return { k: "warn", text: `No ads have been offered to ${chan} yet, so nothing `
      + "has been blocked. That is a normal result — passive viewing draws far "
      + "fewer ads than joining does. Reload the stream to test it." };
  }
  const tail = reached === undefined
    ? "Whether any of them played was not measured — the segment listener is not "
      + "active in this build, so read nothing into the absence."
    : "Nothing measurably reached your screen: the exposed playlists listed ads, "
      + "but the player appears not to have fetched them.";
  if (exposed > 0) {
    return { k: "warn", text: `${blocked} break${blocked === 1 ? "" : "s"} blocked, `
      + `${exposed} left exposed on ${chan}. ${tail}` };
  }
  return { k: "ok", text: `${blocked} ad break${blocked === 1 ? "" : "s"} blocked on `
    + `${chan}, none exposed.`
    + (reached === undefined ? " Whether anything played was not measured."
                             : " Nothing reached your screen.") };
}

/* ---- streams ------------------------------------------------------------ */

function trackLiveness(chans, per, now) {
  for (const c of chans) {
    const s = per[c] || {};
    const polls = (s.served || 0) + (s.passthru || 0);
    const rec = seen.get(c);
    if (!rec) seen.set(c, { polls, moved: now });
    else if (polls !== rec.polls) { rec.polls = polls; rec.moved = now; }
  }
  for (const c of [...seen.keys()]) if (chans.indexOf(c) < 0) seen.delete(c);
}

const isStale = (c, now) => {
  const rec = seen.get(c);
  return !!rec && now - rec.moved > STALE_MS;
};

let chipSig = null;

function renderChips(chans, per, enabled, now) {
  const rows = chans.map((c) => {
    const s = per[c];
    const stale = !s || isStale(c, now);
    const st = readState(s, enabled);
    return { c, stale, k: stale ? "idle" : st.k, word: stale ? "idle" : st.short };
  });
  // Rebuilding the strip every second would drop keyboard focus out of it once
  // a tick, which makes it unusable without a mouse.
  const sig = JSON.stringify([rows, selected]);
  if (sig === chipSig) return;
  chipSig = sig;

  const box = $("chips");
  $("chipsEmpty").style.display = chans.length ? "none" : "";
  box.innerHTML = rows.map((r) =>
    `<button type="button" class="chip${r.stale ? " stale" : ""}"`
    + ` aria-pressed="${r.c === selected}" data-chan="${esc(r.c)}">`
    + `<span class="dot s-${r.k}"></span>`
    + `<b>${esc(r.c)}</b>${esc(r.word)}</button>`).join("");
  for (const b of box.querySelectorAll(".chip")) {
    b.addEventListener("click", () => {
      selected = b.dataset.chan;
      tick();
    });
  }
}

/* ---- render ------------------------------------------------------------- */

function render(base, per) {
  const enabled = base.enabled;
  const chans = (base.channels || []).filter(Boolean);
  const now = Date.now();
  trackLiveness(chans, per, now);

  // Pick a default once, then stay put until that stream goes away. Re-deriving
  // it every tick would make the page hop between two live streams, since
  // "most recently polled" alternates when both players are running.
  if (selected && chans.indexOf(selected) < 0) selected = null;
  if (!selected) {
    selected = (base.channel && chans.indexOf(base.channel) >= 0)
      ? base.channel : (chans[0] || null);
  }
  renderChips(chans, per, enabled, now);

  const s = selected ? per[selected] : null;
  const chan = selected || "—";

  const st = readState(s, enabled);
  $("rail").className = "rail is-" + st.k;
  const pill = $("state");
  pill.className = "pill is-" + st.k;
  pill.textContent = st.label;

  const v = verdict(s, enabled, chan);
  $("vpill").className = "pill is-" + v.k;
  $("vpill").textContent =
    v.k === "ok" ? "clean" : v.k === "bad" ? "attention"
    : v.k === "idle" ? "idle" : "note";
  $("verdict").textContent = v.text;

  $("chan").textContent = chan;
  $("rend").textContent = (s && s.rendition) || "—";
  $("onlyThisLab").textContent = selected ? `Only ${selected}` : "Only this stream";

  // --- seeing a break coming
  const warns = warned.get(selected) || 0;
  if (!s) {
    setStat("s-hold", "<small>—</small>", "idle");
    setStat("s-warned", "<small>—</small>", "idle");
  } else if (s.adIncoming) {
    if ((s.onGrid || 0) > 0) {
      setStat("s-hold", "holding", "good",
        "Twitch has taken your quality out of the list. We are not following "
        + "the player down; it keeps getting the clean feed we already hold.");
    } else {
      setStat("s-hold", "no cover", "warn",
        "Twitch has taken your quality out of the list and there is nothing "
        + "buffered to hold with. This break will probably show.");
    }
    setStat("s-warned", warns, "warn");
  } else {
    setStat("s-hold", "clear", "idle");
    setStat("s-warned", warns, warns > 0 ? null : "idle");
  }

  // --- did any slop get through
  //
  // adSegsPlayed comes from the segment listener. When that listener is not
  // watching the background omits the field rather than sending 0, and "0 ads
  // reached your screen" for something nobody looked at is exactly the claim
  // this page must never make.
  const played = s ? s.adSegsPlayed : undefined;
  if (!s) {
    setStat("s-reached", "<small>—</small>", "idle", "No stream selected.");
  } else if (played === undefined) {
    setStat("s-reached", NOT_MEASURED, "idle",
      "Segment tracking is not active in this build, so we cannot prove "
      + "whether an ad played. Do not read this as zero.");
  } else {
    setStat("s-reached", played, played > 0 ? "bad" : "good");
  }

  const bBrk = s ? (s.blockedBreaks || 0) : null;
  const bSeg = s ? (s.blockedSegs || 0) : 0;
  setStat("s-blocked", s ? bBrk : "<small>—</small>", !s ? "idle" : bBrk > 0 ? "good" : "idle",
    bSeg > 0 ? defNote.get("s-blocked") + ` ${bSeg} ad segments in total.` : null);

  const eBrk = s ? (s.leakedBreaks || 0) : null;
  const eSeg = s ? (s.leakedSegs || 0) : 0;
  setStat("s-exposed", s ? eBrk : "<small>—</small>", !s ? "idle" : eBrk > 0 ? "warn" : "idle",
    eSeg > 0 ? defNote.get("s-exposed") + ` ${eSeg} ad segments were listed in them.` : null);

  // --- where the video came from
  setStat("s-served", s ? (s.served || 0) : "<small>—</small>", s ? null : "idle");
  setStat("s-pass", s ? (s.passthru || 0) : "<small>—</small>",
    !s ? "idle" : (s.passthru || 0) > 0 ? "warn" : null);

  if (!s) {
    setStat("s-grid", "<small>—</small>", "idle");
  } else {
    const grid = `${s.onGrid || 0}<small>/${s.arms || 0}</small>`;
    setStat("s-grid", grid, (s.onGrid || 0) === 0 ? "bad" : "good",
      (s.arms || 0) === 0
        ? "This stream has no background sessions right now — the shared budget "
          + "went to a stream that is still being watched. Coverage here is off "
          + "until it comes back."
        : null);
  }
  setStat("s-held", s ? (s.segs || 0) : "<small>—</small>", s ? null : "idle");

  // --- why we ever miss
  setStat("s-regrid", s ? (s.regrid || 0) : "<small>—</small>",
    !s ? "idle" : (s.regrid || 0) > 0 ? "warn" : "idle");
  setStat("s-rebind", s ? (s.rebind || 0) : "<small>—</small>", s ? null : "idle");
  // flap is absent on older background builds. Absent renders as a dash, never
  // as a confident zero.
  setStat("s-flap", s ? num(s.flap) : "<small>—</small>",
    !s ? "idle" : s.flap === undefined ? "idle" : s.flap > 2 ? "bad" : "idle",
    s && s.flap === undefined
      ? "Not reported by this build. Do not read the dash as zero." : null);

  // --- across every stream
  setStat("s-pools", num(base.pools), (base.pools || 0) > 0 ? null : "idle");
  setStat("s-budget", base.donors === undefined ? "<small>—</small>"
    : `${base.donors}<small>/${base.donorCap === undefined ? "?" : base.donorCap}</small>`,
    (base.donors || 0) >= (base.donorCap || Infinity) ? "warn" : null);
  setStat("s-unk", num(base.unknownVariant),
    (base.unknownVariant || 0) > 0 ? "warn" : "idle");

  // --- the probe. Its own section, never summed with anything above.
  setStat("s-cbrk", num(base.canaryBreaks));
  setStat("s-cjoin", num(base.canaryJoins));
}

/* ---- events ------------------------------------------------------------- */

/* Feed lines are written for someone watching a stream, not for someone reading
 * background.js. Each returns [name, detail, severity, emphasise]. */
function label(e) {
  switch (e.ev) {
    case "up":
      return ["unslop started",
        `${e.arms} background sessions per stream, ${e.donorCap || "?"} shared`, "ok"];

    case "enable":
      return e.on
        ? ["switched on", "", "ok"]
        : ["switched off", "Twitch is serving ads normally", "bad"];

    case "pool":
      if (e.kind === "open") return ["stream opened", `${e.pools} open now`, "ok"];
      if (e.kind === "quota") {
        return ["sessions moved",
          `${e.from} to ${e.to} on this stream, out of ${e.cap} shared`,
          e.to < e.from ? "warn" : null];
      }
      if (e.kind === "idle") {
        return ["stream stopped",
          `no playlist requests for ${secs(e.idleMs)} — ${e.arms} background `
          + "sessions released", null];
      }
      if (e.kind === "stop") return ["stream dropped", e.reason || "", "warn"];
      return null;

    case "bind":
      return ["locked on", `${e.to} — background sessions tuned to match`, "ok"];

    // The only line here that names something before it happened.
    case "ladder_collapse":
      return ["ad break incoming",
        `${e.bound} vanished from the quality list (${e.had} down to ${e.now}). `
        + "The ad itself normally exists 4-12s later; holding the clean feed "
        + "instead of following the player down.",
        "warn", true];

    case "ladder_restore":
      return ["break over", `full quality list back after ${secs(e.heldMs)}`, "ok"];

    case "media":
      if (!e.realAds) return null;             // only ad-bearing polls are news
      return [e.decision === "rewrite" ? "blocked" : "exposed",
        `${e.realAds} ad segments, ${e.decision}`
        + (e.collapsed ? ", served through a collapsed quality list" : ""),
        e.decision === "rewrite" ? "ok" : "bad"];

    case "segment":
      return e.ad ? ["ad played", e.url ? e.url.slice(-44) : "", "bad"] : null;

    case "regrid":
      return ["session retired", `session ${e.n} took an ad`, "warn"];

    case "rebind":
      // A first bind has its own event now. Anything arriving here without a
      // `from` is one, and rendering it as "null to 1080p60" inflated every
      // rebind and flap table with a transition that never happened.
      if (!e.from) return ["locked on", `${e.to}`, "ok"];
      return ["quality change", `${e.from} to ${e.to}`
        + (e.minted ? `, ${e.minted} sessions restarted` : ""), "warn"];

    case "canary":
      return e.kind === "break"
        ? ["probe saw an ad", `${e.adSegs || "?"} segments — the probe rejoins on a `
          + "loop, so this overstates badly", "warn"] : null;

    case "master":
      return ["quality list", `${e.variants ? e.variants.length : "?"} qualities offered`,
        null];

    default:
      return null;
  }
}

function pushEvents(list) {
  for (const e of list || []) {
    if (!e || typeof e.t !== "number" || e.t < lastEvT) continue;
    // Several events can share a millisecond; keying only on `t` dropped the
    // ones behind the first.
    const key = `${e.t}|${e.ev}|${e.chan || ""}|${e.kind || e.to || e.url || ""}`;
    if (e.t === lastEvT && lastEvKeys.has(key)) continue;
    if (e.t > lastEvT) { lastEvT = e.t; lastEvKeys = new Set(); }
    lastEvKeys.add(key);

    if (e.ev === "ladder_collapse" && e.chan) {
      warned.set(e.chan, (warned.get(e.chan) || 0) + 1);
    }
    const l = label(e);
    if (!l) continue;
    feed.unshift({ t: e.t, chan: e.chan || e.channel || "", name: l[0],
                   detail: l[1], sev: l[2], hi: !!l[3] });
  }
  feed.length = Math.min(feed.length, FEED_MAX);
  drawFeed();
}

let feedSig = null;

function drawFeed() {
  const only = $("onlyThis").checked && selected;
  // Redrawing an unchanged table once a second wipes any text the reader was
  // in the middle of selecting.
  const sig = `${feed.length}|${lastEvT}|${only || ""}`;
  if (sig === feedSig) return;
  feedSig = sig;

  // Channel-less events (the extension starting, an unrecognised playlist) are
  // never hidden by a channel filter — there is no channel to disagree with.
  const rows = feed.filter((f) => !only || !f.chan || f.chan === only);
  $("feedEmpty").style.display = rows.length ? "none" : "";
  $("feedEmpty").textContent = feed.length
    ? "Nothing on this stream yet." : "Nothing yet.";
  $("feed").innerHTML = rows.map((f) => {
    const d = new Date(f.t);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    return `<tr class="${f.sev ? "sev-" + f.sev : ""}${f.hi ? " hi" : ""}">`
      + `<td class="t">${hh}:${mm}:${ss}</td>`
      + `<td class="ch">${esc(f.chan || "—")}</td>`
      + `<td class="ev">${esc(f.name)}</td>`
      + `<td>${esc(f.detail || "")}</td></tr>`;
  }).join("");
}

$("onlyThis").addEventListener("change", drawFeed);

/* ---- loop --------------------------------------------------------------- */

async function tick() {
  if (busy) return;
  busy = true;
  try {
    // One unscoped call for the roll-up and the channel list, then one per
    // stream. Never a sum: two streams' counters describe two different
    // viewings and adding them describes neither.
    const base = await send({ cmd: "stats" }) || {};
    const chans = (base.channels || []).filter(Boolean);
    const per = {};
    await Promise.all(chans.map(async (c) => {
      const s = await send({ cmd: "stats", channel: c });
      // Only accept an answer that names the channel we asked about. A pool can
      // be reaped between the roll-up and this call, and a stream we cannot
      // attribute is one we leave out rather than one we guess at.
      if (s && s.channel === c) per[c] = s;
    }));
    render(base, per);
  } catch (e) {
    $("verdict").textContent =
      "Unslop is not running. Reload the extension and reopen this page.";
    $("vpill").className = "pill is-bad";
    $("vpill").textContent = "attention";
    return;
  } finally {
    busy = false;
  }
  try {
    pushEvents(await send({ cmd: "events" }));
  } catch (_) { /* build without the event bridge; feed stays empty */ }
}

tick();
setInterval(tick, 1000);

/* ---- diagnostics -------------------------------------------------------- */

/* One file a tester can attach to a report.
 *
 * The dashboard already shows the numbers, but a screenshot of a number is not
 * evidence — "3 blocked, 0 exposed" says nothing about the break that went
 * wrong. This dumps the raw event stream, which is the same schema
 * ext/extreport.py reads, so a report can be analysed exactly like a lab
 * session instead of being argued about. */
$("diag").addEventListener("click", async () => {
  const b = $("diag");
  const note = $("diagNote");
  b.disabled = true;
  b.textContent = "Collecting…";
  try {
    const d = await send("diag");
    if (!d) throw new Error("no response");
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(d, null, 1)], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    // Sortable, and unambiguous about which run it came from.
    a.download = `unslop-diag-${new Date().toISOString()
      .replace(/[:.]/g, "-").slice(0, 19)}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    b.textContent = "Saved";
    note.textContent = `${(d.events || []).length} events written. `
      + "Attach that file to your report.";
  } catch (e) {
    b.textContent = "Save diagnostics";
    note.textContent = "Could not collect: " + (e && e.message ? e.message : e)
      + ". Reload the extension and try again.";
  } finally {
    setTimeout(() => { b.disabled = false; b.textContent = "Save diagnostics"; }, 2500);
  }
});
