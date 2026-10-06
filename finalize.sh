#!/usr/bin/env bash
# Last thing to run. Waits for every experiment to finish, then regenerates
# FINDINGS.md so what the user reads in the morning reflects ALL the data,
# not whatever the last manual run happened to capture.
cd "$(dirname "$0")"
while pgrep -f '[h]unt\.py|[c]ombotest\.py|[g]eoprobe\.py|[a]fter_hunt\.sh' >/dev/null; do
  sleep 60
done
sleep 15
echo "[finalize] all experiments done at $(date)"
python3 report.py
echo "[finalize] selftest:"
./selftest.sh 2>&1 | tail -3
echo "[finalize] disk: $(du -sh data | cut -f1) used, $(df --output=avail -BG . | tail -1) free"
echo "[finalize] complete $(date)"
