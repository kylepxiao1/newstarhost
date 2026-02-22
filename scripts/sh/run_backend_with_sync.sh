#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() {
  printf '%s [backend] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$*"
}

stop_children() {
  if [ -n "${sync_pid:-}" ] && kill -0 "${sync_pid}" 2>/dev/null; then
    kill "${sync_pid}" 2>/dev/null || true
  fi
  if [ -n "${backend_pid:-}" ] && kill -0 "${backend_pid}" 2>/dev/null; then
    kill "${backend_pid}" 2>/dev/null || true
  fi
  wait || true
}

uvicorn_cmd=(
  python -m uvicorn backend.main:app
  --host 0.0.0.0
  --port "${PORT:-8080}"
  --log-level "${UVICORN_LOG_LEVEL:-warning}"
)

case "${UVICORN_ACCESS_LOG:-false}" in
  1|true|TRUE|True|yes|YES|Yes)
    ;;
  *)
    uvicorn_cmd+=(--no-access-log)
    ;;
esac

trap 'stop_children; exit 0' SIGINT SIGTERM

log "Starting S3 sync worker"
bash "${script_dir}/s3_sync.sh" &
sync_pid=$!

log "Starting backend API: ${uvicorn_cmd[*]}"
"${uvicorn_cmd[@]}" &
backend_pid=$!

set +e
wait -n "${sync_pid}" "${backend_pid}"
exit_code=$?
set -e

log "A backend child exited (status=${exit_code}). Stopping sibling process."
stop_children
exit "${exit_code}"
