#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec /usr/bin/env bash "${script_dir}/run_supervisor_program.sh" \
  app \
  "${APP_MEMORY_LIMIT_MB:-768}" \
  -- \
  /usr/bin/env bash "${script_dir}/run_backend_with_sync.sh"
