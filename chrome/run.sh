#!/usr/bin/env bash
# Load the Chrome build of unslop into Chrome and sit on a channel.
#
#   ./chrome/run.sh <channel> [seconds] [mode] [level]
#
#     seconds  default 45, hard-capped at 120 — bounded runs only, the long
#              sessions are the user's to run
#     mode     rewrite (default) | observe
#              observe is the CONTROL ARM: hook installed, pool running,
#              everything logged, player handed Twitch's own body untouched.
#              Each media event then carries `would`, which says whether the
#              pool had a clean chain ready for that exact poll — so "the pool
#              was warm" stops being a claim the hook makes about itself.
#     level    error|warn|info|debug|trace (default debug). Prose only; events
#              and manifests are recorded in full at any level.
#
# Writes data/chrome/<channel>.<YYmmdd-HHMMSS>/{events.jsonl,manifests/,
# ext.log,meta.json,summary.json}. Profile is in-tree, per mode, never /tmp.
#
#   python ext/extreport.py data/chrome/<channel>.<YYmmdd-HHMMSS>
#
# reads it unchanged — the event schema is the same one ext/EVENTS.md defines.
#
# Sessions opened: the tab, plus the extension's own donor pool (4 arms, ARMS
# in ext/core.js). The Firefox rig runs 4 more if it is up. CLAUDE.md caps the
# whole box at ~16 playlist pollers.
#
# NOTE on binaries: chromedriver comes from the `chromium` package (150) and
# refuses google-chrome-stable (149) outright. drive.py picks whichever
# installed browser matches chromedriver's major version; override with
# CHROME_BIN=/path/to/chrome. HEADLESS=1 for --headless=new.
set -euo pipefail
cd "$(dirname "$0")/.."
exec driver/venv/bin/python chrome/drive.py \
  "${1:-gaules}" "${2:-45}" "${3:-rewrite}" "${4:-debug}"
