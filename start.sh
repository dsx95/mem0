#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export UV_CACHE_DIR="$PWD/.uv-cache"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  installer="$(mktemp)"
  trap 'rm -f "$installer"' EXIT
  curl --fail --location --silent --show-error https://astral.sh/uv/install.sh -o "$installer"
  UV_NO_MODIFY_PATH=1 sh "$installer"
fi
if [[ ! -x .venv/bin/python ]]; then uv venv --python 3.11 .venv; fi
fingerprint="$(.venv/bin/python -c 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path("local_runtime/requirements.lock").read_bytes()+pathlib.Path("pyproject.toml").read_bytes()).hexdigest())')"
if [[ ! -f .venv/.dependencies-ready || "$(cat .venv/.dependencies-ready)" != "$fingerprint" ]]; then
  uv pip sync --python .venv/bin/python local_runtime/requirements.lock
  uv pip check --python .venv/bin/python
  printf '%s\n' "$fingerprint" > .venv/.dependencies-ready
fi
exec .venv/bin/python -m local_runtime.portable "$@"
