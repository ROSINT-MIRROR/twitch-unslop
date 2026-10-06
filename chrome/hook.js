"use strict";
/* chrome/hook.js — MAIN world, document_start. THE HOT PATH.
 *
 * Chrome has no webRequest.filterResponseData, so the only way to change a
 * response body is to be the code that receives it: patch fetch /
 * XMLHttpRequest in the page's own realm, and — because Twitch polls playlists
 * from a Web Worker running the Amazon IVS WASM engine — patch the Worker
 * constructor too, so a worker the page creates installs the same patch before
 * its real script runs. Both were proved by the spike (data/chrome/*,
 * 4 live runs): 0 media playlists missed, every playlist fetched via
 * `fetch@worker`, and the player provably consumed our rewritten body.
 *
 * What this file is NOT: it holds no pool, no PDT chain, no ad logic. All of
 * that is ext/core.js running in the offscreen document's worker, which is a
 * message away — and a message round trip is exactly what must not happen while
 * the player is waiting for a playlist. So the pool PUSHES the current
 * serve-ready body to every tab, this file caches it, and the decision is a
 * synchronous Map lookup.
 *
 * The one place we deliberately DO wait is the very first playlist of a
 * session: `fetch` is async, so we can hold the response open until the pool
 * warms rather than handing over Twitch's body and swapping under the player
 * later. That swap is what froze the Firefox build at readyState 2, and the
 * hold is what removes the join stutter.
 *
 * Config rides in on location.hash so it is readable synchronously at
 * document_start — there is no time to await chrome.storage, and MAIN world
 * cannot see chrome.* anyway. Absent (a real install), the defaults are the
 * shipping behaviour:
 *
 *   #unslopmode=rewrite|observe  observe = watch only, hand back the real body.
 *                               The control arm. Default rewrite.
 *   #warm=<ms>                   how long the first playlist may be held
 *   #mask=0                      leave Function.prototype.toString alone
 */
(function () {
  /* ---------------------------------------------------------------- config */
  var CFG = { mode: "rewrite", warmMs: 8000, maxHolds: 4, mask: 1, where: "window" };
  try {
    var h = String(location.hash || "");
    if (!/unslopmode=/.test(h)) {
      // subframe: the hash only exists on the top document
      try { h = String(top.location.hash || h); } catch (e) { /* cross-origin */ }
    }
    var m;
    if ((m = /unslopmode=(\w+)/.exec(h))) CFG.mode = m[1];
    if ((m = /[#&]warm=(\d+)/.exec(h))) CFG.warmMs = +m[1];
    if ((m = /[#&]mask=(\d)/.exec(h))) CFG.mask = +m[1];
  } catch (e) { /* keep defaults */ }

  /* Marker so a second injection (all_frames, bfcache, SPA remount) is a
   * no-op. Non-enumerable so it does not show up in Object.keys(window). */
  try {
    if (Object.getOwnPropertyDescriptor(window, "__unslop")) return;
    Object.defineProperty(window, "__unslop", { value: 1, enumerable: false });
  } catch (e) { /* fall through, double-install is survivable */ }

  /* --------------------------------------------------------------- relay
   * MAIN world has no chrome.* . Everything goes out through postMessage to
   * bridge.js (ISOLATED world), which forwards to the service worker, which
   * forwards to the pool. Pushes come back the same way. */
  function emit(o) {
    try { window.postMessage({ __unslop: 1, e: o }, location.origin); } catch (e) { /* */ }
  }

  /* ------------------------------------------------------------------ core
   *
   * Self-contained on purpose: it is stringified with Function.prototype
   * .toString() and evaluated inside every Worker the page creates, so it may
   * not close over anything in this file. `coreSrc` is its own source, passed
   * back in so a worker can shim a nested worker.
   */
  function UNSLOP_HOOK(scope, cfg, emit, coreSrc) {
    var NF = scope.fetch;
    var NX = scope.XMLHttpRequest;
    var NW = scope.Worker;

    /* -- the cache the hot path reads ---------------------------------
     * key = origin + pathname of a media playlist URL (the player appends its
     * own query params to the variant URL it got from the master, so exact
     * string matching misses every poll). Filled only by pushes from the pool;
     * nothing in here decides WHAT to serve, only whether something is ready. */
    var SERVE = new Map();
    var ON = true;            // the pool says unslop is switched on
    var LAST = null;          // most recent push, replayed into new workers
    var KIDS = [];            // workers we shimmed, to forward pushes down
    var everServed = false;   // never hold once we have served once
    var holds = 0;

    function applyPush(p) {
      if (!p) return;
      LAST = p;
      ON = p.enabled !== false;
      var next = new Map();
      var map = p.map || {};
      var bodies = p.bodies || {};
      for (var k in map) {
        var m = map[k];
        var b = bodies[m.hash];
        if (typeof b === "string") {
          next.set(k, { body: b, hash: m.hash, chan: m.chan, rend: m.rend,
                        collapsed: !!m.collapsed });
        }
      }
      SERVE = next;
      for (var i = 0; i < KIDS.length; i++) {
        try { KIDS[i].postMessage({ __unslop_push: p }); } catch (e) { /* dead */ }
      }
    }

    /* -- native-code masking ------------------------------------------
     * Only functions WE created are masked. Never mask a page function:
     * bundlers really do build inline workers out of fn.toString(), and
     * handing one "[native code]" would break the page. */
    var MASK = new WeakMap();
    var NTS = Function.prototype.toString;
    function mask(fn, name, len) {
      try { Object.defineProperty(fn, "name", { value: name, configurable: true }); } catch (e) { /* */ }
      try { Object.defineProperty(fn, "length", { value: len, configurable: true }); } catch (e) { /* */ }
      MASK.set(fn, "function " + name + "() { [native code] }");
      return fn;
    }
    if (cfg.mask) {
      var patchedTS = function toString() {
        var s = MASK.get(this);
        return s !== undefined ? s : NTS.apply(this, arguments);
      };
      mask(patchedTS, "toString", 0);
      try { Function.prototype.toString = patchedTS; } catch (e) { /* */ }
    }

    /* -- URL classification -------------------------------------------
     * Same shapes ext/background.js matches on: usher for the master (both
     * /api/channel/hls/ and the player's /api/v2/channel/hls/), and
     * *.playlist.ttvnw.net for the media playlist. */
    function classify(url) {
      try {
        var u = String(url || "");
        if (!u) return null;
        if (u.indexOf("usher.ttvnw.net") >= 0
            && /\/api\/(?:v\d+\/)?channel\/hls\//.test(u)) return "master";
        if (/:\/\/[^/]*\.playlist\.ttvnw\.net\//.test(u)) return "media";
        if (u.indexOf("gql.twitch.tv/gql") >= 0) return "gql";
        return null;
      } catch (e) { return null; }
    }

    /* Origin+path, never the raw URL — the player's query params change every
     * poll. Mirrors core.js's variantKey(); the pool keys its push map the
     * same way. This is two lines of URL handling, not a copy of any engine
     * logic: everything that decides WHAT goes in the map is in core.js. */
    function keyOf(u) {
      try {
        var x = new URL(String(u), scope.location && scope.location.href);
        return x.origin + x.pathname;
      } catch (e) { return String(u); }
    }

    function sleepMs(ms) {
      return new Promise(function (r) { scope.setTimeout(r, ms); });
    }

    var seq = 0;
    function nextId() {
      return (cfg.where === "window" ? "p" : "w") + (++seq) + "."
        + ((Math.random() * 1e9) | 0).toString(36);
    }

    /* The one decision. Returns the replacement body, or null for "hand back
     * whatever Twitch sent".
     *
     * Two messages per poll, both fire-and-forget:
     *   media  — the real body, immediately. The pool needs it to bind the
     *            rendition, seed MEDIA-SEQUENCE, read the ad tells out of the
     *            viewer's OWN playlist and keep it as evidence. Sending it
     *            before we decide is what makes the hold able to succeed.
     *   done   — what we did with it. The pool owns every counter; this file
     *            counts nothing about itself, because a number the hook
     *            reports about the hook is not evidence.
     */
    async function onMedia(url, body, status, via) {
      var key = keyOf(url);
      var id = nextId();
      emit({
        t: Date.now(), ev: "hook_media", lvl: "trace", id: id, key: key,
        url: String(url).split("?")[0], len: body.length, status: status,
        via: via, where: cfg.where, mode: cfg.mode, body: body,
      });

      var t0 = Date.now();
      var live = ON && cfg.mode === "rewrite";
      var hit = live ? SERVE.get(key) : null;
      /* Hold the first playlist open until the pool is warm rather than
       * handing over Twitch's body and swapping underneath the player later.
       * Bounded twice: by warmMs, and by maxHolds — a pool that never warms
       * (channel offline, donors erroring) must not stall every poll forever.
       * An ad beats a frozen player.
       *
       * Never in observe mode: the control arm has to have the same timing as
       * an unhooked player or it is not a control. */
      if (live && !hit && !everServed && holds < cfg.maxHolds) {
        holds++;
        var deadline = t0 + cfg.warmMs;
        while (Date.now() < deadline) {
          await sleepMs(100);
          hit = SERVE.get(key);
          if (hit) break;
        }
      }
      if (hit) everServed = true;
      /* `would` is what makes the observe arm worth running: it says the pool
       * had a body ready for this exact poll and we handed Twitch's through
       * anyway. Without it an observe run only proves the hook can watch, and
       * "the pool was warm" would be a claim with nothing behind it. */
      var can = hit || (ON ? SERVE.get(key) : null);
      emit({
        t: Date.now(), ev: "hook_done", lvl: "trace", id: id, key: key,
        served: !!hit, would: !!can, mode: cfg.mode,
        hash: hit ? hit.hash : null,
        waited: Date.now() - t0, where: cfg.where,
      });
      return hit ? hit.body : null;
    }

    // Master and gql are pass-through: the pool parses them, we never alter
    // them. Reported so the pool can build its variant index and learn the
    // channel without any extension-side request interception.
    function onSide(kind, url, body, status, via) {
      emit({
        t: Date.now(), ev: kind === "master" ? "hook_master" : "hook_gql",
        lvl: "trace", url: String(url).split("?")[0], len: body.length,
        status: status, via: via, where: cfg.where, body: body,
      });
    }

    /* -- fetch --------------------------------------------------------- */
    if (typeof NF === "function") {
      var pf = function fetch(input, init) {
        var url = "";
        try {
          url = (typeof input === "string") ? input
            : (input && typeof input === "object" && input.url) ? input.url
              : String(input);
        } catch (e) { /* */ }
        var kind = classify(url);
        if (kind === "gql") {
          // The channel, learned independently of usher: this is the FIRST
          // thing the player does and it carries the login in the request
          // body. Read-only, and only for the one operation we care about.
          try {
            var rb = init && init.body;
            if (typeof rb === "string" && rb.indexOf("PlaybackAccessToken") >= 0) {
              onSide("gql", url, rb, 0, "fetch");
            }
          } catch (e) { /* */ }
          return NF.apply(scope, arguments);
        }
        var p = NF.apply(scope, arguments);
        if (kind !== "master" && kind !== "media") return p;
        return p.then(function (res) {
          return res.clone().text().then(function (body) {
            if (kind === "master") {
              onSide("master", url, body, res.status, "fetch");
              return res;
            }
            return onMedia(url, body, res.status, "fetch").then(function (out) {
              if (out === null) return res;
              var hdr;
              try {
                hdr = new scope.Headers(res.headers);
                // Our body is a different length than Twitch's; a stale
                // Content-Length truncates it.
                hdr.delete("content-length");
              } catch (e) { hdr = undefined; }
              return new scope.Response(out, {
                status: res.status, statusText: res.statusText, headers: hdr,
              });
            });
          }, function () { return res; });
        });
      };
      mask(pf, "fetch", 1);
      try { scope.fetch = pf; } catch (e) { /* */ }
    }

    /* -- XMLHttpRequest ------------------------------------------------
     * Subclassed rather than prototype-patched so the readystatechange
     * listener is registered in the constructor — i.e. before the page can
     * attach its own and therefore ahead of it in the listener list.
     *
     * Measured: Twitch uses fetch for every playlist (via@where is
     * `fetch@worker` in all four spike runs, XHR never appears). This path
     * exists so a change of engine degrades to "observed but not rewritten"
     * rather than to silence. It cannot hold — readyState 4 is synchronous —
     * so it serves only what is already cached. */
    if (typeof NX === "function") {
      var HookXHR = class extends NX {
        constructor() {
          super();
          var x = this;
          try {
            this.addEventListener("readystatechange", function () {
              if (x.readyState !== 4) return;
              try {
                var url = x.__uUrl || x.responseURL || "";
                var kind = classify(url);
                if (kind !== "media" && kind !== "master") return;
                var rt = x.responseType;
                if (rt !== "" && rt !== "text") {
                  emit({
                    t: Date.now(), ev: "hook_media", lvl: "warn", via: "xhr",
                    where: cfg.where, url: String(url).split("?")[0],
                    len: -1, status: x.status, responseType: rt,
                    note: "non-text responseType — body not readable as text",
                  });
                  return;
                }
                var body = x.responseText || "";
                if (kind === "master") { onSide("master", url, body, x.status, "xhr"); return; }
                // Report it, then serve only from what is already cached.
                onMedia(url, body, x.status, "xhr");
                var hit = (ON && cfg.mode === "rewrite") ? SERVE.get(keyOf(url)) : null;
                if (!hit) return;
                everServed = true;
                Object.defineProperty(x, "responseText", { value: hit.body, configurable: true });
                Object.defineProperty(x, "response", { value: hit.body, configurable: true });
              } catch (e) { /* */ }
            });
          } catch (e) { /* */ }
        }
        open(method, url) {
          try { this.__uUrl = String(url); } catch (e) { /* */ }
          return super.open.apply(this, arguments);
        }
      };
      mask(HookXHR, "XMLHttpRequest", 0);
      try { scope.XMLHttpRequest = HookXHR; } catch (e) { /* */ }
    }

    /* -- Worker --------------------------------------------------------
     * The whole point, and the part with no alternative: Twitch fetches every
     * playlist from inside a classic blob Worker, so patching window.fetch
     * alone catches nothing. new Worker(realUrl) becomes new Worker(blobUrl)
     * where the blob installs this same core into the worker scope, registers
     * the push receiver, and only then importScripts()/import()s the real
     * script. twitch.tv sends no CSP at all (verified 2026-07-29), so blob:
     * workers and cross-origin importScripts are both unrestricted.
     *
     * Costs, all real and all reported:
     *   - self.location inside the worker becomes the blob: URL, so anything
     *     the real script resolves relative to itself now resolves against
     *     the page origin.
     *   - a module worker needs dynamic import(), which is CORS-checked where
     *     classic importScripts() is not.
     *   - stack traces in the worker name a blob: URL. */
    if (typeof NW === "function") {
      var childCfg = {
        mode: cfg.mode, warmMs: cfg.warmMs, maxHolds: cfg.maxHolds,
        mask: cfg.mask,
        where: (cfg.where === "window" ? "worker" : cfg.where + ">worker"),
      };
      var HookWorker = class extends NW {
        constructor(url, opts) {
          var abs, shim = null, mod = !!(opts && opts.type === "module");
          try { abs = new URL(String(url), scope.location && scope.location.href).href; }
          catch (e) { abs = String(url); }
          try {
            /* The push receiver is registered BEFORE importScripts, so it is
             * the first `message` listener on the worker scope and can
             * stopImmediatePropagation our own traffic out of Twitch's
             * handlers — including the `self.onmessage =` property form, which
             * is registered later and therefore behind us. */
            var boot = "var __u=(" + coreSrc + ")(self," + JSON.stringify(childCfg)
              + ",function(o){try{self.postMessage({__unslop_w:1,e:o});}catch(_){}},"
              + JSON.stringify(coreSrc) + ");\n"
              + "self.addEventListener('message',function(ev){var d=ev&&ev.data;"
              + "if(d&&d.__unslop_push){try{ev.stopImmediatePropagation();}catch(_){}"
              + "__u.push(d.__unslop_push);}},false);\n"
              + (mod ? "import(" + JSON.stringify(abs) + ");"
                : "importScripts(" + JSON.stringify(abs) + ");");
            shim = URL.createObjectURL(new Blob([boot], { type: "text/javascript" }));
          } catch (e) { shim = null; }
          emit({
            t: Date.now(), ev: "hook_worker", lvl: "debug", where: cfg.where,
            url: abs, module: mod, shimmed: !!shim,
            blob: /^blob:/.test(abs), opts: opts ? Object.keys(opts) : [],
          });
          super(shim || url, opts);
          var self_ = this;
          try {
            // First listener on the object: the page has not seen it yet, so
            // nothing else can be ahead of us, and stopImmediatePropagation
            // keeps our own chatter out of the page's message handlers.
            this.addEventListener("message", function (ev) {
              var d = ev && ev.data;
              if (d && d.__unslop_w === 1) {
                try { ev.stopImmediatePropagation(); } catch (e) { /* */ }
                emit(d.e);
              }
            });
          } catch (e) { /* */ }
          if (shim) {
            KIDS.push(self_);
            // Replay the current state into a worker created after the last
            // push — otherwise a worker spawned mid-session starts cold and
            // passes ads through until the next push lands.
            if (LAST) {
              try { self_.postMessage({ __unslop_push: LAST }); } catch (e) { /* */ }
            }
            scope.setTimeout(function () {
              try { URL.revokeObjectURL(shim); } catch (e) { /* */ }
            }, 30000);
          }
        }
      };
      mask(HookWorker, "Worker", 1);
      try { scope.Worker = HookWorker; } catch (e) { /* */ }
    }

    return {
      fetch: typeof NF === "function",
      xhr: typeof NX === "function",
      worker: typeof NW === "function",
      push: applyPush,
    };
  }

  /* ------------------------------------------------------------- install */
  var coreSrc = UNSLOP_HOOK.toString();
  var api = null;
  var err = null;
  try {
    api = UNSLOP_HOOK(window, CFG, emit, coreSrc);
  } catch (e) {
    err = String(e && e.stack || e);
  }

  // Pushes arrive from bridge.js in the ISOLATED world. The page scope needs
  // them for its own fetches, and applyPush fans them out to every shimmed
  // worker — which is where the playlists actually are.
  if (api) {
    window.addEventListener("message", function (ev) {
      if (ev.source !== window) return;
      var d = ev.data;
      if (!d || d.__unslop !== 1 || !d.push) return;
      try { api.push(d.push); } catch (e) { /* */ }
    }, false);
  }

  var scripts = -1;
  try { scripts = document.getElementsByTagName("script").length; } catch (e) { /* */ }
  emit({
    t: Date.now(), ev: "hook_install", lvl: "info", where: "window",
    mode: CFG.mode, warmMs: CFG.warmMs, mask: CFG.mask,
    href: String(location.href), top: window === top,
    readyState: (function () { try { return document.readyState; } catch (e) { return "?"; } })(),
    scriptsAtInstall: scripts,
    hasBody: !!(document && document.body),
    perfNow: (function () { try { return +performance.now().toFixed(2); } catch (e) { return null; } })(),
    patched: api ? { fetch: api.fetch, xhr: api.xhr, worker: api.worker } : null,
    error: err,
  });
})();
