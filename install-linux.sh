#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "需要 Python 3.10 或更新版本。" >&2
  exit 1
fi
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "需要 Python 3.10 或更新版本。" >&2
  exit 1
}
"$PYTHON" -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install --disable-pip-version-check -r "$ROOT/requirements.txt"
cat <<'EOF'
环境已准备在此文件夹的 .venv 中。
此步骤不会创建或修改 Supabase 数据，也不会启动服务或运行迁移。
请先阅读 DISTRIBUTION.md；每位使用者需配置自己的 Supabase 项目。
EOF
