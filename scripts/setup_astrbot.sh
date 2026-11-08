#!/bin/sh
set -eu
umask 077
project=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source_dir=${AZUSA_ASTRBOT_SOURCE:-"$project/runtime/AstrBot"}
export UV_CACHE_DIR=${UV_CACHE_DIR:-"$project/.runtime/uv-cache"}
pin=61335414433db721b3f7ecc93f90deafe0a59d78
if [ -L "$source_dir" ] && [ ! -e "$source_dir" ]; then
  rm "$source_dir"  # Remove only a dangling development link after moving the project.
fi
if [ ! -d "$source_dir/.git" ]; then
  mkdir -p "$(dirname -- "$source_dir")"
  git clone --depth 1 --branch v4.29.4 https://github.com/AstrBotDevs/AstrBot.git "$source_dir"
fi
[ "$(git -C "$source_dir" rev-parse HEAD)" = "$pin" ] || { echo 'AstrBot source differs from tested commit; use a separate runtime.' >&2; exit 1; }
if [ ! -x "$source_dir/.venv/bin/python" ]; then
  uv venv --python 3.12 "$source_dir/.venv"
  uv pip install --python "$source_dir/.venv/bin/python" -r "$project/requirements-astrbot.lock"
  uv pip install --python "$source_dir/.venv/bin/python" --no-deps -e "$source_dir"
fi
"$source_dir/.venv/bin/python" "$project/scripts/init_astrbot.py" "$source_dir"
