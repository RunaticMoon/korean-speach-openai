#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -f .env ]]; then
  echo 'Copy .env.example to .env and configure credentials first.' >&2
  exit 1
fi
# Only source an .env you created yourself; it is shell code.
set -a
source .env
set +a
exec .venv/bin/uvicorn app.main:create_app --factory \
  --host "${BIND_IP:-127.0.0.1}" --port "${PORT:-8787}" \
  --workers 1 --limit-concurrency 12 --no-access-log
