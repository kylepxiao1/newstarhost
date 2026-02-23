#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${LISTENER_ENV_FILE:-/app/app.env}"
if [ -f "${ENV_FILE}" ]; then
  set -a
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  set +a
fi

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
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
listener_script="${LISTENER_SCRIPT_PATH:-/app/scripts/tiktok_listener.py}"
if [ ! -f "${listener_script}" ]; then
  listener_script="${script_dir}/tiktok_listener.py"
fi
restart_delay="${LISTENER_SHARD_RESTART_DELAY_SECONDS:-5}"

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log() {
  printf '%s [listener-launcher] %s\n' "$(timestamp)" "$*"
}

prefix_stdout() {
  local shard_tag="$1"
  while IFS= read -r line || [ -n "$line" ]; do
    printf '%s [%s] %s\n' "$(timestamp)" "${shard_tag}" "$line"
  done
}

prefix_stderr() {
  local shard_tag="$1"
  while IFS= read -r line || [ -n "$line" ]; do
    printf '%s [%s] %s\n' "$(timestamp)" "${shard_tag}" "$line" >&2
  done
}

run_shard_loop() {
  local shard_tag="$1"
  local usernames="$2"
  local heartbeat_id="$3"

  if [ -z "${usernames}" ] || [ -z "${heartbeat_id}" ]; then
    log "Skipping ${shard_tag}: usernames or heartbeat_id is empty"
    return 0
  fi

  while true; do
    log "Starting ${shard_tag} heartbeat_id=${heartbeat_id} usernames=${usernames}"
    set +e
    env \
      TIKTOK_USERNAMES="${usernames}" \
      LISTENER_HEARTBEAT_ID="${heartbeat_id}" \
      "${python_bin}" "${listener_script}" \
      > >(prefix_stdout "${shard_tag}") \
      2> >(prefix_stderr "${shard_tag}")
    exit_code=$?
    set -e
    log "${shard_tag} exited status=${exit_code}; restarting in ${restart_delay}s"
    sleep "${restart_delay}"
  done
}

shard1_usernames="${TIKTOK_USERNAMES_SET_1:-wildcard_boys}"
shard2_usernames="${TIKTOK_USERNAMES_SET_2:-afterdark_ns,valentinananaaaa,cardin_v_}"
shard3_usernames="${TIKTOK_USERNAMES_SET_3:-snyki.live,sv_cloveris,superv_sv,visiondance.leo,millarboys233,primalkings_officialjwm,sunsetnova__,bdcuphedc3,play.zr4,play.hero8,vfm.aero,chaos001inc}"

shard1_id="${LISTENER_HEARTBEAT_ID_SET_1:-listener-set-1}"
shard2_id="${LISTENER_HEARTBEAT_ID_SET_2:-listener-set-2}"
shard3_id="${LISTENER_HEARTBEAT_ID_SET_3:-listener-set-3}"

pids=()

run_shard_loop "listener-set-1" "${shard1_usernames}" "${shard1_id}" &
pids+=("$!")
run_shard_loop "listener-set-2" "${shard2_usernames}" "${shard2_id}" &
pids+=("$!")
run_shard_loop "listener-set-3" "${shard3_usernames}" "${shard3_id}" &
pids+=("$!")

cleanup() {
  log "Stopping listener shard processes"
  for pid in "${pids[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
    fi
  done
  wait || true
}

trap 'cleanup; exit 0' SIGINT SIGTERM

wait -n "${pids[@]}"
exit_code=$?
log "A listener shard manager exited unexpectedly status=${exit_code}"
cleanup
exit "${exit_code}"
