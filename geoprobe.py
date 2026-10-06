#!/usr/bin/env python3
"""
D3 geo probe: does geo bind only at token-mint time, or is it re-checked
downstream on the playlist poll?

For each SOCKS exit we mint a session THROUGH the proxy, then poll the
resulting media playlist two ways — through the proxy, and direct from our
own IP. If a proxy-minted session stays clean when polled direct, geo binds
at mint and an extension only has to proxy the tiny control-plane request.

Uses player_type=embed because the hunter showed that's the arm that
actually gets served ads from this IP — a sensitive detector. Comparing
against site would prove nothing, since site gets no ads here anyway.

  python geoprobe.py [--ports 9050,9051,...] [--channel X] [--polls 8]
"""
import argparse
import json
import random
import re
import subprocess
import sys
import time
import pathlib
import urllib.parse

LAB = pathlib.Path(__file__).resolve().parent
OUT = LAB / "data" / "geoprobe"
OUT.mkdir(parents=True, exist_ok=True)

CID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
PQ = "0828119ded1c13477966434e15800ff57ddacf13ba1911c129dc2200705b0712"
UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
      "Gecko/20100101 Firefox/128.0")

RE_SEG = re.compile(r"^#EXTINF:([\d.]+),([^\r\n]*)\r?\n(\S+)", re.M)
RE_SOURCE = re.compile(r'X-TV-TWITCH-STREAM-SOURCE="([^"]*)"')
RE_INFO = re.compile(r"^#EXT-X-TWITCH-INFO:(.*)$", re.M)


def curl(url, socks=None, post=None, headers=None, timeout=30):
    cmd = ["curl", "-s", "--max-time", str(timeout), "-A", UA]
    if socks:
        cmd += ["--socks5-hostname", f"127.0.0.1:{socks}"]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    if post is not None:
        cmd += ["-d", post]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    return r.stdout.decode("utf-8", "replace")


def dev_id():
    return "".join(random.choice("0123456789abcdef") for _ in range(32))


def exit_info(socks):
    ip = curl("https://api.ipify.org", socks=socks).strip()
    cc = curl("https://ipinfo.io/country", socks=socks).strip()
    return ip, cc


def mint(channel, socks, player_type, device):
    body = json.dumps({
        "operationName": "PlaybackAccessToken",
        "variables": {"isLive": True, "login": channel, "isVod": False,
                      "vodID": "", "playerType": player_type},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": PQ}},
    })
    raw = curl("https://gql.twitch.tv/gql", socks=socks, post=body, headers={
        "Client-ID": CID, "Content-Type": "application/json",
        "X-Device-Id": device, "Device-ID": device})
    d = json.loads(raw)
    return (d.get("data") or {}).get("streamPlaybackAccessToken")


def master(channel, node, socks):
    q = urllib.parse.urlencode({
        "client_id": CID, "token": node["value"], "sig": node["signature"],
        "allow_source": "true", "fast_bread": "true",
        "player_backend": "mediaplayer", "supported_codecs": "h264",
        "p": random.randint(1, 9_999_999)})
    return curl(f"https://usher.ttvnw.net/api/channel/hls/{channel}.m3u8?{q}",
                socks=socks)


def variant(txt):
    lines = txt.splitlines()
    best = None
    for i, l in enumerate(lines):
        if l.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
            u = lines[i + 1].strip()
            if u.startswith("http"):
                if "720p60" in l + (lines[i - 1] if i else ""):
                    return u
                if best is None:
                    best = u
    return best


def scan(body):
    segs = RE_SEG.findall(body)
    titles = sorted({t.strip() for _, t, _ in segs if t.strip()})
    src = RE_SOURCE.search(body)
    src = src.group(1) if src else None
    ad = bool(src and src != "live") or any(t != "live" for t in titles)
    return {"ad": ad, "src": src, "titles": titles, "n": len(segs)}


def attrs(line):
    return {k: v.strip('"') for k, v in
            re.findall(r'([A-Z0-9-]+)=("(?:[^"]*)"|[^,]*)', line)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ports", default="9050,9051,9052,9060,9100,9150")
    ap.add_argument("--channel", default="gaules")
    ap.add_argument("--player-type", default="embed")
    ap.add_argument("--polls", type=int, default=8)
    args = ap.parse_args()

    stamp = time.strftime("%m%d-%H%M%S")
    log = OUT / f"geoprobe.{stamp}.jsonl"

    def emit(r):
        r["t"] = time.time()
        with log.open("a") as f:
            f.write(json.dumps(r, default=str) + "\n")
        return r

    ports = [p.strip() for p in args.ports.split(",") if p.strip()]
    dip, dcc = exit_info(None)
    print(f"[*] direct  {dip}  {dcc}")
    emit({"ev": "direct", "ip": dip, "cc": dcc})

    # CONTROL: a session minted from our own IP, same channel, same
    # player_type, polled in the same interleaved loop. Without this we
    # cannot tell "foreign mint suppressed ads" from "no ad break happened".
    ctl_url = None
    try:
        cnode = mint(args.channel, None, args.player_type, dev_id())
        if cnode:
            ctl_url = variant(master(args.channel, cnode, None))
            ctok = json.loads(cnode["value"])
            print(f"[*] control minted direct: user_ip={ctok.get('user_ip')}")
            emit({"ev": "control_mint", "user_ip": ctok.get("user_ip"),
                  "has_variant": bool(ctl_url)})
    except Exception as e:
        print(f"[!] control mint failed: {e!r}")

    for p in ports:
        try:
            ip, cc = exit_info(p)
        except Exception as e:
            print(f"[!] :{p} unusable ({e!r})")
            continue
        if not ip:
            print(f"[!] :{p} no exit")
            continue
        same = (ip == dip)
        print(f"\n[*] socks :{p}  {ip}  {cc}" + ("  (SAME AS DIRECT)" if same else ""))

        node = mint(args.channel, p, args.player_type, dev_id())
        if not node:
            print("    no token (channel offline?)")
            continue
        tok = json.loads(node["value"])
        print(f"    token: user_ip={tok.get('user_ip')} "
              f"server_ads={tok.get('server_ads')} show_ads={tok.get('show_ads')} "
              f"ci_gb={tok.get('ci_gb')}")

        mtxt = master(args.channel, node, p)
        info = RE_INFO.search(mtxt)
        ia = attrs(info.group(1)) if info else {}
        print(f"    master: USER-COUNTRY={ia.get('USER-COUNTRY')} "
              f"CLUSTER={ia.get('MANIFEST-CLUSTER')}")
        v = variant(mtxt)
        emit({"ev": "mint", "port": p, "exit_ip": ip, "cc": cc,
              "token_user_ip": tok.get("user_ip"), "ci_gb": tok.get("ci_gb"),
              "server_ads": tok.get("server_ads"), "show_ads": tok.get("show_ads"),
              "master_country": ia.get("USER-COUNTRY"),
              "cluster": ia.get("MANIFEST-CLUSTER"), "has_variant": bool(v)})
        if not v:
            print("    no variant")
            continue

        # The actual question, polled INTERLEAVED. Running the two modes
        # sequentially confounds them: one ad break spanning both windows
        # makes the second mode look worse purely because of when it ran.
        # Alternating means both modes see the same break, or neither does.
        res = {"via_socks": {"ok": 0, "ads": 0}, "direct": {"ok": 0, "ads": 0},
               "control_home_mint": {"ok": 0, "ads": 0}}
        legs = [("via_socks", v, p), ("direct", v, None)]
        if ctl_url:
            legs.append(("control_home_mint", ctl_url, None))
        for i in range(args.polls):
            for mode, tgt, sk in legs:
                body = curl(tgt, socks=sk, timeout=20)
                if not body.startswith("#EXTM3U"):
                    continue
                res[mode]["ok"] += 1
                s = scan(body)
                if s["ad"]:
                    res[mode]["ads"] += 1
                    (OUT / f"AD.{stamp}.{p}.{mode}.{i}.m3u8").write_text(body)
                emit({"ev": "poll", "port": p, "mode": mode, "i": i, **s})
            time.sleep(2)
        for mode in ("via_socks", "direct", "control_home_mint"):
            r = res[mode]
            print(f"    {mode:10s} polls_ok={r['ok']}/{args.polls} ad_polls={r['ads']}")
            emit({"ev": "summary", "port": p, "mode": mode,
                  "polls_ok": r["ok"], "ad_polls": r["ads"],
                  "interleaved": True})

    print(f"\n[+] {log}")


if __name__ == "__main__":
    main()
