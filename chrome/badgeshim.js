"use strict";
/* chrome/badgeshim.js — the two globals ext/badge.js expects, backed by the
 * snapshot the pool pushes into the service worker.
 *
 * badge.js is shared verbatim with the Firefox build, where it loads into the
 * MV2 background page alongside core.js and calls `statsFor(channel)` directly.
 * Under MV3 core.js is in the offscreen worker and this scope has no access to
 * it, so `statsFor` becomes a synchronous read of the last snapshot — same
 * shape, same contract, at most one second stale against a badge that repaints
 * every two.
 *
 * The fallback matters and is deliberately the same as core.js's: asked about a
 * channel with no pool, return the most-recently-polled pool's numbers. badge.js
 * then checks `s.channel === chan` itself and paints nothing when it does not
 * match, which is what stops one tab's count appearing on another's icon.
 *
 * Also the one place the MV3 canvas question gets answered instead of assumed.
 * badge.js draws with OffscreenCanvas when there is no document — there is no
 * document here — and a service worker that cannot produce ImageData must fall
 * back to the corner badge rather than silently paint nothing. BADGE_PROBE runs
 * that path once and reports the result into the event stream.
 */

/* eslint-disable no-var, no-unused-vars */
var enabled = true;

function statsFor(channel) {
  var snap = (typeof SNAP !== "undefined" && SNAP) || null;
  if (!snap) return null;
  enabled = snap.enabled !== false;
  if (channel && snap.byChan && snap.byChan[channel]) return snap.byChan[channel];
  return snap.global;
}

/* One-shot capability probe, reported rather than assumed.
 *
 * Three things can each fail quietly in an MV3 worker and each one shows up as
 * "the icon never changes": no OffscreenCanvas, a 2d context that refuses
 * getImageData, or chrome.action.setIcon rejecting ImageData. Draw a real 16px
 * frame and hand it to setIcon exactly the way badge.js does, and say which
 * step broke. tabs.query is probed too — badge.js filters tabs by URL, and that
 * filter is ignored without either the "tabs" permission or a host permission
 * matching, which would leave the badge blank on every tab. */
(function () {
  var out = { ev: "badge_probe", lvl: "info", t: Date.now() };
  out.offscreenCanvas = typeof OffscreenCanvas !== "undefined";
  out.hasDocument = typeof document !== "undefined";
  try {
    var c = new OffscreenCanvas(16, 16);
    var g = c.getContext("2d");
    g.fillStyle = "#8b5cf6";
    g.fillRect(0, 0, 16, 16);
    var img = g.getImageData(0, 0, 16, 16);
    out.imageData = !!(img && img.data && img.data.length === 16 * 16 * 4);
    var act = chrome.browserAction || chrome.action;
    out.action = !!act;
    if (act) {
      // No tabId: the default icon. A per-tab setIcon needs a real tab and this
      // runs at worker start, when there may not be one.
      var p = act.setIcon({ imageData: { 16: img } });
      if (p && typeof p.then === "function") {
        p.then(function () { out.setIcon = true; }, function (e) { out.setIcon = String(e); });
      } else {
        out.setIcon = !chrome.runtime.lastError;
        if (chrome.runtime.lastError) out.setIcon = String(chrome.runtime.lastError.message);
      }
    }
  } catch (e) {
    out.error = String(e && e.message || e);
  }
  try {
    chrome.tabs.query({ url: ["*://*.twitch.tv/*"] }, function (t) {
      out.tabsQuery = chrome.runtime.lastError
        ? String(chrome.runtime.lastError.message) : (t ? t.length : -1);
      if (typeof toPool === "function") toPool({ k: "ev", e: out });
    });
  } catch (e) {
    out.tabsQuery = String(e && e.message || e);
    if (typeof toPool === "function") toPool({ k: "ev", e: out });
  }
})();
