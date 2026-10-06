#!/usr/bin/env bash
# Insurance only. Ad segments are the bulk (~7GB/h). We already have far more
# than analysis needs, so if free space gets tight, thin the OLDEST ad
# segments while keeping every manifest and every jsonl (those are the
# evidence; the .ts bytes are just corroboration and we keep a wide sample).
cd "$(dirname "$0")"
S=$(cat .hunt_session)
while pgrep -f '[h]unt\.py' >/dev/null; do
  FREE=$(df --output=avail -BG . | tail -1 | tr -dc '0-9')
  if [ "${FREE:-999}" -lt 60 ]; then
    N=$(ls data/hunt/$S/segments/*.ts 2>/dev/null | wc -l)
    echo "[janitor] $(date) free=${FREE}G segments=$N -> thinning oldest 40%"
    ls -t data/hunt/$S/segments/*.ts 2>/dev/null | tail -n +$(( N*6/10 )) | xargs -r rm -f
  fi
  sleep 300
done
echo "[janitor] $(date) hunter done, standing down"
