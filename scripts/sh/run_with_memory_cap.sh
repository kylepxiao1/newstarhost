#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 3 ]; then
  echo "Usage: run_with_memory_cap.sh <memory_limit_mb|0> -- <command...>" >&2
  exit 2
fi

limit_mb="$1"
shift

if [ "$1" != "--" ]; then
  echo "Expected '--' separator before command arguments." >&2
  exit 2
fi
shift

if [[ ! "$limit_mb" =~ ^[0-9]+$ ]]; then
  echo "Memory limit must be a non-negative integer in MB. Got: $limit_mb" >&2
  exit 2
fi

if [ "$limit_mb" -eq 0 ]; then
  printf '[mem-cap] Memory cap disabled for command\n'
  exec "$@"
fi

limit_kb=$((limit_mb * 1024))
poll_secs="${MEMORY_CAP_POLL_SECONDS:-3}"
kill_grace_secs="${MEMORY_CAP_KILL_GRACE_SECONDS:-10}"

if [[ ! "$poll_secs" =~ ^[0-9]+$ ]] || [ "$poll_secs" -lt 1 ]; then
  poll_secs=3
fi
if [[ ! "$kill_grace_secs" =~ ^[0-9]+$ ]] || [ "$kill_grace_secs" -lt 1 ]; then
  kill_grace_secs=10
fi

printf '[mem-cap] Enforcing RSS limit=%sMB poll=%ss grace=%ss\n' "$limit_mb" "$poll_secs" "$kill_grace_secs"

"$@" &
child_pid=$!
cap_triggered=0

cleanup_child() {
  if kill -0 "$child_pid" 2>/dev/null; then
    kill "$child_pid" 2>/dev/null || true
  fi
}

trap 'cleanup_child' SIGINT SIGTERM

while kill -0 "$child_pid" 2>/dev/null; do
  rss_kb="$(awk '/^VmRSS:/ { print $2; exit }' "/proc/${child_pid}/status" 2>/dev/null || true)"
  if [[ "$rss_kb" =~ ^[0-9]+$ ]] && [ "$rss_kb" -gt "$limit_kb" ]; then
    printf '[mem-cap] RSS exceeded: pid=%s rss_kb=%s limit_kb=%s; sending TERM\n' "$child_pid" "$rss_kb" "$limit_kb"
    cap_triggered=1
    kill "$child_pid" 2>/dev/null || true
    for _ in $(seq 1 "$kill_grace_secs"); do
      if ! kill -0 "$child_pid" 2>/dev/null; then
        break
      fi
      sleep 1
    done
    if kill -0 "$child_pid" 2>/dev/null; then
      printf '[mem-cap] Process did not exit after TERM; sending KILL pid=%s\n' "$child_pid"
      kill -9 "$child_pid" 2>/dev/null || true
    fi
    break
  fi
  sleep "$poll_secs"
done

set +e
wait "$child_pid"
exit_code=$?
set -e

if [ "$cap_triggered" -eq 1 ]; then
  printf '[mem-cap] Process exited after memory-cap enforcement status=%s\n' "$exit_code"
fi

exit "$exit_code"
