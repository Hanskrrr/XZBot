#!/bin/zsh
set -eu
cd "${0:A:h}"
umask 077
if [[ ! -x .venv/bin/python ]]; then
  print '项目运行环境不可用，未停止任何程序。'
  exit 1
fi
exec .venv/bin/python scripts/stop_onebot.py
