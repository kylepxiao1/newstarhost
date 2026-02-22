#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 4 ]; then
  echo "Usage: run_supervisor_program.sh <process_name> <memory_limit_mb|0> -- <command...>" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
run_with_memory_cap_script="${RUN_WITH_MEMORY_CAP_SCRIPT:-${script_dir}/run_with_memory_cap.sh}"

process_name="$1"
memory_limit_mb="$2"
shift 2

if [ "$1" != "--" ]; then
  echo "Expected '--' separator before command arguments." >&2
  exit 2
fi
shift

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

prefix_stdout() {
  while IFS= read -r line || [ -n "$line" ]; do
    printf '%s [%s] %s\n' "$(timestamp)" "$process_name" "$line"
  done
}

prefix_stderr() {
  while IFS= read -r line || [ -n "$line" ]; do
    printf '%s [%s] %s\n' "$(timestamp)" "$process_name" "$line" >&2
  done
}

set +e
/usr/bin/env bash "$run_with_memory_cap_script" "$memory_limit_mb" -- "$@" \
  > >(prefix_stdout) \
  2> >(prefix_stderr)
exit_code=$?
set -e

printf '%s [%s] process exited status=%s\n' "$(timestamp)" "$process_name" "$exit_code" >&2
exit "$exit_code"
