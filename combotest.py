#!/usr/bin/env python3
"""
What makes player_type=site ad-eligible? Ruled out so far:

  cookie        99 site joins across 3 cookie policies -> 0 ads (cookietest.py)
  header shape  25 site joins with the browser's exact headers -> 0 ads
                (headertest.py); embed control got ads in both windows

Two hypotheses left, both testable headlessly:

  trigger   every media playlist carries a twitch-trigger DATERANGE with an
            X-TV-TWITCH-TRIGGER-URL on the playlist host. It returns 200 "OK".
            The real player may have to poke it to arm ad decisioning.
  consume   our arms poll manifests but never download video. A session that
            fetches no segments is not a real viewer and may not be
            monetisable inventory. Uses the lowest rendition to stay cheap.

  site-control   minimal, no trigger, no segment fetch  (reproduces 0 ads)
  site-trigger   hits the trigger URL every poll
  site-consume   downloads segments like a real player
  embed-control  positive control, known to get ads

  python gatetest.py [minutes]
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
OUT = LAB / "data" / "combotest"
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / f"ct2.{time.strftime('%m%d-%H%M%S')}.jsonl"

CID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
PQ = "0828119ded1c13477966434e15800ff57ddacf13ba1911c129dc2200705b0712"
UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:150.0) "
      "Gecko/20100101 Firefox/150.0")
POLL = 2.0
REJOIN_AFTER = 40

POOL = ["xqc", "jynxzi", "gaules", "trymacs", "alanzoka", "stableronaldo",
        "tarik", "cellbit", "loltyler1", "kaicenat", "zackrawrr", "hasanabi",
        "summit1g", "moistcr1tikal", "plaqueboymax", "elded"]

RE_SEG = re.compile(r"^#EXTINF:([\d.]+),([^\r\n]*)\r?\n(\S+)", re.M)
RE_TRIG = re.compile(r'X-TV-TWITCH-TRIGGER-URL="([^"]+)"')
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
    def __init__(self, name, pt, trigger=False, consume=False,
                 cookie=False, browser=False):
        super().__init__(daemon=True, name=name)
        self.n = name
        self.pt = pt
        self.trigger = trigger
        self.consume = consume
        self.cookie = cookie
        self.browser = browser
        self.joins = self.polls = self.triggers = self.bytes = 0
        self.ad_sessions = set()

    def session(self, channel):
        dev = uid()
        body = json.dumps({
            "operationName": "PlaybackAccessToken",
            "variables": {"isLive": True, "login": channel, "isVod": False,
                          "vodID": "", "playerType": self.pt},
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": PQ}},
        }).encode()
        if self.browser:
            h = {"Accept": "*/*", "Accept-Language": "en-US",
                 "Referer": "https://www.twitch.tv/", "Authorization": "undefined",
                 "Client-Id": CID, "Content-Type": "text/plain; charset=UTF-8",
                 "Device-Id": dev, "Origin": "https://www.twitch.tv",
                 "Sec-GPC": "1", "Sec-Fetch-Dest": "empty",
                 "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-site"}
        else:
            h = {"Client-ID": CID, "Content-Type": "application/json",
                 "X-Device-Id": dev, "Device-ID": dev}
        if self.cookie:
            ck = uid()
            h["Cookie"] = f"unique_id={ck}; unique_id_durable={ck}"
        d = json.loads(http("https://gql.twitch.tv/gql", data=body, headers=h))
        node = (d.get("data") or {}).get("streamPlaybackAccessToken")
        if not node:
            return None
        q = urllib.parse.urlencode({
            "client_id": CID, "token": node["value"], "sig": node["signature"],
            "allow_source": "true", "fast_bread": "true",
            "player_backend": "mediaplayer", "supported_codecs": "h264",
            "p": random.randint(1, 9_999_999)})
        txt = http(f"https://usher.ttvnw.net/api/channel/hls/{channel}.m3u8?{q}"
                   ).decode("utf-8", "replace")
        lines = txt.splitlines()
        cands = []          # (bandwidth, url)
        for i, l in enumerate(lines):
            if l.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                u = lines[i + 1].strip()
                if not u.startswith("http"):
                    continue
                bw = re.search(r"BANDWIDTH=(\d+)", l)
                cands.append((int(bw.group(1)) if bw else 1 << 30, u))
        if not cands:
            return None
        # consumers MUST take the lowest-bandwidth rendition; variant order is
        # not sorted, and grabbing source would pull ~5 Mbit/s for hours.
        url = min(cands)[1] if self.consume else cands[0][1]
        self.joins += 1
        log({"ev": "join", "arm": self.n, "pt": self.pt, "channel": channel,
             "trigger": self.trigger, "consume": self.consume})
        return url

    def run(self):
        url, started, last_seg = None, 0, None
        while not _stop.is_set():
            try:
                if url is None:
                    url = self.session(random.choice(POOL))
                    started = time.time()
                    if url is None:
                        _stop.wait(4)
                        continue
                body = http(url, timeout=12).decode("utf-8", "replace")
                self.polls += 1

                if self.trigger:
                    t = RE_TRIG.search(body)
                    if t:
                        try:
                            http(t.group(1), timeout=10)
                            self.triggers += 1
                        except Exception:
                            pass

                segs = RE_SEG.findall(body)
                if self.consume and segs:
                    # fetch the newest segment we haven't already pulled
                    dur, title, su = segs[-1]
                    if su != last_seg:
                        last_seg = su
                        try:
                            self.bytes += len(http(su, timeout=20))
                        except Exception:
                            pass

                titles = {t.strip() for _, t, _ in segs}
                if any(x != "live" for x in titles if x) or "twitch-stitched-ad" in body:
                    for m in re.finditer(r'X-TV-TWITCH-AD-AD-SESSION-ID="([^"]+)"', body):
                        if m.group(1) not in self.ad_sessions:
                            self.ad_sessions.add(m.group(1))
                            rt = re.search(r'ROLL-TYPE=(\w+)', body)
                            log({"ev": "AD", "arm": self.n, "pt": self.pt,
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
    mins = float(sys.argv[1]) if len(sys.argv) > 1 else 180
    arms = [
        Arm("site-plain", "site"),
        Arm("site-cookie", "site", cookie=True),
        Arm("site-browserhdr", "site", browser=True),
        Arm("site-consume", "site", consume=True),
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
        log({"ev": "heartbeat", **{a.n: {
            "joins": a.joins, "polls": a.polls, "ads": len(a.ad_sessions),
            "triggers": a.triggers, "mb": round(a.bytes / 1e6, 1)} for a in arms}})
    _stop.set()
    time.sleep(2)
    print("\n=== RESULT ===")
    for a in arms:
        print(f"  {a.n:16s} pt={a.pt:6s} joins={a.joins:4d} polls={a.polls:5d} "
              f"triggers={a.triggers:4d} MB={a.bytes/1e6:7.1f} "
              f"ads={len(a.ad_sessions)}")
    log({"ev": "done", **{a.n: {
        "joins": a.joins, "polls": a.polls, "ads": len(a.ad_sessions),
        "triggers": a.triggers, "mb": round(a.bytes / 1e6, 1)} for a in arms}})


if __name__ == "__main__":
    main()
