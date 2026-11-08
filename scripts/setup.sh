#!/bin/sh
set -eu
umask 077
project=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export UV_CACHE_DIR=${UV_CACHE_DIR:-"$project/.runtime/uv-cache"}
if [ ! -x "$project/.venv/bin/python" ]; then
  uv venv --python 3.12 "$project/.venv"
fi
uv pip install --python "$project/.venv/bin/python" -r "$project/requirements-lock.txt"
uv pip install --python "$project/.venv/bin/python" --no-deps -e "$project"
mkdir -p "$project/data"
chmod 700 "$project/data"
if [ ! -f "$project/config/local.json" ]; then
  cp "$project/config/example.json" "$project/config/local.json"
  chmod 600 "$project/config/local.json"
fi
mkdir -p "$project/content/lore" "$project/content/assets"
if [ ! -f "$project/content/persona.md" ]; then
  cp "$project/examples/persona.md" "$project/content/persona.md"
  chmod 600 "$project/content/persona.md"
fi
echo '核心环境已就绪。双击 启动管理台.command，或运行 scripts/run.sh serve。'
