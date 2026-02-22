#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${S3_SYNC_ENV_FILE:-/app/s3_sync.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

SRC_DIR="/app/media"
DEST="s3://${S3_BUCKET}/${S3_PREFIX:-media}"
INTERVAL="${S3_SYNC_INTERVAL:-86400}"

log() {
  printf '%s [s3-sync] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$*"
}

if [ -z "${S3_BUCKET:-}" ]; then
  log "ERROR: S3_BUCKET is not set; exiting."
  exit 1
fi

if [ ! -d "$SRC_DIR" ]; then
  log "ERROR: source directory does not exist: $SRC_DIR"
  exit 1
fi

log "Starting sync loop (src=$SRC_DIR dest=$DEST interval=${INTERVAL}s)"

run_id=0
while true; do
  run_id=$((run_id + 1))
  started_at="$(date +%s)"
  log "Run $run_id started"

  sync_log="$(mktemp)"
  set +e
  aws s3 sync "$SRC_DIR" "$DEST" --delete --no-progress >"$sync_log" 2>&1
  sync_status=$?
  set -e

  if [ -s "$sync_log" ]; then
    while IFS= read -r line; do
      [ -n "$line" ] && log "Run $run_id aws: $line"
    done < "$sync_log"
  fi
  rm -f "$sync_log"

  finished_at="$(date +%s)"
  duration=$((finished_at - started_at))
  if [ $sync_status -eq 0 ]; then
    log "Run $run_id succeeded (${duration}s)"
  else
    log "Run $run_id FAILED (${duration}s) exit_code=$sync_status"
  fi

  log "Sleeping ${INTERVAL}s before next run"
  sleep "$INTERVAL"
done
