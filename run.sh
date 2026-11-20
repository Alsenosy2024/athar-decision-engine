#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
port="${PORT:-8780}"
if /usr/sbin/lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $port is already in use." >&2
    exit 1
fi
[ -x .venv/bin/python ] || "${PYTHON:-python3}" -m venv .venv
.venv/bin/pip install --disable-pip-version-check -q -r requirements.txt
.venv/bin/python scripts/build_assets.py
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$port" --no-access-log
