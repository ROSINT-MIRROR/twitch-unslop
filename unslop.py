#!/usr/bin/env python3
"""
Ad-free Twitch playback POC.

Runs M independent Twitch sessions on one channel, merges their media
playlists into a single clean one, and serves it on localhost. Segments
whose EXTINF title isn't "live" (i.e. stitched ads) are never emitted —
their slot is filled from whichever session is still clean.

  python unslop.py <channel> [--port 8778] [--arms 3]
  mpv http://127.0.0.1:8778/playlist.m3u8

We serve only the ~2KB manifest. The player fetches segment bytes straight
from Twitch's CDN, so this costs no video bandwidth.

Splice key is #EXT-X-PROGRAM-DATE-TIME, NOT #EXT-X-TWITCH-LIVE-SEQUENCE.
Measured 2026-07-29 against real captured breaks (data/hunt/*/manifests/AD.*):

  tag                        clean playlist   during an ad
  PROGRAM-DATE-TIME          1 per segment    1 per segment
  EXT-X-TWITCH-LIVE-SEQUENCE present          ABSENT
  EXT-X-MEDIA-SEQUENCE       live-aligned     session-local, restarts

So LIVE-SEQUENCE cannot align an ad session against a clean one — it isn't
there when you need it. PDT is wall-clock and present in both. Independent
sessions that have not taken an ad emit byte-identical PDTs for the same
broadcast moment (measured across 3 concurrent sessions: min |dPDT| = 0), so
clean segments dedupe on the exact PDT with no tolerance.

A stitched ad REPLACES content in wall-clock time rather than delaying it, so
a clean parallel session holds the segments the ad session missed. But it does
NOT hold them on the same grid, which is the part that took a rig to find:

  content segments   4.166 / 4.167s  (250 frames at 60fps)
  ad segments        2.000s exactly
  last ad segment    TRIMMED — measured 1.235s on a 15.235s pod

A pod is not a whole number of content segments, so Twitch trims the final ad
segment and re-cuts content from wherever the pod ended. An arm that has taken
an ad is therefore permanently offset from one that hasn't (measured: 1.43s).
Unioning both by PDT emits segments starting 1.43s apart that each declare
4.167s — overlapping video. chain() drops anything starting before the
previous segment ends and counts it as `skew`.

There is no fixed cadence to quantize to. Every alignment decision uses the
segment's own declared EXTINF duration.

Every first observation of a segment by an arm is written to a ledger
(data/unslop/*.jsonl). Nothing is scored live; run coverage.py over it. The
number that decides whether this is worth porting to an extension is the
fraction of ad wall-clock that some other arm still held clean.
"""
import argparse
import calendar
import json
import pathlib
import random
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LAB = pathlib.Path(__file__).resolve().parent
CID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
PQ = "0828119ded1c13477966434e15800ff57ddacf13ba1911c129dc2200705b0712"
UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
      "Gecko/20100101 Firefox/128.0")

WINDOW = 12          # segments advertised to the player
POLL = 2.0


# PDT immediately precedes its EXTINF/url pair in both clean and ad playlists
RE_PDTSEG = re.compile(
    r"#EXT-X-PROGRAM-DATE-TIME:(\S+)\s*\n(?:#EXT-X-[^\n]*\n)*?"
    r"#EXTINF:([\d.]+),([^\r\n]*)\r?\n(\S+)", re.M)
RE_TARGET = re.compile(r"#EXT-X-TARGETDURATION:(\d+)")
RE_SOURCE = re.compile(r'X-TV-TWITCH-STREAM-SOURCE="([^"]*)"')

# There is no fixed segment cadence. Measured 2026-07-29 on gaules: content
# runs 4.166/4.167s (250 frames at 60fps) while stitched ads are exactly
# 2.000s. Assuming one grid for both put an EXT-X-DISCONTINUITY between every
# pair of segments on a completely clean stream. Every alignment decision below
# uses the segment's own declared EXTINF duration instead.
GAP_MS = 500         # slack before a PDT discontinuity counts as a real hole
REGRID_COOLDOWN = 30 # min seconds between an arm's post-ad rejoins


def pdt_ms(s):
    """2026-07-28T23:22:06.792Z -> epoch ms."""
    t = time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    frac = 0.0
    if "." in s:
        frac = float("0." + re.split(r"[.]", s)[1].rstrip("Z")[:3])
    return int((calendar.timegm(t) + frac) * 1000)

_ctx = ssl.create_default_context()
_stop = threading.Event()


def http(url, data=None, headers=None, timeout=15):
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("User-Agent", UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
        return r.read()


def dev_id():
    return "".join(random.choice("0123456789abcdef") for _ in range(32))


def mint(channel, device, player_type="site"):
    body = json.dumps({
        "operationName": "PlaybackAccessToken",
        "variables": {"isLive": True, "login": channel, "isVod": False,
                      "vodID": "", "playerType": player_type},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": PQ}},
    }).encode()
    d = json.loads(http("https://gql.twitch.tv/gql", data=body, headers={
        "Client-ID": CID, "Content-Type": "application/json",
        "X-Device-Id": device, "Device-ID": device}))
    node = (d.get("data") or {}).get("streamPlaybackAccessToken")
    if not node:
        raise RuntimeError(f"{channel}: no token (offline?)")
    return node


class QualityMissing(RuntimeError):
    """The requested rendition is absent from this token's ladder.

    Fatal for the arm, not retryable — the ladder follows from player_type,
    so retrying just mints forever. Never silently substitute a rendition.
    """


RE_GROUP = re.compile(r'GROUP-ID="([^"]*)"')
RE_NAME = re.compile(r'NAME="([^"]*)"')
RE_RES = re.compile(r"RESOLUTION=(\S+?)[,\s]")


def ladder(channel, node):
    """[(group_id, name, resolution, url)] from the master playlist."""
    q = urllib.parse.urlencode({
        "client_id": CID, "token": node["value"], "sig": node["signature"],
        "allow_source": "true", "allow_audio_only": "true", "fast_bread": "true",
        "player_backend": "mediaplayer", "supported_codecs": "h264",
        "p": random.randint(1, 9_999_999)})
    txt = http(
        f"https://usher.ttvnw.net/api/channel/hls/{channel}.m3u8?{q}"
    ).decode("utf-8", "replace")
    lines = txt.splitlines()
    out, gid, name = [], None, None
    for i, l in enumerate(lines):
        if l.startswith("#EXT-X-MEDIA:") and "TYPE=VIDEO" in l:
            m, n = RE_GROUP.search(l), RE_NAME.search(l)
            gid = m.group(1) if m else None
            name = n.group(1) if n else None
        elif l.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
            u = lines[i + 1].strip()
            if u.startswith("http"):
                r = RE_RES.search(l + ",")
                out.append((gid, name, r.group(1) if r else "audio", u))
            gid = name = None
    return out


def variant_url(channel, node, want):
    """Exact rendition or nothing.

    This used to fall through to the FIRST variant in the master when `want`
    didn't match, which is silent corruption: measured 2026-07-29 on gaules,
    usher orders the ladder differently per player_type (site leads with
    1080p60 source, frontpage with 160p30, thunderdome with 480p30) and
    thunderdome's ladder carries no 720p60 at all. The documented 4-arm
    command would therefore have donated 852x480 segments into a 1280x720
    playlist with nothing to show for it in any log.
    """
    lad = ladder(channel, node)
    if not lad:
        raise RuntimeError("no variant in master")
    for gid, name, res, url in lad:
        if want in (gid, name):
            return url, gid or name, res
    have = ", ".join(f"{g or n}" for g, n, _, _ in lad)
    raise QualityMissing(f"quality {want!r} not in this ladder — have: {have}")


class Store:
    """Clean segments we can serve, plus a raw ledger of what every arm saw.

    Independent sessions emit byte-identical PDTs for the same broadcast
    moment (measured across 3 concurrent sessions: min |dPDT| = 0), so clean
    segments dedupe on the exact PDT with no tolerance. Ads sit on a different
    grid — 2.000s against 4.167s content — which is why alignment is done as
    interval overlap in coverage.py rather than by bucketing here.

    Nothing is scored live. Every first observation of a segment by an arm is
    written out; coverage.py does the interval math offline.
    """

    def __init__(self, ledger=None):
        self.seg = {}            # pdt_ms -> (dur_s, url, iso), clean only
        self.seen = set()        # (arm, pdt_ms) already in the ledger
        self.win = []            # pdt keys currently advertised
        self.mseq = 0            # media sequence of win[0]
        self.newest = 0
        self.target = 6
        self.lock = threading.Lock()
        self.ledger = open(ledger, "a", buffering=1) if ledger else None
        self.rendition = None    # every servable segment must be this one
        self.skewed = set()      # pdt keys chain() could not splice
        self.stats = {"segs": 0, "ad_obs": 0, "polls": 0, "reobs": 0,
                      "skew": 0, "rend_drop": 0, "regrid": 0}

    def claim_rendition(self, arm, rend):
        """First arm to join fixes the rendition for the whole run.

        Segments of two different renditions have identical PDTs, so they
        splice together silently and play as a resolution flip mid-stream.
        A mismatched arm still writes to the ledger — its ad/clean timing is
        valid evidence for coverage.py — it just never donates bytes.
        """
        with self.lock:
            if self.rendition is None:
                self.rendition = rend
            ok = rend == self.rendition
        if not ok:
            print(f"[!] {arm}: rendition {rend!r} != {self.rendition!r} — "
                  f"this arm will log but not donate", file=sys.stderr)
            self.event(ev="rendition_mismatch", arm=arm, got=rend,
                       want=self.rendition)
        return ok

    def offer(self, ms, dur, url, iso, is_ad, arm, rend):
        with self.lock:
            if (arm, ms) in self.seen:
                self.stats["reobs"] += 1
                return
            self.seen.add((arm, ms))
            self.newest = max(self.newest, ms)
            if self.ledger:
                self.ledger.write(json.dumps({
                    "ev": "seg", "arm": arm, "pdt": ms, "iso": iso,
                    "dur": dur, "ad": is_ad, "rend": rend,
                    "t": round(time.time(), 3)}) + "\n")
            if is_ad:
                self.stats["ad_obs"] += 1
                return
            if rend != self.rendition:
                self.stats["rend_drop"] += 1
                return
            if ms not in self.seg:
                self.seg[ms] = (dur, url, iso)
                self.stats["segs"] += 1
            if len(self.seg) > 400:
                for k in sorted(self.seg)[:-200]:
                    del self.seg[k]
                cut = min(self.seg)
                self.skewed = {k for k in self.skewed if k >= cut}
            if len(self.seen) > 20000:
                cut = self.newest - 600_000
                self.seen = {(a, m) for a, m in self.seen if m >= cut}

    def event(self, **kw):
        if self.ledger:
            with self.lock:
                self.ledger.write(json.dumps({"t": round(time.time(), 3),
                                              **kw}) + "\n")

    def chain(self):
        """Walk BACK from the newest segment, emitting a non-overlapping timeline.

        An arm that has taken an ad comes back re-cut: measured 2026-07-29, a
        15.235s pod ended with a TRIMMED 1.235s ad segment and content resumed
        1.43s off the grid every other arm was on. Its segments are perfectly
        valid video but they overlap ours, so unioning them by PDT emits
        segments that start 1.4s apart while each declares 4.167s.

        The walk runs newest-first because the window we serve is the tail.
        Walking forward from the oldest key let one off-phase segment ~14
        minutes back pick the phase for everything after it, and the store
        outlives any single break; anchored on the tail, a bad phase costs at
        most the current window and the next segment corrects it.

        Anything overlapping the segment after it is dropped. It is counted,
        not hidden — skew near the segment count means donors are arriving on
        a phase we cannot splice onto, and coverage.py's number (which unions
        intervals regardless of phase) is then only an upper bound.
        """
        out, nxt = [], None
        for k in sorted(self.seg, reverse=True):
            if nxt is not None and k + int(self.seg[k][0] * 1000) > nxt + GAP_MS:
                self.skewed.add(k)
                continue
            out.append(k)
            nxt = k
            if len(out) >= WINDOW:
                break
        self.stats["skew"] = len(self.skewed)
        out.reverse()
        return out

    def tick(self):
        """Exercise the chain so `skew` is real even with no player attached."""
        with self.lock:
            self.chain()

    def playlist(self):
        with self.lock:
            keys = self.chain()
            if not keys:
                return None
            # HLS semantics: MEDIA-SEQUENCE is the index of the first segment
            # advertised, so it advances by however many slid out the front.
            # Deriving it from the PDT instead made it jump by 2 per segment.
            self.mseq += sum(1 for k in self.win if k < keys[0])
            self.win = keys
            out = ["#EXTM3U", "#EXT-X-VERSION:3",
                   f"#EXT-X-TARGETDURATION:{self.target}",
                   f"#EXT-X-MEDIA-SEQUENCE:{self.mseq}"]
            end = None
            for k in keys:
                dur, url, iso = self.seg[k]
                # a real hole: this segment doesn't start where the last one
                # ended. Declared durations are exact, so only a genuine gap
                # gets a discontinuity — not every segment boundary.
                if end is not None and k - end > GAP_MS:
                    out.append("#EXT-X-DISCONTINUITY")
                out.append(f"#EXT-X-PROGRAM-DATE-TIME:{iso}")
                out.append(f"#EXTINF:{dur:.3f},live")
                out.append(url)
                end = k + int(dur * 1000)
            return "\n".join(out) + "\n"


class Arm(threading.Thread):
    def __init__(self, name, channel, store, quality, player_type="site",
                 regrid=True):
        super().__init__(daemon=True, name=name)
        self.n = name
        self.channel = channel
        self.store = store
        self.quality = quality
        self.player_type = player_type
        self.regrid = regrid
        self.url = None
        self.in_ad = False
        self.rend = None
        self.donates = True
        self.dirty = False       # this session has taken an ad
        self.last_regrid = 0.0
        self.fails = 0

    def join_session(self):
        dev = dev_id()
        node = mint(self.channel, dev, self.player_type)
        self.url, self.rend, res = variant_url(self.channel, node, self.quality)
        self.donates = self.store.claim_rendition(self.n, self.rend)
        self.store.event(ev="join", arm=self.n, pt=self.player_type, dev=dev,
                         rend=self.rend, res=res, donates=self.donates)

    def retire_session(self):
        """Drop a session that has taken an ad — it is off-grid for good.

        Measured 2026-07-29 on gaules, 5 concurrent arms: the three that never
        took an ad emitted byte-identical PDTs (89/89 segments on one grid).
        The two that took a join preroll came back 1431ms off that grid and
        stayed there for every one of the 39 clean segments that followed —
        0/39 back on grid, no re-convergence. chain() cannot splice them, so
        after an ad an arm is dead weight as a donor no matter how clean its
        video is. A fresh mint lands back on the canonical grid.

        The cooldown matters: prerolls fire on join, so a rejoin can draw a new
        ad and this would otherwise become a mint loop.
        """
        now = time.time()
        if now - self.last_regrid < REGRID_COOLDOWN:
            return
        self.last_regrid = now
        self.store.stats["regrid"] += 1
        self.store.event(ev="regrid", arm=self.n)
        self.url = None
        self.dirty = False

    def run(self):
        while not _stop.is_set():
            try:
                if self.url is None:
                    self.join_session()
                body = http(self.url, timeout=12).decode("utf-8", "replace")
                self.store.stats["polls"] += 1
                self.fails = 0

                m = RE_TARGET.search(body)
                if m:
                    self.store.target = max(self.store.target, int(m.group(1)))

                src = RE_SOURCE.search(body)
                saw_ad = bool(src and src.group(1) != "live")

                for iso, dur, title, url in RE_PDTSEG.findall(body):
                    is_ad = title.strip() != "live"
                    saw_ad |= is_ad
                    try:
                        ms, d = pdt_ms(iso), float(dur)
                    except Exception:
                        continue
                    self.store.offer(ms, d, url, iso, is_ad, self.n, self.rend)

                self.in_ad = saw_ad
                if saw_ad:
                    self.dirty = True
                elif self.dirty and self.regrid:
                    self.retire_session()

                _stop.wait(POLL + random.random() * 0.3)
            except QualityMissing as e:
                print(f"[!] {self.n}: {e} — arm stopped", file=sys.stderr)
                self.store.event(ev="arm_dead", arm=self.n, msg=str(e))
                return
            except urllib.error.HTTPError as e:
                # a dead session means this arm stops donating until it rejoins
                self.store.event(ev="rejoin", arm=self.n, code=e.code)
                self.url = None
                _stop.wait(2)
            except Exception as e:
                # Back off. The usual cause over a long run is the channel
                # going offline, and mint() raises for every arm on every
                # retry — at a flat 3s that is 2 GraphQL calls a second for as
                # long as the stream is down.
                self.fails += 1
                wait = min(60, 3 * 2 ** min(self.fails - 1, 5))
                print(f"[{self.n}] {e!r} (retry in {wait}s)", file=sys.stderr)
                self.store.event(ev="err", arm=self.n, msg=repr(e), wait=wait)
                self.url = None
                _stop.wait(wait)


def serve(store, port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/stats"):
                b = json.dumps(store.stats).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            pl = store.playlist()
            if pl is None:
                self.send_error(503, "warming up")
                return
            b = pl.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(b)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("channel")
    ap.add_argument("--port", type=int, default=8778)
    ap.add_argument("--arms", type=int, default=3)
    ap.add_argument("--quality", default="720p60")
    ap.add_argument("--types", default="site",
                    help="comma list of player_types, assigned round-robin. "
                         "Diversify these: arms that differ only by device_id "
                         "may all take the same break, which is a hole.")
    ap.add_argument("--log", default=None,
                    help="slot ledger (default data/unslop/<chan>.<ts>.jsonl, "
                         "'-' to disable)")
    ap.add_argument("--seconds", type=float, default=0, help="0 = forever")
    ap.add_argument("--no-regrid", action="store_true",
                    help="keep sessions after they take an ad. They come back "
                         "~1.43s off the grid their ad-free peers share and "
                         "never re-converge, so they stop being spliceable "
                         "donors — this measures that naive case.")
    args = ap.parse_args()

    # hard rule: never look like a load test. hunt.py alone runs 12.
    if args.arms > 8:
        sys.exit("refusing >8 arms — poller budget is ~16 across all tools")

    if args.log == "-":
        ledger = None
    elif args.log:
        ledger = pathlib.Path(args.log)
    else:
        ledger = (LAB / "data" / "unslop" /
                  f"{args.channel}.{time.strftime('%m%d-%H%M%S')}.jsonl")
    if ledger:
        ledger.parent.mkdir(parents=True, exist_ok=True)

    types = [t.strip() for t in args.types.split(",") if t.strip()]

    # Preflight. usher hands each player_type a DIFFERENT ladder (measured
    # 2026-07-29 on gaules: thunderdome tops out at 480p30, no 720p60 at all),
    # so a type/quality combo can be unsatisfiable. Find that out now, in one
    # mint per type, rather than after a 24h run produced mixed renditions.
    print(f"[.] preflight {args.channel} @ {args.quality}")
    bad = []
    for pt in types:
        try:
            lad = ladder(args.channel, mint(args.channel, dev_id(), pt))
        except Exception as e:
            sys.exit(f"[-] {pt}: {e}")
        hit = [r for g, n, r, _ in lad if args.quality in (g, n)]
        names = ",".join(g or n for g, n, _, _ in lad)
        print(f"    {pt:12s} {'OK ' + hit[0] if hit else 'MISSING'}  [{names}]")
        if not hit:
            bad.append(pt)
    if bad:
        sys.exit(f"[-] {args.quality} missing from: {', '.join(bad)}. "
                 f"Pick a quality every type carries, or drop those types — "
                 f"substituting one silently splices two renditions.")

    store = Store(ledger)
    arms = []
    for i in range(args.arms):
        pt = types[i % len(types)]
        arms.append(Arm(f"arm{i}({pt})", args.channel, store, args.quality, pt,
                        regrid=not args.no_regrid))
    store.event(ev="start", channel=args.channel, quality=args.quality,
                regrid=not args.no_regrid, arms=[a.n for a in arms])
    for a in arms:
        a.start()
        time.sleep(0.5)

    serve(store, args.port)
    url = f"http://127.0.0.1:{args.port}/playlist.m3u8"
    print(f"[+] {args.channel}  {args.arms} arms  {types}")
    print(f"[+] {url}")
    print(f"[+] mpv {url}")
    print(f"[+] ledger {ledger or 'off'}")

    end = time.time() + args.seconds if args.seconds else None
    try:
        while not _stop.is_set():
            time.sleep(5)
            store.tick()
            s = store.stats
            print(f"[.] segs={s['segs']} ad_obs={s['ad_obs']} "
                  f"reobs={s['reobs']} skew={s['skew']} "
                  f"rend_drop={s['rend_drop']} regrid={s['regrid']} "
                  f"polls={s['polls']} "
                  f"store={len(store.seg)} mseq={store.mseq}", flush=True)
            if end and time.time() > end:
                break
    except KeyboardInterrupt:
        pass
    _stop.set()
    store.event(ev="stop", **store.stats)


if __name__ == "__main__":
    main()
