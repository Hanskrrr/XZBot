#!/bin/zsh
set -eu
cd "${0:A:h}"
umask 077
if [[ ! -x .venv/bin/python ]]; then
  print '项目运行环境尚未准备，请先完成安装说明。'
  exit 1
fi
exec .venv/bin/python scripts/start_onebot.py
