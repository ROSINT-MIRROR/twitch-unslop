#!/usr/bin/env python3
"""
Load ext/ into LibreWolf, point it at a channel, and record what happened.

  python ext/tryout.py <channel> [seconds] [level]   0 = stay open until Ctrl-C

Every run gets its own directory under data/ext/, because a single ever-growing
log cannot be handed to an analyser:

  data/ext/<channel>.<YYmmdd-HHMMSS>/
      events.jsonl   the structured stream — the source of truth (ext/EVENTS.md)
      manifests/     every distinct playlist body the extension shipped us
      ext.log        prose, this run only
      meta.json      channel, level, argv, versions, start/end, final counters

  python ext/extreport.py data/ext/<channel>.<YYmmdd-HHMMSS>

data/ext/ext.log (the top-level one) still gets every prose line appended, since
that is the file a human tails while a run is in flight.

Deliberately does NOT use browser/profile — that one forces every request
through mitmdump on :8888, which is not running and is not needed here. A
fresh profile is used instead, with TMPDIR and --profile-root kept in-tree.
"""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

LAB = pathlib.Path(__file__).resolve().parents[1]
EXT = LAB / "ext"
SCRATCH = LAB / "data" / ".scratch"
LOGS = LAB / "data" / "logs"
SCRATCH.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)
os.environ["TMPDIR"] = str(SCRATCH)

BINARY = os.environ.get("LIBREWOLF") or next(
    (c for c in ("/usr/lib/librewolf/librewolf", "/opt/librewolf/librewolf")
     if os.path.isfile(c)), "/usr/bin/librewolf")

EXTDIR = LAB / "data" / "ext"
EXTLOG = EXTDIR / "ext.log"          # legacy, ever-growing, human tails this
SINK_PORT = 8779

# Manifest hashes name files on disk, and they arrive over the wire, so they get
# treated as untrusted: anything outside this set is stripped before it becomes
# a path.
UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# The video element is read straight from the page; everything else arrives
# over the log sink, so nothing here depends on the content script.
# Field names match the `player` event in ext/EVENTS.md so the sample can be
# written to events.jsonl as-is.
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


def probe(cmd):
    """Version string for a binary, or None. Never fatal, never slow."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception:
        return None


class Session:
    """One run's output directory.

    Every writer goes through one lock. The sink is a ThreadingHTTPServer, so
    two POSTs can land at once, and the main loop writes `player` events from a
    third thread — without the lock two half-lines interleave in events.jsonl
    and the file stops being parseable at exactly the moment it matters.
    """

    def __init__(self, channel, level, seconds):
        self.dir = EXTDIR / f"{channel}.{time.strftime('%y%m%d-%H%M%S')}"
        self.mandir = self.dir / "manifests"
        self.mandir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.closed = False
        self.evf = open(self.dir / "events.jsonl", "a", buffering=1,
                        encoding="utf-8")
        self.prose = open(self.dir / "ext.log", "a", buffering=1,
                          encoding="utf-8")
        self.legacy = open(EXTLOG, "a", buffering=1, encoding="utf-8")
        self.seen = {p.name[:-5] for p in self.mandir.glob("*.m3u8")}
        self.counts = {"posts": 0, "lines": 0, "events": 0, "manifests": 0,
                       "dropped": 0}
        self.meta = {
            "channel": channel,
            "level": level,
            "seconds": seconds,
            "argv": sys.argv,
            "dir": str(self.dir),
            "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "started_epoch": time.time(),
            "ended": None,
            "binary": BINARY,
            "geckodriver": probe(["/usr/bin/geckodriver", "--version"]),
            "librewolf": probe([BINARY, "--version"]),
            "counts": self.counts,
            "stats": {},
        }
        header = (f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} {channel} "
                  f"level={level} -> {self.dir} =====")
        with self.lock:
            self.prose.write(header + "\n")
            self.legacy.write("\n" + header + "\n")
        self.write_meta()

    # -- writers ---------------------------------------------------------
    def lines(self, lines):
        """Prose, to this run's log and to the legacy top-level one."""
        stamp = time.strftime("%H:%M:%S")
        blob = "".join(f"{stamp} {l}\n" for l in lines)
        if not blob:
            return
        with self.lock:
            if self.closed:
                return
            self.counts["lines"] += len(lines)
            for f in (self.prose, self.legacy):
                try:
                    f.write(blob)
                    f.flush()
                except Exception:
                    pass

    def events(self, events):
        """Structured stream. One compact object per line, flushed at once —
        a run ends in Ctrl-C and the tail has to survive it."""
        out = []
        drops = 0
        for e in events:
            if not isinstance(e, dict):
                drops += 1
                continue
            if "t" not in e:
                e = dict(e, t=int(time.time() * 1000))
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

    def event(self, name, lvl, fields):
        """Emit one event of our own (tryout.py owns `player`)."""
        e = {"t": int(time.time() * 1000), "ev": name, "lvl": lvl}
        e.update({k: v for k, v in fields.items() if v is not None})
        self.events([e])

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
            # lstrip(".") as well as the class: a leading dot would make the
            # body a hidden file, and `..` a directory walk
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

    def write_meta(self, **extra):
        self.meta.update(extra)
        try:
            (self.dir / "meta.json").write_text(
                json.dumps(self.meta, indent=2, default=str) + "\n")
        except Exception:
            pass

    def close(self, **extra):
        self.write_meta(ended=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        ended_epoch=time.time(), **extra)
        with self.lock:
            self.closed = True
            for f in (self.evf, self.prose, self.legacy):
                try:
                    f.close()
                except Exception:
                    pass


def start_sink(sess, level, port=SINK_PORT):
    """Receive what the extension POSTs us: {lines, events, manifests, stats},
    every field optional (ext/EVENTS.md). Returns the shared state.

    The POST response carries the log level back, which is how `run.sh <chan>
    <secs> trace` reaches the extension — it cannot see our argv. Level filters
    prose only; events and manifests are recorded whatever it is set to.
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
            b = json.dumps({"level": level}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return shared


def main():
    channel = sys.argv[1] if len(sys.argv) > 1 else "gaules"
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0
    level = sys.argv[3] if len(sys.argv) > 3 else "debug"
    if level not in ("error", "warn", "info", "debug", "trace"):
        sys.exit(f"bad log level {level!r}")

    opts = Options()
    opts.binary_location = BINARY
    # LibreWolf ships hardened; undo only what stops a video from starting
    opts.set_preference("media.autoplay.default", 0)
    opts.set_preference("media.autoplay.blocking_policy", 0)
    opts.set_preference("privacy.resistFingerprinting", False)
    opts.set_preference("privacy.query_stripping.enabled", False)
    opts.set_preference("network.cookie.cookieBehavior", 0)
    opts.set_preference("extensions.webextensions.remote", False)

    svc = Service(executable_path="/usr/bin/geckodriver",
                  log_output=str(LOGS / "geckodriver.ext.log"),
                  service_args=["--profile-root", str(SCRATCH)])

    EXTDIR.mkdir(parents=True, exist_ok=True)
    sess = Session(channel, level, seconds)
    shared = start_sink(sess, level)
    print(f"[+] session {sess.dir}")
    print(f"[+] log sink on 127.0.0.1:{SINK_PORT} -> {sess.dir}/ext.log "
          f"(+ {EXTLOG})  (level={level})")

    state = {"last": None, "played": 0.0, "stalls": 0, "elapsed": 0.0}
    drv = None
    try:
        drv = webdriver.Firefox(options=opts, service=svc)
        caps = getattr(drv, "capabilities", None) or {}
        sess.write_meta(browser=caps.get("browserVersion"),
                        geckodriver_caps=caps.get("moz:geckodriverVersion"),
                        platform=caps.get("platformName"))
        drv.install_addon(str(EXT), temporary=True)
        print(f"[+] extension loaded from {EXT}")
        drv.get(f"https://www.twitch.tv/{channel}")
        print(f"[*] https://www.twitch.tv/{channel} "
              f"{'until Ctrl-C' if not seconds else f'for {seconds:.0f}s'}")
        print("[*] watch on_grid: while it is 0 the pool has no clean donor "
              "and you get the real (ad-bearing) playlist\n")

        started = time.time()
        end = started + seconds if seconds else None
        prev_t = None
        while end is None or time.time() < end:
            time.sleep(5)
            state["elapsed"] = time.time() - started
            try:
                v = drv.execute_script(JS_STATE)
            except Exception as e:
                msg = f"[!] page read failed: {e!r}"
                print(msg, flush=True)
                sess.lines([msg])
                continue
            s = shared["stats"]
            if not shared["hits"]:
                print("[!] extension has not reached the log sink yet", flush=True)
            if v:
                stalled = False
                if prev_t is not None:
                    d = v["vt"] - prev_t
                    state["played"] += max(0.0, d)
                    if d <= 0.1 and not v["paused"]:
                        state["stalls"] += 1
                        stalled = True
                prev_t = v["vt"]
                # the same sample twice: prose for the human, `player` for the
                # reporter, which needs playback on the same timeline as the
                # ad breaks to say whether one ever reached the screen
                sess.event("player", "warn" if stalled or v["err"] else "info",
                           {k: v.get(k) for k in PLAYER_FIELDS})
                line = (f"[.] video t={v['vt']:7.1f} {v['w']}x{v['h']} "
                        f"paused={v['paused']} ready={v['ready']} "
                        f"net={v['net']} ahead={v['ahead']:.1f}s")
                if stalled or v["err"] or v["ready"] < 3:
                    # the interesting case — record enough to tell a decoder
                    # stall from an empty buffer from a hard media error
                    rng = ",".join(f"{a:.1f}-{b:.1f}" for a, b in v["ranges"])
                    line += (f"  << STALL ranges=[{rng}] "
                             f"err={v['err']} stalls={state['stalls']}")
            else:
                line = "[.] no video element on the page yet"
            print(line, flush=True)
            sess.lines([line])
            state["last"] = s
    except KeyboardInterrupt:
        print("\n[*] interrupted")
    finally:
        try:
            if drv:
                drv.quit()
        except Exception:
            pass
        state["last"] = shared["stats"] or state["last"]
        sess.close(stats=shared["stats"],
                   elapsed=round(state["elapsed"], 1),
                   played=round(state["played"], 1),
                   stalls=state["stalls"])
    return report(state, sess)


def report(st, sess):
    last, played = st["last"] or {}, st["played"]
    print()
    print("=== result ===")
    print(f"  wall clock          {st['elapsed']:.0f}s")
    print(f"  video advanced      {played:.1f}s")
    print(f"  stalled samples     {st['stalls']}")
    for k in ("channel", "rendition", "sawMaster", "sawMedia", "unknownVariant",
              "skippedOwn", "served", "passthru", "adSegsSeen",
              "segs", "onGrid", "arms", "skew", "regrid", "lastError"):
        print(f"  {k:20s}{last.get(k)}")
    c = sess.counts
    print(f"  events              {c['events']} ({c['dropped']} dropped)")
    print(f"  manifests           {c['manifests']}")
    print(f"  sink posts          {c['posts']}")
    print()
    print(f"  session   {sess.dir}")
    print(f"  read it   python ext/extreport.py {sess.dir}")
    good = last.get("served", 0) > 0 and played > st["elapsed"] * 0.5
    print()
    print("EXTENSION SERVED THE STREAM" if good else "DID NOT TAKE OVER")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
