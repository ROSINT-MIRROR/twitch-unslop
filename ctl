#!/usr/bin/env bash
cd "$(dirname "$0")"; . ./env.sh; exec python -u browser/ctl.py "$@"
