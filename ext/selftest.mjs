/*
 * Headless test of the extension's splice logic — no browser involved.
 * Loads core.js + background.js with the webextension API stubbed out, then
 * runs parseMaster against a REAL usher response and Store/chain/playlist
 * against real media playlists pulled live.
 *
 *   node ext/selftest.mjs [channel]
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";

const CH = process.argv[2] || "gaules";
let fails = 0;
const ok = (c, m) => { console.log(`  ${c ? "ok  " : "FAIL"} ${m}`); if (!c) fails++; };

const src = (f) => readFileSync(new URL(`./${f}`, import.meta.url), "utf8");

/* Exactly the globals core.js is allowed: what a page, a worker and this vm all
 * have. Nothing extension-shaped. Loading core.js against this and no more is
 * the mechanical proof that a port can take it verbatim. */
const plain = () => ({
  console, fetch, setTimeout, clearTimeout, setInterval, clearInterval,
  TextDecoder, TextEncoder,
  URL, URLSearchParams, Date, Math, Number, JSON, Promise, Array, Object, String, Error,
});

const noop = () => {};
const sandbox = Object.assign(plain(), {
  browser: {
    webRequest: {
      onBeforeRequest: { addListener: noop },
      onHeadersReceived: { addListener: noop },
      filterResponseData: noop,
    },
    runtime: { onMessage: { addListener: noop } },
  },
});
const ctx = createContext(sandbox);
// Load order is manifest.json's background.scripts order: core.js, then the
// Firefox shell. One shared script scope, no module system — background.js only
// resolves because core.js already ran.
runInContext(src("core.js"), ctx);
runInContext(src("background.js"), ctx);
runInContext(
  "globalThis.__x = {parseMaster, Store, Pool, mint, ladder, RE_PDTSEG, stats};", ctx);
const { Store, Pool, mint, ladder, stats } = sandbox.__x;

console.log(`[.] ${CH}`);

/* --- the split -----------------------------------------------------------
 * core.js is the half the Chrome port shares verbatim, so it must not name an
 * extension API. Asserted rather than left to discipline: nothing else stops
 * the split rotting back into one file the first time a browser call is
 * convenient. */
{
  const bad = src("core.js").match(/browser\.|chrome\.|webRequest|filterResponseData/g);
  ok(!bad, "core.js names no extension API"
    + (bad ? `: ${[...new Set(bad)].join(" ")}` : ""));

  // ...and it loads with no `browser` object in scope at all
  const bare = createContext(plain());
  let got = null;
  try {
    runInContext(src("core.js"), bare);
    runInContext("globalThis.__t = [typeof Store, typeof Pool, typeof parseMaster,"
      + " typeof ev, typeof hash].join()", bare);
    got = bare.__t;
  } catch (e) { got = String(e); }
  ok(got === "function,function,function,function,function",
     `core.js loads standalone, no extension API present (${got})`);
}

/* --- master parsing ---------------------------------------------------- */
const node = await mint(CH, "0".repeat(32), "site");
const lad = await ladder(CH, node);
ok(lad.length > 2, `ladder parsed: ${lad.map((v) => v.name + "=" + v.rend).join(" ")}`);
ok(lad.every((v) => v.url.startsWith("http")), "every variant has a url");
ok(lad.some((v) => v.rend === "1280x720@60"), "720p60 keyed as 1280x720@60");

// the bug this replaced: taking lad[0] gives a DIFFERENT rendition per mint
const orders = [];
for (let i = 0; i < 3; i++) {
  const n = await mint(CH, "0".repeat(32), "site");
  orders.push((await ladder(CH, n))[0].name);
}
console.log(`  ..  first-variant across 3 mints: ${orders.join(", ")}`);

/* --- the real Pool over REAL segments ------------------------------------
 * Uses Pool, not a lone session: a single arm holds nothing at all while it is
 * in an ad, which is the entire reason the pool exists. */
const want = "1280x720@60";
const pool = new Pool(CH);
pool.setRendition(want);
const store = pool.store;

const deadline = Date.now() + 90000;
while (store.chain().length < 12 && Date.now() < deadline) {
  await new Promise((r) => setTimeout(r, 1000));
}
pool.alive = false;
const onGrid = pool.arms.filter((a) => a.onGrid).length;
ok(store.chain().length >= 6,
   `pool chains ${store.chain().length} of ${store.seg.size} segments (${onGrid}/3 arms on grid)`);

const pl = store.playlist();
ok(pl !== null, "playlist() produced a manifest");
if (pl) {
  const lines = pl.split("\n");
  const pdts = lines.filter((l) => l.startsWith("#EXT-X-PROGRAM-DATE-TIME:"))
    .map((l) => Date.parse(l.split(":").slice(1).join(":")));
  const infs = lines.filter((l) => l.startsWith("#EXTINF:"));
  const segs = lines.filter((l) => l.startsWith("http"));

  ok(segs.length >= 6 && pdts.length === segs.length,
     `${segs.length} segments advertised, one PDT each`);
  ok(infs.every((l) => l.endsWith(",live")), "every segment titled live");
  ok(!pl.includes("#EXT-X-DISCONTINUITY"), "no spurious discontinuity");
  ok(pdts.every((v, i) => i === 0 || v > pdts[i - 1]), "PDTs strictly increasing");
  ok(/#EXT-X-MEDIA-SEQUENCE:\d+/.test(pl), "MEDIA-SEQUENCE present");

  // no overlap: each segment must start at/after the previous one's end
  let overlap = 0, end = null;
  for (let i = 0; i < pdts.length; i++) {
    const d = parseFloat(infs[i].slice(8));
    if (end !== null && pdts[i] < end - 500) overlap++;
    end = pdts[i] + Math.round(d * 1000);
  }
  ok(overlap === 0, `no overlapping segments (${overlap})`);

  const gaps = pdts.slice(1).map((v, i) => v - pdts[i]);
  console.log(`  ..  PDT deltas: ${[...new Set(gaps)].sort((a, b) => a - b).join(", ")}ms`);
  console.log(`  ..  ${stats.adSegsSeen} ad segments dropped, ${stats.regrid} sessions retired`);

  // MEDIA-SEQUENCE must advance by however many slid out, not per render
  const before = store.mseq;
  store.playlist();
  ok(store.mseq === before, "MEDIA-SEQUENCE stable when nothing slid out");
}

/* --- the ladder collapse -------------------------------------------------
 * usher drops the bound rendition out of the master 4-12s before an ad break
 * (gaules 2026-07-29, 4/4 breaks). Treating that as a quality change rebinds
 * the pool and flushes a store full of clean content at exactly the wrong
 * moment — that was 21 of 91 breaks leaking with regrid=0. `pool` is already
 * dead here, so nothing polls during this. */
{
  const held = store.rendition;
  const before = store.seg.size;
  ok(before > 0 && !!held, `store has ${before} segments bound to ${held}`);

  pool.onLadder([held, "852x480@30", "640x360@30", "284x160@30"]);
  ok(!pool.collapsed, "a full ladder is not a collapse");

  // the collapsed ladder measured on gaules: source rendition simply absent
  pool.onLadder(["640x360@30", "284x160@30"]);
  ok(pool.collapsed, "bound rendition missing from the master reads as collapse");
  ok(store.seg.size === before, `store NOT flushed by the collapse (${store.seg.size})`);

  // the player is forced to 360 while collapsed; the pool must not follow
  pool.setRendition("640x360@30");
  ok(store.rendition === held, `pool held ${held} against the forced 360`);
  ok(store.seg.size === before, `store still intact after the forced request`);
  ok(store.playlist() !== null, "still serving a clean manifest mid-collapse");

  pool.onLadder([held, "852x480@30", "640x360@30", "284x160@30"]);
  ok(!pool.collapsed, "full ladder returning ends the collapse");

  // and a genuine quality change, once the ladder is whole again, still works
  pool.lastRebind = 0;
  pool.setRendition("640x360@30");
  ok(store.rendition === "640x360@30", "a real quality change still rebinds");
  ok(store.seg.size === 0, "a real rebind does flush the store");
}

/* --- the phase rule ------------------------------------------------------ */
{
  const s = new Store();
  s.rendition = "x";
  const t0 = 1785326423429;
  for (let i = 0; i < 12; i++) s.offer(t0 + i * 4167, 4.167, `u${i}`, "i", false, "x");
  // an off-grid donor, exactly the 1431ms post-ad offset measured on gaules
  for (let i = 0; i < 12; i++) s.offer(t0 + i * 4167 + 1431, 4.167, `o${i}`, "i", false, "x");
  const keys = s.chain();
  let bad = 0, end = null;
  for (const k of keys) {
    if (end !== null && k < end - 500) bad++;
    end = k + Math.round(s.seg.get(k).dur * 1000);
  }
  ok(bad === 0, "off-grid donor is rejected, not interleaved");
  ok(s.skewed.size > 0, `skew counted (${s.skewed.size})`);
}

console.log(fails ? `\n${fails} FAILED` : "\nALL GREEN");
process.exit(fails ? 1 : 0);
