#!/bin/bash
# pr-proof 서버(api와 worker)를 이 체크아웃의 .venv로 띄운다. 어느 셸에서 불러도, launchd가 불러도 같다.
# 비밀값은 읽지 않는다 (~/.config/pipeline/은 서버가 직접 읽는다).
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
venv="$root/.venv/bin"
# launchd는 /usr/bin:/bin만 준다. gh·claude·docker가 보통 있는 곳을 직접 적는다
system_path="${PR_PROOF_SYSTEM_PATH:-/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin}"
path="$venv:$HOME/.local/bin:$system_path"

for exe in honcho python uvicorn; do
  if [ ! -x "$venv/$exe" ]; then
    echo "start.sh: $venv/$exe 이 없다. $root 에서 python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' 를 먼저 한다" >&2
    exit 1
  fi
done
for tool in gh claude docker git; do
  if ! PATH="$path" command -v "$tool" >/dev/null 2>&1; then
    echo "start.sh: $tool 을 찾지 못했다 (PATH=$path)" >&2
    exit 1
  fi
done

env_args=("HOME=$HOME" "PATH=$path")
for name in USER TMPDIR LANG PIPELINE_CONFIG_DIR; do
  if [ -n "${!name:-}" ]; then
    env_args+=("$name=${!name}")
  fi
done

cd "$root"  # honcho는 여기의 Procfile과 .env를 읽는다
exec /usr/bin/env -i "${env_args[@]}" "$venv/honcho" start
