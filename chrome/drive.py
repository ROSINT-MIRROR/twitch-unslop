#!/usr/bin/env python3
"""
Drive Chrome with chrome/ loaded, run the extension for real, and score it.

  python chrome/drive.py <channel> [seconds] [mode] [level]

    mode     rewrite | observe   (default rewrite)
             observe = the hook is installed, the pool runs, everything is
             logged, and the player is handed Twitch's own body back untouched.
             That is the CONTROL ARM: the `would` field on each media event
             then says whether the pool had a clean chain ready for that exact
             poll, so "the pool was warm" stops being a claim about ourselves.
    level    error|warn|info|debug|trace   (default debug). Prose only —
             events and manifests are recorded in full at any level.

Same sink shape as ext/tryout.py (ext/EVENTS.md), on port 8780 because 8779
belongs to the Firefox rig and both can be running at once. Output:

  data/chrome/<channel>.<YYmmdd-HHMMSS>/
      events.jsonl   the structured stream — the source of truth
      manifests/     playlist bodies worth keeping, deduped by content hash
      ext.log        prose, this run only
      meta.json      argv, versions, start/end, final counters
      summary.json   the verdict — see verdict() below

  python ext/extreport.py data/chrome/<channel>.<YYmmdd-HHMMSS>

reads a Chrome session unchanged; the event schema is identical.

The only Twitch sessions are the browser tab and the extension's own donors
(4 by default, ARMS in ext/core.js). Nothing in this file polls anything.
"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

LAB = pathlib.Path(__file__).resolve().parents[1]
EXT = LAB / "chrome"
OUT = LAB / "data" / "chrome"
LOGS = LAB / "data" / "logs"

# In-tree TMPDIR, and it has to be SHORT. Chromium always builds its process
# singleton socket at $TMPDIR/org.chromium.Chromium.<6>/SingletonSocket — the
# profile only gets a symlink — and a sun_path is 108 bytes including the NUL.
# data/.scratch (what ext/tryout.py uses, 69 chars here) overflows that and
# Chromium dies with FATAL "Socket path too long" before chromedriver ever
# reaches it. 45 = "/" + 28-char unique dir + "/SingletonSocket".
SCRATCH = LAB / "data" / "t"
_budget = 107 - 45
if len(str(SCRATCH)) > _budget:
    sys.exit(f"{SCRATCH} is {len(str(SCRATCH))} chars; Chromium's singleton "
             f"socket needs an in-tree TMPDIR of at most {_budget}. Move the "
             f"lab to a shorter path.")
for d in (OUT, LOGS, SCRATCH):
    d.mkdir(parents=True, exist_ok=True)
os.environ["TMPDIR"] = str(SCRATCH)

SINK_PORT = 8780          # 8779 belongs to ext/tryout.py; do not collide
CHROMEDRIVER = os.environ.get("CHROMEDRIVER", "/usr/bin/chromedriver")
LEVELS = ("error", "warn", "info", "debug", "trace")

# Manifest hashes name files on disk and they arrive over the wire, so they get
# treated as untrusted: anything outside this set is stripped before it becomes
# a path.
UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# Same script ext/tryout.py runs, so "did playback survive" is answered the
# same way in both browsers.
JS_STATE = """
const v = document.querySelector('video');
if (!v) return null;
const b = [];
for (let i = 0; i < v.buffered.length; i++)
  b.push([+v.buffered.start(i).toFixed(3), +v.buffered.end(i).toFixed(3)]);
return {vt: v.currentTime, paused: v.paused, w: v.videoWidth, h: v.videoHeight,
        ready: v.readyState, net: v.networkState, ranges: b,
        ahead: v.buffered.length ? v.buffered.end(v.buffered.length-1) - v.currentTime : 0,
        err: v.error ? (v.error.code + ':' + (v.error.message || '')) : null};
"""

PLAYER_FIELDS = ("vt", "w", "h", "paused", "ready", "net", "ranges", "err")

# The dashboard, read the way a tester reads it. popup.js/dash.js are shared
# verbatim with the Firefox build and both call chrome.runtime.sendMessage
# {cmd:"stats"} — which under MV3 is answered by the service worker out of a
# snapshot the pool pushes, not by the pool itself. That substitution is the
# one part of the UI path that is Chrome-only, so it gets checked rather than
# assumed: if it is wrong, every surface renders zeros and says nothing.
JS_DASH = """
const q = (id) => { const e = document.getElementById(id); return e ? e.textContent.trim() : null; };
return {chan: q('chan'), rend: q('rend'), state: q('state'), verdict: q('verdict'),
        chips: (document.getElementById('chips') || {}).childElementCount || 0,
        feed: (document.getElementById('feed') || {}).childElementCount || 0};
"""


def unpacked_id(path):
    """The extension ID Chrome will give an unpacked load of `path`.

    Deterministic: the first 128 bits of SHA-256 over the absolute path, each
    hex nibble mapped 0->a .. f->p. Cheaper and steadier than scraping
    chrome://extensions, and it is what makes chrome-extension://<id>/dash.html
    reachable from the driver so the UI can be read rather than assumed.
    """
    h = hashlib.sha256(str(path).encode()).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in h)


def ver(path):
    try:
        out = subprocess.run([path, "--version"], capture_output=True,
                             text=True, timeout=10)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception:
        return None


def major(s):
    if not s:
        return None
    for tok in s.split():
        if tok[0].isdigit() and "." in tok:
            return tok.split(".")[0]
    return None


def pick_browser():
    """chromedriver refuses a browser of a different major version, and this
    box has chromedriver 150 (from the `chromium` package) next to
    google-chrome-stable 149. Picking by version rather than by name is the
    difference between a run and a SessionNotCreatedException."""
    forced = os.environ.get("CHROME_BIN")
    want = major(ver(CHROMEDRIVER))
    cands = [forced] if forced else [
        shutil.which("google-chrome-stable"), shutil.which("google-chrome"),
        shutil.which("chromium"), shutil.which("chromium-browser"),
    ]
    seen, rows = set(), []
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        rows.append((c, ver(c)))
    if forced:
        return rows[0][0], rows[0][1], want, rows
    for c, v in rows:
        if want and major(v) == want:
            return c, v, want, rows
    if rows:
        return rows[0][0], rows[0][1], want, rows
    sys.exit("no chrome/chromium binary found")


class Session:
    """One run's output directory.

    Every writer goes through one lock. The sink is a ThreadingHTTPServer, so
    two POSTs can land at once, and the main loop writes `player` events from a
    third thread — without the lock two half-lines interleave in events.jsonl
    and the file stops being parseable at exactly the moment it matters.
    """

    def __init__(self, channel, mode, level, seconds):
        self.dir = OUT / f"{channel}.{time.strftime('%y%m%d-%H%M%S')}"
        self.mandir = self.dir / "manifests"
        self.mandir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.closed = False
        self.evf = open(self.dir / "events.jsonl", "a", buffering=1,
                        encoding="utf-8")
        self.prose = open(self.dir / "ext.log", "a", buffering=1,
                          encoding="utf-8")
        self.seen = {p.name[:-5] for p in self.mandir.glob("*.m3u8")}
        self.counts = {"posts": 0, "lines": 0, "events": 0, "manifests": 0,
                       "dropped": 0}
        self.meta = {
            "channel": channel, "mode": mode, "level": level,
            "seconds": seconds, "argv": sys.argv, "dir": str(self.dir),
            "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "counts": self.counts, "stats": {},
        }

    def lines(self, lines):
        stamp = time.strftime("%H:%M:%S")
        blob = "".join(f"{stamp} {l}\n" for l in lines)
        if not blob:
            return
        with self.lock:
            if self.closed:
                return
            self.counts["lines"] += len(lines)
            try:
                self.prose.write(blob)
                self.prose.flush()
            except Exception:
                pass

    def events(self, evs):
        out, drops = [], 0
        for e in evs:
            if not isinstance(e, dict):
                drops += 1
                continue
            e.setdefault("t", int(time.time() * 1000))
            try:
                out.append(json.dumps(e, separators=(",", ":"), default=str))
            except Exception:
                drops += 1
        with self.lock:
            if self.closed:
                return
            self.counts["dropped"] += drops
            if out:
                self.counts["events"] += len(out)
                try:
                    self.evf.write("\n".join(out) + "\n")
                    self.evf.flush()
                except Exception:
                    pass

    def event(self, name, **fields):
        self.events([dict(fields, t=int(time.time() * 1000), ev=name)])

    def manifests(self, mans):
        """Playlist bodies, deduped by the hash the extension assigned. Written
        via a .part rename so a kill mid-write cannot leave a truncated body
        that later looks already-saved."""
        for m in mans:
            if not isinstance(m, dict):
                continue
            body = m.get("body")
            if not isinstance(body, str) or not body:
                continue
            h = UNSAFE.sub("", str(m.get("hash") or "")).lstrip(".")[:64]
            if not h:
                h = hashlib.sha1(body.encode()).hexdigest()[:12]
            with self.lock:
                if self.closed or h in self.seen:
                    continue
                self.seen.add(h)
                self.counts["manifests"] += 1
            dst = self.mandir / f"{h}.m3u8"
            if dst.exists():
                continue
            try:
                tmp = self.mandir / f".{h}.part"
                tmp.write_bytes(body.encode())
                tmp.replace(dst)
            except Exception:
                pass

    def meta_write(self, **extra):
        self.meta.update(extra)
        try:
            (self.dir / "meta.json").write_text(
                json.dumps(self.meta, indent=2, default=str) + "\n")
        except Exception:
            pass

    def close(self, **extra):
        self.meta_write(ended=time.strftime("%Y-%m-%dT%H:%M:%S%z"), **extra)
        with self.lock:
            self.closed = True
            for f in (self.evf, self.prose):
                try:
                    f.close()
                except Exception:
                    pass


def start_sink(sess, level):
    """Receive what the extension POSTs: {lines, events, manifests, stats},
    every field optional (ext/EVENTS.md). The response carries the log level
    back, which is how run.sh's third argument reaches a scope that cannot see
    argv. Level filters prose only.

    `canary` stays False: it is an `embed` session on a rejoin loop and joining
    is what draws ads, so it manufactures the thing it measures. Off unless a
    measurement run explicitly wants it.
    """
    shared = {"stats": {}, "hits": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            try:
                d = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                d = {}
            if not isinstance(d, dict):
                d = {}
            shared["hits"] += 1
            with sess.lock:
                sess.counts["posts"] += 1
            try:
                if isinstance(d.get("stats"), dict):
                    shared["stats"] = d["stats"]
                lines = [str(x) for x in (d.get("lines") or [])]
                for line in lines:
                    print(f"    | {line}", flush=True)
                sess.lines(lines)
                if isinstance(d.get("events"), list):
                    sess.events(d["events"])
                if isinstance(d.get("manifests"), list):
                    sess.manifests(d["manifests"])
            except Exception as e:      # never let a bad payload kill the sink
                print(f"[!] sink: {e!r}", flush=True)
            b = json.dumps({"level": level, "canary": False}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = ThreadingHTTPServer(("127.0.0.1", SINK_PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return shared, srv


def med(xs):
    return round(statistics.median(xs), 2) if xs else None


def px(res):
    """'1920x1080' -> 2073600. max() over the strings picks '640x360'."""
    try:
        w, h = res.split("x")
        return int(w) * int(h)
    except Exception:
        return -1


def manifest_urls(mandir):
    """Every segment URL in every stored playlist body, indexed by the URL the
    player will actually request.

    The player drops the `?dna=<blob>` query before it fetches the bytes
    (measured 2026-07-29), so the only way to line a fetch up against the body
    that offered it is to strip the query on both sides. Prefetch lookahead
    counts: it is fetched like any other segment.
    """
    out = {}
    for f in sorted(pathlib.Path(mandir).glob("*.m3u8")):
        h = f.name[:-5]
        for line in f.read_text(errors="replace").splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("#EXT-X-TWITCH-PREFETCH:"):
                s = s[23:].strip()
            elif s.startswith("#"):
                continue
            out.setdefault(s.split("?")[0], set()).add(h)
    return out


def verdict(path, mode):
    """Read a run back and answer what a live run exists to answer.

    Every number here comes from one of two independent streams and they are
    kept apart on purpose:

      hook / pool  what the extension says about itself — `media`, `serve`,
                   `arm`, `up`. Useful, but a number the hook reports about the
                   hook is not evidence.
      network      `net`, straight out of chrome.webRequest in the service
                   worker, which no page code can influence. `ours` separates
                   the donor pool's own polling from the player's.

    Four questions:

      1. Did the hook see every media playlist?  hook `media` events vs the
         `net` media stream with ours=false.
      2. Did the pool warm?  `arm` events with onGrid, and `serve`.
      3. Did the player consume OUR body?  `segment` events carry via=ours when
         the URL came out of a manifest we generated, via=real otherwise. The
         page never saw our segment list, so this is the player's own choice.
      4. Did playback advance 1:1?  `player` samples: video seconds gained per
         wall-clock second between the first and last sample.

    And the budget: distinct donor arms, and how many playlist polls were ours.
    """
    net_media = net_media_ours = net_master = net_master_ours = 0
    seg_paths, seg_paths_ours = set(), set()
    seg_urls = set()
    out_hashes, real_hashes = set(), set()
    media, serves, arms, segs, holds = [], [], [], [], []
    installs, workers, ups, badge, ui = [], [], [], [], []
    player, ladders, rebinds, errs = [], [], [], []
    dec = {}
    for line in pathlib.Path(path).read_text(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        ev = e.get("ev")
        if ev == "net":
            k, ours = e.get("kind"), bool(e.get("ours"))
            if k == "media":
                net_media_ours += ours
                net_media += not ours
            elif k == "master":
                net_master_ours += ours
                net_master += not ours
            elif k == "segment" and e.get("path"):
                (seg_paths_ours if ours else seg_paths).add(e["path"])
                if not ours and e.get("url"):
                    seg_urls.add(e["url"].split("?")[0])
        elif ev == "media":
            media.append(e)
            if e.get("outHash"):
                out_hashes.add(e["outHash"])
            if e.get("realHash"):
                real_hashes.add(e["realHash"])
            dec[e.get("decision")] = dec.get(e.get("decision"), 0) + 1
            if isinstance(e.get("waited"), (int, float)) and e["waited"] > 0:
                holds.append(e["waited"])
        elif ev == "serve":
            serves.append(e)
        elif ev == "arm":
            arms.append(e)
        elif ev == "segment":
            segs.append(e)
        elif ev == "hook_install":
            installs.append(e)
        elif ev == "hook_worker":
            workers.append(e)
        elif ev == "badge_probe":
            badge.append(e)
        elif ev == "ui_probe":
            ui.append(e)
        elif ev == "up":
            ups.append(e)
        elif ev == "player":
            player.append(e)
        elif ev in ("ladder_collapse", "ladder_restore"):
            ladders.append(e)
        elif ev == "rebind":
            rebinds.append(e)
        if e.get("lvl") == "error":
            errs.append(e)

    fetched = [s for s in segs if s.get("via")]
    ours_fetched = [s for s in fetched if s["via"] == "ours"]
    ad_fetched = [s for s in fetched if s.get("ad")]

    # The same question asked a second way, entirely offline and without
    # trusting a single counter the extension keeps: take the segment URLs
    # chrome.webRequest saw the page fetch, and look them up in the playlist
    # bodies on disk. A body is ours when its hash appears as some media
    # event's `outHash`, Twitch's when it appears as a `realHash`. This is the
    # check that does not care whether the live attribution was wired up right.
    murls = manifest_urls(pathlib.Path(path).parent / "manifests")
    stored = set().union(*murls.values()) if murls else set()
    from_ours = from_twitch = from_neither = 0
    for u in seg_urls:
        hs = murls.get(u, set())
        if hs & out_hashes:
            from_ours += 1
        elif hs & real_hashes:
            from_twitch += 1
        else:
            from_neither += 1

    vt = [(e.get("t"), e.get("vt")) for e in player
          if isinstance(e.get("vt"), (int, float))]
    ratio = None
    if len(vt) > 1 and vt[-1][0] and vt[0][0] and vt[-1][0] > vt[0][0]:
        wall = (vt[-1][0] - vt[0][0]) / 1000.0
        ratio = round((vt[-1][1] - vt[0][1]) / wall, 3) if wall > 0 else None

    on_grid = [a for a in arms if a.get("onGrid")]
    donors = sorted({a.get("n") for a in arms if a.get("n") is not None})
    warm_at = None
    if serves and ups:
        warm_at = round((serves[0]["t"] - ups[0]["t"]) / 1000.0, 1)

    return {
        "mode": mode,
        "hook": {
            "installs": len(installs),
            "workersShimmed": sum(1 for w in workers if w.get("shimmed")),
            "workers": len(workers),
            "patched": installs[0].get("patched") if installs else None,
            "installError": next((i.get("error") for i in installs
                                  if i.get("error")), None),
        },
        "coverage": {
            "mediaSeenByHook": len(media),
            "mediaSeenByNetwork": net_media,
            "missedByHook": max(0, net_media - len(media)),
            "masterSeenByNetwork": net_master,
        },
        "pool": {
            "armPolls": len(arms),
            "armPollsOnGrid": len(on_grid),
            "donorArms": donors,
            "servesGenerated": len(serves),
            "firstServeAfter_s": warm_at,
            "decisions": dec,
            "medianHold_ms": med(holds),
            "maxHold_ms": max(holds) if holds else None,
            "ladderEvents": len(ladders),
            "rebinds": len(rebinds),
        },
        # The control-arm number. In observe mode the hook hands Twitch's body
        # through on purpose, so `wouldHaveServed` is what says the pool was
        # genuinely warm at the moment we stood aside.
        "control": {
            "wouldHaveServed": sum(1 for m in media if m.get("would")),
            "observePolls": sum(1 for m in media if m.get("mode") == "observe"),
        },
        "consumption": {
            "segmentsFetched": len(fetched),
            "fetchedFromOurBody": len(ours_fetched),
            "fetchedFromTwitchBody": len(fetched) - len(ours_fetched),
            "adSegmentsPlayed": len(ad_fetched),
            "distinctSegmentPathsSeenByNetwork": len(seg_paths),
            # Offline cross-check against the stored bodies. Independent of
            # every counter above.
            "onDisk": {
                "distinctSegmentUrls": len(seg_urls),
                "inAManifestWeWrote": from_ours,
                "inAManifestTwitchWrote": from_twitch,
                # Bodies are only stored when they are evidence (any ad-bearing
                # poll, anything we did not rewrite) plus the first 20 of a
                # session, so `inNeither` grows with run length by design — it
                # is "the body that offered this was not worth disk", not a
                # miss. The ours-vs-Twitch ratio is the readable number.
                "inNeither": from_neither,
                "manifestsStored": len(stored),
                "oursStored": len(out_hashes & stored),
                "twitchStored": len(real_hashes & stored),
            },
        },
        "budget": {
            "ourPlaylistPolls": net_media_ours,
            "ourMasterFetches": net_master_ours,
            "ourSegmentFetches": len(seg_paths_ours),
            "donorArmsSeen": len(donors),
        },
        "playback": {
            "samples": len(player),
            "currentTimeFirst": vt[0][1] if vt else None,
            "currentTimeLast": vt[-1][1] if vt else None,
            "advanced_s": round(vt[-1][1] - vt[0][1], 1) if len(vt) > 1 else 0.0,
            "videoSecondsPerWallSecond": ratio,
            "lastErr": next((e.get("err") for e in reversed(player)
                             if e.get("err")), None),
            "maxRes": max([f"{e.get('w')}x{e.get('h')}" for e in player],
                          key=px, default=None),
        },
        "badgeProbe": badge[0] if badge else None,
        "uiProbe": ui[0] if ui else None,
        "errorEvents": len(errs),
        "errorSample": [e.get("ev") for e in errs[:5]],
    }


def score(v):
    """Pass/fail, and the reason. Deliberately strict about the two things a
    tester would notice: the video kept playing, and it was our video."""
    bad = []
    if v["coverage"]["mediaSeenByHook"] < 3:
        bad.append("hook saw almost no media playlists")
    # A poll in flight when the run ends has no decision yet; two of slack.
    if v["coverage"]["missedByHook"] > 2:
        bad.append(f"hook missed {v['coverage']['missedByHook']} playlists")
    if not v["hook"]["workersShimmed"]:
        bad.append("no worker was shimmed — the playlists are fetched in one")
    if v["playback"]["advanced_s"] <= 0:
        bad.append("video never advanced")
    r = v["playback"]["videoSecondsPerWallSecond"]
    if r is not None and not (0.9 <= r <= 1.1):
        bad.append(f"playback ran at {r}x, not 1:1")
    if v["playback"]["lastErr"]:
        bad.append(f"video element error {v['playback']['lastErr']}")
    if v["budget"]["donorArmsSeen"] > 4:
        bad.append(f"{v['budget']['donorArmsSeen']} donor arms — over budget")
    if v["mode"] == "rewrite":
        if not v["pool"]["servesGenerated"]:
            bad.append("pool never produced a playlist")
        if v["pool"]["decisions"].get("rewrite", 0) < 1:
            bad.append("no playlist was ever served from the donors")
        if not v["consumption"]["fetchedFromOurBody"]:
            bad.append("player never fetched a segment we advertised")
        d = v["consumption"]["onDisk"]
        if d["inAManifestWeWrote"] <= d["inAManifestTwitchWrote"]:
            bad.append("on disk, more fetched segments trace to Twitch's "
                       f"playlists ({d['inAManifestTwitchWrote']}) than to "
                       f"ours ({d['inAManifestWeWrote']})")
    else:
        if not v["control"]["wouldHaveServed"]:
            bad.append("control arm: pool was never warm, so it proves nothing")
        if v["pool"]["decisions"].get("rewrite", 0):
            bad.append("control arm served a playlist — it is not a control")
    return bad


def main():
    channel = sys.argv[1] if len(sys.argv) > 1 else "gaules"
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    mode = sys.argv[3] if len(sys.argv) > 3 else "rewrite"
    level = sys.argv[4] if len(sys.argv) > 4 else "debug"
    if mode not in ("rewrite", "observe"):
        sys.exit(f"bad mode {mode!r}")
    if level not in LEVELS:
        sys.exit(f"bad log level {level!r}")
    if seconds <= 0 or seconds > 120:
        sys.exit("seconds must be in (0, 120] — this rig runs bounded sessions")

    core = EXT / "core.js"
    if not core.exists():
        sys.exit("chrome/core.js is missing — it is a symlink to ext/core.js")
    if core.read_bytes() != (LAB / "ext" / "core.js").read_bytes():
        sys.exit("chrome/core.js has drifted from ext/core.js. The Chrome port "
                 "shares that file verbatim; fix the link, do not fork it.")

    binary, bver, want, rows = pick_browser()
    sess = Session(channel, mode, level, seconds)
    # NOT inside the session dir. Chromium puts its process-singleton socket
    # under the profile, and a UNIX socket path is capped at 108 bytes: with
    # the profile at data/chrome/<channel>.<stamp>/profile/ it overflows,
    # Chromium falls back to $TMPDIR (also in-tree, also too long) and dies
    # with "Socket path too long" before chromedriver ever talks to it.
    # Short, in-tree, one per mode so a rewrite and an observe run can coexist.
    prof = OUT / f".prof-{mode[0]}"
    if prof.exists():
        shutil.rmtree(prof, ignore_errors=True)
    prof.mkdir(parents=True, exist_ok=True)

    o = Options()
    o.binary_location = binary
    o.add_argument(f"--user-data-dir={prof}")
    o.add_argument(f"--load-extension={EXT}")
    # Chrome 137+ kills --load-extension behind this feature flag.
    o.add_argument("--disable-features=DisableLoadExtensionCommandLineSwitch")
    o.add_argument("--no-first-run")
    o.add_argument("--no-default-browser-check")
    o.add_argument("--disable-backgrounding-occluded-windows")
    o.add_argument("--disable-renderer-backgrounding")
    o.add_argument("--autoplay-policy=no-user-gesture-required")
    o.add_argument("--mute-audio")
    o.add_argument("--window-size=1280,800")
    if os.environ.get("HEADLESS") == "1":
        o.add_argument("--headless=new")
    o.add_experimental_option("excludeSwitches", ["enable-automation"])

    svc = Service(executable_path=CHROMEDRIVER,
                  log_output=str(LOGS / "chromedriver.unslop.log"))

    shared, srv = start_sink(sess, level)
    url = f"https://www.twitch.tv/{channel}#unslopmode={mode}"
    print(f"[+] session  {sess.dir}")
    print(f"[+] browser  {binary}  ({bver})  chromedriver major={want}")
    print(f"[+] sink     127.0.0.1:{SINK_PORT}  (level={level})")
    print(f"[*] {url}  for {seconds:.0f}s")
    sess.meta_write(binary=binary, browserVersion=bver,
                    chromedriver=ver(CHROMEDRIVER), url=url,
                    profile=str(prof))

    drv = None
    state = {"elapsed": 0.0, "stalls": 0}
    try:
        drv = webdriver.Chrome(options=o, service=svc)
        sess.meta_write(caps={k2: v for k2, v in drv.capabilities.items()
                              if k2 in ("browserVersion", "platformName")})
        drv.get(url)
        started = time.time()
        prev = None
        while time.time() - started < seconds:
            time.sleep(3)
            state["elapsed"] = time.time() - started
            try:
                v = drv.execute_script(JS_STATE)
            except Exception as e:
                print(f"[!] page read failed: {e!r}", flush=True)
                continue
            if not v:
                print("[.] no video element yet", flush=True)
                continue
            stalled = prev is not None and (v["vt"] - prev) <= 0.1 \
                and not v["paused"]
            state["stalls"] += 1 if stalled else 0
            prev = v["vt"]
            sess.event("player", lvl="warn" if stalled or v["err"] else "info",
                       **{kk: v.get(kk) for kk in PLAYER_FIELDS})
            print(f"[.] video t={v['vt']:7.1f} {v['w']}x{v['h']} "
                  f"paused={v['paused']} ready={v['ready']} "
                  f"ahead={v['ahead']:.1f}s"
                  + ("  << STALL" if stalled else ""), flush=True)
        # One look at the dashboard before the browser goes away, in a second
        # tab so the player is never disturbed.
        try:
            main = drv.current_window_handle
            drv.switch_to.new_window("tab")
            drv.get(f"chrome-extension://{unpacked_id(EXT)}/dash.html")
            time.sleep(3)
            ui = drv.execute_script(JS_DASH)
            sess.event("ui_probe", lvl="info", **(ui or {}))
            print(f"[+] dashboard {ui}", flush=True)
            drv.close()
            drv.switch_to.window(main)
        except Exception as e:
            sess.event("ui_probe", lvl="warn", error=repr(e))
            print(f"[!] dashboard probe failed: {e!r}", flush=True)
    except KeyboardInterrupt:
        print("\n[*] interrupted")
    finally:
        try:
            if drv:
                drv.quit()
        except Exception:
            pass
        time.sleep(1.5)          # let the last sink POST land
        try:
            srv.shutdown()
        except Exception:
            pass
        sess.close(elapsed=round(state["elapsed"], 1),
                   stalls=state["stalls"], sinkPosts=shared["hits"],
                   events=sess.counts["events"], stats=shared["stats"])

    v = verdict(sess.dir / "events.jsonl", mode)
    bad = score(v)
    v["verdict"] = "OK" if not bad else "NOT PROVEN"
    v["failures"] = bad
    (sess.dir / "summary.json").write_text(json.dumps(v, indent=2) + "\n")
    print("\n=== " + mode + " ===")
    print(json.dumps(v, indent=2))
    print(f"\n  session   {sess.dir}")
    print(f"  report    python ext/extreport.py {sess.dir}")
    if bad:
        for b in bad:
            print(f"  !! {b}")
    print("RUN OK" if not bad else "RUN NOT PROVEN")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
