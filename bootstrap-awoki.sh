#!/usr/bin/env bash
set -euo pipefail

# Host-side bootstrap for release/source ZIPs. It safely handles the outer
# "existing checkout -> fresh archive -> guided setup" sequence, then delegates
# runtime configuration to install-awoki.sh.

ARCHIVE_INPUT="${1:-}"
TARGET="${2:-${AWOKI_INSTALL_TARGET:-$HOME/awoki}}"
if [[ $# -ge 1 ]]; then shift; fi
if [[ $# -ge 1 ]]; then shift; fi
INSTALLER_ARGS=("$@")

prompt() {
  local text="$1" default="$2" answer
  printf '%s [%s]: ' "$text" "$default" >&2
  IFS= read -r answer || answer=""
  printf '%s' "${answer:-$default}"
}

prompt_yes_no() {
  local text="$1" default="$2" answer suffix
  if [[ "$default" == "yes" ]]; then suffix="Y/n"; else suffix="y/N"; fi
  while true; do
    printf '%s [%s]: ' "$text" "$suffix" >&2
    IFS= read -r answer || answer=""
    if [[ -z "$answer" ]]; then [[ "$default" == "yes" ]] && return 0 || return 1; fi
    case "$answer" in
      y|Y|yes|YES) return 0 ;;
      n|N|no|NO) return 1 ;;
      *) echo "Please answer y or n." >&2 ;;
    esac
  done
}

abs_path() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().resolve())
PY
}

command -v python3 >/dev/null 2>&1 || { echo "[awoki] python3 is required" >&2; exit 2; }
command -v unzip >/dev/null 2>&1 || { echo "[awoki] unzip is required" >&2; exit 2; }
command -v git >/dev/null 2>&1 || { echo "[awoki] git is required" >&2; exit 2; }

if [[ -z "$ARCHIVE_INPUT" ]]; then
  ARCHIVE_INPUT="$(prompt "Path or HTTPS URL to Awoki ZIP" "$HOME/Downloads/awoki.zip")"
fi
TARGET="$(abs_path "$TARGET")"

tmp="$(mktemp -d "${TMPDIR:-/tmp}/awoki-bootstrap.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

case "$ARCHIVE_INPUT" in
  https://*|http://*)
    command -v curl >/dev/null 2>&1 || { echo "[awoki] curl is required for URL archives" >&2; exit 2; }
    archive="$tmp/awoki-download.zip"
    echo "[awoki] downloading archive: $ARCHIVE_INPUT"
    curl --fail --location --proto '=https' --tlsv1.2 --output "$archive" "$ARCHIVE_INPUT"
    ;;
  *)
    archive="$(abs_path "$ARCHIVE_INPUT")"
    [[ -f "$archive" ]] || { echo "[awoki] archive not found: $archive" >&2; exit 2; }
    ;;
esac

printf '\nAwoki bootstrap\nArchive: %s\nTarget:  %s\n\n' "$ARCHIVE_INPUT" "$TARGET"

if [[ -e "$TARGET" ]]; then
  echo "Existing target detected."
  echo "  1) Move it aside with a timestamp (recommended; preserves everything)"
  echo "  2) Choose another target path"
  echo "  3) Abort"
  while true; do
    choice="$(prompt "Choose" 1)"
    case "$choice" in
      1)
        backup="${TARGET}.previous.$(date +%Y%m%d-%H%M%S)"
        while [[ -e "$backup" ]]; do backup="${backup}.1"; done
        mv "$TARGET" "$backup"
        echo "[awoki] previous checkout moved to: $backup"
        echo "[awoki] running Docker containers are intentionally left alone; the new installer will identify the stale runtime instance and ask how to handle it."
        break
        ;;
      2)
        TARGET="$(abs_path "$(prompt "New target path" "${TARGET}.new")")"
        if [[ -e "$TARGET" ]]; then echo "That path already exists."; else break; fi
        ;;
      3) echo "[awoki] aborted; nothing removed."; exit 0 ;;
      *) echo "Choose 1, 2, or 3." ;;
    esac
  done
fi

parent="$(dirname "$TARGET")"
mkdir -p "$parent"
extract_root="$tmp/extracted"
mkdir -p "$extract_root"
unzip -q "$archive" -d "$extract_root"

source_root=""
if [[ -f "$extract_root/README.md" && -d "$extract_root/.harness" ]]; then
  source_root="$extract_root"
else
  for candidate in "$extract_root"/*; do
    if [[ -d "$candidate" && -f "$candidate/README.md" && -d "$candidate/.harness" ]]; then
      if [[ -n "$source_root" ]]; then
        echo "[awoki] archive contains more than one candidate Awoki root" >&2
        exit 2
      fi
      source_root="$candidate"
    fi
  done
fi
[[ -n "$source_root" ]] || { echo "[awoki] archive does not contain an Awoki checkout" >&2; exit 2; }

mkdir -p "$TARGET"
( cd "$source_root" && tar -cf - . ) | ( cd "$TARGET" && tar -xf - )
[[ -x "$TARGET/install-awoki.sh" ]] || chmod +x "$TARGET/install-awoki.sh" 2>/dev/null || true

echo "[awoki] extracted fresh checkout to $TARGET"
cd "$TARGET"

if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
  echo
  echo "[awoki] This archive contains source files but no Git history (normal for GitHub Download ZIP)."
  echo "[awoki] Awoki needs a valid HEAD for self-development/runtime preflight."
  echo "[awoki] A local baseline commit is sufficient for running/testing, but it is NOT upstream Git history."
  echo "[awoki] For development/publishing, use a real git clone instead."
  if ! prompt_yes_no "Create a local baseline Git commit for this extracted source?" yes; then
    echo "[awoki] cannot continue without a Git HEAD; leaving extracted files at $TARGET" >&2
    exit 2
  fi
  if [[ ! -d .git ]]; then git init -q; fi
  current_branch="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
  if [[ "$current_branch" != "main" ]]; then git checkout -q -B main; fi
  git config user.name "Awoki Installer"
  git config user.email "installer@awoki.local"
  git add -A
  git commit -qm "Import Awoki source archive for local runtime"
  echo "[awoki] local baseline HEAD created: $(git rev-parse --short HEAD)"
fi

exec ./install-awoki.sh "${INSTALLER_ARGS[@]}"
