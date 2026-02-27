#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${DISCORD_WORKER_ENV_FILE:-/app/app.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

# Per-run trendbot command override via `TRENDBOT_RUN_CMD`.
TRENDBOT_MIN_VELOCITY="${TRENDBOT_MIN_VELOCITY:-500}"
TRENDBOT_BROWSER="${TRENDBOT_BROWSER:-chromium}"
TRENDBOT_HEADLESS="${TRENDBOT_HEADLESS:-1}"
TRENDBOT_API_MAX_ATTEMPTS="${TRENDBOT_API_MAX_ATTEMPTS:-2}"
TRENDBOT_API_NAV_TIMEOUT_MS="${TRENDBOT_API_NAV_TIMEOUT_MS:-8000}"
TRENDBOT_VIDEOS="${TRENDBOT_VIDEOS:-60}"
TRENDBOT_TOP="${TRENDBOT_TOP:-25}"
TRENDBOT_DISCOVER_SCROLL_ROUNDS="${TRENDBOT_DISCOVER_SCROLL_ROUNDS:-6}"
TRENDBOT_DISCOVER_DANCES_VIDEOS="${TRENDBOT_DISCOVER_DANCES_VIDEOS:-72}"
TRENDBOT_TOPIC_HASHTAG_PAGES="${TRENDBOT_TOPIC_HASHTAG_PAGES:-10}"
TRENDBOT_TOPIC_HASHTAG_VIDEO_SAMPLES="${TRENDBOT_TOPIC_HASHTAG_VIDEO_SAMPLES:-8}"
TRENDBOT_TOPIC_MAX_RELATED_VIDEOS="${TRENDBOT_TOPIC_MAX_RELATED_VIDEOS:-120}"
VERIFY_BOT_CMD="${DISCORD_VERIFY_BOT_CMD:-python /app/scripts/discord_verify_bot.py}"
HEARTBEAT_WATCHDOG_CMD="${LISTENER_HEARTBEAT_WATCHDOG_CMD:-python /app/scripts/listener_heartbeat_watchdog.py}"
HEARTBEAT_WATCHDOG_ENABLED="${LISTENER_HEARTBEAT_WATCHDOG_ENABLED:-1}"
LISTENER_HEARTBEAT_INTERVAL_SECONDS="${LISTENER_HEARTBEAT_INTERVAL_SECONDS:-60}"
TRENDBOT_ENV_FILE="${TRENDBOT_ENV_FILE:-/app/app.env}"
REMINDER_CMD="${WILDCARDZ_REMINDER_CMD:-python /app/scripts/reminder_bot.py}"
REMINDER_ENABLED="${WILDCARDZ_REMINDER_ENABLED:-1}"

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

  local interval="${TRENDBOT_INTERVAL:-43200}"
  local headless_flag=""
  if [ "${TRENDBOT_HEADLESS:-1}" = "1" ] || [ "${TRENDBOT_HEADLESS:-1}" = "true" ]; then
    headless_flag="--headless"
  fi
  local default_cmd="python /app/scripts/find_viral_trends.py --topic \"dance challenges\" --videos ${TRENDBOT_VIDEOS:-60} --top ${TRENDBOT_TOP:-25} --browser ${TRENDBOT_BROWSER:-chromium} ${headless_flag} --api-max-attempts ${TRENDBOT_API_MAX_ATTEMPTS:-2} --api-navigation-timeout-ms ${TRENDBOT_API_NAV_TIMEOUT_MS:-8000} --discover-scroll-rounds ${TRENDBOT_DISCOVER_SCROLL_ROUNDS:-6} --discover-dances-videos ${TRENDBOT_DISCOVER_DANCES_VIDEOS:-72} --topic-hashtag-pages ${TRENDBOT_TOPIC_HASHTAG_PAGES:-10} --topic-hashtag-video-samples ${TRENDBOT_TOPIC_HASHTAG_VIDEO_SAMPLES:-8} --topic-max-related-videos ${TRENDBOT_TOPIC_MAX_RELATED_VIDEOS:-120} --supabase-min-velocity ${TRENDBOT_MIN_VELOCITY:-500}"
  local cmd="${TRENDBOT_RUN_CMD:-${default_cmd}}"

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
watchdog_pid=""
reminder_pid=""

shutdown_children() {
  log "Shutting down discord worker children"
  if [ -n "${trendbot_pid}" ] && kill -0 "${trendbot_pid}" 2>/dev/null; then
    kill "${trendbot_pid}" 2>/dev/null || true
  fi
  if [ -n "${verify_pid}" ] && kill -0 "${verify_pid}" 2>/dev/null; then
    kill "${verify_pid}" 2>/dev/null || true
  fi
  if [ -n "${watchdog_pid}" ] && kill -0 "${watchdog_pid}" 2>/dev/null; then
    kill "${watchdog_pid}" 2>/dev/null || true
  fi
  if [ -n "${reminder_pid}" ] && kill -0 "${reminder_pid}" 2>/dev/null; then
    kill "${reminder_pid}" 2>/dev/null || true
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

if [ "${HEARTBEAT_WATCHDOG_ENABLED}" = "1" ] || [ "${HEARTBEAT_WATCHDOG_ENABLED}" = "true" ]; then
  log "Starting listener heartbeat watchdog: ${HEARTBEAT_WATCHDOG_CMD} (LISTENER_HEARTBEAT_INTERVAL_SECONDS=${LISTENER_HEARTBEAT_INTERVAL_SECONDS})"
  bash -lc "${HEARTBEAT_WATCHDOG_CMD}" &
  watchdog_pid=$!
else
  log "Listener heartbeat watchdog disabled (LISTENER_HEARTBEAT_WATCHDOG_ENABLED=${HEARTBEAT_WATCHDOG_ENABLED})"
fi

if [ "${REMINDER_ENABLED}" = "1" ] || [ "${REMINDER_ENABLED}" = "true" ]; then
  if [ -z "${WILDCARDZ_CALENDAR_ID:-}" ] || [ -z "${WILDCARDZ_CALENDAR_API_KEY:-}" ]; then
    log "Wildcardz reminder bot not started (missing WILDCARDZ_CALENDAR_ID or WILDCARDZ_CALENDAR_API_KEY)"
  else
    log "Starting Wildcardz reminder bot: ${REMINDER_CMD}"
    bash -lc "${REMINDER_CMD}" &
    reminder_pid=$!
  fi
else
  log "Wildcardz reminder bot disabled (WILDCARDZ_REMINDER_ENABLED=${REMINDER_ENABLED})"
fi

pids=("${trendbot_pid}" "${verify_pid}")
if [ -n "${watchdog_pid}" ]; then
  pids+=("${watchdog_pid}")
fi
if [ -n "${reminder_pid}" ]; then
  pids+=("${reminder_pid}")
fi

set +e
wait -n "${pids[@]}"
exit_code=$?
set -e

log "A child process exited (status=${exit_code}). Stopping the other child and exiting."
shutdown_children
exit "${exit_code}"
