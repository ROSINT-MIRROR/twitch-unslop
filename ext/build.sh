#!/usr/bin/env bash
# Build an installable Unslop package.
#
#   ./ext/build.sh                    -> dist/unslop-<version>.xpi      (Firefox)
#   ./ext/build.sh --target=chrome    -> dist/unslop-chrome-<version>.zip
#
# Firefox
# -------
# The .xpi is a plain zip with manifest.json at the ROOT — zip the contents of
# ext/, never the ext/ directory itself, or Firefox rejects it with a useless
# error. That is the one thing that actually goes wrong here.
#
# Unsigned installs only in Developer Edition / Nightly with
# xpinstall.signatures.required=false, or temporarily via about:debugging.
# For real testers on normal Firefox, sign it (no global install needed):
#
#   npx web-ext sign --channel=unlisted \
#       --api-key=$AMO_JWT_ISSUER --api-secret=$AMO_JWT_SECRET \
#       --source-dir=ext --artifacts-dir=dist
#
# Credentials come from addons.mozilla.org -> Tools -> Manage API Keys.
# `unlisted` is automated review, minutes not weeks, and you host the file
# yourself. `listed` puts it on AMO and queues it for a human.
#
# Chrome
# ------
# Same engine, different shell: chrome/ holds an MV3 service worker, an
# offscreen document running the pool, and the MAIN-world hook, and it SYMLINKS
# core.js and the whole UI back to ext/. A zip cannot carry a symlink usefully,
# so the chrome target stages a real copy into dist/unslop-chrome/ with `cp -L`
# and zips that. The staging directory is left in place: it is what a tester
# loads with chrome://extensions -> Developer mode -> Load unpacked, and it is
# what --load-extension takes.
#
# For a tester who cannot use Developer mode, upload the .zip to the Chrome Web
# Store developer dashboard (one-off $5 registration) and distribute it
# unlisted. There is no offline equivalent of Firefox's unlisted signing: a .crx
# side-loaded outside the store is disabled by Chrome on next launch.
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET=firefox
for a in "$@"; do
  case "$a" in
    --target=*) TARGET="${a#*=}" ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done

check_js() {   # a broken script installs fine and then does nothing at all,
  for js in "$@"; do          # which is the worst possible failure to hand a
    node --check "$js" >/dev/null || { echo "FAIL $js" >&2; exit 1; }
  done                        # tester. Syntax-gate every file before packaging.
}

check_json() {
  python3 -c "import json,sys;json.load(open(sys.argv[1]))" "$1" \
    || { echo "FAIL $1 is not valid JSON" >&2; exit 1; }
}

case "$TARGET" in
firefox)
  VERSION=$(python3 -c 'import json;print(json.load(open("ext/manifest.json"))["version"])')
  OUT="dist/unslop-${VERSION}.xpi"

  # Everything the extension needs at runtime, and nothing else. The rig
  # (tryout.py, run.sh, selftest.mjs, preview.html, EVENTS.md) is developer
  # tooling: shipping it bloats the package and hands a reviewer a pile of
  # unrelated Python to wonder about.
  FILES=(
    manifest.json
    core.js
    background.js
    badge.js
    content.js
    popup.html popup.js
    dash.html dash.js
    ui.css
    icons/unslop-16.png icons/unslop-32.png icons/unslop-48.png
    icons/unslop-96.png icons/unslop-128.png
  )

  for f in "${FILES[@]}"; do
    [ -f "ext/$f" ] || { echo "FAIL missing ext/$f" >&2; exit 1; }
  done
  check_js ext/core.js ext/background.js ext/badge.js ext/content.js \
           ext/popup.js ext/dash.js
  check_json ext/manifest.json

  mkdir -p dist
  rm -f "$OUT"
  ( cd ext && zip -q -X -r "../$OUT" "${FILES[@]}" )
  ;;

chrome)
  VERSION=$(python3 -c 'import json;print(json.load(open("chrome/manifest.json"))["version"])')
  OUT="dist/unslop-chrome-${VERSION}.zip"
  STAGE="dist/unslop-chrome"

  FILES=(
    manifest.json
    sw.js
    badgeshim.js
    offscreen.html offscreen.js
    pool.js
    core.js
    bridge.js hook.js
    badge.js
    popup.html popup.js
    dash.html dash.js
    ui.css
    icons/unslop-16.png icons/unslop-32.png icons/unslop-48.png
    icons/unslop-96.png icons/unslop-128.png
  )

  for f in "${FILES[@]}"; do
    [ -e "chrome/$f" ] || { echo "FAIL missing chrome/$f" >&2; exit 1; }
  done

  # The shared half must be shared, not a stale copy of it. chrome/core.js is a
  # symlink; if someone replaces it with a fork, the Chrome build silently stops
  # being the thing ext/selftest.mjs tests.
  cmp -s chrome/core.js ext/core.js \
    || { echo "FAIL chrome/core.js has drifted from ext/core.js" >&2; exit 1; }

  check_js chrome/sw.js chrome/badgeshim.js chrome/offscreen.js chrome/pool.js \
           chrome/core.js chrome/bridge.js chrome/hook.js chrome/badge.js \
           chrome/popup.js chrome/dash.js
  check_json chrome/manifest.json

  mkdir -p dist
  rm -rf "$STAGE"
  rm -f "$OUT"
  mkdir -p "$STAGE/icons"
  for f in "${FILES[@]}"; do
    cp -L "chrome/$f" "$STAGE/$f"       # -L: resolve the symlinks into real files
  done
  ( cd "$STAGE" && zip -q -X -r "../$(basename "$OUT")" . )

  echo "unpacked $STAGE  (chrome://extensions -> Load unpacked)"
  ;;

*)
  echo "unknown target: $TARGET (firefox|chrome)" >&2
  exit 2
  ;;
esac

echo "built $OUT  ($(du -h "$OUT" | cut -f1))"
unzip -l "$OUT" | tail -n +4 | head -n -2 | awk '{printf "  %s\n", $4}'
