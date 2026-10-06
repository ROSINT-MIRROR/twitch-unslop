"use strict";
/* chrome/offscreen.js — a wire, and only a wire.
 *
 * chrome.* on one side, the pool worker on the other. Every decision is in
 * pool.js (the shell) or core.js (the engine); this file must stay short
 * enough that "the bug is in the relay" is never a plausible theory.
 *
 * An offscreen document can use chrome.runtime messaging and very little else —
 * no chrome.tabs, no chrome.action — which is exactly why the service worker
 * stays in the picture as the switchboard rather than being cut out of it.
 */

const worker = new Worker("pool.js");

// Pool -> service worker. Pushes (playlist bodies for the tabs) and snapshots
// (counters for the popup, dashboard and toolbar icon) both go this way.
worker.onmessage = (ev) => {
  const m = ev && ev.data;
  if (!m) return;
  try {
    const p = chrome.runtime.sendMessage(Object.assign({ __u: "fromPool" }, m));
    // The service worker may be mid-restart. Nothing here is worth retrying:
    // the next push is 400ms away and the next snapshot one second.
    if (p && typeof p.then === "function") p.then(() => { }, () => { });
  } catch (e) { /* */ }
};

worker.onerror = (e) => {
  // A pool that failed to start is the one failure mode that looks exactly
  // like "no ads on this channel", so say so loudly in both places.
  const msg = `pool worker error: ${e && e.message} @${e && e.filename}:${e && e.lineno}`;
  console.log("[unslop]", msg);
  try {
    chrome.runtime.sendMessage({
      __u: "fromPool", k: "snap", byChan: {}, global: { lastError: msg },
      events: [], enabled: true,
    });
  } catch (_) { /* */ }
};

// Service worker -> pool.
chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || msg.__u !== "toPool" || !Array.isArray(msg.msgs)) return;
  for (const m of msg.msgs) {
    try { worker.postMessage(m); } catch (e) { /* */ }
  }
  // No sendResponse and no `return true`: the service worker's own listener
  // answers the UI commands, and two listeners racing to respond to one message
  // is a bug that only shows up under load.
});
