# source this. everything is project-relative. no /tmp, ever.
export LAB="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

export PROFILE="$LAB/browser/profile"
export MITM_CONF="$LAB/mitm/ca"
export MITM_PORT=8888
export MITM_CA="$MITM_CONF/mitmproxy-ca-cert.pem"

export DATA="$LAB/data"
export VENV="$LAB/driver/venv"

# geckodriver otherwise scatters profiles into /tmp
export TMPDIR="$LAB/data/.scratch"
mkdir -p "$TMPDIR"

[ -d "$VENV" ] && . "$VENV/bin/activate"
