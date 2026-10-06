# Twitch HLS delivery — what we're actually measuring

Everything below marked **[obs]** was observed live through our own tap on
2026-07-28 (channel `xqc`, anon, RO exit). Unmarked = inferred, verify it.

## Chain **[obs]**

```
1. gql.twitch.tv/gql          op: PlaybackAccessToken
                              -> { value: <plaintext JSON>, signature: <hmac> }
2. usher.ttvnw.net/api/channel/hls/<chan>.m3u8?token=..&sig=..
                              -> MASTER playlist (variant list)
3. <pop>.playlist.ttvnw.net/v1/playlist/<blob>.m3u8      e.g. euc13.
                              -> MEDIA playlist (segment list, re-polled ~2s)
4. <hash>.j.cloudfront.hls.ttvnw.net/v1/segment/<blob>.ts?dna=..
   or <hash>.rufio.hls.live-video.net/v1/segment/...
                              -> the actual segments
```

NOTE: the old `video-weaver.*` / `video-edge-*.abs.hls.ttvnw.net` names that
every blog post still cites are **stale**. Current topology is
`<pop>.playlist.ttvnw.net` for variants and CloudFront/rufio for segments.
The tap's host filter covers all three families.

Only step 1 knows who you are. 2–4 are dumb CDN that obey the token.
**Targeting is decided at token-mint time.**

## The token (step 1) — plaintext, signed, not encrypted **[obs]**

Ad/quality decisioning inputs actually present in `value`:

```
channel, channel_id, device_id, user_id, user_ip, ci_gb, geoblock_reason
subscriber, turbo, partner, privileged, role
show_ads, hide_ads, server_ads, adblock, blackout_enabled, mature
player_type   <- site | embed | frontpage | thunderdome | picture-by-picture
platform, expires, version, https_required
chansub: { restricted_bitrates[], view_until }
maximum_resolution, maximum_video_bitrate_kbps
maximum_resolution_reasons: { QUAD_HD: [...], ULTRA_HD: [...] }
maximum_video_bitrate_kbps_reasons: [...]
```

Anon sample returned `server_ads:true, show_ads:true, hide_ads:false,
subscriber:false, turbo:false`, and — the interesting part — capped quality
with explicit reason codes:

```
maximum_resolution_reasons.QUAD_HD  = ["AUTHZ_GEO","AUTHZ_NOT_LOGGED_IN"]
maximum_video_bitrate_kbps_reasons  = ["AUTHZ_DISALLOWED_BITRATE"]
```

So the token gates **quality on auth state and geo**, not just ads. That's a
second dependent variable worth logging — same experiment, free extra axis.

Can't forge (sig), but we vary the *inputs* and observe what usher returns.

## How the manifest "switches" — it doesn't

Server-side ad insertion. Ad segments splice into the **same** media playlist,
**same** hostname, **same** URL namespace as live content. No separate ad
domain, by design.

### Three independent tells — MEASURED reliability

Ranked by hit rate across 300+ real captured breaks (see `notes/FINDINGS.md`,
regenerate with `python report.py`). This ranking **corrects** the original
guess in this file, which had it backwards:

1. **stitched-ad DATERANGE** — ~100%. The most reliable, and it can fire
   BEFORE any ad segment appears, so it doubles as an early warning.
2. **`#EXTINF:2.000,<title>`** — ~99%. Title reads `live` during content and
   `Amazon|<creative-id>` during an ad. Per-segment, so this is the one to
   FILTER on: it tells you exactly which bytes to drop.
3. **`X-TV-TWITCH-STREAM-SOURCE`** — only ~80%. Misses a fifth of breaks.
   Do not rely on it alone; it was originally described here as a solid
   playlist-level tell and that was wrong.

Use DATERANGE to predict, title to filter. Details of the DATERANGE:
   ```
   #EXT-X-DATERANGE:ID="stitched-ad-..",CLASS="twitch-stitched-ad",
       START-DATE=..,DURATION=30.0,
       X-TV-TWITCH-AD-ROLL-TYPE=MIDROLL|PREROLL,
       X-TV-TWITCH-AD-POD-LENGTH=2,X-TV-TWITCH-AD-POD-POSITION=1,
       X-TV-TWITCH-AD-ADVERTISER-ID=..,X-TV-TWITCH-AD-LINE-ITEM-ID=..,
       X-TV-TWITCH-AD-CREATIVE-ID=..,X-TV-TWITCH-AD-ORDER-ID=..
   #EXT-X-DISCONTINUITY   <- timeline reset in/out
   ```

The tap flags a sample as `media_ad` if **any** of the three fires, so a change
in Twitch's tagging can't silently blind the capture.

### DATERANGE classes seen on a clean live playlist **[obs]**

`timestamp` (X-SERVER-TIME), `twitch-session` (X-TV-TWITCH-SESSIONID),
`twitch-stream-source` (X-TV-TWITCH-STREAM-SOURCE), `twitch-trigger`
(X-TV-TWITCH-TRIGGER-URL — a callback on `<pop>.playlist.ttvnw.net/trigger/..`,
worth watching; plausibly the ad-decision hook).

Also per-playlist: `#EXT-X-TWITCH-LIVE-SEQUENCE`, `#EXT-X-TWITCH-ELAPSED-SECS`,
`#EXT-X-TWITCH-TOTAL-SECS`, `#EXT-X-TWITCH-PREFETCH` (x2, LL-HLS lookahead).

## Master playlist recon **[obs]**

`#EXT-X-TWITCH-INFO:` carries far more than the old docs claim:

```
NODE, MANIFEST-NODE, MANIFEST-NODE-TYPE (weaver_cluster), MANIFEST-CLUSTER,
CLUSTER (cloudfront_prod_use12_twitch_ispc), ORIGIN, ABS,
SERVER-TIME, STREAM-TIME, BROADCAST-ID, VIDEO-SESSION-ID, SERVING-ID,
USER-IP, USER-COUNTRY, SUPPRESS, FUTURE,
TRANSCODESTACK (2025-Transcode-ELT-V1), TRANSCODEMODE (cbr_v1),
B, C, D, E   <- C and E are base64 segment URLs (rufio.hls.live-video.net)
```

`USER-COUNTRY` + `SERVING-ID` + `VIDEO-SESSION-ID` are the join keys for
correlating arms of the experiment.

## Splicing two sessions — use PDT, NOT LIVE-SEQUENCE **[obs]**

This file originally claimed `#EXT-X-TWITCH-LIVE-SEQUENCE` was the join key
for aligning an ad session against a clean one. **That was wrong; the capture
disproved it.** Measured over 300+ real ad manifests:

```
tag                          clean playlist   during an ad
PROGRAM-DATE-TIME            1 per segment    1 per segment   (~100%)
EXT-X-TWITCH-LIVE-SEQUENCE   present          ABSENT          (~50%)
EXT-X-MEDIA-SEQUENCE         live-aligned     session-local, restarts
```

LIVE-SEQUENCE disappears exactly when you need it. `#EXT-X-PROGRAM-DATE-TIME`
is wall-clock, present in both, and identical across sessions for the same
broadcast moment — that is the splice key.

**Ads REPLACE content in wall-clock time**, they don't delay it. A clean
parallel session therefore holds the segments the ad session missed.

### Segment cadence and the post-ad re-cut **[obs 2026-07-29]**

An earlier version of this file claimed independent sessions phase their PDTs
differently (`.557` vs `.792`) and told you to treat anything within ~0.75 of
a slot as the same moment. **That was wrong on both counts**, and the tolerance
it prescribed was actively hiding the real effect. Measured on `gaules` with
three concurrent sessions:

- Sessions that have **not** taken an ad emit **byte-identical PDTs** for the
  same broadcast moment. min |dPDT| = 0 across all three. Clean segments
  dedupe on the exact PDT; no tolerance is needed or wanted.
- There is **no single segment cadence**:

```
content segments   4.166 / 4.167s   (250 frames at 60fps, TARGETDURATION 6)
ad segments        2.000s exactly
final ad segment   TRIMMED — measured 1.235s closing a 15.235s pod
```

- An ad pod is not a whole number of content segments. Twitch trims the last
  ad segment and **re-cuts the content timeline from wherever the pod ended**.
  A session that has taken an ad is therefore permanently offset from one that
  hasn't — measured 1.43s, and it does not re-converge.

Consequence for splicing: you cannot union segments from arms with different
ad histories. Both are valid video, but they start 1.43s apart while each
declares 4.167s, so the merged playlist emits overlapping segments. Walk a
chain instead — drop anything starting before the previous segment ends.

### The offset is permanent, and a rejoin clears it **[obs 2026-07-29]**

Five concurrent arms on `gaules`, 90s, `data/unslop/gaules.0729-150058.jsonl`:

| arm | took ad | clean segs | on the shared grid |
|---|---|---|---|
| `site`, `frontpage`, `site` | no | 89 | 89 (100%) — byte-identical PDTs |
| `embed` x2 | yes (join preroll) | 39 | 0 (0%), all 1430-1432ms off |

Neither ad-taking arm ever returned: 0 of the 39 clean segments they produced
*after* their pod ended landed back on the grid. So the re-cut is not a
transient — **once a session takes an ad it is off-grid for the rest of its
life, and is worthless as a donor no matter how clean its video is.**

The grid itself is not per-session: three arms that minted at different times
and never took an ad emitted byte-identical PDTs. It is the origin's grid, and
**a fresh mint lands back on it**. So the donor strategy is to retire a session
the moment its pod ends rather than to keep it. `unslop.py` does this
(`Arm.retire_session`, `--no-regrid` to measure the naive case); it cut skew
from 38/68 segments to 10/76 on the runs above.

Watch the interaction: prerolls fire on join, so a retire-and-rejoin can draw a
fresh ad. Hence the 30s cooldown — without it a high-ad-rate arm mint-loops.

This is why a phase-agnostic coverage number is not the go/no-go. An interval
union counts a moment as covered whenever *any* arm held content for it, even
when that arm is on a phase the player cannot splice onto. `coverage.py`
reports both: the union as an upper bound, and `SPLICEABLE` — an exact
weighted-interval-scheduling DP over the distinct clean segments — as the real
one.

The old fixed 2000ms grid also put an `#EXT-X-DISCONTINUITY` between every
pair of segments on a completely clean stream, because real spacing (4167ms)
exceeded the 1.5-slot gap threshold (3000ms). mpv tolerates that; a browser
player resetting its timeline every segment would not. Use the segment's own
declared EXTINF duration for every alignment decision.

## What the real web player does differently **[obs 2026-07-29]**

Everything above was measured against our own curl/python sessions. The browser
does not behave the same way, and every difference silently disabled the
extension rather than raising an error.

**Two master endpoints, two formats.** Our rig calls
`usher.ttvnw.net/api/channel/hls/<chan>.m3u8`. The web player calls
`/api/v2/channel/hls/<chan>.m3u8`, and the response is structured differently:

```
v1   #EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="chunked",NAME="1080p60 (source)"
     #EXT-X-STREAM-INF:...,VIDEO="chunked",FRAME-RATE=60.000
v2   (no #EXT-X-MEDIA lines at all)
     #EXT-X-STREAM-INF:...,FRAME-RATE=60.000,STABLE-VARIANT-ID="1080p60",
                           IVS-NAME="1080p60",IVS-VARIANT-SOURCE="transcode"
```

Same rendition, same bandwidth, different name — `chunked` vs `1080p60`. So a
donor pool minting through v1 and a player on v2 never agree on a rendition by
name. **Key on `RESOLUTION@FRAME-RATE`** (`1920x1080@60`); it is what has to
match for a splice to be legal anyway, and both formats carry it.

**The player fetches playlists from a worker.** `webRequest` reports
`tabId: -1` for those, which is also what an extension's own `fetch` reports.
Telling them apart by tabId disables the extension completely and silently;
use `originUrl`/`documentUrl` starting `moz-extension://`.

**ABR probes low.** The player's first media request is a low rendition
(measured 640x360@30) and it climbs to source within about a second. Binding a
donor pool to "the rendition the player first asked for" binds it to the probe
and every later playlist is a mismatch.

**MEDIA-SEQUENCE must not step backwards.** Serving a rewritten playlist
starting at 0 while Twitch was at 2295 froze playback at `readyState 2`
permanently — the engine reads it as a playlist reset. Seed from the real
playlist being replaced.

**Do not hand over the real playlist and swap later.** Passing through while
warming up and then taking over mid-stream is what caused that freeze. Hold the
response until the pool can serve, so the player's first manifest is already
ours. But hold ONLY on the first bind: holding after a quality change starves
the player's bandwidth estimate, which drops quality, which triggers another
rebind — measured as four rebinds in 19s flapping 1080<->360.

**Segment duration cannot identify an ad.** Stitched ads are exactly 2.000s,
but `nogenerals` serves 2.000s *content* segments while `gaules` serves
4.166/4.167s. Only the `#EXTINF` title is reliable.

## Second, tag-independent signal **[obs]**

`ffprobe` on the actual bytes separates ad from content with zero overlap:

```
AD    256x144 @30fps   /  1920x1080 @30fps
LIVE  284x160 @30fps   /  1280x720  @60fps
```

Twitch's ladder uses non-standard sizes (284x160); ads use standard ones
(256x144), and ads were never observed at 60fps. So segments can be classified
from bytes alone, trusting no Twitch metadata.

## Experiment design

Same channel, same wall clock, N concurrent arms:
- anon (no cookies)
- logged-in, non-sub
- logged-in, sub to that channel
- `player_type=embed` vs `site`

Dependent variables: ad-break timeline, **and** the quality cap
(`maximum_resolution_reasons`). Diff across arms to isolate which token field
actually moves the needle vs. which is decoration.

## Capture gotchas (learned the hard way)

- **HTTP/3**: Twitch edges speak h3 over QUIC. mitmproxy does not proxy QUIC.
  Leave h3 enabled and segments vanish while GraphQL still shows up — looks
  like a working capture, isn't. Killed in `browser/user.js`.
- **geckodriver clones the profile into `/tmp`** unless you pass
  `-profile <abspath>`. Handled in `driver/drive.py`.
- **LibreWolf defaults**: RFP, ETP strict, `privacy.query_stripping` (eats
  `?token=&sig=`!) and sanitize-on-shutdown (nukes login cookies). All undone
  in `browser/user.js` — we're measuring Twitch, not LibreWolf.
- **mitm CA** goes into the profile's NSS db (`certutil -d sql:<profile>`),
  not the OS trust store.
- **usher token must be URL-encoded** — it's raw JSON. `curl -G
  --data-urlencode` or the request silently 4xx's.
