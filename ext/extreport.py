#!/usr/bin/env python3
"""
Score one extension session. Answers, in order: did an ad reach the screen, was
one on offer, when we missed it was it our own fault, was the pool healthy, and
did the video actually play.

  python ext/extreport.py [data/ext/<chan>.<ts>/ | .../events.jsonl]

With no argument, the newest session under data/ext/. Schema is ext/EVENTS.md.
ext.log is a rendering of that stream, not the source — nothing here parses it.

Two numbers are easy to get wrong, so both are labelled in the output:

  * a manifest listing an ad is not an ad on screen. `media.realAds` only says
    Twitch offered one; a `segment` with ad=true says the player fetched it.
  * `realAds` is a per-poll count over a sliding window, so the same ad segment
    is counted again on every poll it survives. Summing polls is meaningless.
    The honest figures are the distinct URLs actually fetched (exact) and the
    largest single-poll count in each break (a floor — an ad we never fetched
    has no URL for us to count).
"""
import contextlib
import io
import json
import pathlib
import sys
import time
from collections import Counter, defaultdict

GAP_POLLS = 3        # ad-free polls in a row that close a break. The window
                     # keeps listing a pod for a poll or two after Twitch stops
                     # extending it, so 1 clean poll does not mean it is over.
NEAR_MS = 10_000     # a rebind/regrid this close before a miss -> we caused it
FLAP_MS = 15_000     # a rebind undone inside this is flapping, not tuning
FETCH_MS = 10_000    # ad fetch this soon after a break's last poll is still it
STALL_EPS = 0.1      # mirrors tryout.py: <=0.1s of video in a sample is a stall
DECS = ["rewrite", "pass_cold", "pass_mismatch", "pass_unknown"]   # column order


def num(v, d=0.0):
    """Live logs carry nulls, and a field can arrive as a string mid-refactor."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def hms(ms):
    return time.strftime("%H:%M:%S", time.localtime(ms / 1000))


def span(ms):
    s = ms / 1000.0
    return f"{s:.1f}s" if s < 90 else f"{int(s // 60)}m{s % 60:04.1f}s"


def table(head, rows, align=""):
    """Aligned plain-text columns. `align` is one char per column, l or r."""
    cols = list(zip(*([head] + rows))) if rows else [(h,) for h in head]
    w = [max(len(str(c)) for c in col) for col in cols]
    align = (align + "l" * len(head))[:len(head)]
    line = lambda r: "  " + "  ".join(
        f"{str(c):{'<' if align[i] == 'l' else '>'}{w[i]}}"
        for i, c in enumerate(r)).rstrip()
    return "\n".join([line(head)] + [line(r) for r in rows])


def ranges(v):
    """Contract is [[s,e],...]; tryout.py's page probe still ships the joined
    'a-b,c-d' string. Accept both rather than silently report no buffer holes."""
    out = []
    if isinstance(v, str):
        for part in v.split(","):
            a, _, b = part.partition("-")
            try:
                out.append((float(a), float(b)))
            except ValueError:
                pass
        return out
    for r in v or []:
        try:
            out.append((float(r[0]), float(r[1])))
        except (TypeError, ValueError, IndexError, KeyError):
            pass
    return out


def parse(lines):
    """Tolerant by design: a truncated last line is normal on a live rig, and
    unknown `ev` values are expected — the extension ships schema changes
    before this script learns them."""
    out, t = [], 0.0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(r, dict):
            continue
        t = num(r.get("t"), t)   # append-only, so file order is time order: a
        r["t"] = t               # line missing `t` inherits the last stamp
        out.append(r)            # rather than sorting itself back to 1970
    out.sort(key=lambda r: r["t"])      # stable — file order breaks ties
    return out


def load(src):
    p = pathlib.Path(src)
    if p.is_dir():
        p = p / "events.jsonl"
    if not p.is_file():
        sys.exit(f"no events.jsonl at {p}")
    return p, parse(p.read_text(errors="replace").splitlines())


def newest(root):
    """data/ext also holds ext.log and other leftovers, so pick a session on
    the presence of the stream, not on the directory name."""
    cands = [d for d in root.glob("*") if (d / "events.jsonl").is_file()]
    if not cands:
        sys.exit(f"no session with an events.jsonl under {root}")
    return max(cands, key=lambda d: (d / "events.jsonl").stat().st_mtime)


def group(media):
    """Consecutive ad-bearing polls, split when GAP_POLLS clean polls run.

    Only the ad-bearing polls are kept: a passthrough on a poll that carried no
    ad exposed nothing, and counting it would invent misses.
    """
    out, cur, zeros = [], [], 0
    for m in media:
        if num(m.get("realAds")) > 0:
            cur.append(m)
            zeros = 0
        elif cur:
            zeros += 1
            if zeros >= GAP_POLLS:
                out.append(cur)
                cur, zeros = [], 0
    if cur:
        out.append(cur)
    return out


def describe(brk, adsegs):
    t0, t1 = brk[0]["t"], brk[-1]["t"]
    dec = Counter(m.get("decision") or "(none)" for m in brk)
    hit = [s for s in adsegs if t0 <= s["t"] <= t1 + FETCH_MS]
    return {"t0": t0, "t1": t1, "polls": len(brk), "dec": dec,
            "peak": int(max(num(m.get("realAds")) for m in brk)),
            "naive": int(sum(num(m.get("realAds")) for m in brk)),
            "blocked": set(dec) == {"rewrite"},
            "fetched": len(hit),
            "urls": len({s.get("url") for s in hit if s.get("url")})}


def why(media, marks):
    """Every exposed poll, paired with any rebind/regrid just before it.

    That pairing is the whole report: a miss with churn behind it means we
    retuned or retired the pool and then had nothing to serve — self-inflicted,
    and fixable in our code. A miss without it means the pool was genuinely
    short of donors, which is a different and much harder problem.
    """
    out = []
    for m in media:
        if num(m.get("realAds")) <= 0 or m.get("decision") == "rewrite":
            continue
        out.append((m, [k for k in marks if 0 <= m["t"] - k["t"] <= NEAR_MS]))
    return out


def pool(evs):
    arms = defaultdict(lambda: {"ptype": None, "polls": 0, "ads": 0,
                                "grid": 0, "new": 0, "regrid": 0})
    for a in evs["arm"]:
        s = arms[a.get("n")]
        s["ptype"] = a.get("ptype") or s["ptype"]
        s["polls"] += 1
        s["ads"] += bool(a.get("ad"))
        s["grid"] += bool(a.get("onGrid"))
        s["new"] += int(num(a.get("new")))
    for r in evs["regrid"]:
        s = arms[r.get("n")]        # a donor can be retired before it ever
        s["regrid"] += 1            # logged a poll, so this may add an arm
        s["ptype"] = s["ptype"] or r.get("ptype")
    return arms


def flaps(rebinds):
    """A rebind that undoes a recent one. Twitch's ABR probes low and climbs
    within ~1s, so a pool that chases `rend` oscillates — and each flap costs a
    fresh mint and a cold pool, which shows up as misses in section 3."""
    n = 0
    for i, r in enumerate(rebinds):
        if any(r["t"] - q["t"] <= FLAP_MS and q.get("from") == r.get("to")
               and q.get("to") == r.get("from") for q in rebinds[:i]):
            n += 1
    return n


def playback(ps):
    if len(ps) < 2:
        return None
    adv, stalls, hole, hole_t, prev = 0.0, 0, 0.0, None, None
    for p in ps:
        vt = num(p.get("vt"), -1)
        if vt >= 0:
            if prev is not None:
                d = vt - prev
                if d > 0:
                    adv += d          # a reload restarts currentTime at 0; a
                if d <= STALL_EPS and not p.get("paused"):   # backwards jump is
                    stalls += 1                              # not negative play
            prev = vt
        rs = ranges(p.get("ranges"))
        for a, b in zip(rs, rs[1:]):
            if b[0] - a[1] > hole:
                hole, hole_t = b[0] - a[1], p["t"]
    return {"n": len(ps), "wall": ps[-1]["t"] - ps[0]["t"], "adv": adv,
            "stalls": stalls, "hole": hole, "hole_t": hole_t}


def analyse(events):
    evs = defaultdict(list)
    for e in events:
        evs[e.get("ev")].append(e)
    media = evs["media"]
    adsegs = [s for s in evs["segment"] if s.get("ad")]      # ground truth
    brks = [describe(b, adsegs) for b in group(media)]
    marks = sorted(evs["rebind"] + evs["regrid"], key=lambda r: r["t"])
    miss = why(media, marks)
    return {
        "evs": evs,
        "counts": Counter(e.get("ev") for e in events),
        "span": (events[0]["t"], events[-1]["t"]) if events else (0, 0),
        "onscreen": len(adsegs),
        "onscreen_urls": len({s.get("url") for s in adsegs if s.get("url")}),
        "breaks": brks,
        "blocked": sum(1 for b in brks if b["blocked"]),
        "exposed": sum(1 for b in brks if not b["blocked"]),
        "peaksum": sum(b["peak"] for b in brks),
        "naive": sum(b["naive"] for b in brks),
        "adpolls": sum(b["polls"] for b in brks),
        "orphan": sum(1 for s in adsegs if not any(
            b["t0"] <= s["t"] <= b["t1"] + FETCH_MS for b in brks)),
        "miss": miss,
        "missdec": Counter(m.get("decision") or "(none)" for m, _ in miss),
        "missnear": Counter(m.get("decision") or "(none)" for m, n in miss if n),
        "selfinflicted": sum(1 for _, n in miss if n),
        "shortage": sum(1 for _, n in miss if not n),
        "arms": pool(evs),
        "rebinds": evs["rebind"],
        "flaps": flaps(evs["rebind"]),
        "play": playback(evs["player"]),
    }


def show(path, a):
    evs, t0, t1 = a["evs"], *a["span"]
    print(f"extreport  {path}")
    print(f"           {sum(a['counts'].values())} events   "
          f"{hms(t0)} -> {hms(t1)}   ({span(t1 - t0)})")
    print("           " + "  ".join(
        f"{k if k else '(no ev)'}={v}" for k, v in
        sorted(a["counts"].items(), key=lambda kv: (-kv[1], str(kv[0])))))
    if evs["up"]:
        u = evs["up"][0]
        print(f"           up: arms={u.get('arms')} "
              f"types={','.join(str(x) for x in u.get('types') or []) or '?'} "
              f"minServe={u.get('minServe')} window={u.get('window')} "
              f"canary={u.get('canary')}")
    if evs["master"]:
        m = evs["master"][-1]
        print(f"           master: {m.get('chan')} fmt={m.get('fmt')} "
              f"variants={len(m.get('variants') or [])}")

    print()
    print("=== 1. VERDICT ===")
    print(f"  ads reaching the screen   {a['onscreen']:>6d}   "
          f"segment events with ad=true")
    if not a["onscreen"]:
        print("  NO AD SEGMENT WAS EVER FETCHED BY THE PLAYER.")
    print()
    print("  ad segments offered to this session")
    print(table(["measure", "n", "kind", ""],
                [["distinct ad URLs fetched", a["onscreen_urls"], "EXACT",
                  "these reached the screen"],
                 ["per-break peak realAds, summed", a["peaksum"], "FLOOR",
                  "one poll listed this many at once"],
                 ["realAds summed over all polls", a["naive"], "INFLATED",
                  "the window relists each segment"]],
                "lrll"))
    print("  an ad the player never fetched leaves no URL to count, so no line")
    print("  above is an exact count of what Twitch offered - quote the FLOOR.")
    print()
    print(f"  breaks      {len(a['breaks']):>4d}")
    print(f"    blocked   {a['blocked']:>4d}   every ad-bearing poll rewritten")
    print(f"    exposed   {a['exposed']:>4d}   at least one poll passed through")

    print()
    print("=== 2. BREAK TIMELINE ===")
    if not a["breaks"]:
        print("  none - no poll ever listed an ad segment")
    else:
        seen = {d for b in a["breaks"] for d in b["dec"]}
        cols = [d for d in DECS if d in seen] + sorted(seen - set(DECS))
        rows = [[hms(b["t0"]), span(b["t1"] - b["t0"]), b["peak"], b["polls"]]
                + [b["dec"].get(d, 0) for d in cols]
                + ["blocked" if b["blocked"] else "EXPOSED"]
                + [f"YES ({b['fetched']}, {b['urls']} urls)" if b["fetched"]
                   else "no"]
                for b in a["breaks"]]
        print(table(["start", "dur", "peak", "polls"] + cols
                    + ["state", "ad fetched"],
                    rows, "llrr" + "r" * len(cols) + "ll"))
        print(f"  (ad fetched = a segment ev with ad=true inside the break, "
              f"+{FETCH_MS // 1000}s for fetch lag)")
        if a["orphan"]:
            print(f"  {a['orphan']} ad fetch(es) fell outside every break - "
                  f"something listed them that we never logged")

    print()
    print("=== 3. WHY WE MISSED ===")
    n = len(a["miss"])
    print(f"  {n} of {a['adpolls']} ad-bearing polls were not rewritten")
    if n:
        print()
        print(table(["decision", "misses",
                     f"churn <={NEAR_MS // 1000}s before", "no churn"],
                    [[d, c, a["missnear"].get(d, 0), c - a["missnear"].get(d, 0)]
                     for d, c in a["missdec"].most_common()], "lrrr"))
        print()
        w = len(str(n))
        loud = [f"  SELF-INFLICTED  {a['selfinflicted']:>{w}d} / {n}  "
                f"({a['selfinflicted'] / n * 100:5.1f}%)   a rebind/regrid "
                f"landed first - we emptied our own pool",
                f"  DONOR SHORTAGE  {a['shortage']:>{w}d} / {n}  "
                f"({a['shortage'] / n * 100:5.1f}%)   no churn to blame - the "
                f"pool simply had no donor"]
        bar = "  " + "-" * (max(len(x) for x in loud) - 2)
        print("\n".join([bar] + loud + [bar]))

    print()
    print("=== 4. POOL HEALTH ===")
    if not a["arms"]:
        print("  no arm events")
    else:
        rows = []
        # keys come off a live log: sort ints and anything else separately so a
        # mixed pool can never raise here
        for k in sorted(a["arms"], key=lambda x: (0, x) if isinstance(x, int)
                        else (1, str(x))):
            s = a["arms"][k]
            rows.append([k, s["ptype"] or "?", s["polls"], s["ads"],
                         f"{s['grid'] / s['polls'] * 100:.1f}%" if s["polls"]
                         else "-", s["new"], s["regrid"]])
        print(table(["arm", "ptype", "polls", "ads", "on-grid", "segs",
                     "retired"], rows, "llrrrrr"))
    rb = a["rebinds"]
    print(f"  rebinds {len(rb)}   flaps {a['flaps']}"
          f"   retuned {int(sum(num(r.get('retuned')) for r in rb))}"
          f"   minted {int(sum(num(r.get('minted')) for r in rb))}"
          + ("   <-- the pool is chasing the player's ABR probe"
             if a["flaps"] else ""))
    for p, c in Counter(f"{r.get('from')} -> {r.get('to')}"
                        for r in rb).most_common():
        print(f"    {c:>3d}x  {p}")

    print()
    print("=== 5. PLAYBACK ===")
    p = a["play"]
    if not p:
        print("  fewer than two player events - nothing to measure")
    else:
        wall = max(p["wall"] / 1000.0, 1e-9)
        print(f"  samples               {p['n']}")
        print(f"  wall clock            {wall:.1f}s")
        print(f"  video advanced        {p['adv']:.1f}s   "
              f"{p['adv'] / wall * 100:.1f}% of wall clock (1:1 is healthy)")
        print(f"  stalls                {p['stalls']}   samples where vt "
              f"advanced <={STALL_EPS:.2f}s and paused=false")
        if p["hole_t"] is None:
            print("  largest buffer hole   none - buffered was always contiguous")
        else:
            print(f"  largest buffer hole   {p['hole']:.2f}s at "
                  f"{hms(p['hole_t'])}")


def selftest():
    assert ranges("0.0-12.0,15.5-20.0") == [(0.0, 12.0), (15.5, 20.0)]
    assert ranges([[1, 2], [3.5, 4]]) == [(1.0, 2.0), (3.5, 4.0)]
    assert ranges(None) == [] and ranges("junk") == [] and ranges([[1]]) == []
    assert num(None) == 0.0 and num("3") == 3.0 and num({}, 7) == 7

    base = 1_700_000_000_000
    E = lambda t, ev, **kw: dict(t=base + t, ev=ev, lvl="info", **kw)
    poll = lambda t, ads, dec: E(t, "media", rend="1920x1080@60",
                                 poolRend="1920x1080@60", mseq=100 + t // 2000,
                                 realAds=ads, decision=dec, chain=6, store=40)

    ev = [E(0, "up", arms=3, types=["site", "embed"], minServe=6, window=8,
            canary=True),
          E(100, "master", chan="gaules", fmt="v2",
            variants=[{"rend": "1920x1080@60", "bw": 6000000, "url": "u"}])]
    ev += [poll(t, 0, "rewrite") for t in (2000, 4000, 6000)]
    # break 1: every ad-bearing poll rewritten, nothing fetched -> blocked
    ev += [poll(20000, 2, "rewrite"), poll(22000, 4, "rewrite"),
           poll(24000, 4, "rewrite"), poll(26000, 1, "rewrite")]
    ev += [poll(t, 0, "rewrite") for t in (28000, 30000, 32000, 34000, 36000)]
    # break 2: a rebind at 40s leaves the next two polls cold -> exposed, and
    # self-inflicted. The 52s miss is 12s out, past NEAR_MS: donor shortage.
    # The 50s clean poll is interior — one clean poll must not split a break.
    ev += [E(40000, "rebind", **{"from": "1280x720@60", "to": "1920x1080@60"},
             retuned=3, minted=1),
           poll(42000, 2, "pass_mismatch"), poll(44000, 3, "pass_mismatch"),
           poll(46000, 5, "rewrite"), poll(50000, 0, "rewrite"),
           poll(52000, 2, "pass_cold")]
    ev += [poll(t, 0, "rewrite") for t in (54000, 56000, 58000)]
    # two distinct ad segments reached the screen, one of them fetched twice
    ev += [E(45000, "segment", url="https://c/ad0.ts", ad=True, via="real", dur=2.0),
           E(47000, "segment", url="https://c/ad1.ts", ad=True, via="real", dur=2.0),
           E(49000, "segment", url="https://c/ad0.ts", ad=True, via="real", dur=2.0),
           E(51000, "segment", url="https://c/l9.ts", ad=False, via="ours", dur=4.167)]
    # these two reverse each other inside FLAP_MS -> one flap; both land after
    # the last miss, so neither can be blamed for one
    ev += [E(62000, "rebind", **{"from": "1920x1080@60", "to": "1280x720@60"}),
           E(68000, "rebind", **{"from": "1280x720@60", "to": "1920x1080@60"})]
    ev += [E(t, "arm", n=0, ptype="site", rend="1920x1080@60", ad=(t == 21000),
             onGrid=(t != 21000), new=2) for t in range(1000, 26000, 5000)]
    ev += [E(t, "arm", n=1, ptype="embed", rend="1920x1080@60", ad=False,
             onGrid=True, new=2) for t in range(1500, 21500, 5000)]
    ev += [E(30000, "regrid", n=0, ptype="site")]
    ev += [E(0, "player", vt=10.0, paused=False, ready=4, ranges=[[0.0, 12.0]]),
           E(5000, "player", vt=15.0, paused=False, ready=4, ranges=[[0.0, 20.0]]),
           E(10000, "player", vt=20.0, paused=False, ready=4,
             ranges=[[0.0, 24.0], [27.5, 40.0]]),
           E(15000, "player", vt=20.0, paused=False, ready=2, ranges=[[0.0, 24.0]]),
           E(20000, "player", vt=25.0, paused=False, ready=4, ranges=[[0.0, 45.0]])]

    # round-trip through JSON so this exercises the same parse path as a file
    a = analyse(parse(json.dumps(e) for e in ev))
    assert a["onscreen"] == 3, a["onscreen"]
    assert a["onscreen_urls"] == 2
    assert len(a["breaks"]) == 2, a["breaks"]
    assert (a["blocked"], a["exposed"]) == (1, 1)
    b1, b2 = a["breaks"]
    assert b1["blocked"] and b1["peak"] == 4 and b1["polls"] == 4
    assert b1["fetched"] == 0 and b1["dec"] == Counter({"rewrite": 4})
    assert not b2["blocked"] and b2["peak"] == 5 and b2["polls"] == 4
    assert b2["t1"] - b2["t0"] == 10000
    assert b2["fetched"] == 3 and b2["urls"] == 2
    assert (a["peaksum"], a["naive"], a["adpolls"]) == (9, 23, 8)
    assert a["orphan"] == 0
    assert len(a["miss"]) == 3
    assert a["missdec"] == Counter({"pass_mismatch": 2, "pass_cold": 1})
    assert (a["selfinflicted"], a["shortage"]) == (2, 1)
    assert len(a["rebinds"]) == 3 and a["flaps"] == 1
    assert a["arms"][0]["polls"] == 5 and a["arms"][0]["ads"] == 1
    assert a["arms"][0]["grid"] == 4 and a["arms"][0]["regrid"] == 1
    assert a["arms"][1]["polls"] == 4 and a["arms"][1]["ads"] == 0
    p = a["play"]
    assert p["wall"] == 20000 and abs(p["adv"] - 15.0) < 1e-9
    assert p["stalls"] == 1 and abs(p["hole"] - 3.5) < 1e-9

    # a fistful of malformed lines: nothing may raise, and none of the answers
    # above may move. The rig ships schema changes before this script sees them.
    junk = [{"t": base + 80000, "ev": "wat", "payload": [1, {"a": None}]},
            {"ev": "media"},                                   # no t at all
            {"t": base + 81000, "ev": "media", "realAds": None, "decision": None},
            {"t": base + 82000, "ev": "segment", "ad": None},
            {"t": base + 83000, "ev": "arm"},
            {"t": None, "ev": "regrid"},
            {"t": base + 84000},                               # no ev
            {"t": base + 85000, "ev": "player", "vt": None, "ranges": "0.0-1.0"}]
    b = analyse(parse([json.dumps(e) for e in ev + junk]
                      + ["", "   ", "{not json", '"a string"', "[]"]))
    for k in ("onscreen", "onscreen_urls", "peaksum", "naive", "adpolls",
              "blocked", "exposed", "selfinflicted", "shortage", "flaps"):
        assert b[k] == a[k], (k, b[k], a[k])
    assert len(b["breaks"]) == 2 and b["arms"][0] == a["arms"][0]

    # rendering must survive both of those and an all-but-empty session
    empty = analyse(parse(['{"t": 1, "ev": "up"}', '{"t": 2, "ev": "nope"}']))
    assert empty["breaks"] == [] and empty["play"] is None
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        show(pathlib.Path("synthetic"), a)
        show(pathlib.Path("synthetic"), b)
        show(pathlib.Path("synthetic"), empty)
    text = out.getvalue()
    assert "SELF-INFLICTED  2 / 3  ( 66.7%)" in text, text
    assert "NO AD SEGMENT WAS EVER FETCHED" in text
    assert "none - no poll ever listed an ad segment" in text
    print("ok")
    return 0


def main(argv):
    if "--selftest" in argv:
        return selftest()
    if {"-h", "--help"} & set(argv):
        print(__doc__.strip())
        return 0
    root = pathlib.Path(__file__).resolve().parents[1] / "data" / "ext"
    path, events = load(argv[1] if len(argv) > 1 else newest(root))
    if not events:
        sys.exit(f"{path} has no parseable events")
    show(path, analyse(events))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
