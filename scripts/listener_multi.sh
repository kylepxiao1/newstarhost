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
  local child=""

  if [ -z "${usernames}" ] || [ -z "${heartbeat_id}" ]; then
    log "Skipping ${shard_tag}: usernames or heartbeat_id is empty"
    return 0
  fi

  # Stopping this loop (account list changed, or shutdown) also stops its listener.
  trap 'if [ -n "${child}" ]; then kill "${child}" 2>/dev/null || true; wait "${child}" 2>/dev/null || true; fi; exit 0' SIGINT SIGTERM

  while true; do
    log "Starting ${shard_tag} heartbeat_id=${heartbeat_id} usernames=${usernames}"
    set +e
    env \
      TIKTOK_USERNAMES="${usernames}" \
      LISTENER_HEARTBEAT_ID="${heartbeat_id}" \
      "${python_bin}" "${listener_script}" \
      > >(prefix_stdout "${shard_tag}") \
      2> >(prefix_stderr "${shard_tag}") &
    child=$!
    wait "${child}"
    exit_code=$?
    child=""
    set -e
    log "${shard_tag} exited status=${exit_code}; restarting in ${restart_delay}s"
    sleep "${restart_delay}" &
    wait $! || true
  done
}

default_usernames_for_shard() {
  local shard_idx="$1"
  case "${shard_idx}" in
    1) printf '%s\n' "wildcard_ns" ;;
    2) printf '%s\n' "viiceversa_ns,valentinananaaaa,teru_shima" ;;
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

# Sets 1-3 (primary account, group accounts, dancer accounts) come from Supabase via
# listener_accounts.py and are re-checked every LISTENER_ACCOUNTS_REFRESH_SECONDS, so new
# groups and dancers are picked up without editing app.env. Sets 4+ stay in app.env.
accounts_script="${script_dir}/listener_accounts.py"
refresh_seconds="${LISTENER_ACCOUNTS_REFRESH_SECONDS:-300}"
AUTO_SETS=(1 2 3)
declare -A shard_users=()
declare -A shard_pids=()

auto_accounts() {
  # Prints "<set>\t<handles>" lines, or fails (callers then keep what they have).
  "${python_bin}" "${accounts_script}" 2>/dev/null
}

usernames_for_shard() {
  local shard_idx="$1"
  local usernames_var="TIKTOK_USERNAMES_SET_${shard_idx}"
  local usernames="${!usernames_var:-}"
  if [ -z "${usernames}" ]; then
    usernames="$(default_usernames_for_shard "${shard_idx}")"
  fi
  printf '%s\n' "${usernames}"
}

start_shard() {
  local shard_idx="$1"
  local usernames="$2"
  local heartbeat_var="LISTENER_HEARTBEAT_ID_SET_${shard_idx}"
  local heartbeat_id="${!heartbeat_var:-listener-set-${shard_idx}}"
  shard_users["${shard_idx}"]="${usernames}"
  if [ -z "${usernames}" ]; then
    log "listener-set-${shard_idx}: no accounts"
    unset 'shard_pids[$shard_idx]'
    return 0
  fi
  run_shard_loop "listener-set-${shard_idx}" "${usernames}" "${heartbeat_id}" &
  shard_pids["${shard_idx}"]=$!
}

stop_shard() {
  local shard_idx="$1"
  local pid="${shard_pids[$shard_idx]:-}"
  if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}" 2>/dev/null || true
    wait "${pid}" 2>/dev/null || true
  fi
  unset 'shard_pids[$shard_idx]'
}

declare -A initial=()
if auto_output="$(auto_accounts)"; then
  while IFS=$'\t' read -r idx handles; do
    [ -n "${idx}" ] && initial["${idx}"]="${handles}"
  done <<< "${auto_output}"
  log "Accounts for sets ${AUTO_SETS[*]} loaded from Supabase"
else
  log "WARNING: couldn't load accounts from Supabase; using app.env for sets ${AUTO_SETS[*]}"
  for idx in "${AUTO_SETS[@]}"; do
    initial["${idx}"]="$(usernames_for_shard "${idx}")"
  done
fi

for idx in "${AUTO_SETS[@]}"; do
  start_shard "${idx}" "${initial[$idx]:-}"
done
while IFS= read -r shard_idx; do
  [ -n "${shard_idx}" ] || continue
  case " ${AUTO_SETS[*]} " in *" ${shard_idx} "*) continue ;; esac
  start_shard "${shard_idx}" "$(usernames_for_shard "${shard_idx}")"
done < <(collect_shard_indices)

if [ "${#shard_pids[@]}" -le 0 ]; then
  log "No listener shards started; no accounts found."
  exit 1
fi
log "Started ${#shard_pids[@]} listener shard manager(s): sets ${!shard_pids[*]}"

cleanup() {
  log "Stopping listener shard processes"
  for idx in "${!shard_pids[@]}"; do
    stop_shard "${idx}"
  done
}

trap 'cleanup; exit 0' SIGINT SIGTERM

while true; do
  sleep "${refresh_seconds}" &
  wait $! || true
  for idx in "${!shard_pids[@]}"; do
    if ! kill -0 "${shard_pids[$idx]}" 2>/dev/null; then
      log "listener-set-${idx} manager exited unexpectedly"
      cleanup
      exit 1
    fi
  done
  if ! auto_output="$(auto_accounts)"; then
    log "WARNING: account refresh from Supabase failed; keeping current accounts"
    continue
  fi
  while IFS=$'\t' read -r idx handles; do
    [ -n "${idx}" ] || continue
    if [ "${handles}" != "${shard_users[$idx]:-}" ]; then
      log "listener-set-${idx} accounts changed: '${shard_users[$idx]:-}' -> '${handles}'; restarting it"
      stop_shard "${idx}"
      start_shard "${idx}" "${handles}"
    fi
  done <<< "${auto_output}"
done
