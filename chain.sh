#!/usr/bin/env bash
# Poller budget: hunt.py holds 12. Wait for headertest's 4 to free up before
# starting the geo control probe, so we never exceed ~16 concurrent pollers.
cd "$(dirname "$0")"
while pgrep -f '[c]ombotest\.py' >/dev/null; do sleep 60; done
S=$(cat .hunt_session)
CH=$(jq -r 'select(.ev=="AD_BREAK")|.channel' "data/hunt/$S/hunt.jsonl" | tail -1)
[ -z "$CH" ] && CH=jynxzi
echo "[chain] combotest done, geoprobe on $CH at $(date)"
python3 geoprobe.py --channel "$CH" --ports 9050,9051,9052 --polls 40
echo "[chain] geoprobe done at $(date)"
