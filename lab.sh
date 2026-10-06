#!/usr/bin/env bash
# One terminal, whole lab. Ctrl-C kills everything.
#
#   ./lab.sh                    bare browser, open tabs yourself
#   ./lab.sh xqc jynxzi ...     preload those channels
#   ./lab.sh --fresh [chan..]   burn the ad identity first (keeps bookmarks)
#
# Then, from any other terminal:
#   ./ctl open <chan>   ./ctl probe   ./ctl mute
#   ./watch             live ad-break feed
set -uo pipefail
cd "$(dirname "$0")"
. ./env.sh

FRESH=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --fresh) FRESH=1 ;;
    *) ARGS+=("$a") ;;
  esac
done
set -- ${ARGS+"${ARGS[@]}"}

SESSION="${LAB_SESSION:-$(date +%m%d-%H%M%S)}"
export LAB_SESSION="$SESSION"

[ -f "$PROFILE/cert9.db" ] || { echo "!! run ./browser/setup-profile.sh first"; exit 1; }

for p in 8888 4444; do
  ss -ltn 2>/dev/null | grep -q ":$p " && { echo "!! port $p busy — lab already running?"; exit 1; }
done

PIDS=()
cleanup() {
  echo
  echo "[*] shutting down"
  for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done
  sleep 2
  for p in "${PIDS[@]}"; do kill -9 "$p" 2>/dev/null; done
  rm -f "$DATA/.session.json"
  echo "[+] session '$SESSION' -> $DATA/logs/tap.$SESSION.jsonl"
}
trap cleanup EXIT INT TERM

echo "=== session $SESSION ==="
[ "$FRESH" = 1 ] && ./browser/fresh.sh

# --- tap ---
./mitm/run.sh "$SESSION" > "$DATA/logs/mitm.$SESSION.log" 2>&1 &
PIDS+=($!)
for i in $(seq 1 40); do ss -ltn 2>/dev/null | grep -q ":8888 " && break; sleep 0.25; done
ss -ltn 2>/dev/null | grep -q ":8888 " || { echo "!! mitmdump failed:"; cat "$DATA/logs/mitm.$SESSION.log"; exit 1; }
echo "[+] tap up on :8888"

# --- browser (unbuffered so you see it live) ---
python -u browser/start.py "$@" &
PIDS+=($!)
for i in $(seq 1 80); do [ -f "$DATA/.session.json" ] && break; sleep 0.5; done
[ -f "$DATA/.session.json" ] || { echo "!! browser failed to come up"; exit 1; }

cat <<EOF

  ctl:    ./ctl open <chan> [chan..]   ./ctl probe   ./ctl mute   ./ctl close <n>
  watch:  ./watch
  ctrl-c here tears it all down

EOF

wait
