"use strict";
/* chrome/bridge.js — ISOLATED world, document_start.
 *
 * hook.js runs in MAIN world where chrome.* does not exist, so this is the only
 * way its events reach the extension and the only way the pool's pushes reach
 * it. Both directions, one long-lived port.
 *
 * A Port rather than sendMessage per event, for two reasons: one sendMessage
 * per intercepted request wakes the MV3 service worker constantly, and an open
 * port is what keeps that worker alive between events in the first place. The
 * port dying is normal (the SW is allowed to be recycled), so reconnect —
 * silently, with backoff, forever, because a dead bridge means the pool's
 * pushes stop arriving and the tab quietly goes back to Twitch's ads.
 *
 * Nothing here decides anything. Batching is 250ms for events (playlist bodies
 * ride in these, so a batch is a few KB); pushes go straight through, since a
 * push held back is a playlist not served.
 */
var Q = [];
var port = null;
var backoff = 250;
var timer = null;

function toPage(push) {
  try {
    window.postMessage({ __unslop: 1, push: push }, location.origin);
  } catch (e) { /* */ }
}

function connect() {
  if (port) return;
  try {
    port = chrome.runtime.connect({ name: "unslop" });
  } catch (e) {
    port = null;
    schedule();
    return;
  }
  backoff = 250;
  port.onMessage.addListener(function (msg) {
    if (msg && msg.k === "push") toPage(msg.push);
  });
  port.onDisconnect.addListener(function () {
    port = null;
    // Chrome parks the reason in lastError; reading it keeps it from being
    // logged as an unchecked error, and there is nothing useful to do with it.
    try { void chrome.runtime.lastError; } catch (e) { /* */ }
    schedule();
  });
  // Announce, so the service worker can make sure the pool is actually up
  // before the first playlist arrives.
  try { port.postMessage({ k: "hello", href: String(location.href) }); }
  catch (e) { /* raced with a disconnect */ }
}

function schedule() {
  if (timer) return;
  timer = setTimeout(function () {
    timer = null;
    connect();
  }, backoff);
  backoff = Math.min(5000, backoff * 2);
}

window.addEventListener("message", function (ev) {
  if (ev.source !== window) return;
  var d = ev.data;
  if (!d || d.__unslop !== 1 || !d.e) return;
  Q.push(d.e);
  // Playlist bodies ride in these. Dropping the OLDEST is right: a stale
  // playlist is worthless and the newest one is the one the pool needs.
  if (Q.length > 400) Q.splice(0, Q.length - 400);
}, false);

setInterval(function () {
  if (!Q.length) return;
  if (!port) { connect(); return; }
  var batch = Q.splice(0, Q.length);
  try {
    port.postMessage({ k: "hook", events: batch });
  } catch (e) {
    // Port died between the check and the post. Put the batch back, capped.
    Q = batch.concat(Q).slice(-400);
    port = null;
    schedule();
  }
}, 250);

connect();
