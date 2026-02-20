#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${DISCORD_WORKER_ENV_FILE:-/app/app.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

# Per-run trendbot command override via `TRENDBOT_CMD`.
TRENDBOT_RUN_CMD='python /app/scripts/find_viral_trends.py --topic "dance challenges" --videos 120 --top 25 --browser webkit --api-max-attempts 5 --api-navigation-timeout-ms 10000 --discover-scroll-rounds 12 --discover-dances-videos 180 --topic-hashtag-pages 24 --topic-hashtag-video-samples 20 --topic-max-related-videos 400 --supabase-min-velocity 100'
VERIFY_BOT_CMD="${DISCORD_VERIFY_BOT_CMD:-python /app/scripts/discord_verify_bot.py}"
TRENDBOT_ENV_FILE="${TRENDBOT_ENV_FILE:-/app/app.env}"
TRENDBOT_INTERVAL_DEFAULT="${TRENDBOT_INTERVAL:-43200}"

log() {
  printf '%s [discord] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$*"
}

trendbot_log() {
  printf '%s [trendbot] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$*"
}

trendbot_loop() {
  if [ -f "${TRENDBOT_ENV_FILE}" ]; then
    set -a
    # shellcheck disable=SC1090
    . "${TRENDBOT_ENV_FILE}"
    set +a
  fi

  local interval="${TRENDBOT_INTERVAL:-${TRENDBOT_INTERVAL_DEFAULT}}"
  local cmd="${TRENDBOT_RUN_CMD}"

  trendbot_log "Starting trendbot loop (interval=${interval}s)"
  trendbot_log "Command: ${cmd}"

  local run_id=0
  while true; do
    run_id=$((run_id + 1))
    local started_at
    started_at="$(date +%s)"
    trendbot_log "Run ${run_id} started"

    local run_log
    run_log="$(mktemp)"
    set +e
    bash -lc "${cmd}" >"${run_log}" 2>&1
    local run_status=$?
    set -e

    if [ -s "${run_log}" ]; then
      while IFS= read -r line; do
        [ -n "${line}" ] && trendbot_log "Run ${run_id} output: ${line}"
      done < "${run_log}"
    fi
    rm -f "${run_log}"

    local finished_at
    finished_at="$(date +%s)"
    local duration=$((finished_at - started_at))
    if [ "${run_status}" -eq 0 ]; then
      trendbot_log "Run ${run_id} succeeded (${duration}s)"
    else
      trendbot_log "Run ${run_id} FAILED (${duration}s) exit_code=${run_status}"
    fi

    trendbot_log "Sleeping ${interval}s before next run"
    sleep "${interval}"
  done
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

log "Starting trendbot loop in worker process"
trendbot_loop &
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
