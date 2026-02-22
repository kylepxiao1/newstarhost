#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

resolve_python_bin() {
  if [ -n "${PYTHON_BIN:-}" ]; then
    printf '%s\n' "${PYTHON_BIN}"
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    printf '%s\n' "python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "python3"
    return 0
  fi
  echo "ERROR: python interpreter not found (checked PYTHON_BIN, python, python3)." >&2
  exit 127
}

python_bin="$(resolve_python_bin)"

log() {
  printf '%s\n' "$*"
}

is_truthy() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|y|on) return 0 ;;
    *) return 1 ;;
  esac
}

load_env_file() {
  local file_path="$1"
  if [ -f "${file_path}" ]; then
    set -a
    # shellcheck disable=SC1090
    . "${file_path}"
    set +a
  fi
}

run_app() {
  local bind_host="${APP_HOST:-0.0.0.0}"
  local bind_port="${PORT:-8080}"
  if [ -z "${bind_host}" ]; then
    bind_host="0.0.0.0"
  fi
  if [[ ! "${bind_port}" =~ ^[0-9]+$ ]]; then
    log "Invalid PORT='${bind_port}'; defaulting to 8080"
    bind_port="8080"
  fi

  local uvicorn_cmd=(
    "${python_bin}" -m uvicorn backend.main:app
    --host "${bind_host}"
    --port "${bind_port}"
    --log-level "${UVICORN_LOG_LEVEL:-warning}"
  )

  case "${UVICORN_ACCESS_LOG:-false}" in
    1|true|TRUE|True|yes|YES|Yes)
      ;;
    *)
      uvicorn_cmd+=(--no-access-log)
      ;;
  esac

  log "Starting backend API: ${uvicorn_cmd[*]}"
  exec "${uvicorn_cmd[@]}"
}

run_listener() {
  log "Starting TikTok listener process"
  exec "${python_bin}" "${repo_root}/scripts/tiktok_listener.py"
}

run_discord_verify_bot() {
  local env_file="${DISCORD_WORKER_ENV_FILE:-/app/app.env}"
  load_env_file "${env_file}"

  if [ -n "${DISCORD_VERIFY_BOT_CMD:-}" ]; then
    log "Starting custom verify bot command: ${DISCORD_VERIFY_BOT_CMD}"
    exec bash -lc "${DISCORD_VERIFY_BOT_CMD}"
  fi

  log "Starting default verify bot"
  exec "${python_bin}" "${repo_root}/scripts/discord_verify_bot.py"
}

run_watchdog() {
  local env_file="${WATCHDOG_ENV_FILE:-${DISCORD_WORKER_ENV_FILE:-/app/app.env}}"
  load_env_file "${env_file}"

  local enabled="${WATCHDOG_ENABLED:-${LISTENER_HEARTBEAT_WATCHDOG_ENABLED:-1}}"
  if ! is_truthy "${enabled}"; then
    log "Disabled via WATCHDOG_ENABLED/LISTENER_HEARTBEAT_WATCHDOG_ENABLED=${enabled}; sleeping."
    exec sleep infinity
  fi

  local watchdog_cmd="${WATCHDOG_CMD:-${LISTENER_HEARTBEAT_WATCHDOG_CMD:-}}"
  if [ -n "${watchdog_cmd}" ]; then
    log "Starting custom watchdog command: ${watchdog_cmd}"
    exec bash -lc "${watchdog_cmd}"
  fi

  log "Starting default watchdog"
  exec "${python_bin}" "${repo_root}/scripts/watchdog.py" --mode heartbeat
}

run_s3_sync_once() {
  local env_file="${S3_SYNC_ENV_FILE:-/app/s3_sync.env}"
  load_env_file "${env_file}"

  local src_dir="/app/media"
  local dest="s3://${S3_BUCKET:-}/${S3_PREFIX:-media}"

  if [ -z "${S3_BUCKET:-}" ]; then
    log "ERROR: S3_BUCKET is not set; exiting."
    exit 1
  fi
  if [ ! -d "${src_dir}" ]; then
    log "ERROR: source directory does not exist: ${src_dir}"
    exit 1
  fi

  log "Run started (src=${src_dir} dest=${dest})"

  local sync_log
  sync_log="$(mktemp)"
  set +e
  aws s3 sync "${src_dir}" "${dest}" --delete --no-progress >"${sync_log}" 2>&1
  local sync_status=$?
  set -e

  if [ -s "${sync_log}" ]; then
    while IFS= read -r line; do
      [ -n "${line}" ] && log "aws: ${line}"
    done < "${sync_log}"
  fi
  rm -f "${sync_log}"

  if [ "${sync_status}" -eq 0 ]; then
    log "Run succeeded"
  else
    log "Run FAILED exit_code=${sync_status}"
  fi

  return "${sync_status}"
}

run_trendbot_once() {
  local discord_env_file="${DISCORD_WORKER_ENV_FILE:-/app/app.env}"
  load_env_file "${discord_env_file}"

  local trendbot_env_file="${TRENDBOT_ENV_FILE:-/app/app.env}"
  load_env_file "${trendbot_env_file}"

  local trendbot_min_velocity="${TRENDBOT_MIN_VELOCITY:-500}"
  local trendbot_browser="${TRENDBOT_BROWSER:-chromium}"
  local trendbot_headless="${TRENDBOT_HEADLESS:-1}"
  local trendbot_api_max_attempts="${TRENDBOT_API_MAX_ATTEMPTS:-2}"
  local trendbot_api_nav_timeout_ms="${TRENDBOT_API_NAV_TIMEOUT_MS:-8000}"
  local trendbot_videos="${TRENDBOT_VIDEOS:-60}"
  local trendbot_top="${TRENDBOT_TOP:-25}"
  local trendbot_discover_scroll_rounds="${TRENDBOT_DISCOVER_SCROLL_ROUNDS:-6}"
  local trendbot_discover_dances_videos="${TRENDBOT_DISCOVER_DANCES_VIDEOS:-72}"
  local trendbot_topic_hashtag_pages="${TRENDBOT_TOPIC_HASHTAG_PAGES:-10}"
  local trendbot_topic_hashtag_video_samples="${TRENDBOT_TOPIC_HASHTAG_VIDEO_SAMPLES:-8}"
  local trendbot_topic_max_related_videos="${TRENDBOT_TOPIC_MAX_RELATED_VIDEOS:-120}"

  if [ -n "${TRENDBOT_RUN_CMD:-}" ]; then
    log "Starting custom trendbot command: ${TRENDBOT_RUN_CMD}"
    exec bash -lc "${TRENDBOT_RUN_CMD}"
  fi

  local trendbot_cmd=(
    "${python_bin}" "${repo_root}/scripts/find_viral_trends.py"
    --topic "dance challenges"
    --videos "${trendbot_videos}"
    --top "${trendbot_top}"
    --browser "${trendbot_browser}"
    --api-max-attempts "${trendbot_api_max_attempts}"
    --api-navigation-timeout-ms "${trendbot_api_nav_timeout_ms}"
    --discover-scroll-rounds "${trendbot_discover_scroll_rounds}"
    --discover-dances-videos "${trendbot_discover_dances_videos}"
    --topic-hashtag-pages "${trendbot_topic_hashtag_pages}"
    --topic-hashtag-video-samples "${trendbot_topic_hashtag_video_samples}"
    --topic-max-related-videos "${trendbot_topic_max_related_videos}"
    --supabase-min-velocity "${trendbot_min_velocity}"
  )

  if is_truthy "${trendbot_headless}"; then
    trendbot_cmd+=(--headless)
  fi

  log "Starting default trendbot command: ${trendbot_cmd[*]}"
  exec "${trendbot_cmd[@]}"
}

if [ "$#" -lt 1 ]; then
  echo "Usage: run_program.sh <program>" >&2
  exit 2
fi

program="$1"
shift || true

case "${program}" in
  app)
    run_app
    ;;
  listener)
    run_listener
    ;;
  discord-verify-bot)
    run_discord_verify_bot
    ;;
  watchdog)
    run_watchdog
    ;;
  s3-sync-once)
    run_s3_sync_once
    ;;
  trendbot-once)
    run_trendbot_once
    ;;
  *)
    echo "Unknown program: ${program}" >&2
    exit 2
    ;;
esac
