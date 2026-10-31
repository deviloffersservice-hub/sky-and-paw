#!/usr/bin/env bash
# Smoke test: start the backend on a spare port, wait for /healthz, open / with Playwright
# (title "Sky & Paw Ops", WebSocket hello), save a screenshot, then open the built console from
# a second origin with ?api=<backend> (CORS, localStorage), stop both servers.
#
#   scripts/smoke.sh                       # screenshot -> frontend/test-results/smoke-shell.png
#   SMOKE_SCREENSHOT=path.png scripts/smoke.sh
#
# Expects `make setup` and `make build` to have run (the Makefile's `smoke` target does both).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${SMOKE_PORT:-7870}"
STATIC_PORT="${SMOKE_STATIC_PORT:-7871}"
URL="http://127.0.0.1:${PORT}"
STATIC_URL="http://localhost:${STATIC_PORT}"
SHOT="${SMOKE_SCREENSHOT:-${ROOT}/frontend/test-results/smoke-shell.png}"
LOG="${SMOKE_LOG:-${ROOT}/frontend/test-results/smoke-server.log}"
export DATA_DIR="${DATA_DIR:-${ROOT}/data}"

mkdir -p "$(dirname "$SHOT")" "$(dirname "$LOG")"

"${ROOT}/.venv/bin/uvicorn" skypaw.app:create_app --factory --host 127.0.0.1 --port "$PORT" \
  >"$LOG" 2>&1 &
SERVER_PID=$!
# Second origin serving the built console, standing in for GitHub Pages (CORS + ?api= check).
"${ROOT}/.venv/bin/python" -m http.server "$STATIC_PORT" --bind 127.0.0.1 \
  --directory "${ROOT}/frontend/dist" >/dev/null 2>&1 &
STATIC_PID=$!
cleanup() {
  kill "$SERVER_PID" "$STATIC_PID" 2>/dev/null || true
  wait "$SERVER_PID" "$STATIC_PID" 2>/dev/null || true
}
trap cleanup EXIT

for _ in $(seq 1 100); do
  if curl -fsS "${URL}/healthz" >/dev/null 2>&1 && curl -fsS "${STATIC_URL}/" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then break; fi
  sleep 0.2
done
if ! curl -fsS "${URL}/healthz" >/dev/null 2>&1; then
  echo "backend did not become healthy on ${URL}; server log:" >&2
  cat "$LOG" >&2
  exit 1
fi

echo "curl ${URL}/healthz -> $(curl -sS -w ' (HTTP %{http_code})' "${URL}/healthz")"
cd "${ROOT}/frontend"
SMOKE_URL="$URL" SMOKE_STATIC_URL="$STATIC_URL" SMOKE_SCREENSHOT="$SHOT" node tests/smoke.cjs
