#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  printf '%s\n' 'Create .venv and install requirements.txt first; see README.md.' >&2
  exit 1
fi
# Parse dotenv as data, never source it as executable shell code.
exec .venv/bin/python - <<'PY'
import os

import uvicorn
from dotenv import dotenv_values

values = {**dotenv_values('.env'), **os.environ}
uvicorn.run(
    'speech_proxy.app:app',
    host=values.get('BIND_IP') or '127.0.0.1',
    port=int(values.get('PORT') or '8787'),
    access_log=False,
)
PY
