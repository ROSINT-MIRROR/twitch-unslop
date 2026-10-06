#!/usr/bin/env bash
# Verify the whole capture path with curl only — no browser, nothing on screen.
# Proves: CA trusted, gql decrypted, usher captured, media polled, SEGMENT BYTES
# actually landed on disk. Run this before any real session.
#
#   ./selftest.sh [channel]
set -uo pipefail
cd "$(dirname "$0")"
. ./env.sh

S="selftest"
CID=kimne78kx3ncx6brgo4mv6wki5h1ko

# Don't hardcode a channel — whoever you pick will be offline sooner or later
# and the rig then looks broken when it isn't. Probe a pool and take the first
# one that is actually live.
pick_live() {
  for c in "$@"; do
    code=$(curl -s -o /dev/null -w '%{http_code}' \
      "https://gql.twitch.tv/gql" -H "Client-ID: $CID" \
      -d '{"operationName":"PlaybackAccessToken","variables":{"isLive":true,"login":"'"$c"'","isVod":false,"vodID":"","playerType":"site"},"extensions":{"persistedQuery":{"version":1,"sha256Hash":"0828119ded1c13477966434e15800ff57ddacf13ba1911c129dc2200705b0712"}}}' 2>/dev/null)
    [ "$code" = "200" ] || continue
    # a token alone isn't proof of live; usher is
    tok=$(curl -s "https://gql.twitch.tv/gql" -H "Client-ID: $CID" \
      -d '{"operationName":"PlaybackAccessToken","variables":{"isLive":true,"login":"'"$c"'","isVod":false,"vodID":"","playerType":"site"},"extensions":{"persistedQuery":{"version":1,"sha256Hash":"0828119ded1c13477966434e15800ff57ddacf13ba1911c129dc2200705b0712"}}}')
    v=$(jq -r '.data.streamPlaybackAccessToken.value // empty' <<<"$tok")
    g=$(jq -r '.data.streamPlaybackAccessToken.signature // empty' <<<"$tok")
    [ -n "$v" ] || continue
    st=$(curl -s -G -o /dev/null -w '%{http_code}' \
      --data-urlencode "client_id=$CID" --data-urlencode "token=$v" \
      --data-urlencode "sig=$g" --data-urlencode allow_source=true \
      "https://usher.ttvnw.net/api/channel/hls/$c.m3u8")
    [ "$st" = "200" ] && { echo "$c"; return 0; }
  done
  return 1
}

if [ -n "${1:-}" ]; then
  CH="$1"
else
  CH=$(pick_live xqc jynxzi gaules alanzoka kaicenat zackrawrr hasanabi \
                 loltyler1 summit1g tarik ibai elded illojuan trymacs) || {
    echo "!! no channel from the pool is live right now — cannot self-test"
    exit 1; }
  echo "[*] live channel: $CH"
fi
W="$DATA/.scratch"
PX=(-x "http://127.0.0.1:$MITM_PORT" --cacert "$MITM_CA")

ss -ltn 2>/dev/null | grep -q ":$MITM_PORT " && { echo "!! port $MITM_PORT busy"; exit 1; }

rm -f "$DATA/logs/tap.$S.jsonl" "$DATA/raw/$S.flows"
rm -rf "$DATA/segments/$S" "$DATA/gql/$S"

LAB_SESSION="$S" ./mitm/run.sh "$S" > "$DATA/logs/mitm.$S.log" 2>&1 &
MPID=$!
trap 'kill $MPID 2>/dev/null; wait $MPID 2>/dev/null' EXIT
for i in $(seq 1 40); do ss -ltn 2>/dev/null | grep -q ":$MITM_PORT " && break; sleep 0.25; done
ss -ltn 2>/dev/null | grep -q ":$MITM_PORT " || { echo "!! mitmdump died:"; cat "$DATA/logs/mitm.$S.log"; exit 1; }
echo "[+] tap up"

TOK=$(curl -s "${PX[@]}" -H "Client-ID: $CID" -H 'Content-Type: application/json' \
  -d '{"operationName":"PlaybackAccessToken","variables":{"isLive":true,"login":"'"$CH"'","isVod":false,"vodID":"","playerType":"site"},"extensions":{"persistedQuery":{"version":1,"sha256Hash":"0828119ded1c13477966434e15800ff57ddacf13ba1911c129dc2200705b0712"}}}' \
  https://gql.twitch.tv/gql)
V=$(jq -r '.data.streamPlaybackAccessToken.value' <<<"$TOK")
SIG=$(jq -r '.data.streamPlaybackAccessToken.signature' <<<"$TOK")
[ -n "$V" ] && [ "$V" != null ] || { echo "!! no token (channel offline?)"; exit 1; }
echo "[+] token"

# token is raw JSON -> must be url-encoded or usher 4xx's
curl -s -G "${PX[@]}" -o "$W/master.m3u8" \
  --data-urlencode "client_id=$CID" --data-urlencode "token=$V" \
  --data-urlencode "sig=$SIG" --data-urlencode allow_source=true \
  "https://usher.ttvnw.net/api/channel/hls/$CH.m3u8"
# Take the HIGHEST-BANDWIDTH variant, not the first one. usher randomises the
# order of the ladder between mints (measured 2026-07-29), so `grep -m1` drew a
# different rendition every run — when it landed on 160p/480p the segments were
# ~180KB and the >1MB check below failed with "streaming is truncating" while
# the rig was fine. Source is ~9Mbps, so the check is only meaningful pinned.
M=$(awk '
  /^#EXT-X-STREAM-INF/ { bw = 0
    if (match($0, /BANDWIDTH=[0-9]+/)) bw = substr($0, RSTART+10, RLENGTH-10) + 0
    pend = 1; next }
  pend && /^https/ { if (bw > best) { best = bw; url = $0 }; pend = 0 }
  END { print url }' "$W/master.m3u8")
[ -n "$M" ] || { echo "!! no variant in master"; head -3 "$W/master.m3u8"; exit 1; }
echo "[+] master ($(awk -v u="$M" '/^#EXT-X-STREAM-INF/{l=$0} $0==u{print l; exit}' \
     "$W/master.m3u8" | grep -o 'VIDEO="[^"]*"' || echo '?'))"

for r in 1 2 3; do
  curl -s "${PX[@]}" -o "$W/media.m3u8" "$M"
  grep '^https' "$W/media.m3u8" | tail -2 | while read -r seg; do
    [ -n "$seg" ] && curl -s "${PX[@]}" -o /dev/null "$seg"
  done
  sleep 2
done
echo "[+] polled + fetched segments"

kill $MPID 2>/dev/null; wait $MPID 2>/dev/null; trap - EXIT
sleep 1

L="$DATA/logs/tap.$S.jsonl"
echo
echo "=== events ==="
jq -r .ev "$L" | sort | uniq -c
echo "=== segment bytes ==="
jq -c 'select(.ev=="segment")|{bytes,title,ad,saved}' "$L" | head -4
echo "=== on disk ==="
printf 'segments  %s files, %s\n' "$(ls "$DATA/segments/$S" 2>/dev/null | wc -l)" \
   "$(du -sh "$DATA/segments/$S" 2>/dev/null | cut -f1)"
printf 'gql       %s files\n' "$(ls "$DATA/gql/$S" 2>/dev/null | wc -l)"
printf 'manifests %s files\n' "$(ls "$DATA"/manifests/$S.* 2>/dev/null | wc -l)"
printf 'flows     %s\n' "$(du -sh "$DATA/raw/$S.flows" 2>/dev/null | cut -f1)"

echo
FAIL=0
chk() { if [ "$2" -gt 0 ]; then echo "  ok   $1"; else echo "  FAIL $1"; FAIL=1; fi; }
chk "token decrypted"  "$(jq -r 'select(.ev=="playback_token")|.ev' "$L" | wc -l)"
chk "master captured"  "$(jq -r 'select(.ev=="master")|.ev' "$L" | wc -l)"
chk "media polled"     "$(jq -r 'select(.ev|startswith("media"))|.ev' "$L" | wc -l)"
chk "segment bytes"    "$(jq -r 'select(.ev=="segment" and .bytes>0)|.ev' "$L" | wc -l)"
chk "segments on disk" "$(ls "$DATA/segments/$S" 2>/dev/null | wc -l)"
# empty 200 = real loss. 204 (prefetch not ready yet) is normal, ignore.
NB=$(jq -r 'select(.ev=="segment_nobody")|.ev' "$L" | wc -l)
[ "$NB" -eq 0 ] && echo "  ok   no truncated bodies" || { echo "  FAIL $NB segments lost their body"; FAIL=1; }
BIG=$(jq -r 'select(.ev=="segment")|.bytes' "$L" | sort -n | tail -1)
[ "${BIG:-0}" -gt 1000000 ] && echo "  ok   large bodies intact (${BIG}B)" \
  || { echo "  FAIL biggest segment only ${BIG:-0}B — streaming is truncating"; FAIL=1; }
echo
[ "$FAIL" -eq 0 ] && echo "ALL GREEN" || echo "SOMETHING BROKE"
exit $FAIL
