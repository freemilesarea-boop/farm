#!/usr/bin/env bash
# Start backend (127.0.0.1:8000) and dashboard (127.0.0.1:3000). Ctrl+C stops both,
# and the backend closes every Chromium it launched. Works with macOS's bash 3.2.
set -uo pipefail
cd "$(dirname "$0")/.."

API_PORT=${LAB_PORT:-8000}
for port in "$API_PORT" 3000; do
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    echo "Port $port is already in use. Stop the process using it first:  lsof -i :$port" >&2
    exit 1
  fi
done

(cd backend && exec .venv/bin/python -m app.main) &
BACK=$!
(cd frontend && exec npm run dev) &
FRONT=$!

cleanup() {
  trap - INT TERM EXIT
  kill -TERM "$FRONT" 2>/dev/null || true
  pkill -TERM -P "$FRONT" 2>/dev/null || true
  kill -TERM "$BACK" 2>/dev/null || true   # graceful: stops sessions, reaps browsers
  wait "$BACK" 2>/dev/null || true
  wait "$FRONT" 2>/dev/null || true
}
trap cleanup INT TERM EXIT
echo "Dashboard: http://127.0.0.1:3000   API: http://127.0.0.1:$API_PORT/docs"

# Exit (and clean up) as soon as either server stops.
while kill -0 "$BACK" 2>/dev/null && kill -0 "$FRONT" 2>/dev/null; do
  sleep 1
done
echo "A server exited; shutting down the other." >&2
