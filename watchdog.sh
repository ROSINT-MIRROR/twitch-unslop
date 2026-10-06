#!/usr/bin/env bash
# Unattended insurance: if hunt.py dies before its deadline, restart it into
# the SAME session dir so the jsonl and manifests keep accumulating.
cd "$(dirname "$0")"
S=$(cat .hunt_session)
DEADLINE=$1                     # epoch seconds
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if ! pgrep -f '[h]unt\.py' >/dev/null; then
    LEFT=$(( (DEADLINE - $(date +%s)) / 3600 ))
    REM=$(python3 -c "print(max(0.05,($DEADLINE-$(date +%s))/3600))")
    echo "[watchdog] $(date) hunter down, restarting for ${REM}h"
    HUNT_SESSION="$S" setsid nohup python3 hunt.py "$REM" >> "data/hunt_$S.out" 2>&1 < /dev/null &
    sleep 30
  fi
  sleep 60
done
echo "[watchdog] $(date) deadline reached, standing down"
