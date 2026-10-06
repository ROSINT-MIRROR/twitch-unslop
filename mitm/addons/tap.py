"""
Passive tap. Observes only — never mutates a flow.

Dump-complete by design: anything we can't reconstruct offline gets written to
disk now. Analysis happens later, from data/, with no browser running.

  data/logs/tap.<s>.jsonl    event stream (the index)
  data/manifests/            every distinct m3u8, deduped by content hash
  data/gql/<s>/              full GraphQL req+resp bodies
  data/segments/<s>/         .ts bytes: all ad segments + a live baseline
  data/raw/<s>.flows         everything except segment bodies (replayable)
  data/logs/run.<s>.json     session metadata

  mitmdump --set confdir=mitm/ca -s mitm/addons/tap.py -p 8888
"""
import base64
import hashlib
import json
import os
import pathlib
import re
import time
import urllib.parse

LAB = pathlib.Path(__file__).resolve().parents[2]
SESSION = os.environ.get("LAB_SESSION", "default")

LOGS = LAB / "data" / "logs"
MANIFESTS = LAB / "data" / "manifests"
GQL = LAB / "data" / "gql" / SESSION
SEGS = LAB / "data" / "segments" / SESSION
for d in (LOGS, MANIFESTS, GQL, SEGS):
    d.mkdir(parents=True, exist_ok=True)

TAP = LOGS / f"tap.{SESSION}.jsonl"

# how many non-ad segments to keep per playlist as an ffprobe baseline.
# ad segments are ALWAYS kept — they're the whole point and they're bounded.
LIVE_BASELINE = 8

HOSTS = {
    "gql.twitch.tv", "usher.ttvnw.net", "spade.twitch.tv",
    "countess.twitch.tv", "pubsub-edge.twitch.tv", "eventsub.wss.twitch.tv",
}
# current topology (verified 2026-07): variants on <pop>.playlist.ttvnw.net,
# segments on *.cloudfront.hls.ttvnw.net / *.rufio.hls.live-video.net.
# legacy video-weaver/video-edge *.hls.ttvnw.net still appears on some pops.
HOST_SUFFIX = (".playlist.ttvnw.net", ".hls.ttvnw.net", ".live-video.net",
               ".ttvnw.net", ".twitch.tv")

RE_DATERANGE = re.compile(r"^#EXT-X-DATERANGE:(.*)$", re.M)
RE_TWITCH_INFO = re.compile(r"^#EXT-X-TWITCH-INFO:(.*)$", re.M)
RE_ATTR = re.compile(r'([A-Z0-9-]+)=("(?:[^"]*)"|[^,]*)')
RE_SOURCE = re.compile(r'X-TV-TWITCH-STREAM-SOURCE="([^"]*)"')
RE_PREFETCH = re.compile(r"^#EXT-X-TWITCH-PREFETCH:(\S+)", re.M)
# (duration, title, url) — title is "live" during content, something else in a break
RE_SEG = re.compile(r"^#EXTINF:([\d.]+),([^\r\n]*)\r?\n(\S+)", re.M)

_seen = {}            # playlist url -> content hash
_seg_state = {}       # segment url (no query) -> title from the playlist
_live_kept = {}       # playlist key -> count of baseline segments saved
_counts = {}


def _emit(rec):
    rec["t"] = time.time()
    rec["s"] = SESSION
    with TAP.open("a") as f:
        f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
    _counts[rec["ev"]] = _counts.get(rec["ev"], 0) + 1


def _attrs(line):
    return {k: v.strip('"') for k, v in RE_ATTR.findall(line)}


def _first(m):
    return m.group(1) if m else None


def _interesting(host):
    return host in HOSTS or host.endswith(HOST_SUFFIX)


def _key(url):
    return url.split("?")[0]


def _tag(s, n=8):
    return hashlib.sha1(s.encode()).hexdigest()[:n]


def load(loader):
    (LOGS / f"run.{SESSION}.json").write_text(json.dumps({
        "session": SESSION,
        "started": time.time(),
        "started_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "live_baseline_per_playlist": LIVE_BASELINE,
    }, indent=2))


def done():
    _emit({"ev": "session_end", "counts": _counts})


# ---------------------------------------------------------------- GraphQL
def _handle_gql(flow):
    raw_req = flow.request.get_text() or ""
    raw_res = flow.response.get_text() or ""

    # persist the full bodies regardless of whether we can parse them
    stamp = f"{int(time.time()*1000)}.{_tag(raw_req, 6)}"
    (GQL / f"{stamp}.json").write_text(json.dumps({
        "t": time.time(),
        "status": flow.response.status_code,
        "req_headers": dict(flow.request.headers),
        "req": raw_req,
        "res": raw_res,
    }, indent=2))

    try:
        req = json.loads(raw_req)
        res = json.loads(raw_res)
    except Exception as e:
        _emit({"ev": "gql_unparsed", "file": stamp, "msg": str(e)})
        return

    reqs = req if isinstance(req, list) else [req]
    ress = res if isinstance(res, list) else [res]

    for q, r in zip(reqs, ress):
        op = (q or {}).get("operationName") or "?"
        data = ((r or {}).get("data") or {})

        for field in ("streamPlaybackAccessToken", "videoPlaybackAccessToken"):
            node = data.get(field)
            if not node:
                continue
            try:
                token = json.loads(node["value"])
            except Exception:
                token = {"_raw": node.get("value")}
            _emit({"ev": "playback_token", "field": field, "file": stamp,
                   "vars": q.get("variables"), "sig": node.get("signature"),
                   "token": token})

        # ad-adjacent ops keep their payload inline for convenience;
        # everything else is still on disk under data/gql/
        if "ad" in op.lower():
            _emit({"ev": "gql_ad_op", "op": op, "file": stamp,
                   "vars": q.get("variables"), "data": data})
        else:
            _emit({"ev": "gql", "op": op, "file": stamp})


# ---------------------------------------------------------------- playlists
def _handle_m3u8(flow, host, body):
    url = flow.request.pretty_url
    k = _key(url)
    h = hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()[:12]
    fresh = _seen.get(k) != h
    _seen[k] = h

    is_master = "#EXT-X-STREAM-INF" in body
    kind = "master" if is_master else "media"
    if fresh:
        p = MANIFESTS / f"{SESSION}.{kind}.{_tag(k)}.{int(time.time()*1000)}.{h}.m3u8"
        p.write_text(body)

    if is_master:
        info = RE_TWITCH_INFO.search(body)
        _emit({"ev": "master", "host": host, "url": url, "sha": h, "new": fresh,
               "twitch_info": _attrs(info.group(1)) if info else None,
               "variants": re.findall(r'VIDEO="([^"]+)"', body)})
        return

    segs = RE_SEG.findall(body)
    titles = sorted({t.strip() for _, t, _ in segs if t.strip()})

    # remember what each segment URL is, so the segment handler knows
    # whether the bytes it's about to see are ad or content
    for _, title, segurl in segs:
        _seg_state[_key(urllib.parse.urljoin(url, segurl))] = title.strip()
    for pf in RE_PREFETCH.findall(body):
        _seg_state.setdefault(_key(urllib.parse.urljoin(url, pf)), "prefetch")

    ads = []
    for line in RE_DATERANGE.findall(body):
        a = _attrs(line)
        if "twitch-stitched-ad" in (a.get("CLASS") or "") or any(
                x.startswith("X-TV-TWITCH-AD") for x in a):
            ads.append(a)

    src = _first(RE_SOURCE.search(body))
    rec = {
        "ev": "media", "host": host, "url": url, "sha": h, "new": fresh,
        "segments": len(segs),
        "discontinuities": body.count("#EXT-X-DISCONTINUITY"),
        "prefetch": body.count("#EXT-X-TWITCH-PREFETCH"),
        "stream_source": src,
        "seg_titles": titles,
        "media_sequence": _first(re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", body)),
        "elapsed": _first(re.search(r"#EXT-X-TWITCH-ELAPSED-SECS:([\d.]+)", body)),
        "daterange_classes": sorted({_attrs(l).get("CLASS")
                                     for l in RE_DATERANGE.findall(body)} - {None}),
    }
    # three independent tells; any one flags the sample so a tagging change
    # on Twitch's side can't silently blind the capture
    if ads or (src and src != "live") or any(t != "live" for t in titles):
        rec["ev"] = "media_ad"
        if ads:
            rec["ads"] = ads
    _emit(rec)


# ---------------------------------------------------------------- segments
def _handle_segment(flow, host):
    url = flow.request.pretty_url
    k = _key(url)
    body = flow.response.content or b""
    title = _seg_state.get(k)
    is_ad = title is not None and title not in ("live", "prefetch")

    rec = {"ev": "segment", "host": host, "url": url, "bytes": len(body),
           "status": flow.response.status_code, "http": flow.response.http_version,
           "title": title, "ad": is_ad}

    # ad segments are always kept; live ones only until the baseline is met
    keep = False
    if body:
        if is_ad:
            keep = True
        elif title == "live":
            pk = _tag(flow.request.path.split("/v1/segment/")[0] or host)
            if _live_kept.get(pk, 0) < LIVE_BASELINE:
                _live_kept[pk] = _live_kept.get(pk, 0) + 1
                keep = True

    if keep:
        name = (f"{int(time.time()*1000)}.{'AD' if is_ad else 'live'}."
                f"{_tag(k)}.ts")
        (SEGS / name).write_bytes(body)
        rec["saved"] = name
    elif not body:
        # 204 = LL-HLS prefetch probed before the segment exists. Normal.
        # An empty 200 means we actually lost the bytes — surface that loudly.
        rec["ev"] = ("segment_empty" if flow.response.status_code != 200
                     else "segment_nobody")
    _emit(rec)


# ---------------------------------------------------------------- hooks
def response(flow):
    host = flow.request.pretty_host
    if not _interesting(host):
        return
    try:
        ctype = flow.response.headers.get("content-type", "")
        path = flow.request.path.split("?")[0]

        if host == "gql.twitch.tv":
            _handle_gql(flow)
        elif "mpegurl" in ctype or path.endswith(".m3u8"):
            _handle_m3u8(flow, host, flow.response.get_text())
        elif "video/" in ctype or path.endswith(".ts") or "/v1/segment/" in path:
            _handle_segment(flow, host)
        elif host == "spade.twitch.tv":
            # client-side telemetry incl. ad impressions; base64 JSON
            raw = flow.request.get_text() or ""
            payload = None
            try:
                b = raw.split("data=", 1)[1] if "data=" in raw else raw
                payload = json.loads(base64.b64decode(
                    urllib.parse.unquote(b) + "=="))
            except Exception:
                pass
            _emit({"ev": "spade", "status": flow.response.status_code,
                   "events": [e.get("event") for e in payload] if isinstance(payload, list) else None,
                   "payload": payload if payload is not None else raw[:2000]})
        elif "trigger" in path:
            _emit({"ev": "trigger", "host": host, "url": flow.request.pretty_url,
                   "status": flow.response.status_code,
                   "body": (flow.response.get_text() or "")[:4000]})
        else:
            _emit({"ev": "other", "host": host, "path": path,
                   "status": flow.response.status_code, "ctype": ctype})
    except Exception as e:
        _emit({"ev": "err", "host": host, "url": flow.request.pretty_url,
               "msg": repr(e)})


def websocket_message(flow):
    host = flow.request.pretty_host
    if not _interesting(host):
        return
    m = flow.websocket.messages[-1]
    # log everything; pubsub carries commercial / stream_up / viewcount
    _emit({"ev": "ws", "host": host, "from_client": m.from_client,
           "msg": (m.text or "")[:8000]})
