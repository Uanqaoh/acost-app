#!/bin/zsh
set -eu
cd -- "${0:A:h}"
if ! command -v python3 >/dev/null 2>&1; then
  echo '需要先安装 Python 3.10 或更新版本。'
  read '?按回车关闭。'
  exit 1
fi
python3 viewer.py || { read '?启动未完成；按回车关闭。'; exit 1; }
