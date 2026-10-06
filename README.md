# twitch-unslop

Research tooling for the passive reverse-engineering of Twitch HLS delivery and
server-side ad insertion (SSAI), plus a proof-of-concept that reconstructs an
ad-free playlist from several parallel clean sessions.

This is instrumentation for understanding how Twitch stitches ads into the HLS
media playlist, not a product. It observes the delivery layer and mints its own
playback tokens exactly as a normal player does; it never logs into an account
and never modifies a flow in flight.

## What it does

A stitched ad on Twitch replaces content in wall-clock time rather than delaying
it, so a second session on the same channel that did not take that ad still holds
the segments the first one missed. Everything here is built on that one fact:

- **hunt.py** — headless multi-arm ad hunter. Mints its own GraphQL playback
  tokens, polls the media playlist as a player would, and records ad breaks via
  three independent tells (the `#EXTINF` title, `X-TV-TWITCH-STREAM-SOURCE`, and
  `#EXT-X-DATERANGE` ad markers). The data source for everything else.
- **unslop.py** — runs several independent sessions on one channel, merges their
  clean media playlists into one, and serves it on `127.0.0.1`. Ad segments are
  never emitted; their slot is filled from whichever session is still clean. It
  serves only the ~2KB manifest, so it costs no video bandwidth — the player
  still fetches segment bytes straight from Twitch's CDN.
- **coverage.py** — scores an `unslop` ledger by interval overlap: of the
  wall-clock time some session spent in an ad, how much another session still
  held clean and spliceable.
- **ext/** — the same splice as a Firefox (MV2) extension, running inside real
  Twitch playback. **chrome/** — the Chrome (MV3) port; it shares most of its
  code with `ext/` via symlink and adds a `document_start` MAIN-world hook.
- **combotest / cookietest / gatetest / headertest / geoprobe** — controlled
  experiments isolating what drives ad eligibility (identity, headers, the
  trigger URL, geography via SOCKS). Kept for reproducibility.
- **mitm/addons/tap.py** — an optional passive mitmproxy addon for observing the
  real browser's own traffic (GraphQL, usher, spade, pubsub). Read-only.

## Run

Headless (no browser):

```sh
./selftest.sh                 # verify the rig, curl only
python hunt.py 6.5            # multi-arm ad hunter -> data/hunt/<session>/
python unslop.py <channel>    # ad-free POC; mpv http://127.0.0.1:8778/playlist.m3u8
python coverage.py data/unslop/<chan>.<ts>.jsonl
```

Browser extension:

```sh
./ext/run.sh <channel> 0 trace    # LibreWolf + the Firefox extension
node ext/selftest.mjs <channel>   # splice logic only, no browser
```

## Install (Firefox)

Download the signed extension from the latest release:

<https://github.com/ROSINT-MIRROR/twitch-unslop/releases/latest/download/twitch-unslop.xpi>

It is signed by Mozilla through the unlisted channel, so it installs on normal
release Firefox. Opening the install link from rosint.org installs it in the
same tab; Firefox may show its yellow permission bar once, with a "Continue to
Installation" button — click it, then accept the install prompt. After
installing, the toolbar button opens the popup; open a Twitch channel and it
runs its own donor pool.

Verify the download (release `v0.1.0`):

```
sha256  23b64b3a90fea8d9d76f0fd6c3fbefad4d4888047b541106c6f295d62ba79623  twitch-unslop.xpi
```

No store listing is involved — the add-on is self-distributed, not published on
addons.mozilla.org. If you build your own unsigned `.xpi` with `ext/build.sh`
instead, it only loads on Firefox Developer Edition / Nightly / unbranded-ESR
with `xpinstall.signatures.required=false`, or temporarily via
`about:debugging#/runtime/this-firefox` -> Load Temporary Add-on.

## Install (Chrome)

Chrome is **not a one-click install** off the Web Store, by design. Chrome only
accepts a one-click `.crx` from its own Web Store, and we do not use the store.
The supported direct path is Load unpacked:

1. Download the zip:
   <https://github.com/ROSINT-MIRROR/twitch-unslop/releases/latest/download/twitch-unslop-chrome.zip>
2. Unzip it to a folder you keep (Chrome loads it from that path, so don't delete it).
3. Open `chrome://extensions`.
4. Turn on **Developer mode** (top right).
5. Click **Load unpacked** and pick the unzipped folder (the one with `manifest.json`).

Chrome shows a standing "Developer mode extensions" warning — that is expected
for any non-store extension and is not an error. The Web Store is the only
one-click route and is intentionally not used here, so there is no `.crx`.

Verify the download (release `v0.1.0`):

```
sha256  d3614ea6e646923133cb498c6ec6553be2bee6f5f706bc3493eeb50ced4c4a46  twitch-unslop-chrome.zip
```

Passive browser tap (mitmproxy):

```sh
. ./env.sh
./browser/setup-profile.sh        # once: build profile + trust the mitm CA
./mitm/run.sh baseline            # terminal 1
python driver/drive.py <chan> 30  # terminal 2
```

`env.sh` is fully path-relative (`$LAB` is the repo root); source it before the
browser path. The mitmproxy CA and the browser profile it builds are generated
locally and are deliberately not tracked.

## Requirements

Python 3, `curl`, `jq`, `ffprobe` (for the segment baseline), `mpv` to watch the
POC output. The browser path additionally needs LibreWolf (or Firefox),
`geckodriver` + `selenium`, and `mitmproxy`. Node is needed only for the
extension self-test.

## Notes & TODO

- **No ladder substitution.** usher orders the variant ladder differently per
  `player_type` and some types (e.g. `thunderdome`) carry no `720p60` at all.
  Both `unslop.py` and `hunt.py` match the requested rendition exactly and fail
  loudly when it is absent, rather than silently splicing two resolutions.
- `hunt.py` currently partitions its channel `POOL` into three slices of eight
  and leaves any channels past the first 24 unused. Harmless, but the arm table
  should be widened to cover the whole pool.

## Legal

This is research and measurement tooling. Using it to watch Twitch, automate a
client, or bypass advertising may conflict with Twitch's Terms of Service and
with the rights of broadcasters and advertisers. It is published for study of
HLS/SSAI mechanics and for defensive and interoperability research. You are
responsible for how you use it and for staying within applicable terms and law.
Keep the poller count low — the tools cap concurrency on purpose so a run never
resembles a load test.
