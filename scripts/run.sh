#!/usr/bin/env bash
set -u

cd "$(dirname "$0")/.."

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

echo "[run.sh] using $PY"
"$PY" scripts/check_env.py || exit 1

while true; do
  "$PY" main.py
  code=$?
  echo "[run.sh] agentos exited with code $code, restarting in 5s"
  sleep 5
done
