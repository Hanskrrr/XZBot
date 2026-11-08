#!/bin/sh
set -eu
umask 077
project=${AZUSA_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}
export AZUSA_ROOT="$project"
source_dir=$(cat "$project/data/astrbot-source.path")
export ASTRBOT_ROOT="$project/data/astrbot"
export ASTRBOT_DISABLE_METRICS=1
export DASHBOARD_HOST=127.0.0.1
export DASHBOARD_PORT=6195
export AZUSA_BRIDGE_TOKEN_FILE=${AZUSA_BRIDGE_TOKEN_FILE:-"$project/data/bridge.token"}
export AZUSA_MEDIA_ROOT=${AZUSA_MEDIA_ROOT:-"$project/data/media"}
export AZUSA_ASTRBOT_SOURCE="$source_dir"
cd "$source_dir"
exec "$source_dir/.venv/bin/python" "$project/scripts/run_astrbot.py"
