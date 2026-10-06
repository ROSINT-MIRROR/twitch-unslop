#!/usr/bin/env python3
"""
Headless multi-arm ad hunter. No browser, no mitmproxy — we ARE the client.

Two arm families, because the two ad types are caught differently:

  REJOINER  mint a fresh session, poll ~45s, throw it away, mint again.
            Every rejoin is a fresh PREROLL roll. This is what actually
            catches ads overnight.
  WATCHER   mint once, poll forever. Only way to see a MIDROLL, which
            requires the broadcaster to trigger a break.

Crossed with the identity axis (sticky vs fresh device_id) and the
player_type axis, so the dump answers the capping question directly.

  python hunt.py [hours]        default 6.5
"""
import itertools
import json
import os
import pathlib
import random
import re
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

LAB = pathlib.Path(__file__).resolve().parent
SESSION = os.environ.get("HUNT_SESSION") or time.strftime("%m%d-%H%M%S")
OUT = LAB / "data" / "hunt" / SESSION
MAN = OUT / "manifests"
SEG = OUT / "segments"
for d in (OUT, MAN, SEG):
    d.mkdir(parents=True, exist_ok=True)

LOG = OUT / "hunt.jsonl"
CID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
PQ = "0828119ded1c13477966434e15800ff57ddacf13ba1911c129dc2200705b0712"
UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
      "Gecko/20100101 Firefox/128.0")

POLL = 2.0          # seconds between media playlist polls (matches a real player)
REJOIN_AFTER = 45   # seconds a rejoiner keeps a session before re-minting
MAX_ARMS = 16       # hard cap: never look like a load test

# Big channels likely live across an EEST night (= NA prime time).
POOL = [
    "xqc", "jynxzi", "caseoh_", "hasanabi", "quin69", "zackrawrr",
    "summit1g", "shroud", "tarik", "lirik", "sodapoppin", "moistcr1tikal",
    "kaicenat", "jasontheween", "stableronaldo", "plaqueboymax",
    "loltyler1", "esl_csgo", "blastpremier", "gaules", "alanzoka",
    "paulinholokobr", "cellbit", "elded", "illojuan", "ibai",
    "papaplatte", "trymacs", "montanablack88", "nickeh30", "pestily",
]

_lock = threading.Lock()
_stop = threading.Event()
_stats = {"polls": 0, "mints": 0, "ads": 0, "errors": 0}
_ctx = ssl.create_default_context()

RE_SEG = re.compile(r"^#EXTINF:([\d.]+),([^\r\n]*)\r?\n(\S+)", re.M)
RE_SOURCE = re.compile(r'X-TV-TWITCH-STREAM-SOURCE="([^"]*)"')
RE_DATERANGE = re.compile(r"^#EXT-X-DATERANGE:(.*)$", re.M)
RE_ATTR = re.compile(r'([A-Z0-9-]+)=("(?:[^"]*)"|[^,]*)')


def log(rec):
    rec["t"] = time.time()
    with _lock:
        with LOG.open("a") as f:
            f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")


def bump(k, n=1):
    with _lock:
        _stats[k] = _stats.get(k, 0) + n


def dev_id():
    return "".join(random.choice("0123456789abcdef") for _ in range(32))


def http(url, data=None, headers=None, timeout=15):
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("User-Agent", UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
        return r.status, r.read()


def mint(channel, device, player_type):
    body = json.dumps({
        "operationName": "PlaybackAccessToken",
        "variables": {"isLive": True, "login": channel, "isVod": False,
                      "vodID": "", "playerType": player_type},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": PQ}},
    }).encode()
    st, raw = http("https://gql.twitch.tv/gql", data=body, headers={
        "Client-ID": CID, "Content-Type": "application/json",
        "X-Device-Id": device, "Device-ID": device,
    })
    d = json.loads(raw)
    node = (d.get("data") or {}).get("streamPlaybackAccessToken")
    if not node:
        return None
    bump("mints")
    tok = json.loads(node["value"])
    return {"value": node["value"], "sig": node["signature"], "token": tok}


def master(channel, m):
    q = urllib.parse.urlencode({
        "client_id": CID, "token": m["value"], "sig": m["sig"],
        "allow_source": "true", "allow_audio_only": "true",
        "fast_bread": "true", "player_backend": "mediaplayer",
        "playlist_include_framerate": "true", "supported_codecs": "h264",
        "p": random.randint(1, 9_999_999),
    })
    st, raw = http(f"https://usher.ttvnw.net/api/channel/hls/{channel}.m3u8?{q}")
    return raw.decode("utf-8", "replace")


class QualityMissing(RuntimeError):
    """The requested rendition is absent from this token's ladder.

    Fatal for the arm, not retryable — the ladder follows from player_type,
    so retrying just mints forever. Never silently substitute a rendition:
    usher orders the ladder differently per player_type and some (thunderdome)
    carry no 720p60 at all, so falling through to "the first variant" donates
    the wrong resolution with nothing in any log to show for it.
    """


def pick_variant(txt, want="720p60"):
    """Exact rendition or nothing — mirrors unslop.py's variant_url().

    A mid rendition catches ads identically at 1/6th the bandwidth, but it must
    be THE requested one: match on the EXT-X-MEDIA group/name or the STREAM-INF
    line, and fail loudly when it is absent rather than grabbing an arbitrary
    (possibly audio-only) variant.
    """
    lines = txt.splitlines()
    have = []
    for i, l in enumerate(lines):
        if l.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
            url = lines[i + 1].strip()
            if not url.startswith("http"):
                continue
            label = l + " " + (lines[i - 1] if i else "")
            have.append(label)
            if want in label:
                return url
    tags = ", ".join(sorted({t for lab in have
                             for t in RE_RES.findall(lab)})) or "none"
    raise QualityMissing(f"quality {want!r} not in this ladder — have: {tags}")


RE_RES = re.compile(r'(?:RESOLUTION=|GROUP-ID="|NAME=")([^",\s]+)')


def attrs(line):
    return {k: v.strip('"') for k, v in RE_ATTR.findall(line)}


def analyse(body):
    """The three independent ad tells."""
    segs = RE_SEG.findall(body)
    titles = sorted({t.strip() for _, t, _ in segs if t.strip()})
    src = RE_SOURCE.search(body)
    src = src.group(1) if src else None
    ads = []
    for line in RE_DATERANGE.findall(body):
        a = attrs(line)
        if "twitch-stitched-ad" in (a.get("CLASS") or "") or any(
                x.startswith("X-TV-TWITCH-AD") for x in a):
            ads.append(a)
    is_ad = bool(ads) or (src and src != "live") or any(t != "live" for t in titles)
    return {
        "segs": segs, "titles": titles, "src": src, "ads": ads, "is_ad": is_ad,
        "media_seq": (re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", body) or [None, None])[1]
        if re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", body) else None,
        "live_seq": (re.search(r"#EXT-X-TWITCH-LIVE-SEQUENCE:(\d+)", body).group(1)
                     if re.search(r"#EXT-X-TWITCH-LIVE-SEQUENCE:(\d+)", body) else None),
        "disc": body.count("#EXT-X-DISCONTINUITY"),
    }


def dump_break(arm, channel, body, a):
    """An ad fired. Save everything: manifest + the actual ad bytes."""
    bump("ads")
    stamp = f"{int(time.time()*1000)}.{arm}.{channel}"
    (MAN / f"AD.{stamp}.m3u8").write_text(body)
    log({"ev": "AD_BREAK", "arm": arm, "channel": channel,
         "src": a["src"], "titles": a["titles"], "ads": a["ads"],
         "media_seq": a["media_seq"], "live_seq": a["live_seq"],
         "disc": a["disc"], "manifest": f"AD.{stamp}.m3u8"})
    saved = 0
    for dur, title, url in a["segs"]:
        if saved >= 12:
            break
        try:
            st, raw = http(url, timeout=25)
            if raw:
                tag = "AD" if title.strip() != "live" else "live"
                (SEG / f"{stamp}.{saved:02d}.{tag}.ts").write_bytes(raw)
                saved += 1
        except Exception as e:
            log({"ev": "seg_err", "arm": arm, "msg": repr(e)})
    log({"ev": "AD_SEGMENTS_SAVED", "arm": arm, "channel": channel, "n": saved})


class Arm(threading.Thread):
    def __init__(self, name, kind, player_type, sticky, channels):
        super().__init__(daemon=True, name=name)
        self.name_ = name
        self.kind = kind                  # rejoiner | watcher
        self.player_type = player_type
        self.sticky = sticky              # a fixed device_id, or None for fresh
        self.channels = itertools.cycle(channels)
        self.channel = next(self.channels)

    def device(self):
        return self.sticky or dev_id()

    def session(self):
        """Mint -> master -> variant url. None if the channel isn't live."""
        dev = self.device()
        try:
            m = mint(self.channel, dev, self.player_type)
        except Exception as e:
            log({"ev": "mint_err", "arm": self.name_, "channel": self.channel,
                 "msg": repr(e)})
            bump("errors")
            return None
        if not m:
            return None
        try:
            txt = master(self.channel, m)
        except urllib.error.HTTPError as e:
            log({"ev": "offline", "arm": self.name_, "channel": self.channel,
                 "code": e.code})
            return None
        except Exception as e:
            bump("errors")
            log({"ev": "usher_err", "arm": self.name_, "msg": repr(e)})
            return None
        url = pick_variant(txt)
        if not url:
            return None
        tk = m["token"]
        log({"ev": "join", "arm": self.name_, "channel": self.channel,
             "kind": self.kind, "player_type": self.player_type,
             "device_id": dev, "sticky": bool(self.sticky),
             "tok_device_id": tk.get("device_id"), "user_ip": tk.get("user_ip"),
             "geo": tk.get("ci_gb"), "server_ads": tk.get("server_ads"),
             "show_ads": tk.get("show_ads"), "hide_ads": tk.get("hide_ads"),
             "sub": tk.get("subscriber"), "turbo": tk.get("turbo")})
        return url

    def run(self):
        url, started, first = None, 0, True
        while not _stop.is_set():
            try:
                if url is None:
                    self.channel = next(self.channels)
                    url = self.session()
                    started, first = time.time(), True
                    if url is None:
                        _stop.wait(5 + random.random() * 5)
                        continue

                st, raw = http(url, timeout=12)
                body = raw.decode("utf-8", "replace")
                bump("polls")
                a = analyse(body)

                if a["is_ad"]:
                    dump_break(self.name_, self.channel, body, a)
                else:
                    log({"ev": "poll", "arm": self.name_, "channel": self.channel,
                         "kind": self.kind, "pt": self.player_type,
                         "src": a["src"], "media_seq": a["media_seq"],
                         "live_seq": a["live_seq"], "first": first,
                         "n": len(a["segs"])})
                first = False

                # rejoiners throw the session away to buy another preroll roll
                if self.kind == "rejoiner" and time.time() - started > REJOIN_AFTER:
                    url = None
                    _stop.wait(1 + random.random() * 2)
                    continue

                _stop.wait(POLL + random.random() * 0.4)

            except QualityMissing as e:
                # the ladder follows from player_type: retrying mints forever.
                log({"ev": "arm_dead", "arm": self.name_, "msg": str(e)})
                return
            except urllib.error.HTTPError as e:
                # playlist blob expired or channel dropped -> remint
                log({"ev": "poll_http", "arm": self.name_, "code": e.code,
                     "channel": self.channel})
                url = None
                _stop.wait(2)
            except Exception as e:
                bump("errors")
                log({"ev": "poll_err", "arm": self.name_, "msg": repr(e)})
                url = None
                _stop.wait(3)


def main():
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 6.5
    (OUT / "hunt.pid").write_text(str(os.getpid()))

    random.shuffle(POOL)
    a, b, c = POOL[0:8], POOL[8:16], POOL[16:24]
    STICKY = dev_id()   # one identity reused all night -> tests device_id capping

    arms = [
        # rejoiners: the preroll lottery. fresh vs sticky identity, same job.
        Arm("rejoin-fresh-site-1",  "rejoiner", "site",      None,   a),
        Arm("rejoin-fresh-site-2",  "rejoiner", "site",      None,   b),
        Arm("rejoin-stick-site-1",  "rejoiner", "site",      STICKY, a),
        Arm("rejoin-stick-site-2",  "rejoiner", "site",      STICKY, b),
        Arm("rejoin-fresh-embed",   "rejoiner", "embed",     None,   c),
        Arm("rejoin-fresh-frontpg", "rejoiner", "frontpage", None,   c),
        # watchers: the only way to see a broadcaster-triggered midroll
        Arm("watch-fresh-site-1",   "watcher",  "site",      None,   a),
        Arm("watch-fresh-site-2",   "watcher",  "site",      None,   b),
        Arm("watch-stick-site",     "watcher",  "site",      STICKY, c),
        Arm("watch-fresh-embed",    "watcher",  "embed",     None,   a),
        Arm("watch-fresh-thunder",  "watcher",  "thunderdome", None, b),
        Arm("watch-fresh-frontpg",  "watcher",  "frontpage", None,   c),
    ]
    assert len(arms) <= MAX_ARMS, "too many pollers"

    log({"ev": "start", "session": SESSION, "hours": hours,
         "arms": [x.name_ for x in arms], "sticky_device": STICKY,
         "pool": POOL, "poll_interval": POLL, "rejoin_after": REJOIN_AFTER})

    for x in arms:
        x.start()
        time.sleep(0.7)          # stagger so we don't burst

    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: _stop.set())

    end = time.time() + hours * 3600
    last = 0
    while time.time() < end and not _stop.is_set():
        _stop.wait(10)
        if time.time() - last > 300:
            last = time.time()
            log({"ev": "heartbeat", "left_s": int(end - time.time()), **_stats})
    _stop.set()
    time.sleep(3)
    log({"ev": "done", **_stats})


if __name__ == "__main__":
    main()
