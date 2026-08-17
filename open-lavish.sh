#!/usr/bin/env bash
set -euo pipefail

ROOT="${AWOKI_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
PORT="${AWOKI_LAVISH_PORT:-}"

# Docker Compose reads .env automatically, but a normal host shell does not.
# Resolve only this non-secret scalar without sourcing arbitrary .env shell code.
if [[ -z "$PORT" && -f "$ROOT/.env" ]]; then
  PORT="$({ awk -F= '
    /^[[:space:]]*AWOKI_LAVISH_PORT[[:space:]]*=/ {
      value=$0
      sub(/^[^=]*=/, "", value)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      sub(/[[:space:]]+#.*$/, "", value)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      if ((substr(value,1,1)=="\"" && substr(value,length(value),1)=="\"") || (substr(value,1,1)=="\047" && substr(value,length(value),1)=="\047")) {
        value=substr(value,2,length(value)-2)
      }
      print value
    }
  ' "$ROOT/.env"; } | tail -n 1)"
fi
PORT="${PORT:-4387}"
URL="http://127.0.0.1:${PORT}"

if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "[awoki] invalid AWOKI_LAVISH_PORT: $PORT" >&2
  exit 2
fi

if command -v open >/dev/null 2>&1; then
  open "$URL"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 &
else
  echo "$URL"
  exit 0
fi
printf 'Opened %s\n' "$URL"
