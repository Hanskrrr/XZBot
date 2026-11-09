#!/bin/sh
set -eu
cd -- "$(dirname -- "$0")"
export PYTHONUTF8=1
exec ./tools/uv run --no-project --python 3.12 scripts/install_release.py "$@"
