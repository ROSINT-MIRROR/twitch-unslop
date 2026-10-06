#!/usr/bin/env python3
"""
Why did the user's browser get a preroll on player_type=site while 100
cookie-less `site` joins from this rig got zero?

Hypothesis: ad eligibility on `site` needs a recognised viewer identity —
the `unique_id` cookie the browser sends — not just an X-Device-Id header.
A bare anonymous mint may simply not be monetisable inventory.

Four arms, rejoin loop, same channel pool, same time window:

  site-nocookie     baseline; reproduces the 0/100 result
  site-cookie       same but sends a stable unique_id cookie like a browser
  site-cookie-fresh new unique_id every rejoin (tests capping vs eligibility)
  embed-nocookie    positive control; this arm is known to get ads

If site-cookie gets ads and site-nocookie doesn't, the cookie is the gate.
If neither does while embed does, player_type is the gate on its own.

  python cookietest.py [minutes]
"""
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
OUT = LAB / "data" / "cookietest"
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / f"ct.{time.strftime('%m%d-%H%M%S')}.jsonl"

CID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
PQ = "0828119ded1c13477966434e15800ff57ddacf13ba1911c129dc2200705b0712"
UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
      "Gecko/20100101 Firefox/128.0")
POLL = 2.0
REJOIN_AFTER = 40

POOL = ["xqc", "jynxzi", "gaules", "trymacs", "alanzoka", "stableronaldo",
        "tarik", "cellbit", "loltyler1", "kaicenat", "zackrawrr", "hasanabi"]

RE_SEG = re.compile(r"^#EXTINF:([\d.]+),([^\r\n]*)\r?\n(\S+)", re.M)
_ctx = ssl.create_default_context()
_stop = threading.Event()
_lock = threading.Lock()


def log(r):
    r["t"] = time.time()
    with _lock:
        with LOG.open("a") as f:
            f.write(json.dumps(r, default=str) + "\n")


def http(url, data=None, headers=None, timeout=15):
    q = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    q.add_header("User-Agent", UA)
    for k, v in (headers or {}).items():
        q.add_header(k, v)
    with urllib.request.urlopen(q, timeout=timeout, context=_ctx) as r:
        return r.read()


def uid():
    return "".join(random.choice("0123456789abcdef") for _ in range(32))


class Arm(threading.Thread):
    def __init__(self, name, player_type, cookie_mode):
        super().__init__(daemon=True, name=name)
        self.n = name
        self.pt = player_type
        self.cookie_mode = cookie_mode        # none | sticky | fresh
        self.sticky_uid = uid()
        self.joins = 0
        self.ad_sessions = set()
        self.polls = 0

    def cookie(self):
        if self.cookie_mode == "none":
            return None
        return self.sticky_uid if self.cookie_mode == "sticky" else uid()

    def session(self, channel):
        dev = uid()
        ck = self.cookie()
        h = {"Client-ID": CID, "Content-Type": "application/json",
             "X-Device-Id": dev, "Device-ID": dev}
        if ck:
            # what a real browser sends; unique_id IS the ad identity
            h["Cookie"] = f"unique_id={ck}; unique_id_durable={ck}"
        body = json.dumps({
            "operationName": "PlaybackAccessToken",
            "variables": {"isLive": True, "login": channel, "isVod": False,
                          "vodID": "", "playerType": self.pt},
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": PQ}},
        }).encode()
        d = json.loads(http("https://gql.twitch.tv/gql", data=body, headers=h))
        node = (d.get("data") or {}).get("streamPlaybackAccessToken")
        if not node:
            return None
        tok = json.loads(node["value"])
        q = urllib.parse.urlencode({
            "client_id": CID, "token": node["value"], "sig": node["signature"],
            "allow_source": "true", "fast_bread": "true",
            "player_backend": "mediaplayer", "supported_codecs": "h264",
            "p": random.randint(1, 9_999_999)})
        txt = http(f"https://usher.ttvnw.net/api/channel/hls/{channel}.m3u8?{q}"
                   ).decode("utf-8", "replace")
        lines = txt.splitlines()
        url = None
        for i, l in enumerate(lines):
            if l.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                u = lines[i + 1].strip()
                if u.startswith("http"):
                    url = u
                    break
        if not url:
            return None
        self.joins += 1
        log({"ev": "join", "arm": self.n, "pt": self.pt,
             "cookie_mode": self.cookie_mode, "channel": channel,
             "cookie_uid": ck, "tok_device_id": tok.get("device_id"),
             "show_ads": tok.get("show_ads"), "server_ads": tok.get("server_ads")})
        return url

    def run(self):
        url = None
        started = 0
        while not _stop.is_set():
            try:
                if url is None:
                    ch = random.choice(POOL)
                    url = self.session(ch)
                    started = time.time()
                    if url is None:
                        _stop.wait(4)
                        continue
                body = http(url, timeout=12).decode("utf-8", "replace")
                self.polls += 1
                titles = {t.strip() for _, t, _ in RE_SEG.findall(body)}
                if any(x != "live" for x in titles if x) or "twitch-stitched-ad" in body:
                    for m in re.finditer(r'X-TV-TWITCH-AD-AD-SESSION-ID="([^"]+)"', body):
                        if m.group(1) not in self.ad_sessions:
                            self.ad_sessions.add(m.group(1))
                            log({"ev": "AD", "arm": self.n, "pt": self.pt,
                                 "cookie_mode": self.cookie_mode,
                                 "ad_session": m.group(1),
                                 "roll": (re.search(r'ROLL-TYPE=(\w+)', body) or
                                          [None, "?"])[1] if re.search(
                                              r'ROLL-TYPE=(\w+)', body) else "?"})
                if time.time() - started > REJOIN_AFTER:
                    url = None
                    continue
                _stop.wait(POLL + random.random() * 0.3)
            except urllib.error.HTTPError:
                url = None
                _stop.wait(2)
            except Exception as e:
                log({"ev": "err", "arm": self.n, "msg": repr(e)})
                url = None
                _stop.wait(3)


def main():
    mins = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    arms = [
        Arm("site-nocookie", "site", "none"),
        Arm("site-cookie", "site", "sticky"),
        Arm("site-cookie-fresh", "site", "fresh"),
        Arm("embed-nocookie", "embed", "none"),   # positive control
    ]
    log({"ev": "start", "minutes": mins, "arms": [a.n for a in arms]})
    for a in arms:
        a.start()
        time.sleep(0.6)
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: _stop.set())
    end = time.time() + mins * 60
    while time.time() < end and not _stop.is_set():
        _stop.wait(30)
        line = "  ".join(f"{a.n}: {a.joins}j/{len(a.ad_sessions)}ads" for a in arms)
        print(f"[.] {line}", flush=True)
        log({"ev": "heartbeat", **{a.n: {"joins": a.joins, "polls": a.polls,
                                         "ads": len(a.ad_sessions)} for a in arms}})
    _stop.set()
    time.sleep(2)
    print("\n=== RESULT ===")
    for a in arms:
        print(f"  {a.n:20s} pt={a.pt:6s} cookie={a.cookie_mode:7s} "
              f"joins={a.joins:4d} polls={a.polls:5d} ad_sessions={len(a.ad_sessions)}")
    log({"ev": "done", **{a.n: {"joins": a.joins, "polls": a.polls,
                                "ads": len(a.ad_sessions)} for a in arms}})


if __name__ == "__main__":
    main()
