#!/usr/bin/env bash
set -euo pipefail

ROOT="${AWOKI_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
cd "$ROOT"

.harness/bin/init-layout

echo
echo "Awoki base layout is initialized."
echo "Next: ./run-opencode.sh"
echo "Guided first install: ./install-awoki.sh"
