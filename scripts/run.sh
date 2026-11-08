#!/bin/zsh
set -eu
cd "${AZUSA_ROOT:-${0:A:h}/..}"
export AZUSA_ROOT="$PWD"
export PYTHONPATH="$PWD/src"
umask 077
exec "$PWD/.venv/bin/python" -m azusa_bot.cli "${@:-serve}"
