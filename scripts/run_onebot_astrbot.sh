#!/bin/sh
set -eu
umask 077
project=${AZUSA_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}
export AZUSA_ROOT="$project"
source_dir=$(cat "$project/data/astrbot-source.path")
exec "$source_dir/.venv/bin/python" "$project/scripts/run_onebot_astrbot.py"
