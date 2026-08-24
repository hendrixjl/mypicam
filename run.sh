#!/bin/bash
# Wrapper used by the systemd service: pulls the latest code from GitHub
# (best-effort — won't block startup if the Pi isn't online yet or the
# repo has no remote configured) then launches the app.

set -u
cd "$(dirname "$0")"

if git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "[run.sh] Pulling latest code..."
    if git pull --ff-only; then
        echo "[run.sh] git pull succeeded."
    else
        echo "[run.sh] git pull failed (offline or conflict?) — continuing with code already on disk."
    fi
else
    echo "[run.sh] Not inside a git repo — skipping git pull."
fi

echo "[run.sh] Starting app.py..."
exec python3 app.py
