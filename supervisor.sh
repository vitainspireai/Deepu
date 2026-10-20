#!/bin/bash
# Keeps the app alive on this machine (local preview / dev only —
# this machine can't give family phones a public link; see DEPLOY.md).
cd "$(dirname "$0")"
mkdir -p logs
set -a; [ -f .env ] && source .env; set +a
while true; do
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 >> logs/app.log 2>&1
  echo "$(date -u): app exited, restarting" >> logs/supervisor.log
  sleep 3
done
