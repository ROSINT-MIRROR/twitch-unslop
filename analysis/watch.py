#!/usr/bin/env python3
"""
Tail the tap and shout when a stitched ad break starts/ends.

  python analysis/watch.py [session]      # default: newest tap.*.jsonl
"""
import json
import pathlib
import sys
import time

LAB = pathlib.Path(__file__).resolve().parents[1]
LOGS = LAB / "data" / "logs"

R = "\033[31m"; G = "\033[32m"; Y = "\033[33m"; D = "\033[2m"; X = "\033[0m"


def pick():
    if len(sys.argv) > 1:
        p = LOGS / f"tap.{sys.argv[1]}.jsonl"
        if not p.exists():
            sys.exit(f"!! {p} not found")
        return p
    cands = sorted(LOGS.glob("tap.*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not cands:
        sys.exit("!! no tap.*.jsonl yet — start mitm/run.sh")
    return cands[-1]


def short(url):
    # <pop>.playlist.ttvnw.net/v1/playlist/<blob> -> pop + blob tail
    try:
        host = url.split("//")[1].split(".")[0]
        return f"{host}/{url.rstrip('.m3u8')[-8:]}"
    except Exception:
        return url[:32]


def main():
    path = pick()
    print(f"{D}watching {path}{X}\n")
    state, polls, breaks = {}, 0, 0

    with path.open() as f:
        f.seek(0, 2)                       # live tail only
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.25)
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue

            ev = r.get("ev")
            ts = time.strftime("%H:%M:%S", time.localtime(r.get("t", 0)))

            if ev == "playback_token":
                t = r.get("token") or {}
                print(f"{Y}{ts} TOKEN{X} {t.get('channel')} "
                      f"server_ads={t.get('server_ads')} show_ads={t.get('show_ads')} "
                      f"sub={t.get('subscriber')} type={t.get('player_type')}")

            elif ev == "master":
                i = r.get("twitch_info") or {}
                print(f"{Y}{ts} MASTER{X} cc={i.get('USER-COUNTRY')} "
                      f"cluster={i.get('MANIFEST-CLUSTER')} bcast={i.get('BROADCAST-ID')}")

            elif ev in ("media", "media_ad"):
                polls += 1
                k = short(r.get("url", ""))
                src = r.get("stream_source")
                now = "ad" if ev == "media_ad" else "live"
                was = state.get(k)
                state[k] = now

                if was is not None and was != now:
                    if now == "ad":
                        breaks += 1
                        ads = r.get("ads") or []
                        print(f"\n{R}{ts} ══ AD BREAK #{breaks} ══{X} {k}  src={src}")
                        for a in ads:
                            print(f"   {R}roll={a.get('X-TV-TWITCH-AD-ROLL-TYPE')} "
                                  f"pod={a.get('X-TV-TWITCH-AD-POD-POSITION')}"
                                  f"/{a.get('X-TV-TWITCH-AD-POD-LENGTH')} "
                                  f"dur={a.get('DURATION')} "
                                  f"adv={a.get('X-TV-TWITCH-AD-ADVERTISER-ID')} "
                                  f"line={a.get('X-TV-TWITCH-AD-LINE-ITEM-ID')}{X}")
                        if not ads:
                            print(f"   {R}(no stitched-ad DATERANGE — caught via "
                                  f"titles={r.get('seg_titles')}){X}")
                    else:
                        print(f"{G}{ts} ══ back to live ══{X} {k}\n")

            if polls and polls % 50 == 0:
                live = sum(1 for v in state.values() if v == "live")
                print(f"{D}{ts} .. {polls} polls, {len(state)} playlists "
                      f"({live} live), {breaks} breaks{X}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
