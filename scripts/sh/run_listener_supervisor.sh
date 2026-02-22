#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec /usr/bin/env bash "${script_dir}/run_supervisor_program.sh" \
  listener \
  "${LISTENER_MEMORY_LIMIT_MB:-1024}" \
  -- \
  python "${script_dir}/../tiktok_listener.py"
