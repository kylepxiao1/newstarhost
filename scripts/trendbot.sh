#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${TRENDBOT_ENV_FILE:-/app/app.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

INTERVAL="${TRENDBOT_INTERVAL:-43200}"
CMD="${TRENDBOT_CMD:-python scripts/find_viral_trends.py --topic \"dance challenges\" --videos 120 --top 25 --browser webkit --api-max-attempts 5 --api-navigation-timeout-ms 10000 --discover-scroll-rounds 12 --discover-dances-videos 180 --topic-hashtag-pages 24 --topic-hashtag-video-samples 20 --topic-max-related-videos 400}"

log() {
  printf '%s [trendbot] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$*"
}

log "Starting trendbot loop (interval=${INTERVAL}s)"
log "Command: ${CMD}"

run_id=0
while true; do
  run_id=$((run_id + 1))
  started_at="$(date +%s)"
  log "Run $run_id started"

  run_log="$(mktemp)"
  set +e
  bash -lc "$CMD" >"$run_log" 2>&1
  run_status=$?
  set -e

  if [ -s "$run_log" ]; then
    while IFS= read -r line; do
      [ -n "$line" ] && log "Run $run_id output: $line"
    done < "$run_log"
  fi
  rm -f "$run_log"

  finished_at="$(date +%s)"
  duration=$((finished_at - started_at))
  if [ $run_status -eq 0 ]; then
    log "Run $run_id succeeded (${duration}s)"
  else
    log "Run $run_id FAILED (${duration}s) exit_code=$run_status"
  fi

  log "Sleeping ${INTERVAL}s before next run"
  sleep "$INTERVAL"
done
