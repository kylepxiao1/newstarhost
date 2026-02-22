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

if [ "$limit_mb" -gt 0 ]; then
  limit_kb=$((limit_mb * 1024))
  ulimit -S -v "$limit_kb"
  ulimit -H -v "$limit_kb"
  printf '%s [mem-cap] Applied %sMB virtual memory cap\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$limit_mb"
else
  printf '%s [mem-cap] Memory cap disabled for command\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
fi

exec "$@"
