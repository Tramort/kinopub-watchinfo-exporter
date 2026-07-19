#!/bin/sh
set -eu

if [ "$#" -lt 1 ]; then
  echo "Usage: <script.py> [args...]" >&2
  echo "Examples:" >&2
  echo "  kinopub-exporter.py" >&2
  echo "  traktv-importer.py --mismatch-auto-approve" >&2
  echo "  trakt-sonarr-nextup.py --dry-run" >&2
  echo "Set CRON_SCHEDULE to run periodically (e.g. '0 */6 * * *')." >&2
  exit 1
fi

script_name="$1"
shift

case "$script_name" in
  */*|.*|*..*)
    echo "Script must be a bare filename under /app (got: ${script_name})" >&2
    exit 1
    ;;
esac

script="/app/${script_name}"
if [ ! -f "$script" ]; then
  echo "Script not found: ${script}" >&2
  exit 1
fi

run_once() {
  exec python "$script" "$@"
}

if [ -z "${CRON_SCHEDULE:-}" ]; then
  run_once "$@"
fi

crontab_file="/tmp/crontab"
{
  printf '%s' "$CRON_SCHEDULE"
  printf ' cd /app && python %s' "$script"
  for arg in "$@"; do
    escaped=$(printf '%s' "$arg" | sed "s/'/'\\\\''/g")
    printf " '%s'" "$escaped"
  done
  printf '\n'
} >"$crontab_file"

echo "Running on schedule: ${CRON_SCHEDULE}" >&2
echo "Command: python ${script}$(printf ' %s' "$@")" >&2
exec supercronic "$crontab_file"
