#!/usr/bin/env python3
"""
cookietest proved the cookie is NOT the gate: 99 `site` joins across three
cookie policies got 0 ads, while 33 `embed` joins in the same window got 14.

But the user's real browser DID get a preroll on `site`. So something else
about a genuine web client makes `site` monetizable. Diffing our mint against
the browser's captured request headers (data/gql/0729-01*):

  browser                              us
  content-type: text/plain;charset=UTF-8   application/json
  origin:  https://www.twitch.tv           (absent)
  referer: https://www.twitch.tv/          (absent)
  authorization: undefined                 (absent)
  device-id: <id>                          X-Device-Id + Device-ID

Hypothesis: Twitch only serves monetizable inventory to requests that look
like the real web app. Origin/Referer are the obvious tell.

  site-minimal        our headers; reproduces the 0-ads result
  site-browser        byte-for-byte browser header set
  site-browser-cookie browser headers + a unique_id cookie
  embed-minimal       positive control, known to get ads

  python headertest.py [minutes]
"""
import json
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
OUT = LAB / "data" / "headertest"
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / f"ht.{time.strftime('%m%d-%H%M%S')}.jsonl"

CID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
PQ = "0828119ded1c13477966434e15800ff57ddacf13ba1911c129dc2200705b0712"
# the browser in data/gql/0729-01* reported rv:150.0
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:150.0) "
              "Gecko/20100101 Firefox/150.0")
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
    for k, v in (headers or {}).items():
        q.add_header(k, v)
    q.add_header("User-Agent", headers.get("User-Agent", BROWSER_UA)
                 if headers else BROWSER_UA)
    with urllib.request.urlopen(q, timeout=timeout, context=_ctx) as r:
        return r.read()


def uid():
    return "".join(random.choice("0123456789abcdef") for _ in range(32))


class Arm(threading.Thread):
    def __init__(self, name, player_type, style, cookie=False):
        super().__init__(daemon=True, name=name)
        self.n = name
        self.pt = player_type
        self.style = style              # minimal | browser
        self.cookie = cookie
        self.joins = 0
        self.polls = 0
        self.ad_sessions = set()

    def headers(self, dev):
        if self.style == "minimal":
            return {"Client-ID": CID, "Content-Type": "application/json",
                    "X-Device-Id": dev, "Device-ID": dev,
                    "User-Agent": BROWSER_UA}
        h = {
            "Accept": "*/*",
            "Accept-Language": "en-US",
            "Referer": "https://www.twitch.tv/",
            "Authorization": "undefined",
            "Client-Id": CID,
            "Content-Type": "text/plain; charset=UTF-8",
            "Device-Id": dev,
            "Origin": "https://www.twitch.tv",
            "Sec-GPC": "1",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "User-Agent": BROWSER_UA,
        }
        if self.cookie:
            c = uid()
            h["Cookie"] = f"unique_id={c}; unique_id_durable={c}"
        return h

    def session(self, channel):
        dev = uid()
        body = json.dumps({
            "operationName": "PlaybackAccessToken",
            "variables": {"isLive": True, "login": channel, "isVod": False,
                          "vodID": "", "playerType": self.pt},
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": PQ}},
        }).encode()
        d = json.loads(http("https://gql.twitch.tv/gql", data=body,
                            headers=self.headers(dev)))
        node = (d.get("data") or {}).get("streamPlaybackAccessToken")
        if not node:
            return None
        tok = json.loads(node["value"])
        q = urllib.parse.urlencode({
            "client_id": CID, "token": node["value"], "sig": node["signature"],
            "allow_source": "true", "fast_bread": "true",
            "player_backend": "mediaplayer", "supported_codecs": "h264",
            "p": random.randint(1, 9_999_999)})
        txt = http(f"https://usher.ttvnw.net/api/channel/hls/{channel}.m3u8?{q}",
                   headers={"User-Agent": BROWSER_UA,
                            "Referer": "https://www.twitch.tv/",
                            "Origin": "https://www.twitch.tv"}
                   if self.style == "browser" else {"User-Agent": BROWSER_UA}
                   ).decode("utf-8", "replace")
        url = None
        lines = txt.splitlines()
        for i, l in enumerate(lines):
            if l.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                u = lines[i + 1].strip()
                if u.startswith("http"):
                    url = u
                    break
        if not url:
            return None
        self.joins += 1
        log({"ev": "join", "arm": self.n, "pt": self.pt, "style": self.style,
             "cookie": self.cookie, "channel": channel,
             "tok_device_id": tok.get("device_id"),
             "show_ads": tok.get("show_ads"),
             "server_ads": tok.get("server_ads")})
        return url

    def run(self):
        url, started = None, 0
        while not _stop.is_set():
            try:
                if url is None:
                    url = self.session(random.choice(POOL))
                    started = time.time()
                    if url is None:
                        _stop.wait(4)
                        continue
                body = http(url, timeout=12,
                            headers={"User-Agent": BROWSER_UA}
                            ).decode("utf-8", "replace")
                self.polls += 1
                titles = {t.strip() for _, t, _ in RE_SEG.findall(body)}
                if any(x != "live" for x in titles if x) or "twitch-stitched-ad" in body:
                    for m in re.finditer(r'X-TV-TWITCH-AD-AD-SESSION-ID="([^"]+)"', body):
                        if m.group(1) not in self.ad_sessions:
                            self.ad_sessions.add(m.group(1))
                            rt = re.search(r'ROLL-TYPE=(\w+)', body)
                            log({"ev": "AD", "arm": self.n, "pt": self.pt,
                                 "style": self.style, "cookie": self.cookie,
                                 "ad_session": m.group(1),
                                 "roll": rt.group(1) if rt else "?"})
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
    mins = float(sys.argv[1]) if len(sys.argv) > 1 else 120
    arms = [
        Arm("site-minimal", "site", "minimal"),
        Arm("site-browser", "site", "browser"),
        Arm("site-browser-cookie", "site", "browser", cookie=True),
        Arm("embed-minimal", "embed", "minimal"),      # positive control
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
        print("[.] " + "  ".join(
            f"{a.n}:{a.joins}j/{len(a.ad_sessions)}ads" for a in arms), flush=True)
        log({"ev": "heartbeat", **{a.n: {"joins": a.joins, "polls": a.polls,
                                         "ads": len(a.ad_sessions)} for a in arms}})
    _stop.set()
    time.sleep(2)
    print("\n=== RESULT ===")
    for a in arms:
        print(f"  {a.n:22s} pt={a.pt:6s} style={a.style:8s} "
              f"joins={a.joins:4d} ads={len(a.ad_sessions)}")
    log({"ev": "done", **{a.n: {"joins": a.joins, "polls": a.polls,
                                "ads": len(a.ad_sessions)} for a in arms}})


if __name__ == "__main__":
    main()
