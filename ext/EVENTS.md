# Extension event schema

`data/ext/<session>/events.jsonl` — one JSON object per line, append-only.
`ext.log` is a *rendering* of this stream, not the source of truth. Never parse
the prose log; parse this.

Session dir is `data/ext/<channel>.<YYmmdd-HHMMSS>/`:

```
events.jsonl         the stream
manifests/<hash>.m3u8   playlist bodies worth keeping, deduped by content hash
                        (FNV-1a + base36 length, e.g. `ab12cd34-9z`). The sink
                        falls back to sha1[:12] only if the extension omits it.
```

Bodies are kept when they are **evidence**, not on every poll: any playlist with
`realAds > 0`, anything we did not rewrite, anything served through a ladder
collapse, and the first 20 of a session. Keeping every distinct body ran to
~18MB/hour. The hash is in the event either way, so identity stays provable even
when the body itself was not stored.

Every event has:

| field  | meaning                                      |
|--------|----------------------------------------------|
| `t`    | epoch millis                                 |
| `ev`   | event name                                   |
| `lvl`  | error/warn/info/debug/trace — filtering only  |
| `chan` | channel, on every event that has one         |

`chan` matters: several streams can be open at once, each with its own pool and
its own counters. An event without it cannot be attributed.

## Events

### `up`
Extension started. `arms`, `types[]`, `minServe`, `window`, `canary`.

### `master`
usher master parsed. `chan`, `fmt` (`v1`|`v2`), `variants[]` of
`{rend, bw, url}`. `rend` is always `WxH@FPS` — names do not survive across
the two master formats.

### `media`
The **player's own** media playlist request. One per poll. This is the
central event.

| field      | meaning                                                     |
|------------|-------------------------------------------------------------|
| `rend`     | rendition the player asked for                               |
| `poolRend` | what the pool was bound to                                   |
| `mseq`     | MEDIA-SEQUENCE in the real body                              |
| `realAds`  | ad segments **Twitch put in this session's playlist**        |
| `realSrc`  | `X-TV-TWITCH-STREAM-SOURCE` from the real body               |
| `decision` | `rewrite` \| `pass_cold` \| `pass_mismatch` \| `pass_unknown`|
| `waited`   | ms held before deciding                                      |
| `chain`    | chain length available at decision time                      |
| `store`    | segments in the store                                        |
| `realHash` | manifest hash of the body Twitch sent                        |
| `outHash`  | manifest hash of what we wrote (null on passthrough)         |
| `collapsed`| true when we served our held rendition to a player that a    |
|            | collapsed ladder had forced onto a different one             |

`realAds > 0 && decision == "rewrite"` -> we covered it.
`realAds > 0 && decision != "rewrite"` -> we **exposed** it. Exposed is not
the same as played; see `segment`.

### `segment`
The player actually fetched a segment. **This is the only ground truth for
"did an ad reach the screen."** A manifest listing an ad proves nothing — the
player may never request it.

`url`, `ad` (bool: this URL appeared under a non-`live` EXTINF title in a real
playlist we saw), `via` (`ours` | `real`), `dur`.

### `arm`
Donor poll. `n`, `ptype`, `rend`, `ad`, `onGrid`, `new` (segments added),
`hash`.

### `serve`
The manifest we wrote. `mseq`, `n`, `firstPdt`, `lastPdt`, `hash`.

### `bind`
The pool bound a rendition for the first time. `to`.

Its own event, not a `rebind` with `from: null` — as a rebind it rendered in the
dashboard as "quality change: null to 1080p60" and inflated every rebind and
flap table with a transition that never happened.

### `rebind`
`from`, `to`, `retuned`, `minted`. A quality change the pool followed, which
flushes the store — a miss in the seconds after one is our own doing, not a
donor shortage.

### `ladder_collapse`
usher removed the bound rendition from the master. `had` (variants before),
`now` (variants after), `bound` (the rendition we are holding).

**The earliest ad signal there is.** Measured on gaules 2026-07-29, 4/4 breaks
and no false positives, it lands 4-12s before the first ad segment exists. The
player is *forced* off the source rendition rather than choosing to move, so the
pool holds its binding and keeps its store instead of treating it as a quality
change. Line this up against `media.realAds` to time a break's true start.

### `ladder_restore`
The full ladder came back, or the collapse ceiling expired. `now`, `bound`,
`heldMs`.

### `pool`
Pool lifecycle. `kind`:

| kind    | meaning                                                        |
|---------|----------------------------------------------------------------|
| `open`  | first playlist request for a channel; `pools`                  |
| `quota` | donors parked or woken for the global cap; `from`, `to`, `cap` |
| `idle`  | no player poll for `idleMs`; torn down. `arms`, `pools`        |
| `stop`  | torn down for `reason` (e.g. the user switched unslop off)     |

`idle` is the whole lifecycle rule: liveness is "is this channel's player still
polling", never mute/focus/visibility, which guess at intent and get PiP, second
monitors and audio-only listeners wrong.

### `enable`
The user toggled the extension. `on`.

### `regrid`
A donor took an ad and was retired. `n`, `ptype`.

### `canary`
`kind`: `join` | `break`. `adSegs`, `onGrid`.

### `player`
Video element state, emitted by `tryout.py` not the extension.
`vt` (currentTime), `w`, `h`, `paused`, `ready`, `net`, `ranges[[s,e]...]`,
`err`.

### `stat`
Periodic counter snapshot, every 5s — **one per open channel**, flat, carrying
that pool's counters including `channel`. With no pool open, one event with the
global roll-up instead.

## Wire contract (extension -> sink)

`POST http://127.0.0.1:8779/log`, JSON body:

```json
{
  "lines":     ["INFO  ...", ...],        // prose, for ext.log. may be []
  "events":    [{"t":..., "ev":"...", ...}, ...],   // -> events.jsonl
  "manifests": [{"hash":"ab12cd34-9z", "body":"#EXTM3U\n..."}, ...],
  "stats":     { ... }
}
```

Response: `{"level": "trace"}` — the sink tells the extension what prose level
to print. The background page cannot see argv, so this is how `run.sh`'s third
argument reaches it. **Level never filters `events` or `manifests`**, only
`lines`.

Every field may be absent or empty. The sink must tolerate all of them missing.

## The questions this has to answer

1. **Did an ad reach the screen?** `segment` where `ad==true`. Nothing else
   counts.
2. **Was an ad on offer to this session?** `media.realAds > 0`. Independent of
   the canary, which is an `embed` session on a rejoin loop and overstates.
3. **When we missed, why?** `media.decision` + the `rebind`/`regrid` around it.
   A `ladder_collapse` just before a miss means usher forced the player off our
   rendition — self-inflicted if we then rebound, covered if we held.
4. **What exactly did we hand over?** `manifests/<realHash>` vs
   `manifests/<outHash>`.
