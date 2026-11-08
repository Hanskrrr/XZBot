#!/bin/zsh
set -eu
cd "${0:A:h}"
export AZUSA_ROOT="$PWD"
export PYTHONPATH="$PWD/src"
exec "$PWD/.venv/bin/python" "$PWD/scripts/start.py"
