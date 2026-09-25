#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  printf '%s\n' '请先执行 bash start.sh --doctor 安装项目环境。' >&2
  exit 1
fi
exec .venv/bin/python -m local_runtime.migration "$@"
