#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
export UV_CACHE_DIR="$project_dir/.uv-cache"
if [[ ! -x .venv/bin/python ]]; then
    uv venv --python python3 .venv
fi
if [[ ! -f local_runtime/requirements.lock ]]; then
    uv pip compile --python .venv/bin/python local_runtime/requirements.in -o local_runtime/requirements.lock
fi
uv pip sync --python .venv/bin/python local_runtime/requirements.lock
uv pip check --python .venv/bin/python
if [[ ! -f .env ]]; then
    (umask 077; cp local_runtime/env/api.env.example .env)
fi
.venv/bin/python -m local_runtime config
