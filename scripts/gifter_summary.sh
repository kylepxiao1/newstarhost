#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
summary_names_override="${GIFTER_SUMMARY_USERNAMES:-}"
config_file="${GIFTER_SUMMARY_CONFIG:-$repo_dir/config/gifter_summary.env}"
if [ -f "$config_file" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$config_file"
  set +a
fi
if [ -n "$summary_names_override" ]; then
  export GIFTER_SUMMARY_USERNAMES="$summary_names_override"
fi
if [ -n "${PYTHON_BIN:-}" ]; then
  summary_python="$PYTHON_BIN"
elif [ -x "$repo_dir/.venv/bin/python" ]; then
  summary_python="$repo_dir/.venv/bin/python"
else
  summary_python=python3
fi
exec "$summary_python" scripts/tiktok_gifter_summary.py "$@"
