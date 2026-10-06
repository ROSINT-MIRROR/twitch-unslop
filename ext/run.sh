#!/usr/bin/env bash
# Launch LibreWolf with the unslop extension loaded and sit on a channel.
# Run this in YOUR terminal — it opens a browser window and holds it.
#
#   ./ext/run.sh [channel] [seconds] [level]
#     seconds  omitted/0 = until Ctrl-C
#     level    error|warn|info|debug|trace   (default debug)
#
# Each run writes its own directory, printed on startup and again on exit:
#
#   data/ext/<channel>.<YYmmdd-HHMMSS>/
#     events.jsonl   the structured stream — the source of truth (ext/EVENTS.md)
#     manifests/     every distinct playlist body, deduped by hash
#     ext.log        prose, this run only
#     meta.json      channel, level, argv, versions, start/end, final counters
#
# Read a run with:
#
#   python ext/extreport.py data/ext/<channel>.<YYmmdd-HHMMSS>
#
# Never parse the prose log — it is a rendering of events.jsonl, not the record.
# data/ext/ext.log (top level) still collects every line from every run, and is
# the file to tail while one is in flight.
#
# `level` only controls how much prose is printed; events and manifests are
# recorded in full at any level. Use `trace` when hunting a preroll — it renders
# every intercepted request, master parse, arm poll and serve/passthrough
# decision as it happens:
#
#   ./ext/run.sh nogenerals 0 trace
#
# The stats line is the interesting part:
#   grid=N/M       donors holding clean, on-grid content. While this is 0 the
#                  pool has nothing to splice and you get the REAL playlist —
#                  ads included. An ad beats a black screen.
#   served / pass  playlists we rewrote vs handed through untouched
#   ads            ad segments the donors saw and we never emitted
#   canary=Bbrk/Sseg/Jj
#                  a separate non-donating `embed` session, purely ground truth
#                  for "was there an ad to block at all". Without it, ads=0 is
#                  unreadable: it could mean the splice worked or that nothing
#                  was on offer.
#   regrid         sessions retired after taking an ad — one that has taken an
#                  ad is off the segment grid permanently and cannot donate
#   rebind         times we followed the player changing quality
set -euo pipefail
cd "$(dirname "$0")/.."
exec driver/venv/bin/python ext/tryout.py "${1:-gaules}" "${2:-0}" "${3:-debug}"
