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

while true; do
  aws s3 sync "$SRC_DIR" "$DEST" --delete --only-show-errors || true
  sleep "$INTERVAL"
done
