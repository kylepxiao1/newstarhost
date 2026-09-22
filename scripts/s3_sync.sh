#!/usr/bin/env bash
set -euo pipefail

# Mirrors /app/media to a Supabase Storage bucket via Supabase's S3-compatible
# Storage API (https://supabase.com/docs/guides/storage/s3/authentication).
# Uses the AWS CLI pointed at the Supabase S3 endpoint, so behavior (recursive
# sync + delete of removed files) matches the previous AWS S3 setup.

ENV_FILE="${S3_SYNC_ENV_FILE:-/app/s3_sync.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

SRC_DIR="/app/media"
DEST="s3://${SUPABASE_S3_BUCKET:-}/${SUPABASE_S3_PREFIX:-media}"
INTERVAL="${S3_SYNC_INTERVAL:-86400}"

log() {
  printf '%s [supabase-sync] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$*"
}

for var in SUPABASE_S3_ENDPOINT SUPABASE_S3_REGION SUPABASE_S3_BUCKET SUPABASE_S3_ACCESS_KEY_ID SUPABASE_S3_SECRET_ACCESS_KEY; do
  if [ -z "${!var:-}" ]; then
    log "ERROR: $var is not set; exiting."
    exit 1
  fi
done

if [ ! -d "$SRC_DIR" ]; then
  log "ERROR: source directory does not exist: $SRC_DIR"
  exit 1
fi

export AWS_ACCESS_KEY_ID="$SUPABASE_S3_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$SUPABASE_S3_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION="$SUPABASE_S3_REGION"

# Supabase Storage's S3 API requires path-style requests
# (https://endpoint/bucket/key), not the AWS CLI's default virtual-hosted-style
# (https://bucket.endpoint/key), which resolves to the wrong host and fails
# with an unparsable error on PutObject.
aws configure set default.s3.addressing_style path

# AWS CLI 2.23+ defaults to streaming uploads with a CRC64NVME trailer
# checksum, which Supabase Storage (and most non-AWS S3-compatible services)
# rejects with a spurious SignatureDoesNotMatch. Fall back to legacy
# behavior where checksums are only added when the operation requires them.
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required
export AWS_RESPONSE_CHECKSUM_VALIDATION=when_required

log "Starting sync loop (src=$SRC_DIR dest=$DEST endpoint=$SUPABASE_S3_ENDPOINT interval=${INTERVAL}s)"

run_id=0
while true; do
  run_id=$((run_id + 1))
  started_at="$(date +%s)"
  log "Run $run_id started"

  sync_log="$(mktemp)"
  set +e
  aws s3 sync "$SRC_DIR" "$DEST" --delete --no-progress \
    --endpoint-url "$SUPABASE_S3_ENDPOINT" >"$sync_log" 2>&1
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
