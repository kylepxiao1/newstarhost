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

default_usernames_for_shard() {
  local shard_idx="$1"
  case "${shard_idx}" in
    1) printf '%s\n' "wildcard_ns" ;;
    2) printf '%s\n' "viiceversa_ns,valentinananaaaa,cardin_v_" ;;
    3) printf '%s\n' "snyki.live,sv_cloveris,superv_sv,visiondance.leo,millarboys233,primalkings_officialjwm,sunsetnova__,bdcuphedc3,play.zr4,play.hero8,vfm.aero,chaos001inc" ;;
    *) printf '%s\n' "" ;;
  esac
}

collect_shard_indices() {
  local var_name=""
  local shard_idx=""
  local found=()
  while IFS= read -r var_name; do
    shard_idx="${var_name#TIKTOK_USERNAMES_SET_}"
    if [[ "${shard_idx}" =~ ^[0-9]+$ ]]; then
      found+=("${shard_idx}")
    fi
  done < <(compgen -A variable TIKTOK_USERNAMES_SET_)

  # Keep existing behavior when no shard env vars are defined.
  if [ "${#found[@]}" -eq 0 ]; then
    found=(1 2 3)
  fi

  printf '%s\n' "${found[@]}" | sort -n -u
}

pids=()
started=0
started_tags=()

while IFS= read -r shard_idx; do
  [ -n "${shard_idx}" ] || continue

  usernames_var="TIKTOK_USERNAMES_SET_${shard_idx}"
  heartbeat_var="LISTENER_HEARTBEAT_ID_SET_${shard_idx}"
  shard_tag="listener-set-${shard_idx}"

  usernames="${!usernames_var:-}"
  if [ -z "${usernames}" ]; then
    usernames="$(default_usernames_for_shard "${shard_idx}")"
  fi
  heartbeat_id="${!heartbeat_var:-listener-set-${shard_idx}}"

  if [ -z "${usernames}" ] || [ -z "${heartbeat_id}" ]; then
    log "Skipping ${shard_tag}: usernames or heartbeat_id is empty"
    continue
  fi

  run_shard_loop "${shard_tag}" "${usernames}" "${heartbeat_id}" &
  pids+=("$!")
  started=$((started + 1))
  started_tags+=("${shard_tag}:${heartbeat_id}")
done < <(collect_shard_indices)

if [ "${started}" -le 0 ]; then
  log "No listener shards started; configure TIKTOK_USERNAMES_SET_<N>."
  exit 1
fi

log "Started ${started} listener shard manager(s): ${started_tags[*]}"

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
