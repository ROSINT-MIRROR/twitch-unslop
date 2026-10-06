"use strict";

/* The simple surface. It answers one question — am I covered right now — and
 * refuses to answer any other. Everything explanatory lives in the dashboard.
 * Rule for this file: if a beta tester cannot act on a number, it does not
 * belong here.
 *
 * "Right now" means the tab this popup was opened over. Several Twitch tabs can
 * be open at once, each with its own pool and its own counters, so asking the
 * background for "the current channel" would hand this surface whichever tab
 * polled last — quite possibly a muted one in another window. */

// `chrome` is defined in both browsers, `browser` only in Firefox. Using
// chrome.* everywhere is what lets one build ship to both.
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
const queryTabs = (q) => call(api.tabs.query.bind(api.tabs), q);

/* twitch.tv paths that are not a channel. A channel lives at the root, so
 * anything with a reserved first segment is a page, not a stream, and saying
 * "no stream here" is the honest answer. Two exceptions carry a channel in the
 * second segment and are worth resolving because a tester will have them open. */
const NOT_A_CHANNEL = new Set([
  "directory", "videos", "video", "settings", "u", "subscriptions", "wallet",
  "drops", "downloads", "jobs", "turbo", "prime", "friends", "inventory",
  "payments", "search", "store", "team", "products", "following", "dashboard",
  "broadcast", "event", "collections", "checkout", "bits", "subs", "redeem",
  "activate", "login", "signup", "legal", "privacy", "security", "purchase",
  "gift", "gifts", "help", "about", "p", "s", "friend", "gaming",
]);

/* Channel out of a tab URL.
 *
 * `tab.url` is only populated when the extension has either the "tabs"
 * permission or a host permission matching that tab — the manifest already
 * lists *://*.twitch.tv/*, so Twitch tabs give us a URL and everything else
 * gives us nothing. That is the same answer either way: not a Twitch channel. */
function channelFromUrl(raw) {
  if (!raw) return null;
  let u;
  try { u = new URL(raw); } catch (_) { return null; }
  if (u.protocol !== "https:" && u.protocol !== "http:") return null;
  const host = u.hostname.toLowerCase();
  if (host !== "twitch.tv" && !host.endsWith(".twitch.tv")) return null;

  // The embed players carry the channel in a query parameter instead.
  if (host === "player.twitch.tv" || host === "embed.twitch.tv") {
    return clean(u.searchParams.get("channel"));
  }

  const parts = u.pathname.split("/").filter(Boolean);
  if (!parts.length) return null;
  const first = parts[0].toLowerCase();
  if (first === "popout" || first === "moderator") return clean(parts[1]);
  if (NOT_A_CHANNEL.has(first)) return null;
  // A clip or a VOD under a channel's name is a recording. There is no live
  // playlist behind it, so there is nothing for us to be covering.
  const second = (parts[1] || "").toLowerCase();
  if (second === "clip" || second === "video" || second === "v") return null;
  return clean(first);
}

// Twitch logins are the same character class as our pool keys, and the
// background lowercases before it stores them.
function clean(name) {
  if (!name) return null;
  const c = String(name).toLowerCase();
  return /^[a-z0-9_]{2,25}$/.test(c) ? c : null;
}

/* The states, ranked by what matters to a viewer:
 *
 *   off        the user switched us off; Twitch is serving ads normally
 *   exposed    we handed Twitch's own playlist through, ads included
 *   holding    usher took our rendition out of the ladder, which ran 4-12s
 *              ahead of the ad in every break measured. Predictive, not a
 *              failure: we saw it coming and we are holding the clean feed
 *   covered    pool is warm and serving
 *   warming    neither yet; normal for the first ~20s after a join
 *   idle       this tab is not a Twitch channel, or nothing is playing on it
 *
 * `exposed` deliberately outranks a healthy block count and outranks the
 * incoming warning: ten breaks blocked earlier is irrelevant if slop is going
 * through right now. */
function readState(chan, s, enabled) {
  if (enabled === false) {
    return { k: "bad", label: "off",
      why: "Unslop is switched off. Twitch is serving you ads normally." };
  }
  if (!chan) {
    return { k: "idle", label: "no stream here",
      why: "This tab isn't a Twitch channel. Open one and Unslop starts on its own." };
  }
  if (!s) {
    return { k: "idle", label: "waiting",
      why: `Nothing playing on ${chan} yet. Unslop starts when the stream does.` };
  }
  if (s.exposed) {
    return { k: "bad", label: "exposed",
      why: "The clean feed ran short, so Twitch's own stream is going through — "
         + "ads included. This usually clears within a few seconds." };
  }
  if (s.adIncoming) {
    // Warm enough to actually hold, or not. Claiming a hold with nothing in the
    // buffer is the kind of overstatement this whole surface exists to avoid.
    if ((s.onGrid || 0) > 0) {
      return { k: "hold", label: "ad break incoming",
        why: "Twitch is lining one up. We spotted it early and we're holding the "
           + "clean feed — you shouldn't see the break." };
    }
    return { k: "warn", label: "ad break incoming",
      why: "Twitch is lining one up and the clean feed isn't ready. This one "
         + "may get through." };
  }
  if (s.served > 0) {
    return { k: "ok", label: "covered",
      why: "You're on a clean feed. Ads are replaced before they reach you." };
  }
  return { k: "warn", label: "warming up",
    why: "Building a clean feed. Takes about twenty seconds after you open a stream." };
}

// The other open streams, named but never counted into this one. "and 2 more"
// beats a list that overflows the popup at four streams.
function alsoLine(chan, channels) {
  const rest = (channels || []).filter((c) => c && c !== chan);
  if (!rest.length) return "";
  const shown = rest.slice(0, 2).map((c) => `<b>${esc(c)}</b>`).join(", ");
  const more = rest.length - Math.min(rest.length, 2);
  const tail = `${shown}${more ? ` and ${more} more` : ""}.`;
  return chan ? `Also covering ${tail}` : `Covering ${tail}`;
}

const esc = (s) => String(s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function render(chan, s) {
  // Scope the answer to THIS tab before anything reads a counter off it.
  //
  // Asked with no channel — which is what a non-Twitch tab sends — the
  // background answers with the most recently polled pool instead. Those are
  // some other tab's numbers, and showing them here as if they were yours is
  // the exact bug this surface was rewritten to remove. Identity, not
  // truthiness, decides whether a counter is ours.
  const mine = (chan && s && s.channel === chan) ? s : null;
  const st = readState(chan, mine, s && s.enabled);

  $("rail").className = "rail is-" + st.k;
  const pill = $("state");
  pill.className = "pill is-" + st.k;
  pill.textContent = st.label;
  $("why").textContent = st.why;

  // blockedBreaks counts ad breaks Twitch put in THIS channel's playlist and we
  // replaced. Never the canary's count — that is a probe on a rejoin loop and
  // joining is what draws ads, so it overstates what a viewer would have seen.
  //
  // With no pool for this tab there is no number at all: a zero would read as
  // "nothing got past us" when the truth is "we were not there".
  $("tally").hidden = !mine;
  if (mine) {
    const n = mine.blockedBreaks || 0;
    const c = $("count");
    c.textContent = n;
    c.classList.toggle("zero", n === 0);
    $("countLabel").textContent = n === 1 ? "ad break blocked" : "ad breaks blocked";
  }

  $("chan").textContent = chan || "not Twitch";
  $("rend").textContent = (mine && mine.rendition) || "—";

  const also = $("also");
  const line = alsoLine(chan, s && s.channels);
  also.innerHTML = line;
  also.hidden = !line;

  const err = $("err");
  err.textContent = (s && s.lastError) || "";
  err.classList.toggle("on", !!(s && s.lastError));

  const on = $("on");
  on.checked = !(s && s.enabled === false);
  $("onLab").textContent = on.checked ? "On" : "Off";
}

let tabChan = null;
let busy = false;

async function tick() {
  if (busy) return;
  busy = true;
  try {
    // Re-read the tab every time: the popup survives a background tab switch in
    // some builds, and a stale channel here is the exact bug this replaces.
    let tab = null;
    try {
      const tabs = await queryTabs({ active: true, currentWindow: true });
      tab = (tabs && tabs[0]) || null;
    } catch (_) { /* no tab access; fall through as "not a channel" */ }
    tabChan = channelFromUrl(tab && (tab.url || tab.pendingUrl));
    render(tabChan, await send({ cmd: "stats", channel: tabChan }));
  } catch (e) {
    // Background page gone. Say so, rather than showing a stale confident zero.
    render(tabChan, { lastError: "not running — reload the extension" });
  } finally {
    busy = false;
  }
}

$("on").addEventListener("change", async (e) => {
  const want = e.target.checked;
  $("onLab").textContent = want ? "On" : "Off";
  try { await send({ cmd: "enable", on: want, channel: tabChan }); } catch (_) {}
  tick();
});

$("adv").addEventListener("click", (e) => {
  e.preventDefault();
  api.tabs.create({ url: api.runtime.getURL("dash.html") });
  window.close();
});

tick();
setInterval(tick, 1000);
