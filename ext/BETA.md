# Unslop — beta

Replaces Twitch's server-inserted ads with clean video.

## What it actually does, before you install it

Twitch stitches ads into the same video stream as the content, from the same
servers, under the same filenames. There is no ad request to block. So Unslop
does something blunter: **it opens 4 extra background sessions to whatever
channel you are watching**, and when Twitch drops an ad into yours, it answers
your player with video from one of those instead.

Three things follow from that, and you should know all three:

- **It uses more bandwidth.** Four extra playlist pollers per channel you watch.
  They fetch playlists, not video, so it is small — but it is not nothing.
- **This almost certainly breaks Twitch's Terms of Service.** Not illegal.
  Twitch can ban accounts. Nobody has been banned for this that we know of,
  which is not the same as it being safe.
- **It is beta and it can fail visibly.** When the clean feed runs short you get
  Twitch's own stream, ads included. A visible ad beats a black screen, so that
  is deliberate. The popup tells you when it is happening.

If any of that is not a trade you want, don't install it.

**Expect a gap right after you install.** For the first ~20 seconds on a stream
there is no clean video buffered yet, so if a break starts in that window you
will see it. The popup says `warming up` while this is true.

## Install

Normal Firefox only installs signed extensions, so use the signed `.xpi`:

1. Download `unslop-<version>.xpi`
2. Open it in Firefox (drag onto the window, or Ctrl-O)
3. Accept the permission prompt
4. Open a Twitch stream. The shield icon appears in the toolbar.

Unsigned builds only install in Firefox Developer Edition or Nightly with
`xpinstall.signatures.required=false` in `about:config`, or temporarily via
`about:debugging` → This Firefox → Load Temporary Add-on (gone on restart).

Chrome and Brave are not supported yet.

## Using it

Click the shield. One number, one state:

| state | meaning |
|---|---|
| **covered** | You are on a clean feed. |
| **warming up** | Building one. Takes ~20s after you open a stream. |
| **ad break incoming** | Twitch signalled a break before it started and we are holding through it. Not a problem. |
| **exposed** | The clean feed ran short. Twitch's stream is going through, ads included. Usually clears in seconds. |
| **off** | Switched off. |

**Details** opens the full page: every number with an explanation, a live event
feed, and how the thing works.

## What we need from you

The useful report is not "it worked". It is:

- **an ad you actually saw** — channel, roughly when, and what the popup said at
  the time. Whether the popup said `exposed` is the single most useful bit.
- **playback that stuttered, froze, or fell out of sync with chat**
- **quality that changed on its own and stayed wrong**
- **anything that behaves differently with several Twitch tabs open** — this is
  the newest code and the least tested.

Ads are far more common when you *join* a stream than when you sit on one. If
you want to test it hard, reload the page a few times.

## Turning it off

The toggle in the popup switches it off for every stream at once, immediately.
No reload needed. Remove it from `about:addons`.

## Privacy

No account, no telemetry, no server. Nothing about your viewing leaves your
machine.

The one exception is deliberate and off unless you go looking for it: the
extension tries to POST debug logs to `127.0.0.1:8779` — your own machine, not
ours — for a few seconds at startup. Unless you are running the developer log
collector, nothing is listening, it gives up after five attempts and stops for
good.
