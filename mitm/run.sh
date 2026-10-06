#!/usr/bin/env bash
# Passive tap. Ctrl-C to stop. Everything lands under data/.
set -euo pipefail
. "$(dirname "$0")/../env.sh"

export LAB_SESSION="${1:-$(date +%Y%m%d-%H%M%S)}"
echo "[*] session  $LAB_SESSION"
echo "[*] jsonl    $DATA/logs/tap.$LAB_SESSION.jsonl"
echo "[*] flows    $DATA/raw/$LAB_SESSION.flows"

# stream_large_bodies MUST stay unset: streaming makes flow.response.content
# empty, so the addon can't read segment bytes and the dump truncates them.
# Source segments are ~2.25MB, so any low limit silently eats them.
# save_stream_filter keeps segment bodies OUT of the .flows file (they'd make
# it enormous) — the addon writes the ones we care about to data/segments/.
exec mitmdump \
  --set confdir="$MITM_CONF" \
  --set save_stream_filter='!(~u /v1/segment/)' \
  --set termlog_verbosity=warn \
  --set flow_detail=0 \
  -s "$LAB/mitm/addons/tap.py" \
  -w "$DATA/raw/$LAB_SESSION.flows" \
  -p "$MITM_PORT"
