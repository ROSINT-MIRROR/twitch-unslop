"use strict";
/*
 * The toolbar icon, drawn per tab.
 *
 * uBlock puts its count in the corner badge, which the browser renders in its
 * own font and always reads cleanly. We knock the number through the middle of
 * the shield instead, because the mark already has a slot punched through it —
 * the count just becomes that slot, so there is one object in the toolbar
 * rather than a mark with a sticker on it.
 *
 * The cost is real and bounded: at 16px a knocked-out digit is about 9px tall.
 * One or two digits read; three do not fit, so 100+ shows as "99+" in the
 * corner badge and the shield goes back to its plain slot. Two digits covers
 * any realistic session — gaules runs four midrolls in 36 minutes.
 *
 * Per TAB, not per extension: the count belongs to the channel in that tab, and
 * showing another tab's number on this one is the same lie the popup was
 * rewritten to stop telling.
 */

const B_API = typeof chrome !== "undefined" ? chrome : browser;
const B_ACTION = B_API.browserAction || B_API.action;

const B_VIOLET = "#8b5cf6";
const B_SIZES = [16, 32];
const B_TICK_MS = 2000;

/* Same reserved-path logic as popup.js. Deliberately duplicated rather than
 * shared: this file has to keep working if the popup is rewritten, and the list
 * is inert data. */
const B_NOT_CHANNEL = new Set([
  "directory", "videos", "video", "settings", "u", "subscriptions", "wallet",
  "drops", "downloads", "jobs", "turbo", "prime", "friends", "inventory",
  "payments", "search", "store", "team", "products", "following", "dashboard",
  "broadcast", "event", "collections", "checkout", "bits", "subs", "redeem",
  "activate", "login", "signup", "legal", "privacy", "security", "purchase",
  "gift", "gifts", "help", "about", "p", "s", "friend", "gaming",
]);

function bChannel(raw) {
  if (!raw) return null;
  let u;
  try { u = new URL(raw); } catch (e) { return null; }
  const host = u.hostname.toLowerCase();
  if (host !== "twitch.tv" && !host.endsWith(".twitch.tv")) return null;
  if (host === "player.twitch.tv" || host === "embed.twitch.tv") {
    return bClean(u.searchParams.get("channel"));
  }
  const parts = u.pathname.split("/").filter(Boolean);
  if (!parts.length) return null;
  const first = parts[0].toLowerCase();
  if (first === "popout" || first === "moderator") return bClean(parts[1]);
  if (B_NOT_CHANNEL.has(first)) return null;
  const second = (parts[1] || "").toLowerCase();
  if (second === "clip" || second === "video" || second === "v") return null;
  return bClean(first);
}

function bClean(n) {
  if (!n) return null;
  const c = String(n).toLowerCase();
  return /^[a-z0-9_]{2,25}$/.test(c) ? c : null;
}

/* Shield outline in the 128-unit space the SVG source uses, so the drawn icon
 * and icons/unslop.svg cannot drift apart. */
function bShield(g, s) {
  const p = (n) => n * s / 128;
  g.beginPath();
  g.moveTo(p(64), p(3));
  g.lineTo(p(120), p(24));
  g.lineTo(p(120), p(62));
  g.bezierCurveTo(p(120), p(93), p(98), p(117), p(64), p(127));
  g.bezierCurveTo(p(30), p(117), p(8), p(93), p(8), p(62));
  g.lineTo(p(8), p(24));
  g.closePath();
}

/* Returns ImageData, or null if this runtime has no canvas. MV2 background
 * pages have a DOM; an MV3 service worker does not, and would need
 * OffscreenCanvas. Returning null lets the caller fall back to the corner badge
 * instead of throwing on a port. */
function bDraw(size, text) {
  let c;
  try {
    c = (typeof OffscreenCanvas !== "undefined")
      ? new OffscreenCanvas(size, size)
      : document.createElement("canvas");
  } catch (e) { return null; }
  if (!c) return null;
  c.width = size; c.height = size;
  const g = c.getContext("2d");
  if (!g) return null;

  g.clearRect(0, 0, size, size);
  g.fillStyle = B_VIOLET;
  bShield(g, size);
  g.fill();

  // Punch the glyph straight through, rather than painting it on top: a filled
  // digit on a violet field needs a second colour that works on both light and
  // dark browser chrome, and there isn't one. A hole takes whatever is behind.
  g.globalCompositeOperation = "destination-out";
  if (text) {
    const fs = (text.length > 1 ? 74 : 86) * size / 128;
    g.font = `700 ${fs}px ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif`;
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(text, size / 2, size * 0.56);
  } else {
    // Resting mark: the slot from icons/unslop.svg.
    const p = (n) => n * size / 128;
    g.beginPath();
    if (g.roundRect) g.roundRect(p(38), p(56), p(52), p(16), p(8));
    else g.rect(p(38), p(56), p(52), p(16));
    g.fill();
  }
  g.globalCompositeOperation = "source-over";
  return g.getImageData(0, 0, size, size);
}

const bCache = new Map();     // tabId -> last text drawn

function bPaint(tabId, count) {
  // 3 digits do not fit inside a 16px shield. Past 99 the shield goes back to
  // its plain slot and the number moves to the corner badge, which the browser
  // renders small enough to be legible.
  const over = count > 99;
  const text = !count ? "" : over ? "" : String(count);

  if (bCache.get(tabId) === text + "|" + over) return;
  bCache.set(tabId, text + "|" + over);

  const imageData = {};
  for (const s of B_SIZES) {
    const d = bDraw(s, text);
    if (d) imageData[s] = d;
  }
  if (Object.keys(imageData).length) {
    try { B_ACTION.setIcon({ tabId, imageData }); } catch (e) { /* tab closed */ }
  }
  try {
    B_ACTION.setBadgeText({ tabId, text: over ? "99+" : "" });
    if (B_ACTION.setBadgeBackgroundColor) {
      B_ACTION.setBadgeBackgroundColor({ tabId, color: B_VIOLET });
    }
  } catch (e) { /* tab closed */ }
}

async function bTick() {
  let tabs;
  try { tabs = await B_API.tabs.query({ url: ["*://*.twitch.tv/*"] }); }
  catch (e) { return; }

  const live = new Set();
  for (const t of tabs || []) {
    live.add(t.id);
    const chan = bChannel(t.url);
    let n = 0;
    // Defensive: statsFor lives in core.js and this file must degrade to a
    // plain shield rather than throw if that ever moves.
    if (chan && typeof enabled !== "undefined" && enabled
        && typeof statsFor === "function") {
      try {
        const s = statsFor(chan);
        // statsFor falls back to the most recently polled pool when it has none
        // for the channel asked about, so confirm the answer is actually this
        // tab's before showing a number on it.
        if (s && s.channel === chan) n = s.blockedBreaks || 0;
      } catch (e) { n = 0; }
    }
    bPaint(t.id, n);
  }
  for (const id of [...bCache.keys()]) if (!live.has(id)) bCache.delete(id);
}

setInterval(bTick, B_TICK_MS);
if (B_API.tabs && B_API.tabs.onUpdated) {
  B_API.tabs.onUpdated.addListener((id, ch) => { if (ch.url) bTick(); });
}
bTick();
