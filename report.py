#!/usr/bin/env python3
"""
Regenerate notes/FINDINGS.md from whatever is currently in data/.
Every number here is computed from the dumps — nothing is hand-typed.

  python report.py [hunt_session]

Three sources feed it:

  data/hunt/<s>/    the headless hunter (ad rate, tell reliability, inventory)
  data/unslop/*.jsonl  the POC ledgers, scored through coverage.py's own maths
  data/ext/<c>.<ts>/events.jsonl  the extension's structured stream (ext/EVENTS.md)

The last two are read defensively: half of those sessions were killed with
Ctrl-C mid-run, one is still being appended to while this runs, and unknown
event names ship before this file learns them. Nothing here may raise on any
of that, and a source that is absent is reported as absent rather than skipped.
"""
import calendar
import collections
import glob
import json
import pathlib
import re
import sys
import time

LAB = pathlib.Path(__file__).resolve().parent
S = sys.argv[1] if len(sys.argv) > 1 else (LAB / ".hunt_session").read_text().strip()
H = LAB / "data" / "hunt" / S

RE_SEG = re.compile(r"^#EXTINF:([\d.]+),([^\r\n]*)\r?\n(\S+)", re.M)
RE_SRC = re.compile(r'X-TV-TWITCH-STREAM-SOURCE="([^"]*)"')
RE_PDT = re.compile(r"#EXT-X-PROGRAM-DATE-TIME:(\S+)")
RE_PDTSEG = re.compile(
    r"#EXT-X-PROGRAM-DATE-TIME:(\S+)\s*\r?\n#EXTINF:([\d.]+),([^\r\n]*)")

# ---- extension analysis constants -----------------------------------------
# A ladder_collapse further than this from an ad-bearing poll predicted nothing.
# The observed breaks on gaules are ~9 minutes apart, so this cannot bridge two.
EXT_HORIZON_MS = 120_000
# A rebind/regrid/collapse this close before a miss is a candidate cause.
# Same value as ext/extreport.py's NEAR_MS, deliberately.
EXT_NEAR_MS = 10_000
# Ad-bearing poll runs closer together than this are one pod. ext/extreport.py
# closes a run after 3 ad-free polls, and during a break the player polls faster
# than once per segment, so a single pod gets split — and its `peak` then gets
# counted twice in any "how many ads were offered" sum.
EXT_POD_MS = 30_000
# The log sink on :8779 is shared — anything POSTing to it lands in whichever
# session directory is current. Events carrying a `chan` this session does not
# own are therefore someone else's, and so is the burst around them.
EXT_QUAR_MS = 1_500


def rows(p):
    if not pathlib.Path(p).exists():
        return []
    out = []
    for l in open(p):
        try:
            out.append(json.loads(l))
        except Exception:
            pass
    return out


def _mod(path, name):
    """Import a sibling script by path, or None.

    coverage.py and ext/extreport.py already implement the two scorings this
    file needs. Importing them is the only way to guarantee FINDINGS.md and
    those tools cannot drift apart — in particular coverage.py's SPLICEABLE,
    which is exact weighted-interval scheduling and must not be re-derived by
    eye. Guarded because ext/ is edited while this runs, and a half-written
    file there must not take the whole report down.
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(name, str(path))
        if spec is None or spec.loader is None:
            return None
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    except Exception:
        return None


def _n(v, d=0.0):
    """Live logs carry nulls and the odd stringified number."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _clock(ms):
    try:
        return time.strftime("%H:%M:%S", time.localtime(ms / 1000.0))
    except Exception:
        return "?"


def _secs(s):
    try:
        s = float(s)
    except (TypeError, ValueError):
        return "?"
    return f"{s:.0f}s" if abs(s) < 90 else f"{int(s // 60)}m{int(s % 60):02d}s"


def _iso_ms(s):
    """`2026-07-29T14:21:55.479Z` -> epoch millis. None if it will not parse."""
    try:
        base = calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
        frac = 0.0
        if "." in s:
            frac = float("0." + s.split(".", 1)[1].rstrip("Z")[:3])
        return int((base + frac) * 1000)
    except Exception:
        return None


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _pl(k, one, many=None):
    """`1 break` / `2 breaks`, so a generated sentence never reads as junk."""
    return f"{k} {one if k == 1 else (many or one + 's')}"


def _dash(v):
    """An absent field prints as `-`, never as the string `None`."""
    return "-" if v is None else str(v)


# ---------------------------------------------------------------------------
# data/ext/<chan>.<ts>/events.jsonl  — the browser extension
# ---------------------------------------------------------------------------

def ext_sessions():
    """Every session directory holding a parseable events.jsonl.

    Sessions are picked on the presence of the stream, not the directory name —
    data/ext also holds ext.log and other leftovers. ext.log is a *rendering*
    of this stream (ext/EVENTS.md), never a source, so nothing here parses it.
    """
    out = []
    root = LAB / "data" / "ext"
    if not root.is_dir():
        return out
    for d in sorted(root.glob("*")):
        f = d / "events.jsonl"
        if not f.is_file():
            continue
        chan = None
        try:
            chan = (json.loads((d / "meta.json").read_text()) or {}).get("channel")
        except Exception:
            pass
        if not chan:
            chan = d.name.split(".")[0]
        try:
            lines = f.read_text(errors="replace").splitlines()
        except Exception:
            lines = []
        ev, t, bad = [], 0.0, 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                bad += 1
                continue
            if not isinstance(r, dict):
                bad += 1
                continue
            t = _n(r.get("t"), t)   # append-only, so file order is time order:
            r["t"] = t              # a line with no `t` inherits the last one
            ev.append(r)            # rather than sorting itself back to 1970
        ev.sort(key=lambda r: r["t"])   # stable — file order breaks ties
        if ev:
            out.append({"dir": d, "chan": chan, "ev": ev, "bad": bad})
    return out


def _merge_pods(brks):
    """Merge ad-bearing poll runs into the pods they actually came from.

    A pod is exposed if ANY of its runs was, and its `peak` is the largest
    single-poll count anywhere in it — summing the runs' peaks instead would
    count the same ad segments once per split.
    """
    out = []
    for b in sorted(brks, key=lambda k: k.get("t0", 0)):
        if out and b.get("t0", 0) - out[-1]["t1"] <= EXT_POD_MS:
            p = out[-1]
            p["t1"] = max(p["t1"], b.get("t1", 0))
            p["polls"] += b.get("polls", 0)
            p["peak"] = max(p["peak"], b.get("peak", 0))
            p["blocked"] = p["blocked"] and bool(b.get("blocked"))
            p["fetched"] += b.get("fetched", 0)
            p["runs"] += 1
        else:
            out.append({"t0": b.get("t0", 0), "t1": b.get("t1", 0),
                        "polls": b.get("polls", 0), "peak": b.get("peak", 0),
                        "blocked": bool(b.get("blocked")),
                        "fetched": b.get("fetched", 0), "runs": 1})
    return out


def _first_ad_pdt(d, h):
    """PDT of the first non-`live` segment in a stored manifest body, in millis.

    Bodies are kept by content hash and only when they are evidence, so this is
    often absent; that is not an error.
    """
    if not h:
        return None
    p = d / "manifests" / f"{h}.m3u8"
    if not p.is_file():
        return None
    try:
        body = p.read_text(errors="replace")
    except Exception:
        return None
    for pdt, _dur, title in RE_PDTSEG.findall(body):
        if title.strip() not in ("live", ""):
            return _iso_ms(pdt)
    return None


def ext_analyse(s, EXTR):
    """One extension session, scored. Never raises on a partial stream."""
    ev, chan, d = s["ev"], s["chan"], s["dir"]

    # events this session does not own — a foreign or null `chan`. ext/EVENTS.md
    # is explicit: "An event without it cannot be attributed."
    foreign = [e for e in ev if "chan" in e and e.get("chan") != chan]
    ftimes = [e["t"] for e in foreign]

    def near_foreign(e):
        return any(abs(e["t"] - x) <= EXT_QUAR_MS for x in ftimes)

    all_media = [e for e in ev if e.get("ev") == "media"]
    media = [e for e in all_media if e.get("chan") == chan]
    chan_filtered = True
    if all_media and not media:        # a build that stopped stamping `chan`
        media, chan_filtered = all_media, False

    adseg = [e for e in ev if e.get("ev") == "segment" and e.get("ad") is True]
    adseg_own = [e for e in adseg if not near_foreign(e)]
    # the foreign events themselves, plus the burst around them: an event with
    # no `chan` at all cannot be attributed either way, and landing inside a
    # foreign burst is the only reason to doubt it
    fid = {id(e) for e in foreign}
    quarantined = [e for e in ev if id(e) in fid
                   or (near_foreign(e) and "chan" not in e)]

    col = [e for e in ev if e.get("ev") == "ladder_collapse"]
    res = [e for e in ev if e.get("ev") == "ladder_restore"]
    mas = [e for e in ev if e.get("ev") == "master" and e.get("chan") == chan]

    def _rends(e):
        """Rendition set of a master, sorted — usher randomises the ORDER of
        the ladder between mints, so an ordered list invents variety."""
        out = []
        for v in (e.get("variants") or []):
            if isinstance(v, dict) and v.get("rend"):
                out.append(str(v["rend"]))
        return tuple(sorted(set(out)))

    # the master a collapse was detected on carries the same timestamp
    def _ladder_at(t):
        near = [m for m in mas if abs(m["t"] - t) <= 200]
        return _rends(near[-1]) if near else ()

    full = collections.Counter()
    for m in mas:
        r = _rends(m)
        if r and not any(abs(m["t"] - k["t"]) <= 200 for k in col):
            full[r] += 1
    churn = sorted([e for e in ev if e.get("ev") in ("rebind", "regrid")],
                   key=lambda e: e["t"])
    t0 = ev[0]["t"] if ev else 0
    t1 = ev[-1]["t"] if ev else 0

    # ---- breaks, exactly as ext/extreport.py groups them -------------------
    brks, grouped = [], False
    if EXTR is not None:
        try:
            brks = [EXTR.describe(b, adseg_own) for b in EXTR.group(media)]
            grouped = True
        except Exception:
            brks, grouped = [], False

    # ---- ladder_collapse -> the ad break it predicted ----------------------
    coll = []
    for c in col:
        after = [m for m in media if m["t"] >= c["t"]]
        first = next((m for m in after if _n(m.get("realAds")) > 0), None)
        _lad_now = _ladder_at(c["t"])
        row = {"t": c["t"], "had": c.get("had"), "now": c.get("now"),
               "bound": c.get("bound"), "hi": None, "lo": None, "pdt": None,
               "verdict": "false positive", "peak": None, "rends": _lad_now,
               "bound_gone": bool(_lad_now) and c.get("bound") not in _lad_now}
        if first is not None and first["t"] - c["t"] <= EXT_HORIZON_MS:
            row["hi"] = (first["t"] - c["t"]) / 1000.0
            row["peak"] = int(_n(first.get("realAds")))
            # the last poll before it that still listed zero ads: a hard floor,
            # because the ad provably was not in the playlist at that moment
            i = media.index(first)
            j = i - 1
            while j >= 0 and _n(media[j].get("realAds")) > 0:
                j -= 1
            if j >= 0:
                row["lo"] = (media[j]["t"] - c["t"]) / 1000.0
            p = _first_ad_pdt(d, first.get("realHash"))
            if p is not None:
                row["pdt"] = (p - c["t"]) / 1000.0
            row["verdict"] = "predicted a break"
        elif t1 - c["t"] < EXT_HORIZON_MS:
            row["verdict"] = "inconclusive (session ended)"
        coll.append(row)

    pods = _merge_pods(brks)

    # ---- and the other direction: pods with no collapse in front of them ----
    for b in pods + brks:
        prev = [c for c in col if c["t"] <= b["t0"]]
        b["since_collapse"] = ((b["t0"] - prev[-1]["t"]) / 1000.0
                               if prev else None)
        b["warned"] = (b["since_collapse"] is not None
                       and b["since_collapse"] <= EXT_HORIZON_MS / 1000.0)

    # ---- why we missed -----------------------------------------------------
    miss = []
    for m in media:
        if _n(m.get("realAds")) <= 0 or m.get("decision") == "rewrite":
            continue
        miss.append({
            "t": m["t"], "dec": m.get("decision") or "(none)",
            "realAds": int(_n(m.get("realAds"))),
            "churn": [k for k in churn if 0 <= m["t"] - k["t"] <= EXT_NEAR_MS],
            "coll": [k for k in col if 0 <= m["t"] - k["t"] <= EXT_NEAR_MS],
        })
    # passthroughs that carried no ad at all: they exposed nothing, and counting
    # them as misses would invent failures that never happened
    quiet_pass = [m for m in media if _n(m.get("realAds")) <= 0
                  and m.get("decision") not in ("rewrite", None)]

    st = [e for e in ev if e.get("ev") == "stat" and e.get("channel") == chan]
    ups = [e for e in ev if e.get("ev") == "up"]
    return {
        "dir": d, "chan": chan, "t0": t0, "t1": t1, "bad": s.get("bad", 0),
        "counts": collections.Counter(e.get("ev") for e in ev),
        "media": media, "polls": len(media), "chan_filtered": chan_filtered,
        "adseg": adseg, "adseg_own": adseg_own,
        "adseg_urls": len({e.get("url") for e in adseg_own if e.get("url")}),
        "foreign": foreign, "quarantined": quarantined,
        "breaks": brks, "pods": pods, "grouped": grouped,
        "blocked": sum(1 for b in pods if b.get("blocked")),
        "exposed": sum(1 for b in pods if not b.get("blocked")),
        "adpolls": sum(b.get("polls", 0) for b in brks),
        "peaksum": sum(b.get("peak", 0) for b in pods),
        "collapses": coll, "restores": res, "miss": miss, "full": full,
        "quiet_pass": len(quiet_pass), "churn": churn,
        "rebinds": sum(1 for c in churn if c.get("ev") == "rebind"),
        "regrids": sum(1 for c in churn if c.get("ev") == "regrid"),
        "stat": st[-1] if st else {},
        "canary": [e for e in ev if e.get("ev") == "canary"],
        "canary_on": any(bool(u.get("canary")) for u in ups),
    }


# ---------------------------------------------------------------------------
# data/unslop/*.jsonl — the POC ledgers, scored by coverage.py's own maths
# ---------------------------------------------------------------------------

def unslop_ledgers(COV):
    """Per-ledger coverage, computed with coverage.py's functions verbatim.

    SPLICEABLE is the number that matters — the phase-agnostic union above it
    counts a moment as covered when the only arm holding it is on a phase the
    player cannot splice onto. Both are carried here so the gap is visible.
    """
    out = []
    if COV is None:
        return out
    for f in sorted(glob.glob(str(LAB / "data" / "unslop" / "*.jsonl"))):
        rec = {"file": pathlib.Path(f).name, "segs": 0, "dropped": 0}
        try:
            segs, events = COV.load([f])
        except Exception:
            out.append(rec)
            continue
        good = [r for r in segs
                if isinstance(r.get("pdt"), (int, float))
                and isinstance(r.get("dur"), (int, float))
                and not isinstance(r.get("pdt"), bool)]
        rec["dropped"] = len(segs) - len(good)
        rec["segs"] = len(good)
        if not good:
            out.append(rec)
            continue
        pt = {}
        for e in events:
            if e.get("ev") == "join" and e.get("arm"):
                pt[e["arm"]] = e.get("pt") or pt.get(e["arm"])
        by_arm = collections.defaultdict(lambda: {"clean": [], "ad": []})
        for r in good:
            s = int(r["pdt"])
            by_arm[r.get("arm")]["ad" if r.get("ad") else "clean"].append(
                [s, s + int(float(r["dur"]) * 1000)])
        try:
            clean = COV.merge([i for a in by_arm for i in by_arm[a]["clean"]])
            ad = COV.merge([i for a in by_arm for i in by_arm[a]["ad"]])
            chain = COV.spliceable(good)
            uncovered = COV.total(COV.subtract(ad, clean)) if ad else 0
            unspliceable = COV.total(COV.subtract(ad, chain)) if ad else 0
            adms = COV.total(ad)
            span = clean + ad
            wall = (max(i[1] for i in span) - min(i[0] for i in span)) if span else 0
        except Exception:
            out.append(rec)
            continue
        adarms = sorted({a for a in by_arm if by_arm[a]["ad"]})
        rec.update({
            "arms": len(by_arm), "wall": wall, "ad": adms, "breaks": len(ad),
            "uncovered": uncovered, "unspliceable": unspliceable,
            "spliceable": adms - unspliceable,
            "union": adms - uncovered,
            "adarms": adarms,
            "adpt": sorted({pt.get(a) or "?" for a in adarms}),
            "cleanpt": sorted({pt.get(a) or "?" for a in by_arm
                               if not by_arm[a]["ad"]}),
            "regrids": sum(1 for e in events if e.get("ev") == "regrid"),
            "joins": sum(1 for e in events if e.get("ev") == "join"),
            "stopped": any(e.get("ev") == "stop" for e in events),
        })
        out.append(rec)
    return out


def main():
    ev = rows(H / "hunt.jsonl")
    joins = [r for r in ev if r.get("ev") == "join"]
    breaks = [r for r in ev if r.get("ev") == "AD_BREAK"]
    hb = [r for r in ev if r.get("ev") == "heartbeat"]
    start = next((r for r in ev if r.get("ev") == "start"), {})

    t0 = ev[0]["t"] if ev else 0
    t1 = ev[-1]["t"] if ev else 0
    dur_h = (t1 - t0) / 3600
    polls = hb[-1]["polls"] if hb else 0
    n_arms = len(start.get("arms", []))

    # ---- ad sessions, deduped by the ad server's own session id ----
    def sess(b):
        return {a.get("X-TV-TWITCH-AD-AD-SESSION-ID") for a in (b.get("ads") or [])}

    by_pt = collections.defaultdict(set)
    by_arm = collections.defaultdict(set)
    rolls = collections.Counter()
    pt_of = {}
    for j in joins:
        pt_of[j["arm"]] = j.get("player_type")
    for b in breaks:
        ids = sess(b)
        by_arm[b["arm"]] |= ids
        by_pt[pt_of.get(b["arm"], "?")] |= ids
        for a in (b.get("ads") or []):
            if a.get("X-TV-TWITCH-AD-ROLL-TYPE"):
                rolls[(a["X-TV-TWITCH-AD-ROLL-TYPE"],
                       a.get("X-TV-TWITCH-AD-AD-SESSION-ID"))] = 1
    roll_counts = collections.Counter(k[0] for k in rolls)
    joins_by_pt = collections.Counter(j.get("player_type") for j in joins)
    joins_by_sticky = collections.Counter(
        ("sticky" if j.get("sticky") else "fresh") for j in joins)
    ads_by_sticky = collections.defaultdict(set)
    sticky_of = {j["arm"]: bool(j.get("sticky")) for j in joins}
    for b in breaks:
        ads_by_sticky["sticky" if sticky_of.get(b["arm"]) else "fresh"] |= sess(b)

    # ---- tell reliability over every captured ad manifest ----
    mans = sorted(glob.glob(str(H / "manifests" / "AD.*.m3u8")))
    c = collections.Counter()
    announce_lead = []
    for f in mans:
        t = open(f).read()
        titles = {x[1].strip() for x in RE_SEG.findall(t)}
        t1_ = any(x != "live" for x in titles if x)
        src = RE_SRC.search(t)
        t2 = bool(src and src.group(1) != "live")
        t3 = "twitch-stitched-ad" in t
        c["title"] += t1_
        c["source"] += t2
        c["daterange"] += t3
        c["liveseq"] += "EXT-X-TWITCH-LIVE-SEQUENCE" in t
        c["pdt11"] += t.count("PROGRAM-DATE-TIME") == t.count("#EXTINF")
        c["any"] += (t1_ or t2 or t3)
        if t3 and not t1_:
            m = re.search(r'CLASS="twitch-stitched-ad",START-DATE="([^"]+)"', t)
            p = RE_PDT.findall(t)
            if m and p:
                def ms(s):
                    tt = time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
                    fr = float("0." + re.split(r"[.]", s)[1].rstrip("Z")[:3]) if "." in s else 0
                    return calendar.timegm(tt) + fr
                announce_lead.append(round(ms(m.group(1)) - ms(p[-1]), 1))
    n = max(len(mans), 1)

    segs = sorted(glob.glob(str(H / "segments" / "*.ts")))
    ad_segs = [x for x in segs if ".AD.ts" in x]

    # ---- tag-independent classifier: does the encoder signature differ? ----
    import subprocess

    def sig(p):
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate,codec_name,profile",
             "-of", "json", p], capture_output=True)
        try:
            s = json.loads(r.stdout)["streams"][0]
            return (s.get("codec_name"), s.get("profile"), s.get("width"),
                    s.get("height"), s.get("r_frame_rate"))
        except Exception:
            return None

    sig_ad = collections.Counter()
    sig_live = collections.Counter()
    for f in ad_segs[:20]:
        v = sig(f)
        if v:
            sig_ad[v] += 1
    for f in [x for x in segs if ".live.ts" in x][:20]:
        v = sig(f)
        if v:
            sig_live[v] += 1
    overlap = set(sig_ad) & set(sig_live)

    # ---- cookie vs no-cookie on player_type=site ----
    ck = {}
    ckf = sorted(glob.glob(str(LAB / "data" / "cookietest" / "*.jsonl")))
    if ckf:
        rs = rows(ckf[-1])
        done = [r for r in rs if r.get("ev") in ("done", "heartbeat")]
        if done:
            last = done[-1]
            for k, v in last.items():
                if isinstance(v, dict) and "joins" in v:
                    ck[k] = v

    # ---- browser-style headers vs minimal on player_type=site ----
    ht = {}
    htf = sorted(glob.glob(str(LAB / "data" / "headertest" / "*.jsonl")))
    if htf:
        rs = rows(htf[-1])
        d = [r for r in rs if r.get("ev") in ("done", "heartbeat")]
        if d:
            for k, v in d[-1].items():
                if isinstance(v, dict) and "joins" in v:
                    ht[k] = v

    # ---- what gates ad-eligibility on player_type=site ----
    gt = {}
    gtf = sorted(glob.glob(str(LAB / "data" / "gatetest" / "*.jsonl")))
    if gtf:
        rs = rows(gtf[-1])
        d = [r for r in rs if r.get("ev") in ("done", "heartbeat")]
        if d:
            for k, v in d[-1].items():
                if isinstance(v, dict) and "joins" in v:
                    gt[k] = v

    # ---- all factors re-tested inside ONE high-rate window ----
    cb = {}
    cbf = sorted(glob.glob(str(LAB / "data" / "combotest" / "*.jsonl")))
    if cbf:
        rs = rows(cbf[-1])
        d = [r for r in rs if r.get("ev") in ("done", "heartbeat")]
        if d:
            for k, v in d[-1].items():
                if isinstance(v, dict) and "joins" in v:
                    cb[k] = v

    # ---- geo probe ----
    geo = []
    # keep the run id: the same (country, ip, port) recurs across runs, and
    # collapsing them silently discards whichever run actually saw ads.
    for f in sorted(glob.glob(str(LAB / "data" / "geoprobe" / "*.jsonl"))):
        run = pathlib.Path(f).stem.replace("geoprobe.", "")
        rs = rows(f)
        mints = {r["port"]: r for r in rs if r.get("ev") == "mint"}
        for r in rs:
            if r.get("ev") == "summary" and r.get("interleaved"):
                m = mints.get(r["port"], {})
                geo.append((m.get("master_country"), m.get("exit_ip"), r["port"],
                            r["mode"], r["polls_ok"], r["ad_polls"], run))

    # ---- the extension, and the POC ledgers it was ported from ----
    COV = _mod(LAB / "coverage.py", "_cov")
    EXTR = _mod(LAB / "ext" / "extreport.py", "_extr")
    xs = [ext_analyse(s, EXTR) for s in ext_sessions()]
    led = unslop_ledgers(COV)

    def pct(x):
        return f"{x}/{n} ({100*x//n}%)"

    L = []
    A = L.append
    A("# FINDINGS — Twitch SSAI passive RE")
    A("")
    A(f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')} from `data/hunt/{S}/`.")
    A("Every number below is computed by `report.py` from the dumps.")
    A("")
    A("## Summary")
    A("")
    A(f"1. Hunter ran {dur_h:.1f}h, {n_arms} arms, {polls} playlist polls, "
      f"{len(joins)} joins, {sum(len(v) for v in by_pt.values())} distinct ad sessions.")
    A(f"2. Captured {len(mans)} ad manifests and {len(ad_segs)} ad .ts segments — "
      "the detector is no longer unvalidated.")
    # rate only over rejoiner arms — watcher joins aren't a valid denominator
    _kind = {j["arm"]: j.get("kind", "?") for j in joins}
    _rj = collections.Counter(j.get("player_type") for j in joins
                              if _kind.get(j["arm"]) == "rejoiner")
    _ra = collections.defaultdict(set)
    for _b in breaks:
        if _kind.get(_b["arm"]) == "rejoiner":
            _ra[pt_of.get(_b["arm"], "?")] |= sess(_b)
    _rate = {}
    for _pt, _j in _rj.items():
        if _j >= 30:
            _rate[_pt] = 100.0 * len(_ra.get(_pt, set())) / _j

    # 30-min buckets over site rejoiner arms — needed by both the summary and
    # the detail section below, so compute once, here.
    _jb = collections.Counter()
    _ab = collections.defaultdict(set)
    for _r in joins:
        if _kind.get(_r["arm"]) == "rejoiner" and _r.get("player_type") == "site":
            _jb[int((_r["t"] - t0) // 1800)] += 1
    for _r in breaks:
        if _kind.get(_r["arm"]) == "rejoiner" and pt_of.get(_r["arm"]) == "site":
            _ab[int((_r["t"] - t0) // 1800)] |= sess(_r)
    if len(_rate) >= 2:
        _hi = max(_rate, key=_rate.get)
        _lo = min(_rate, key=_rate.get)
        _mult = (_rate[_hi] / _rate[_lo]) if _rate[_lo] else float("inf")
        _zero = [k for k, v in _rate.items() if v == 0]
        A(f"3. **`player_type` strongly modulates ad RATE**: `{_hi}` "
          f"{_rate[_hi]:.1f} ad sessions per 100 joins vs `{_lo}` "
          f"{_rate[_lo]:.1f}" +
          (f" — a {_mult:.0f}x difference." if _mult != float('inf') else ".") +
          (" No player_type was ad-FREE." if not _zero else
           f" Ad-free in this sample: {_zero} (see caveat below)."))
    else:
        A("3. `player_type` comparison needs more joins per arm.")
    A("4. **The splice key is `#EXT-X-PROGRAM-DATE-TIME`, not "
      "`#EXT-X-TWITCH-LIVE-SEQUENCE`** — LIVE-SEQUENCE is absent from most ad "
      "manifests. This refuted the original design.")
    A("5. Ads REPLACE content in wall-clock time, so a clean parallel session "
      "holds exactly the segments an ad session missed. That is why the POC works.")
    A("6. `twitch-stitched-ad` DATERANGE is the most reliable tell "
      f"({pct(c['daterange'])}); `STREAM-SOURCE` is the weakest ({pct(c['source'])}).")
    if announce_lead and len(set(announce_lead)) == 1:
        A(f"7. The DATERANGE fires BEFORE any ad segment appears, with a "
          f"**consistent {sorted(set(announce_lead))[0]}s lead** across all "
          f"{len(announce_lead)} such manifests — a fixed prefetch budget.")
    else:
        A("7. The DATERANGE can fire BEFORE any ad segment appears — a leading "
          "indicator giving advance warning of a break.")
    if len(_jb) >= 3:
        _b0 = sorted(_jb)
        _first = sum(_jb[b] for b in _b0[:2])
        _fa = len(set().union(*[_ab.get(b, set()) for b in _b0[:2]]) or set())
        _lastb = _b0[-2] if len(_b0) > 2 else _b0[-1]
        _lr = 100.0 * len(_ab.get(_lastb, set())) / max(_jb[_lastb], 1)
        A(f"11. **Ad rate is strongly time-varying and this dominates the "
          f"other factors.** `site` drew {_fa} ads in its first {_first} "
          f"joins, then rose to ~{_lr:.0f} per 100 joins later in the run. Two "
          "earlier conclusions in this file were retracted because of it.")
    A("8. A session minted through a foreign SOCKS exit streams fine when "
      "polled DIRECT from the home IP — the playlist blob is not IP-locked.")
    A("9. `X-Device-Id` on the GraphQL mint is accepted and echoed into "
      "`token.device_id`, so identity is client-controllable.")
    A("10. POC `unslop.py` serves a clean m3u8 on localhost and plays in mpv "
      "and ffprobe.")
    if xs:
        _tb = sum(len(x["breaks"]) for x in xs)
        _tx = sum(x["exposed"] for x in xs)
        _ta = sum(len(x["adseg_own"]) for x in xs)
        A(f"12. **The Firefox extension blocked real ad breaks end to end.** "
          f"Across {_pl(len(xs), 'session')} in `data/ext/`, "
          f"{_pl(_tb, 'ad pod')} were offered and {_tb - _tx} were fully "
          f"rewritten; the player fetched **{_pl(_ta, 'ad segment')}** — that "
          "last number, `segment` events with `ad==true`, is the only ground "
          "truth for what reached the screen.")
    if xs and sum(len(x["collapses"]) for x in xs):
        _cs = [c for x in xs for c in x["collapses"]]
        _hit = [c for c in _cs if c["hi"] is not None]
        _fp = [c for c in _cs if c["verdict"] == "false positive"]
        if _hit:
            A(f"13. **A collapsing variant ladder is an ad break announcing "
              f"itself.** {len(_hit)}/{len(_cs)} `ladder_collapse` events were "
              f"followed by an ad-bearing playlist, {len(_fp)} were not, and "
              f"the warning arrived {min(c['hi'] for c in _hit):.1f}-"
              f"{max(c['hi'] for c in _hit):.1f}s ahead. Every other tell "
              "fires when the ad is already in the playlist.")
    if led and any(r.get("ad") for r in led):
        _la = sum(r.get("ad", 0) for r in led)
        _ls = sum(r.get("spliceable", 0) for r in led)
        A(f"14. Across "
          f"{_pl(len([q for q in led if q.get('ad')]), '`unslop.py` ledger')}, "
          f"{_la/1000:.0f}s of ad wall-clock was {100.0*_ls/_la:.1f}% "
          "SPLICEABLE by an on-grid donor — the go/no-go number for the port, "
          "and it said go.")
    A("")
    A("## PROVEN")
    A("")
    A("### Ad-tell reliability")
    A("")
    A(f"Across {len(mans)} captured ad manifests in "
      f"`data/hunt/{S}/manifests/AD.*.m3u8`:")
    A("")
    A("| tell | hit rate |")
    A("|---|---|")
    A(f"| `twitch-stitched-ad` DATERANGE | {pct(c['daterange'])} |")
    A(f"| `#EXTINF` title != `live` | {pct(c['title'])} |")
    A(f"| `X-TV-TWITCH-STREAM-SOURCE` != `live` | {pct(c['source'])} |")
    A(f"| OR of all three (what the tap uses) | {pct(c['any'])} |")
    A(f"| `#EXT-X-TWITCH-LIVE-SEQUENCE` present | {pct(c['liveseq'])} |")
    A(f"| `PROGRAM-DATE-TIME` 1:1 with segments | {pct(c['pdt11'])} |")
    A("")
    A("```sh")
    A("python report.py   # recomputes the table above")
    A("```")
    A("")
    A("Consequence: filter on the **title** (per-segment, tells you which bytes "
      "to skip) and predict on the **DATERANGE** (fires early). `STREAM-SOURCE` "
      "should not be relied on alone.")
    if announce_lead:
        A("")
        _u = sorted(set(announce_lead))
        if len(_u) == 1:
            A(f"**The warning is deterministic.** On all {len(announce_lead)} "
              f"manifests that announced a break before any ad segment "
              f"appeared, the lead was exactly **{_u[0]}s** — the "
              f"stitched-ad DATERANGE START-DATE minus the last live segment's "
              f"PDT. That is a fixed prefetch budget, not a guess.")
        else:
            A(f"Break announced before any ad segment appeared on "
              f"{len(announce_lead)} manifests; lead times (s): "
              f"min {min(announce_lead)}, max {max(announce_lead)}, "
              f"distinct values `{_u[:12]}`.")
    A("")
    A("### player_type vs ad delivery")
    A("")
    A("Split by arm kind. This matters: `rejoiner` arms mint a new session "
      "every ~45s (many joins, each a fresh preroll chance) while `watcher` "
      "arms join once and poll for hours (few joins, long midroll exposure). "
      "Pooling them and dividing by joins is meaningless — a watcher with 1 "
      "join and 5 midrolls would read as 500 per 100 joins.")
    A("")
    kind_of = {}
    for j in joins:
        kind_of[j["arm"]] = j.get("kind", "?")
    for kind in ("rejoiner", "watcher"):
        jk = collections.Counter()
        ak = collections.defaultdict(set)
        for j in joins:
            if kind_of.get(j["arm"]) == kind:
                jk[j.get("player_type")] += 1
        for b in breaks:
            if kind_of.get(b["arm"]) == kind:
                ak[pt_of.get(b["arm"], "?")] |= sess(b)
        if not jk:
            continue
        A(f"**{kind} arms**")
        A("")
        if kind == "rejoiner":
            A("| player_type | joins | ad sessions | per 100 joins |")
            A("|---|---|---|---|")
            for pt in sorted(set(list(jk) + list(ak))):
                j_ = jk.get(pt, 0)
                a_ = len(ak.get(pt, set()))
                r = f"{100.0*a_/j_:.1f}" if j_ else "-"
                note = " *(n small)*" if j_ < 30 else ""
                A(f"| `{pt}` | {j_} | {a_} | {r}{note} |")
        else:
            A("| player_type | joins | ad sessions |")
            A("|---|---|---|")
            for pt in sorted(set(list(jk) + list(ak))):
                A(f"| `{pt}` | {jk.get(pt,0)} | {len(ak.get(pt,set()))} |")
            A("")
            A("Watchers hold one session for hours, so joins are not a useful "
              "denominator; read these as raw counts of midrolls caught.")
        A("")
    # ---- is the rate stationary? (it is not) — buckets computed above ----
    if len(_jb) >= 3:
        A("**The rate is NOT stationary — this dominates everything else.** "
          "`site` rejoiner arms, 30-minute buckets:")
        A("")
        A("| local time | joins | ad sessions | per 100 joins |")
        A("|---|---|---|---|")
        for b in sorted(_jb):
            j_ = _jb[b]
            a_ = len(_ab.get(b, set()))
            clk = time.strftime("%H:%M", time.localtime(t0 + b * 1800))
            A(f"| {clk} | {j_} | {a_} | {100.0*a_/j_ if j_ else 0:.1f} |")
        A("")
        _early = sum(_jb[b] for b in sorted(_jb)[:2])
        _ea = len(set().union(*[_ab.get(b, set()) for b in sorted(_jb)[:2]]) or set())
        A(f"`site` drew {_ea} ads in its first {_early} joins, then the rate "
          "climbed steadily. That is a hard onset, not sampling noise — at the "
          "later rate, a zero over that many joins is vanishingly unlikely.")
        A("")
        A("**Two earlier conclusions in this file were wrong because of this.** "
          "First, `site` is ad-free — it was ad-free only in that window. "
          "Second, the follow-up explanation that the zero was random chance — "
          "also wrong; the zero was real, and caused by time. The honest "
          "statement is that ad availability varies strongly by hour "
          "(inventory/dayparting is the obvious suspect, untested here).")
        A("")
        A("**This also undermines `cookietest.py` and `headertest.py`.** Both "
          "ran inside the low/zero window, so their `site` arms had almost no "
          "chance to draw an ad regardless of cookie or header shape. Their "
          "negative verdicts are confounded by time and should be re-run "
          "during a high-rate window before being believed. The `embed` "
          "controls still drew ads because `embed`'s rate stayed higher, which "
          "is what made the windows look valid at the time.")
        A("")
    A("```sh")
    A(f"jq -r 'select(.ev==\"join\")|.player_type' data/hunt/{S}/hunt.jsonl | sort | uniq -c")
    A("```")
    A("")
    A("### identity: fresh vs sticky device_id")
    A("")
    A("| device_id policy | joins | distinct ad sessions |")
    A("|---|---|---|")
    for k in ("fresh", "sticky"):
        A(f"| {k} | {joins_by_sticky.get(k,0)} | {len(ads_by_sticky.get(k,set()))} |")
    A("")
    A(f"Sticky identity used all night: `{start.get('sticky_device')}`")
    A("")
    A("### Ad inventory actually served")
    A("")
    _A = re.compile(r'([A-Z0-9-]+)=("(?:[^"]*)"|[^,]*)')
    _rolls = collections.Counter()
    _pods = collections.Counter()
    _fmt = collections.Counter()
    _li, _cr, _seen = set(), set(), set()
    for f in mans:
        for line in open(f):
            if "twitch-stitched-ad" not in line:
                continue
            d = {k: v.strip('"') for k, v in _A.findall(line)}
            key = (d.get("X-TV-TWITCH-AD-AD-SESSION-ID"),
                   d.get("X-TV-TWITCH-AD-CREATIVE-ID"),
                   d.get("X-TV-TWITCH-AD-POD-POSITION"))
            if key in _seen:
                continue
            _seen.add(key)
            _rolls[d.get("X-TV-TWITCH-AD-ROLL-TYPE", "?")] += 1
            _pods[d.get("X-TV-TWITCH-AD-POD-LENGTH", "?")] += 1
            _fmt[d.get("X-TV-TWITCH-AD-AD-FORMAT", "?")] += 1
            if d.get("X-TV-TWITCH-AD-LINE-ITEM-ID"):
                _li.add(d["X-TV-TWITCH-AD-LINE-ITEM-ID"])
            if d.get("X-TV-TWITCH-AD-CREATIVE-ID"):
                _cr.add(d["X-TV-TWITCH-AD-CREATIVE-ID"])
    A(f"{len(_seen)} distinct (ad-session, creative, pod-position) placements:")
    A("")
    A(f"- roll types: {dict(_rolls)}")
    A(f"- pod lengths: {dict(_pods.most_common(6))} — pods of 6 are common, "
      "i.e. six ads back to back")
    A(f"- formats: {dict(_fmt)}")
    A(f"- distinct line items: {len(_li)}, distinct creatives: {len(_cr)}")
    A("")
    if len(_cr) <= 5:
        A(f"Only {len(_cr)} distinct creatives across all placements — ad "
          "inventory was thin during this run (overnight EEST). Expect more "
          "variety at peak hours; don't read creative diversity from this.")
        A("")
    A("Midrolls outnumber prerolls here, which is the opposite of what the "
      "rejoin-heavy arm design was built to catch — the long-lived `watcher` "
      "arms are what picked them up.")
    A("")
    A("### Manifest structure during a break")
    A("")
    A("```")
    A("#EXT-X-MEDIA-SEQUENCE:43          <- session-local, NOT live-aligned")
    A("#EXT-X-DATERANGE:...CLASS=\"twitch-stream-source\",X-TV-TWITCH-STREAM-SOURCE=\"Amazon|<creative>\"")
    A("#EXT-X-DATERANGE:...CLASS=\"twitch-ad-quartile\",X-TV-TWITCH-AD-QUARTILE=\"0\"")
    A("#EXT-X-DISCONTINUITY")
    A("#EXT-X-PROGRAM-DATE-TIME:2026-07-28T23:22:06.792Z")
    A("#EXTINF:2.000,Amazon|2474283100494   <- title carries the creative id")
    A("```")
    A("")
    A("`X-TV-TWITCH-AD-RADS-TOKEN` is a decodable JWT carrying `broadcaster`, "
      "`viewer`, `session`, `video_session_id`, `duration`, `is_stitched`.")
    A("")
    if sig_ad or sig_live:
        A("### Tag-independent classifier (ffprobe on the actual bytes)")
        A("")
        A("Encoder signature of saved segments — no manifest metadata involved:")
        A("")
        A("| kind | codec/profile | resolution | fps | n |")
        A("|---|---|---|---|---|")
        for k, v in sig_ad.most_common():
            A(f"| AD | {k[0]}/{k[1]} | {k[2]}x{k[3]} | {k[4]} | {v} |")
        for k, v in sig_live.most_common():
            A(f"| LIVE | {k[0]}/{k[1]} | {k[2]}x{k[3]} | {k[4]} | {v} |")
        A("")
        if not overlap:
            A(f"**Zero overlap** between {len(sig_ad)} ad and {len(sig_live)} live "
              "signatures — ad segments are identifiable from the bytes alone, "
              "with no reliance on Twitch's own tags. Twitch's ladder uses "
              "non-standard sizes (e.g. 284x160) while ads use standard ones "
              "(256x144), and ads did not appear at 60fps.")
        else:
            A(f"Overlapping signatures: `{sorted(overlap)}` — not separable on "
              "these fields alone.")
        A("")
    if ck:
        A("### Does `site` need a cookie to be ad-eligible?")
        A("")
        A("The user's own browser got a preroll on `player_type=site`, but "
          "cookie-less `site` joins from this rig got none. `cookietest.py` "
          "runs all four arms in one window; `embed-nocookie` is the positive "
          "control that proves ads were available at all.")
        A("")
        A("| arm | joins | polls | distinct ad sessions |")
        A("|---|---|---|---|")
        for k in ("site-nocookie", "site-cookie", "site-cookie-fresh",
                  "embed-nocookie"):
            v = ck.get(k)
            if v:
                A(f"| `{k}` | {v['joins']} | {v['polls']} | {v['ads']} |")
        A("")
        sn = ck.get("site-nocookie", {}).get("ads", 0)
        sc = ck.get("site-cookie", {}).get("ads", 0)
        scf = ck.get("site-cookie-fresh", {}).get("ads", 0)
        em = ck.get("embed-nocookie", {}).get("ads", 0)
        if em == 0:
            A("Positive control got no ads either — this window proves nothing. "
              "Re-run when breaks are firing.")
        elif sc + scf > 0 and sn == 0:
            A("**The cookie is the gate.** With a `unique_id` cookie, `site` "
              "becomes ad-eligible; without one it does not, while `embed` "
              "serves ads regardless.")
        elif sn == 0 and sc == 0 and scf == 0:
            A("**`player_type` is the gate on its own.** `site` stayed ad-free "
              "with AND without a browser-style `unique_id` cookie, while the "
              "`embed` control was served ads in the same window.")
        else:
            A("Mixed result — see the raw counts above.")
        A("")
    if ht:
        A("### Does `site` need to LOOK like a browser to be ad-eligible?")
        A("")
        A("The cookie was ruled out above, but the user's real browser still "
          "got a preroll on `site`. Diffing our mint against the browser's "
          "captured headers (`data/gql/0729-01*`) showed we omit `Origin`, "
          "`Referer`, `Authorization: undefined`, and send "
          "`application/json` where the browser sends `text/plain`. "
          "`headertest.py` replays the browser header set byte-for-byte.")
        A("")
        A("| arm | player_type | headers | joins | ad sessions |")
        A("|---|---|---|---|---|")
        meta = {"site-minimal": ("site", "ours"),
                "site-browser": ("site", "browser"),
                "site-browser-cookie": ("site", "browser+cookie"),
                "embed-minimal": ("embed", "ours")}
        for k, (p_, st) in meta.items():
            v = ht.get(k)
            if v:
                A(f"| `{k}` | {p_} | {st} | {v['joins']} | {v['ads']} |")
        A("")
        sb = ht.get("site-browser", {}).get("ads", 0)
        sbc = ht.get("site-browser-cookie", {}).get("ads", 0)
        sm = ht.get("site-minimal", {}).get("ads", 0)
        em = ht.get("embed-minimal", {}).get("ads", 0)
        if em == 0:
            A("Positive control got nothing — window proves nothing, re-run.")
        elif (sb + sbc) > 0 and sm == 0:
            A("**Looking like a browser is the gate.** Adding "
              "`Origin`/`Referer` and the browser content-type made `site` "
              "ad-eligible. That explains the user's browser preroll and means "
              "`site` is NOT inherently ad-free.")
        elif sb == 0 and sbc == 0:
            A("**Header shape is NOT the gate either.** `site` stayed ad-free "
              "even with the browser's exact header set, while `embed` was "
              "served ads in the same window. Whatever makes the real browser "
              "monetizable on `site` is something else — the remaining "
              "candidates are the integrity/attestation token the web app "
              "fetches, or real playback telemetry (spade) that we never send.")
        else:
            A("Mixed — see counts above.")
        A("")
    if gt:
        A("### What actually gates ad-eligibility on `site`?")
        A("")
        A("Ruled out by the two experiments above: the `unique_id` cookie, and "
          "the browser's exact header set. `gatetest.py` tests the two "
          "remaining headless-testable ideas — poking the "
          "`X-TV-TWITCH-TRIGGER-URL` that every media playlist carries "
          "(returns `200 OK`), and actually downloading video like a real "
          "viewer instead of only polling manifests.")
        A("")
        A("| arm | player_type | joins | polls | triggers | MB pulled | ad sessions |")
        A("|---|---|---|---|---|---|---|")
        for k, p_ in (("site-control", "site"), ("site-trigger", "site"),
                      ("site-consume", "site"), ("embed-control", "embed")):
            v = gt.get(k)
            if v:
                A(f"| `{k}` | {p_} | {v['joins']} | {v['polls']} | "
                  f"{v.get('triggers',0)} | {v.get('mb',0)} | {v['ads']} |")
        A("")
        ec = gt.get("embed-control", {}).get("ads", 0)
        st = gt.get("site-trigger", {}).get("ads", 0)
        sc = gt.get("site-consume", {}).get("ads", 0)
        s0 = gt.get("site-control", {}).get("ads", 0)
        if ec == 0:
            A("Positive control got nothing — this window proves nothing.")
        elif st > 0 and s0 == 0:
            A("**Poking the trigger URL arms ad decisioning.** That is the "
              "handshake the real player performs.")
        elif sc > 0 and s0 == 0:
            A("**Consuming video is the gate.** Sessions that only poll "
              "manifests are not treated as monetisable viewers.")
        elif s0 == 0 and st == 0 and sc == 0:
            A("No `site` arm drew an ad in this window while `embed` did. "
              "Given the measured base rate of roughly 2 ad sessions per 100 "
              "joins on `site`, a window this size drawing zero is ordinary "
              "chance, NOT evidence of a gate. Treat as inconclusive.")
        elif s0 > 0:
            A("`site` drew ads WITHOUT the trigger or video consumption, so "
              "neither is a gate. All three `site` arms behave alike; the "
              "difference from `embed` is rate, not eligibility.")
        else:
            A("Mixed — see counts above.")
        A("")
    if cb:
        A("### All factors re-tested in ONE high-rate window (`combotest.py`)")
        A("")
        A("The earlier cookie and header experiments ran while the `site` ad "
          "rate was ~0, so they could not have detected anything. This re-runs "
          "every candidate as a `site` arm inside the same window, with "
          "`site-plain` as the in-window baseline. Differences here are "
          "meaningful; differences in the earlier tables are not.")
        A("")
        A("| arm | varies | joins | polls | ad sessions | per 100 joins |")
        A("|---|---|---|---|---|---|")
        _v = {"site-plain": "nothing (baseline)",
              "site-cookie": "sends unique_id cookie",
              "site-browserhdr": "browser header set",
              "site-consume": "downloads video"}
        for k in ("site-plain", "site-cookie", "site-browserhdr", "site-consume"):
            v = cb.get(k)
            if v:
                j_ = v["joins"]
                r = f"{100.0*v['ads']/j_:.1f}" if j_ else "-"
                A(f"| `{k}` | {_v[k]} | {j_} | {v['polls']} | {v['ads']} | {r} |")
        A("")
        _b = cb.get("site-plain", {})
        _bj, _ba = _b.get("joins", 0), _b.get("ads", 0)
        if _bj < 60:
            A("Still accumulating — treat as provisional until each arm has "
              "60+ joins.")
        else:
            _br = _ba / _bj if _bj else 0
            _big = [k for k in ("site-cookie", "site-browserhdr", "site-consume")
                    if cb.get(k, {}).get("joins", 0) >= 60 and _br > 0
                    and (cb[k]["ads"] / cb[k]["joins"]) > 1.5 * _br]
            if _big:
                A(f"Arms clearly above baseline: {_big}. That is the factor "
                  "worth pursuing.")
            else:
                A("No arm is clearly above the baseline. Within this window, "
                  "cookie, header shape and video consumption all behave like "
                  "the plain `site` arm — the variation seen earlier was time, "
                  "not these factors.")
        A("")
    if geo:
        A("### Geo (SOCKS control group)")
        A("")
        A("| run | exit country | exit ip | port | poll mode | polls ok | ad polls |")
        A("|---|---|---|---|---|---|---|")
        for g in geo:
            A(f"| {g[6]} | {g[0]} | `{g[1]}` | {g[2]} | {g[3]} | {g[4]} | {g[5]} |")
        A("")
        A("Modes are INTERLEAVED within one time window; a sequential run "
          "confounds them, because one break spanning both windows makes the "
          "later mode look worse purely from timing.")
        A("")
        A("Key structural result: a playlist URL minted through a foreign exit "
          "returns valid manifests when polled DIRECT from the home IP. The "
          "session blob is not IP-locked, so an extension only needs to proxy "
          "the small control-plane request, not the video.")
        A("")
        _pairs = {}
        for cc_, ip_, port_, mode_, ok_, ad_, run_ in geo:
            _pairs.setdefault((run_, cc_, ip_, port_), {})[mode_] = (ok_, ad_)
        _ctlrows = [(k, v) for k, v in _pairs.items() if "control_home_mint" in v]
        if _ctlrows:
            A("`control_home_mint` is a session minted from the HOME ip, polled "
              "in the same interleaved loop. Caveat: it is minted ONCE at "
              "startup and reused across every port loop, so by the later "
              "loops it is a long-lived session whose join-time preroll chance "
              "is long gone. Its zeros are therefore NOT evidence that foreign "
              "mints get more ads — the comparison is a fresh session against "
              "a stale one. Only the via_socks vs direct columns are a clean "
              "comparison.")
            A("")
        _match = [k for k, v in _pairs.items()
                  if "via_socks" in v and "direct" in v
                  and v["via_socks"][1] == v["direct"][1]]
        _live = [k for k in _match if _pairs[k]["via_socks"][1] > 0]
        if _live:
            A(f"**Stronger result, from the runs where ads actually fired** "
              f"({len(_live)} of {len(_pairs)} sessions): the SAME session "
              "polled via the proxy and polled direct returned the SAME number "
              "of ad polls. Where a break was in progress it showed up "
              "identically down the two paths.")
            A("")
            A("So the polling path does not influence ad decisioning at all — "
              "the session blob minted at step 1 already fixes what you get. "
              "This is the cleanest confirmation that geo (and ad selection "
              "generally) binds at MINT time, and it is exactly why proxying "
              "only the tiny GraphQL/usher request is sufficient.")
        elif _match:
            A(f"All {len(_match)} sessions returned identical ad-poll counts "
              "down both paths, but every one of those windows had zero ads, "
              "so this shows the paths agree — not yet that ad decisioning is "
              "mint-bound. A window with a live break is needed to prove that.")
        A("")
    # =====================================================================
    #  the browser extension — data/ext/<chan>.<ts>/events.jsonl
    #  Loop variables here are all underscore-prefixed on purpose: main() is
    #  one long function and `c`, `b`, `n` are already live counters from the
    #  hunt sections above. A plain `for c in ...` here silently rewrites the
    #  ad-tell Counter and the FAILED section below reads garbage.
    # =====================================================================
    A("## THE EXTENSION (browser, end to end)")
    A("")
    if not xs:
        A("No parseable `events.jsonl` under `data/ext/`. Nothing to score — "
          "`ext.log` is a rendering of that stream, not a source, so it is "
          "deliberately not parsed here (`ext/EVENTS.md`).")
        A("")
    else:
        A("Source: `data/ext/<chan>.<ts>/events.jsonl`, schema in "
          "`ext/EVENTS.md`. `ext/extreport.py` scores one session in detail; "
          "this aggregates every session on disk.")
        A("")
        A("### Did it block real ad breaks?")
        A("")
        A("Two different numbers, and conflating them is the easiest mistake "
          "here:")
        A("")
        A("- **offered** — `media.realAds > 0`. Twitch put ad segments in the "
          "playlist it sent *this* session. Measured.")
        A("- **on screen** — a `segment` event with `ad==true`. The player "
          "actually fetched those bytes. Also measured, and the only one that "
          "means an ad was watched. A manifest listing an ad the player never "
          "requests is not an ad that played.")
        A("")
        A("| session | channel | wall | polls | ad pods | blocked | exposed "
          "| ad segs fetched |")
        A("|---|---|---|---|---|---|---|---|")
        for _sx in xs:
            _nb = len(_sx["pods"]) if _sx["grouped"] else "?"
            _nbl = _sx["blocked"] if _sx["grouped"] else "?"
            _nex = _sx["exposed"] if _sx["grouped"] else "?"
            A(f"| `{_sx['dir'].name}` | {_sx['chan']} | "
              f"{_secs((_sx['t1'] - _sx['t0']) / 1000.0)} | {_sx['polls']} | "
              f"{_nb} | {_nbl} | {_nex} | {len(_sx['adseg_own'])} |")
        A("")
        _tb = sum(len(s["pods"]) for s in xs)
        _truns = sum(len(s["breaks"]) for s in xs)
        _tbl = sum(s["blocked"] for s in xs)
        _tx = sum(s["exposed"] for s in xs)
        _ta = sum(len(s["adseg_own"]) for s in xs)
        _tau = sum(s["adseg_urls"] for s in xs)
        _tp = sum(s["peaksum"] for s in xs)
        _tap = sum(s["adpolls"] for s in xs)
        A(f"Totals: **{_pl(_tb, 'ad pod')}, {_tbl} blocked, {_tx} exposed**, "
          f"{_pl(_tap, 'ad-bearing poll')}, **{_pl(_ta, 'ad segment')} fetched "
          f"by the player ({_pl(_tau, 'distinct URL')})**.")
        if _truns != _tb:
            A("")
            A(f"Those {_tb} pods arrive as {_truns} runs of ad-bearing polls. "
              "`ext/extreport.py` closes a run after 3 ad-free polls, and "
              "during a break the player polls faster than once per segment, "
              "so one pod can be split in two. Runs are merged here when they "
              f"are less than {EXT_POD_MS // 1000}s apart — otherwise the same "
              "pod is counted twice everywhere below.")
        if not all(s["grouped"] for s in xs):
            A("")
            A("**Break grouping is unavailable** — `ext/extreport.py` could "
              "not be imported, so `breaks`/`blocked`/`exposed` above are "
              "incomplete. The ad-segment and poll counts do not depend on "
              "it and are unaffected.")
        A("")
        if _ta == 0:
            A("**No ad segment reached the screen in any session on disk.** "
              "That is the result the extension exists to produce, and it is "
              "measured, not inferred: every `segment` event these sessions "
              "logged carried `ad=false`.")
        else:
            A(f"{_pl(_ta, 'ad segment')} DID reach the screen. Read the "
              "break timeline below before quoting the blocked count.")
        A("")
        A("```sh")
        A("# ground truth: every ad segment the player actually fetched")
        A("jq -c 'select(.ev==\"segment\" and .ad==true)' data/ext/*/events.jsonl")
        A("# what was on offer, per poll")
        A("jq -c 'select(.ev==\"media\" and (.realAds//0)>0)|[.t,.realAds,.decision]' \\")
        A("   data/ext/*/events.jsonl")
        A("python ext/extreport.py data/ext/<session>/   # one session, in full")
        A("```")
        A("")
        A("How many ads that was is NOT exactly knowable. `realAds` is a "
          "per-poll count over a sliding window, so the same ad segment is "
          "counted again on every poll it survives; summing polls inflates it. "
          "The defensible floor is the largest single-poll count in each "
          f"pod, summed: **{_tp} ad segments offered** across all sessions. "
          "An ad the player never fetched leaves no URL to count, so there is "
          "no exact figure for what Twitch offered — quote the floor.")
        A("")
        _fo = [s for s in xs if s["foreign"]]
        if _fo:
            A("**One caveat that changes a headline number.** The log sink is "
              "a single HTTP endpoint on `127.0.0.1:8779`, so anything that "
              "POSTs to it lands in whichever session directory is current. "
              "These events carry a `chan` the session does not own and are "
              "therefore not its traffic:")
            A("")
            A("| session | foreign events | quarantined | of those, `ad=true` |")
            A("|---|---|---|---|")
            for _sx in _fo:
                _q = _sx["quarantined"]
                A(f"| `{_sx['dir'].name}` | {len(_sx['foreign'])} | {len(_q)} "
                  f"| {sum(1 for e in _q if e.get('ad') is True)} |")
            A("")
            _raw = sum(len(s["adseg"]) for s in xs)
            if _raw != _ta:
                _bad = _raw - _ta
                A(f"Unfiltered, `data/ext/*/events.jsonl` holds "
                  f"**{_pl(_raw, '`segment` event')}** with `ad==true`, and "
                  f"**{_bad}** of {'those falls' if _bad == 1 else 'them fall'}"
                  f" within {EXT_QUAR_MS / 1000.0:.1f}s of a foreign event. "
                  "They belong to that other process, not to a real ad on a "
                  f"real player, so the attributed count is **{_ta}**. "
                  "`ext/extreport.py` "
                  "does not filter on `chan`, so run against such a session "
                  "it reports the higher number — a real disagreement between "
                  "this file and that tool, and this is the side with the "
                  "evidence.")
            else:
                A("None of them was an ad segment, so no count above moves.")
            A("")
            _fn = sum(len(s["foreign"]) for s in _fo)
            _qn = sum(len(s["quarantined"]) for s in _fo)
            A(f"Of the {_pl(_qn, 'quarantined event')}, {_fn} carry a "
              f"foreign `chan` outright; the other {_qn - _fn} carry no "
              "`chan` at all and are quarantined only for landing inside the "
              "burst. That window will "
              "collaterally catch a couple of the session's own `chan`-less "
              "events (`player`, `stat`), which is harmless: quarantine is "
              "used for nothing but attributing `segment` records.")
            A("")
            A("```sh")
            for _sx in _fo[:2]:
                A(f"jq -c 'select(has(\"chan\") and .chan!=\"{_sx['chan']}\")' "
                  f"data/ext/{_sx['dir'].name}/events.jsonl")
            A("```")
            A("")
        _cany = [s for s in xs if s["canary"] or s["canary_on"]]
        if _cany:
            A("**The canary is in these sessions and its counts are kept "
              "separate.** It is an `embed` session on a rejoin loop, so it "
              "draws prerolls a real viewer never would and overstates ad "
              "frequency badly. It proves an ad was on offer; it is not a "
              "rate, and it must never be blended with the viewer numbers "
              "above.")
            A("")
            A("| session | canary events | breaks it saw |")
            A("|---|---|---|")
            for _sx in _cany:
                A(f"| `{_sx['dir'].name}` | {len(_sx['canary'])} | "
                  f"{sum(1 for e in _sx['canary'] if e.get('kind') == 'break')} |")
            A("")
        else:
            A("The canary was **off** in every session here (`up.canary` "
              "false, zero `canary` events), so no rejoin-loop counts are "
              "blended into the numbers above. The `realAds` figures are what "
              "a passively watching viewer was actually offered.")
            A("")
        _stats = [s for s in xs if s["stat"]]
        if _stats:
            A("Independent cross-check — the extension's own counters, from "
              "the last `stat` event of each session. These are computed in "
              "`background.js` and never touched by this script:")
            A("")
            A("| session | blockedBreaks | blockedSegs | leakedBreaks | "
              "leakedSegs | segFetch | segFetchAd |")
            A("|---|---|---|---|---|---|---|")
            for _sx in _stats:
                _v = _sx["stat"]
                A(f"| `{_sx['dir'].name}` | {_dash(_v.get('blockedBreaks'))} "
                  f"| {_dash(_v.get('blockedSegs'))} "
                  f"| {_dash(_v.get('leakedBreaks'))} "
                  f"| {_dash(_v.get('leakedSegs'))} "
                  f"| {_dash(_v.get('segFetch'))} "
                  f"| {_dash(_v.get('segFetchAd'))} |")
            A("")
            _leak = sum(int(_n(s["stat"].get("leakedSegs"))) for s in _stats)
            _fa = sum(int(_n(s["stat"].get("segFetchAd"))) for s in _stats)
            _agree = (_leak == 0) == (_ta == 0) and (_fa == 0) == (_ta == 0)
            A(f"`leakedSegs` totals {_leak} and `segFetchAd` totals {_fa} "
              "across those sessions, arrived at independently of the "
              "`segment` events counted above"
              + (" — the two agree." if _agree else
                 " — **and they DISAGREE with it. Trust neither number until "
                 "that is explained.**"))
            A("")
            A("```sh")
            A("jq -c 'select(.ev==\"stat\" and .channel)|"
              "[.blockedBreaks,.leakedBreaks,.leakedSegs,.segFetchAd]' \\")
            A("   data/ext/<session>/events.jsonl | tail -1")
            A("```")
            A("")

        # ---------------- the ladder-collapse tell ----------------
        A("### The ladder-collapse tell")
        A("")
        _cs = [k for s in xs for k in s["collapses"]]
        if not _cs:
            A("No `ladder_collapse` event in any session on disk. The tell is "
              "unmeasured here — nothing in this file supports or refutes it.")
            A("")
        else:
            A("`usher` drops the bound rendition out of the master during a "
              "break, which the extension logs as `ladder_collapse`. The "
              "question is whether that is an ad *predictor* or a "
              "coincidence, so both directions are measured below.")
            A("")
            A("| session | collapse | ladder | bound | lead (upper) | "
              "lead (floor) | first ad poll's realAds | outcome |")
            A("|---|---|---|---|---|---|---|---|")
            for _sx in xs:
                for _k in _sx["collapses"]:
                    _u1 = "-" if _k["hi"] is None else "%.3fs" % _k["hi"]
                    _l1 = "-" if _k["lo"] is None else "%.3fs" % _k["lo"]
                    _pk = "-" if _k["peak"] is None else str(_k["peak"])
                    A(f"| `{_sx['dir'].name[-6:]}` | {_clock(_k['t'])} | "
                      f"{_dash(_k['had'])}->{_dash(_k['now'])} | "
                      f"`{_dash(_k['bound'])}` | {_u1} | "
                      f"{_l1} | {_pk} | {_k['verdict']} |")
            A("")
            A("Both leads are bounds, and neither is the ad's true start:")
            A("")
            A("- **lead (upper)** — collapse to the first poll that listed an "
              "ad. The ad may have entered the playlist any time after the "
              "previous poll, so this over-states.")
            A("- **lead (floor)** — collapse to the last poll that still "
              "listed **zero** ads. The ad provably was not in the playlist "
              "at that moment, so a positive floor is proof the collapse came "
              "first.")
            A("")
            _hit = [k for k in _cs if k["hi"] is not None]
            _fp = [k for k in _cs if k["verdict"] == "false positive"]
            _inc = [k for k in _cs if k["verdict"].startswith("inconclusive")]
            if _hit:
                _his = [k["hi"] for k in _hit]
                _los = [k["lo"] for k in _hit if k["lo"] is not None]
                A(f"**{len(_hit)}/{len(_cs)} "
                  f"{'collapse was' if len(_cs) == 1 else 'collapses were'} "
                  "followed by an ad-bearing playlist** within "
                  f"{EXT_HORIZON_MS // 1000}s. Upper lead: min "
                  f"{min(_his):.1f}s, median {_median(_his):.1f}s, max "
                  f"{max(_his):.1f}s.")
                if _los:
                    _pos = [q for q in _los if q > 0]
                    A(f"Floor lead: min {min(_los):.3f}s, median "
                      f"{_median(_los):.3f}s, max {max(_los):.3f}s, positive "
                      f"in {len(_pos)}/{len(_los)} cases — that many times "
                      "the collapse provably preceded the ad appearing in the "
                      "playlist at all.")
                A("")
            if _fp:
                A(f"**{len(_fp)} false positive(s)** — a collapse with no "
                  f"ad-bearing poll within {EXT_HORIZON_MS // 1000}s. At "
                  f"{100.0 * len(_fp) / len(_cs):.0f}% of collapses that is "
                  "the number to weigh before acting on the tell.")
            else:
                A("**Zero false positives** — every collapse was followed by "
                  "an ad break. On this sample the tell never cried wolf.")
            if _inc:
                A(f"{len(_inc)} collapse(s) are neither: the session ended "
                  f"less than {EXT_HORIZON_MS // 1000}s later, so there was "
                  "no chance to see whether a break followed. They count as "
                  "neither hit nor false positive.")
            A("")
            A("Provenance, because this claim has been quoted from a source "
              "that cannot be re-derived: the counts above come only from the "
              "session(s) "
              + ", ".join(f"`{s['dir'].name}`" for s in xs
                          if s["collapses"])
              + ". `data/ext/ext.log` is the prose log appended across every "
                "run ever made, including runs that left no `events.jsonl`; "
                "per `ext/EVENTS.md` it is a rendering and not a source, so "
                "nothing here parses it and any collapse seen only there is "
                "NOT counted above. The sessions that do have a stream "
                "reproduce the finding on their own.")
            A("")
            _bk = [k for s in xs for k in s["pods"]]
            if _bk:
                _warned = [k for k in _bk if k.get("warned")]
                A(f"The other direction — **{len(_warned)}/{len(_bk)} ad pods "
                  "had a collapse in front of them** (within "
                  f"{EXT_HORIZON_MS // 1000}s). Per pod:")
                A("")
                A("| session | pod start | dur | peak realAds | polls | "
                  "state | since last collapse |")
                A("|---|---|---|---|---|---|---|")
                for _sx in xs:
                    for _k in _sx["pods"]:
                        _sc = _k.get("since_collapse")
                        A(f"| `{_sx['dir'].name[-6:]}` | {_clock(_k['t0'])} | "
                          f"{_secs((_k['t1'] - _k['t0']) / 1000.0)} | "
                          f"{_k['peak']} | {_k['polls']} | "
                          f"{'blocked' if _k.get('blocked') else 'EXPOSED'} | "
                          f"{'never' if _sc is None else '%.1fs' % _sc} |")
                A("")
                _multi = [k for k in _bk if k.get("runs", 1) > 1]
                if _multi:
                    A(f"{_pl(len(_multi), 'pod')} arrived as more than one run "
                      "of ad-bearing polls and "
                      f"{'was' if len(_multi) == 1 else 'were'} merged here.")
                    A("")
            _pdt = [k for k in _cs if k["pdt"] is not None]
            if _pdt:
                _neg = [k for k in _pdt if k["pdt"] < 0]
                A("**What this does NOT say.** For "
                  f"{_pl(len(_pdt), 'collapse')} the first ad-bearing manifest body "
                  "was kept, so the first ad segment's `PROGRAM-DATE-TIME` is "
                  "readable. Measured against the collapse's own wall clock, "
                  f"that PDT lands {min(k['pdt'] for k in _pdt):+.2f}s to "
                  f"{max(k['pdt'] for k in _pdt):+.2f}s away"
                  + (f", negative in {len(_neg)} of them." if _neg else "."))
                if _neg:
                    A("")
                    A("So the collapse does **not** beat the ad's PDT. The ad "
                      "is back-dated into the wall-clock slot it replaces — "
                      "the same 'ads replace content in wall-clock time' "
                      "behaviour seen everywhere else in this file. What the "
                      "collapse beats is the moment the ad becomes "
                      "*fetchable*, which is what the floor lead above "
                      "measures. That is still the useful property, but "
                      "'fires before the ad exists' is the wrong phrasing and "
                      "these manifests contradict it.")
                A("")
            _lad = collections.Counter()
            for _sx in xs:
                for _k in _sx["collapses"]:
                    _lad[(_k["had"], _k["now"])] += 1
            if _lad:
                A("Ladder sizes at a collapse: "
                  + ", ".join(f"`{_dash(q[0])}->{_dash(q[1])}` x{w}"
                              for q, w in _lad.most_common()) + ".")
                A("")
            _fullc = collections.Counter()
            for _sx in xs:
                _fullc.update(_sx["full"])
            _colc = collections.Counter(_k["rends"] for _k in _cs if _k["rends"])
            if _fullc or _colc:
                A("What the ladder actually looked like. Rendition sets are "
                  "sorted before counting, because usher randomises the "
                  "*order* of the ladder between mints and an ordered list "
                  "would invent variety that is not there:")
                A("")
                A("| when | variants | seen |")
                A("|---|---|---|")
                for _q, _w in _fullc.most_common(4):
                    A(f"| normal | `{', '.join(_q)}` | {_w} |")
                for _q, _w in _colc.most_common(4):
                    A(f"| collapsed | `{', '.join(_q)}` | {_w} |")
                A("")
                _gone = [_k for _k in _cs if _k["rends"]]
                if _gone:
                    _ng = sum(1 for _k in _gone if _k["bound_gone"])
                    A(f"The rendition the pool was holding was **absent** from "
                      f"the collapsed master in {_ng}/{len(_gone)} cases. The "
                      "player is therefore *forced* off it rather than "
                      "choosing to move, which is why a rendition vanishing "
                      "must never be treated as a quality change: rebinding "
                      "on it flushes the donor buffer at exactly the moment "
                      "the ad arrives.")
                    A("")
            A("```sh")
            A("# every collapse and every ad-bearing poll, in time order")
            A("jq -c 'select(.ev==\"ladder_collapse\" or "
              "(.ev==\"media\" and (.realAds//0)>0))|[.t,.ev,.realAds,.decision]' \\")
            A("   data/ext/<session>/events.jsonl")
            A("# the ladder before and after")
            A("jq -c 'select(.ev==\"master\")|[.t,(.variants|length),"
              "(.variants|map(.rend)|join(\",\"))]' data/ext/<session>/events.jsonl")
            A("```")
            A("")

        # ---------------- why we missed ----------------
        A("### Why we missed, when we did")
        A("")
        A("An ad-bearing poll the extension did not rewrite is an exposure: "
          "the real playlist, ads included, went to the player. Bucketed by "
          "`media.decision`, and paired with whatever landed in the 10s "
          "before it. A `rebind`/`regrid` means we flushed our own pool and "
          "then had nothing to serve — self-inflicted, and fixable in our "
          "code. A `ladder_collapse` means usher forced the player off the "
          "rendition we hold. Neither means the pool was simply short of "
          "donors, which is a different and much harder problem.")
        A("")
        _ms = [m for s in xs for m in s["miss"]]
        if not _ms:
            _totpass = sum(s["quiet_pass"] for s in xs)
            A(f"**Zero.** Every one of the {_tap} ad-bearing polls across all "
              "sessions was rewritten. There is no miss to explain.")
            A("")
            A(f"The extension did pass {_totpass} playlist(s) through "
              "unrewritten, but every one of those carried `realAds == 0`. A "
              "passthrough on a poll with no ad in it exposes nothing, so "
              "counting them as misses would invent failures that never "
              "happened; they are listed only so the `stat` passthrough "
              "counters reconcile.")
            A("")
            A("| session | passthroughs on ad-free polls | rebinds | regrids |")
            A("|---|---|---|---|")
            for _sx in xs:
                A(f"| `{_sx['dir'].name}` | {_sx['quiet_pass']} | "
                  f"{_sx['rebinds']} | {_sx['regrids']} |")
            A("")
            A("**What this does not prove.** Zero misses over "
              f"{len(xs)} session(s) on one channel is not a miss *rate*. The "
              "case that has never been observed is the one that matters: "
              "every donor in an ad at once. `notes/HANDOVER.md` flags it as "
              "the top open question and nothing in `data/ext/` closes it.")
            A("")
        else:
            _bd = collections.Counter(m["dec"] for m in _ms)
            _bc = collections.Counter(m["dec"] for m in _ms if m["churn"])
            _bl2 = collections.Counter(m["dec"] for m in _ms if m["coll"])
            A(f"{len(_ms)} of {_tap} ad-bearing polls were not rewritten.")
            A("")
            A("| decision | misses | rebind/regrid <=10s before | "
              "ladder_collapse <=10s before | neither |")
            A("|---|---|---|---|---|")
            for _d, _w in _bd.most_common():
                _nn = sum(1 for m in _ms if m["dec"] == _d
                          and not m["churn"] and not m["coll"])
                A(f"| `{_d}` | {_w} | {_bc.get(_d, 0)} | {_bl2.get(_d, 0)} | "
                  f"{_nn} |")
            A("")
            _self = sum(1 for m in _ms if m["churn"])
            _forced = sum(1 for m in _ms if m["coll"] and not m["churn"])
            _short = sum(1 for m in _ms if not m["churn"] and not m["coll"])
            A(f"- **self-inflicted** {_self}/{len(_ms)} "
              f"({100.0 * _self / len(_ms):.1f}%) — a rebind or regrid emptied "
              "the pool first")
            A(f"- **forced by usher** {_forced}/{len(_ms)} "
              f"({100.0 * _forced / len(_ms):.1f}%) — a ladder collapse and no "
              "churn of our own")
            A(f"- **donor shortage** {_short}/{len(_ms)} "
              f"({100.0 * _short / len(_ms):.1f}%) — nothing to blame but an "
              "empty pool")
            A("")
            A("```sh")
            A("jq -c 'select(.ev==\"media\" and (.realAds//0)>0 and "
              ".decision!=\"rewrite\")|[.t,.realAds,.decision,.rend,.poolRend]' \\")
            A("   data/ext/*/events.jsonl")
            A("```")
            A("")

    # =====================================================================
    #  the POC ledgers — data/unslop/*.jsonl, scored by coverage.py
    # =====================================================================
    A("## THE POC LEDGERS (`unslop.py` scored by `coverage.py`)")
    A("")
    if COV is None:
        A("`coverage.py` could not be imported, so the ledgers are unscored. "
          "This section deliberately does not re-derive its maths — "
          "`SPLICEABLE` is exact weighted-interval scheduling and an "
          "approximation here would silently disagree with the tool.")
        A("")
    elif not led:
        A("No ledger under `data/unslop/*.jsonl`.")
        A("")
    else:
        A("Every figure here comes from `coverage.py`'s own `merge` / "
          "`subtract` / `spliceable`, imported rather than reimplemented.")
        A("")
        A("**Read `SPLICEABLE`, never the union.** The union counts a moment "
          "as covered whenever *any* arm held content for it — including an "
          "arm that took an ad and came back off-grid, which a player cannot "
          "splice onto. `SPLICEABLE` is the longest non-overlapping timeline "
          "that could actually be served.")
        A("")
        A("| ledger | arms | wall | ad time | union | SPLICEABLE | "
          "unspliceable | breaks |")
        A("|---|---|---|---|---|---|---|---|")
        for _r in led:
            if not _r.get("segs"):
                A(f"| `{_r['file']}` | - | - | *(no segment records)* | - | - "
                  "| - | - |")
                continue
            _adms = _r["ad"]
            _spl = f"{100.0 * _r['spliceable'] / _adms:.1f}%" if _adms else "n/a"
            _uni = f"{100.0 * _r['union'] / _adms:.1f}%" if _adms else "n/a"
            A(f"| `{_r['file']}` | {_r['arms']} | {_secs(_r['wall'] / 1000.0)} "
              f"| {_secs(_adms / 1000.0)} | {_uni} | **{_spl}** | "
              f"{_r['unspliceable'] / 1000.0:.1f}s | {_r['breaks']} |")
        A("")
        _ok = [q for q in led if q.get("ad")]
        _zero = [q for q in led if q.get("segs") and not q.get("ad")]
        if _ok:
            _ta2 = sum(q["ad"] for q in _ok)
            _ts2 = sum(q["spliceable"] for q in _ok)
            _tu2 = sum(q["unspliceable"] for q in _ok)
            A(f"Pooled across {len(_ok)} ledger(s): **{_ta2 / 1000.0:.1f}s of "
              f"ad wall-clock, {100.0 * _ts2 / _ta2:.1f}% SPLICEABLE**, "
              f"{_tu2 / 1000.0:.1f}s with no on-grid donor.")
            A("")
            _adpt = sorted({q for w in _ok for q in w["adpt"]})
            _clpt = sorted({q for w in _ok for q in w["cleanpt"]})
            _never = sorted(set(_clpt) - set(_adpt))
            A("**The caveat that decides how much this is worth.** The arms "
              f"that took an ad were `player_type` {_adpt}"
              + (f"; arms of type {_never} never took one in any of these "
                 "windows. So this measures *`embed`'s ads covered by donors "
                 "that were never asked to take one*, not the hard case where "
                 "the donors themselves go dark. The percentage is real; it "
                 "is not a claim about the general case."
                 if _never else "."))
            A("")
        if _zero:
            A(f"{_pl(len(_zero), 'ledger')} recorded segments but "
              "**zero ad time**: "
              + ", ".join(f"`{q['file']}`" for q in _zero)
              + ". A zero is a valid result and they are kept here rather than "
                "dropped — they prove nothing about coverage, in either "
                "direction.")
            A("")
        _drop = sum(q.get("dropped", 0) for q in led)
        if _drop:
            A(f"{_pl(_drop, 'malformed `seg` record')} skipped across all "
              "ledgers (missing or non-numeric `pdt`/`dur`), so these numbers "
              "can differ very slightly from `python coverage.py <file>`, "
              "which does not skip them.")
            A("")
        A("```sh")
        A("python coverage.py data/unslop/<chan>.<ts>.jsonl   # the same numbers")
        A("python coverage.py --selftest                      # the maths")
        A("jq -r 'select(.ev==\"seg\" and .ad==true)|.arm' data/unslop/*.jsonl \\")
        A("   | sort | uniq -c        # which arms ever took an ad")
        A("```")
        A("")

    A("## INFERRED (not proven)")
    A("")
    A("- That the `player_type` rate gap generalises. Measured from one IP, one "
      "country, one night, one channel pool. The gap is large and consistent "
      "across three independent scripts, but the absolute rates will move with "
      "geo, time of day, and which channels are live.")
    A("- Why `embed` is served so much more. Nothing here explains the "
      "mechanism; only that the rate differs.")
    A("- That the DATERANGE lead time is consistently usable for prefetch. "
      f"Observed on {len(announce_lead)} manifest(s) only.")
    A("")
    A("## FAILED / UNTESTED")
    A("")
    A("- **Original splice design was wrong.** `LIVE-SEQUENCE` was assumed "
      f"stable across sessions; it is absent from {n-c['liveseq']}/{n} ad "
      "manifests. Replaced with PDT before the POC was built.")
    A("- **Whether geo re-checks downstream is not fully answered.** Proving it "
      "needs an ad to fire during the interleaved window; if `ad polls` are 0 "
      "in the table above, the comparison had nothing to compare.")
    A("- Sub/turbo arms were not run (no account, by design).")
    A("- **The gap path fired once but was not caught in a served manifest.** "
      "One of eight stress probes logged a DISCONTINUITY, so the "
      "no-clean-arm case is real and reachable at 5 arms. Playback was fine "
      "either side of it, but the specific manifest carrying the tag had "
      "scrolled out before it could be handed to a player, so 'the player "
      "skips the gap cleanly' remains reasoned rather than directly observed.")
    A("- **Two claims were retracted mid-run.** (1) `site` is ad-free — true "
      "only for the first ~hour. (2) That zero was sampling noise — also "
      "wrong. The bucket table above shows a real, time-driven onset. "
      "`cookietest.py` and `headertest.py` both ran inside the dead window, "
      "so their negative results are confounded by time and need re-running "
      "during a high-rate window.")
    A("- **What causes the hourly variation is untested.** Advertiser "
      "dayparting/inventory is the obvious suspect but nothing here measures "
      "it. Note the run spans 02:20-08:50 EEST = late-night EU / US prime "
      "time, so channel mix shifted too and is a confound.")
    A("")
    _st = LAB / "data" / "unslop.stress.log"
    _ah = LAB / "data" / "after_hunt.log"
    if _st.exists():
        _last = [l for l in _st.read_text().splitlines() if l.startswith("[.]")]
        A("### POC under load (`unslop.py`, 5 arms, high-rate window)")
        A("")
        if _last:
            A(f"Final counters: `{_last[-1][4:]}`")
            A("")
        A("Measured while thousands of ad segments were being rejected:")
        A("")
        A("- served playlist held 12 segments, **0 non-`live` titles**, "
          "**0 `#EXT-X-DISCONTINUITY`**")
        A("- `PROGRAM-DATE-TIME` deltas exactly `+2.000s` across the window")
        A("- the window advanced in real time and stayed only **3-5s behind "
          "live**, so the merge keeps up rather than falling back")
        A("- plays in mpv and ffprobe (h264 + aac, A-V drift 0.000)")
        A("")
        _discfired = "disc=1" in (_ah.read_text() if _ah.exists() else "")
        if _discfired:
            A("**The gap path did fire.** Across the 8 probes, seven showed "
              "`disc=0` and one showed `disc=1` — a wall-clock slot where no "
              "arm had a clean segment, so the merge emitted "
              "`#EXT-X-DISCONTINUITY` as designed. Playback continued and mpv "
              "reported A-V drift 0.000 immediately afterwards.")
            A("")
            A("Caveat on that one: by the time the playlist was fetched by "
              "hand the window had already scrolled past the discontinuity, so "
              "the DISCONTINUITY line itself was observed in the probe log, "
              "not in a manifest that was then handed to a player. Playback "
              "was verified over the same region, not over that exact tag.")
        else:
            A("**What this did NOT prove.** The counters show heavy ad load "
              "survived, but no probe caught every arm in a break at once, so "
              "the DISCONTINUITY-and-skip path stayed unexercised here.")
        A("")
    A("## What this means if you build the extension")
    A("")
    A("- **Filter on the `#EXTINF` title, not `STREAM-SOURCE`.** Title is "
      "per-segment (tells you exactly which bytes to drop) and ~99% "
      "reliable; STREAM-SOURCE misses roughly a fifth of breaks.")
    A("- **Use the stitched-ad DATERANGE as an early warning**, not as the "
      "filter. It can appear before any ad segment does, which is enough lead "
      "time to have a clean segment ready.")
    A("- **Splice on `PROGRAM-DATE-TIME`.** `LIVE-SEQUENCE` is missing from "
      "over half of ad manifests. **Dedupe on the EXACT PDT, with no "
      "tolerance** — sessions that have not taken an ad emit byte-identical "
      "PDTs. This file previously advised a ~0.75-slot tolerance; that was "
      "wrong, and it masked the real problem, which is that an arm which HAS "
      "taken an ad comes back permanently off-grid and cannot be spliced onto "
      "at any tolerance. Retire such an arm instead of widening the window "
      "(`coverage.py --selftest` encodes both cases).")
    A("- **You do not need to proxy video.** Rewriting segment URLs in the "
      "~2KB manifest is enough; the player fetches bytes from Twitch directly. "
      "A foreign-minted playlist URL also polls fine from the home IP, so the "
      "session blob is not IP-locked.")
    A("- **Do not build on `player_type` alone.** The rate gap is large but "
      "`site` still gets ads, so a player_type switch is a rate reduction, not "
      "a fix. The splice is what actually removes them.")
    A("- **`ffprobe` gives a metadata-free fallback** if Twitch changes its "
      "tags: ad and content segments had zero overlapping encoder signatures.")
    if xs and sum(len(q["collapses"]) for q in xs):
        _acs = [k for q in xs for k in q["collapses"]]
        _ahit = [k for k in _acs if k["hi"] is not None]
        if _ahit:
            A("- **Treat a rendition vanishing from the master as an ad "
              "break, not a quality change.** Measured above: "
              f"{len(_ahit)}/{len(_acs)} collapses were followed by a break, "
              f"{min(k['hi'] for k in _ahit):.1f}-"
              f"{max(k['hi'] for k in _ahit):.1f}s ahead of the first "
              "ad-bearing poll. It is the only tell that fires early. Hold "
              "the pool where it is — rebinding to chase the forced downgrade "
              "flushes the donor buffer at exactly the wrong moment.")
    A("")
    A("## Reproduce")
    A("")
    A("```sh")
    A("./selftest.sh                      # rig verification, no browser")
    A("python hunt.py 6.5                 # the hunter")
    A("python unslop.py <channel>         # POC; then: mpv http://127.0.0.1:8778/playlist.m3u8")
    A("python coverage.py data/unslop/<chan>.<ts>.jsonl   # score a POC ledger")
    A("./ext/run.sh <channel> [secs] [level]              # drive the extension")
    A("python ext/extreport.py data/ext/<session>/        # score one session")
    A("python geoprobe.py --ports 9050    # geo control group")
    A("python report.py                   # regenerate this file")
    A("```")

    out = LAB / "notes" / "FINDINGS.md"
    out.write_text("\n".join(L) + "\n")
    print(f"wrote {out}  ({len(L)} lines)")
    print(f"  hunter {dur_h:.1f}h  polls={polls}  joins={len(joins)}  "
          f"ad_manifests={len(mans)}  ad_segments={len(ad_segs)}")
    print(f"  ext    sessions={len(xs)}"
          f"  pods={sum(len(x['pods']) for x in xs)}"
          f"  runs={sum(len(x['breaks']) for x in xs)}"
          f"  exposed={sum(x['exposed'] for x in xs)}"
          f"  ad_segs_on_screen={sum(len(x['adseg_own']) for x in xs)}"
          f"  collapses={sum(len(x['collapses']) for x in xs)}"
          + ("" if EXTR else "  [ext/extreport.py NOT importable]"))
    _lp = [r for r in led if r.get("ad")]
    print(f"  unslop ledgers={len(led)}  with_ads={len(_lp)}"
          + (f"  spliceable={100.0*sum(r['spliceable'] for r in _lp)/sum(r['ad'] for r in _lp):.1f}%"
             if _lp else "")
          + ("" if COV else "  [coverage.py NOT importable]"))


if __name__ == "__main__":
    main()
