#!/usr/bin/env bash
# When the hunter finishes it frees 12 poller slots. Use them to exercise the
# one POC path never observed: every arm in a break at once. High ad rate +
# 5 arms on one channel makes that reachable. Runs unattended.
cd "$(dirname "$0")"
while pgrep -f '[h]unt\.py' >/dev/null; do sleep 60; done
sleep 10
S=$(cat .hunt_session)
CH=$(jq -r 'select(.ev=="AD_BREAK")|.channel' "data/hunt/$S/hunt.jsonl" | tail -1)
[ -z "$CH" ] && CH=xqc
echo "[after] hunter done, POC stress on $CH at $(date)"
setsid nohup python3 unslop.py "$CH" --port 8790 --arms 5 --seconds 1500 \
  > data/unslop.stress.log 2>&1 < /dev/null &
sleep 240
for i in 1 2 3 4 5 6 7 8; do
  echo "--- probe $i $(date +%H:%M:%S) ---"
  curl -s --max-time 10 http://127.0.0.1:8790/stats
  echo
  P=$(curl -s --max-time 10 http://127.0.0.1:8790/playlist.m3u8)
  echo "  segments=$(grep -c '^#EXTINF' <<<"$P") nonlive=$(grep '^#EXTINF' <<<"$P" | grep -vc ',live$') disc=$(grep -c DISCONTINUITY <<<"$P")"
  sleep 120
done
echo "[after] mpv check"
timeout 60 mpv --vo=null --ao=null --length=20 --msg-level=all=status \
  http://127.0.0.1:8790/playlist.m3u8 2>&1 | grep -E '^AV|Exiting' | tail -2
echo "[after] done $(date)"
