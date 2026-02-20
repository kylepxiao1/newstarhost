#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${DISCORD_WORKER_ENV_FILE:-/app/app.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

TRENDBOT_CMD="${DISCORD_TRENDBOT_CMD:-bash /app/scripts/trendbot.sh}"
VERIFY_BOT_CMD="${DISCORD_VERIFY_BOT_CMD:-python /app/scripts/discord_verify_bot.py}"

log() {
  printf '%s [discord] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$*"
}

trendbot_pid=""
verify_pid=""

shutdown_children() {
  log "Shutting down discord worker children"
  if [ -n "${trendbot_pid}" ] && kill -0 "${trendbot_pid}" 2>/dev/null; then
    kill "${trendbot_pid}" 2>/dev/null || true
  fi
  if [ -n "${verify_pid}" ] && kill -0 "${verify_pid}" 2>/dev/null; then
    kill "${verify_pid}" 2>/dev/null || true
  fi
  wait || true
}

trap 'shutdown_children; exit 0' SIGINT SIGTERM

log "Starting trendbot: ${TRENDBOT_CMD}"
bash -lc "${TRENDBOT_CMD}" &
trendbot_pid=$!

log "Starting discord verify bot: ${VERIFY_BOT_CMD}"
bash -lc "${VERIFY_BOT_CMD}" &
verify_pid=$!

set +e
wait -n "${trendbot_pid}" "${verify_pid}"
exit_code=$?
set -e

log "A child process exited (status=${exit_code}). Stopping the other child and exiting."
shutdown_children
exit "${exit_code}"
