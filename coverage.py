#!/usr/bin/env python3
"""
Score a unslop.py ledger. Answers the one question that decides whether any of
this is worth porting to a browser extension:

  while an arm was in an ad, did another arm still hold that wall-clock time?

Ads and content are on different grids — measured 2.000s ad segments against
4.166/4.167s content — so this is done as interval overlap, not by bucketing
into slots. Every arm observation is [pdt, pdt+dur). Uncovered ad time is
wall-clock seconds the player would have nothing to show.

  python coverage.py data/unslop/<chan>.<ts>.jsonl [...]

Reads only what unslop.py wrote. Nothing here is estimated.
"""
import bisect
import json
import pathlib
import sys
from collections import Counter, defaultdict

GAP_MS = 500     # mirrors unslop.py — slack before a gap is a real hole


def load(paths):
    segs, events = [], []
    for p in paths:
        for line in pathlib.Path(p).read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            (segs if r.get("ev") == "seg" else events).append(r)
    return segs, events


def merge(iv):
    """Union of [start, end) intervals, sorted and coalesced."""
    out = []
    for s, e in sorted(iv):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def subtract(a, b):
    """Intervals in a not covered by b. Both must be merged."""
    out, j = [], 0
    for s, e in a:
        cur = s
        while j < len(b) and b[j][1] <= cur:
            j += 1
        k = j
        while k < len(b) and b[k][0] < e:
            if b[k][0] > cur:
                out.append([cur, min(b[k][0], e)])
            cur = max(cur, b[k][1])
            if cur >= e:
                break
            k += 1
        if cur < e:
            out.append([cur, e])
    return [x for x in out if x[1] > x[0]]


def total(iv):
    return sum(e - s for s, e in iv)


def spliceable(segs):
    """The longest non-overlapping timeline that could actually be served.

    The union number below is a phase-agnostic UPPER BOUND: it counts a moment
    as covered whenever any arm held content for it, even when that arm is on a
    phase the player cannot splice onto. That is not a corner case — measured
    2026-07-29 on gaules, arms that took an ad came back 1431ms off the grid
    their ad-free peers shared (0 of 39 post-ad segments back on grid, no
    re-convergence), so a donor is either in phase or worthless.

    So: exact weighted-interval scheduling over the distinct clean segments,
    maximising served wall-clock. This is what unslop.py's chain() approximates
    greedily at run time, computed optimally here.
    """
    iv = {}
    for r in segs:
        if r["ad"]:
            continue
        s = r["pdt"]
        e = s + int(r["dur"] * 1000)
        iv[s] = max(iv.get(s, 0), e)
    items = sorted(iv.items(), key=lambda x: x[1])      # by end
    ends = [e for _, e in items]
    best = [0] * (len(items) + 1)
    keep = [False] * len(items)
    for i, (s, e) in enumerate(items):
        j = min(bisect.bisect_right(ends, s + GAP_MS), i)
        cand = best[j] + (e - s)
        if cand > best[i]:
            best[i + 1], keep[i] = cand, True
        else:
            best[i + 1] = best[i]
    out, i = [], len(items) - 1
    while i >= 0:
        if keep[i]:
            s, e = items[i]
            out.append([s, e])
            i = min(bisect.bisect_right(ends, s + GAP_MS), i) - 1
        else:
            i -= 1
    return merge(out)


def hms(ms):
    s = ms / 1000
    return f"{int(s // 3600)}h{int(s % 3600 // 60):02d}m{s % 60:04.1f}s"


def selftest():
    assert merge([[0, 5], [3, 8], [20, 25]]) == [[0, 8], [20, 25]]
    assert merge([]) == []
    assert subtract([[0, 10]], [[2, 4]]) == [[0, 2], [4, 10]]
    assert subtract([[0, 10]], [[0, 10]]) == []
    assert subtract([[0, 10]], []) == [[0, 10]]
    assert total([[0, 10], [20, 25]]) == 15

    seg = lambda p, d, a: {"pdt": p, "dur": d, "ad": a}
    # a lone clean arm: the whole timeline is spliceable
    grid = [seg(p, 4.167, False) for p in range(0, 30000, 4167)]
    assert total(spliceable(grid)) > 29000

    # an arm in ads 10-20s, with the ONLY donor 1431ms off the grid. The union
    # calls that fully covered; a player cannot splice it. This is the case the
    # phase-agnostic number gets wrong, measured for real on gaules 2026-07-29.
    segs = ([seg(p, 4.167, False) for p in range(0, 10000, 4167)]
            + [seg(p, 2.0, True) for p in range(10000, 20000, 2000)]
            + [seg(p + 1431, 4.167, False) for p in range(10000, 20000, 4167)]
            + [seg(p, 4.167, False) for p in range(20004, 30000, 4167)])
    iv = lambda pred: merge([[r["pdt"], r["pdt"] + int(r["dur"] * 1000)]
                             for r in segs if pred(r)])
    ad, clean, ch = iv(lambda r: r["ad"]), iv(lambda r: not r["ad"]), spliceable(segs)
    assert total(subtract(ad, clean)) == 0, "union should see it as covered"
    assert total(subtract(ad, ch)) > 0, "spliceable must not"
    assert all(ch[i][1] <= ch[i + 1][0] for i in range(len(ch) - 1)), "overlap"
    print("coverage.py selftest ok")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip())
    if sys.argv[1] == "--selftest":
        return selftest()
    segs, events = load(sys.argv[1:])
    if not segs:
        sys.exit("no segment records in ledger")

    by_arm = defaultdict(lambda: {"clean": [], "ad": []})
    for r in segs:
        s = r["pdt"]
        by_arm[r["arm"]]["ad" if r["ad"] else "clean"].append(
            [s, s + int(r["dur"] * 1000)])

    arms = sorted(by_arm)
    clean_all = merge([i for a in arms for i in by_arm[a]["clean"]])
    ad_all = merge([i for a in arms for i in by_arm[a]["ad"]])
    span = [min(i[0] for i in clean_all + ad_all),
            max(i[1] for i in clean_all + ad_all)]
    wall = span[1] - span[0]

    # ad time that no arm covered with real content
    uncovered = subtract(ad_all, clean_all)
    # gaps in what we could serve at all, ads aside
    holes = subtract([span], clean_all)
    # the same, restricted to a timeline a player can actually be handed
    chain = spliceable(segs)
    unspliceable = subtract(ad_all, chain)

    print(f"ledger    {len(segs)} observations, {len(arms)} arms")
    print(f"          {hms(wall)} wall clock")
    print()

    print("=== per-arm ===")
    for a in arms:
        c, d = merge(by_arm[a]["clean"]), merge(by_arm[a]["ad"])
        seen = total(c) + total(d)
        print(f"  {a:20s} clean={hms(total(c)):>14s}  ad={hms(total(d)):>12s}"
              f"  ad_share={total(d) / max(1, seen) * 100:5.2f}%"
              f"  gap={hms(wall - seen)}")
    print()

    print("=== coverage ===")
    print(f"  wall clock observed    {hms(wall)}")
    print(f"  someone was in an ad   {hms(total(ad_all))}"
          f"  ({total(ad_all) / wall * 100:.2f}%)")
    if ad_all:
        cov = total(ad_all) - total(uncovered)
        spl = total(ad_all) - total(unspliceable)
        print(f"  ...covered by a donor  {hms(cov)}"
              f"  ({cov / total(ad_all) * 100:.2f}%)   upper bound, any phase")
        print(f"  ...uncovered           {hms(total(uncovered))}"
              f"  in {len(uncovered)} stretches")
        print(f"  ...SPLICEABLE          {hms(spl)}"
              f"  ({spl / total(ad_all) * 100:.2f}%)   <-- THE NUMBER")
        print(f"  ...lost to phase       {hms(cov - spl)}"
              f"  a donor held it, off-grid")
    print(f"  total unservable time  {hms(total(holes))}"
          f"  ({total(holes) / wall * 100:.3f}% of wall clock)")
    if holes:
        w = sorted(holes, key=lambda x: x[0] - x[1])[:5]
        print("  worst unservable stretches:")
        for s, e in w:
            in_ad = any(s < b and a < e for a, b in ad_all)
            print(f"    {(e - s) / 1000:7.2f}s  "
                  f"{'during an ad' if in_ad else 'no arm reported — poll miss'}")
    print()

    if ad_all:
        print("=== ad breaks ===")
        hrs = wall / 3.6e6
        print(f"  count {len(ad_all)}   rate {len(ad_all) / max(hrs, 1e-9):.2f}/h")
        for s, e in sorted(ad_all, key=lambda x: x[0] - x[1])[:8]:
            miss = total(subtract([[s, e]], clean_all))
            who = sorted({a for a in arms
                          for x, y in by_arm[a]["ad"] if x < e and s < y})
            print(f"    {(e - s) / 1000:6.1f}s  uncovered={miss / 1000:6.2f}s"
                  f"  hit={who}")
        print()

    ev = Counter(e.get("ev") for e in events)
    if ev:
        print("=== arm events ===")
        print("  " + "  ".join(f"{k}={v}" for k, v in sorted(ev.items())))
        for e in events:
            if e.get("ev") in ("err", "rejoin"):
                print(f"    {e.get('arm')}: {e.get('msg') or e.get('code')}")
        print()

    if not ad_all:
        print("VERDICT: no ads in this ledger — it proves nothing about "
              "coverage. Rerun on a channel/window that actually serves them.")
    elif not unspliceable:
        print("VERDICT: every second of ad time had an on-grid donor. "
              "Splice is viable — port it.")
    else:
        u = total(unspliceable)
        print(f"VERDICT: {u / 1000:.1f}s of ad time had no spliceable donor "
              f"({u / total(ad_all) * 100:.1f}% of ad time). The player "
              f"stalls or skips there.")
        if total(uncovered) < u:
            print(f"         {(u - total(uncovered)) / 1000:.1f}s of that was "
                  f"held by an off-grid arm — reachable if donors are kept "
                  f"in phase (unslop.py rejoins after an ad; --no-regrid "
                  f"disables that).")


if __name__ == "__main__":
    main()
