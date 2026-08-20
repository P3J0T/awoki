#!/usr/bin/env bash
set -euo pipefail

ROOT="${AWOKI_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
cd "$ROOT"

INTERACTIVE=1
CONFIGURE_ONLY=0
SKIP_RUNTIME_CHECK=0

usage() {
  cat <<'USAGE'
Usage: ./install-awoki.sh [options]

Interactive first-run installer for the Docker + OpenCode Web + SSH runtime.

Options:
  --configure-only      Configure .env/OpenCode choices and initialize layout, but do not build/start.
  --skip-runtime-check  Start the runtime but do not run make opencode-runtime-check afterward.
  --non-interactive     Use .env.example/current .env without prompts; equivalent to the deterministic install path.
  -h, --help            Show this help.

For automation, the existing ./init-awoki.sh + make install-opencode-ssh path remains supported.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --configure-only) CONFIGURE_ONLY=1 ;;
    --skip-runtime-check) SKIP_RUNTIME_CHECK=1 ;;
    --non-interactive) INTERACTIVE=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[awoki] unknown installer option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if (( INTERACTIVE )) && [[ ! -t 0 || ! -t 1 ]]; then
  if [[ "${AWOKI_INSTALL_FORCE_INTERACTIVE:-0}" != "1" ]]; then
    echo "[awoki] interactive installer requires a terminal." >&2
    echo "[awoki] Run it directly, or use --non-interactive for automation." >&2
    exit 2
  fi
fi

ENV_FILE="$ROOT/.env"
ENV_EXAMPLE="$ROOT/.env.example"
LAYOUT_MARKER="$ROOT/.harness/state/layout_initialized.json"
COMPOSE_FILE="$ROOT/docker-compose.opencode.yml"
OPENCODE_USER_CONFIG="$ROOT/.opencode-state/config/opencode.jsonc"

ENV_BASELINE="$(mktemp "${TMPDIR:-/tmp}/awoki-env-baseline.XXXXXX")"
chmod 600 "$ENV_BASELINE" 2>/dev/null || true
trap 'rm -f "$ENV_BASELINE"' EXIT

command -v python3 >/dev/null 2>&1 || { echo "[awoki] Python 3 is required." >&2; exit 2; }
command -v git >/dev/null 2>&1 || { echo "[awoki] git is required." >&2; exit 2; }
[[ -f "$ENV_EXAMPLE" ]] || { echo "[awoki] missing .env.example" >&2; exit 2; }

if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
  echo "[awoki] this checkout has no Git HEAD." >&2
  echo "[awoki] Install from a real clone/package with history before building the runtime." >&2
  exit 2
fi

prompt_yes_no() {
  local prompt="$1" default="$2" answer suffix
  if [[ "$default" == "yes" ]]; then suffix="Y/n"; else suffix="y/N"; fi
  while true; do
    printf '%s [%s]: ' "$prompt" "$suffix"
    IFS= read -r answer || answer=""
    if [[ -z "$answer" ]]; then [[ "$default" == "yes" ]] && return 0 || return 1; fi
    case "$answer" in
      y|Y|yes|YES) return 0 ;;
      n|N|no|NO) return 1 ;;
      *) echo "Please answer y or n." ;;
    esac
  done
}

prompt_value() {
  local prompt="$1" current="$2" answer
  if [[ -n "$current" ]]; then
    printf '%s [%s]: ' "$prompt" "$current" >&2
  else
    printf '%s: ' "$prompt" >&2
  fi
  IFS= read -r answer || answer=""
  if [[ -z "$answer" ]]; then answer="$current"; fi
  printf '%s' "$answer"
}

prompt_secret() {
  local prompt="$1" has_current="$2" answer
  if [[ "$has_current" == "1" ]]; then
    printf '%s [Enter keeps existing, type - to clear]: ' "$prompt" >&2
  else
    printf '%s [Enter leaves empty]: ' "$prompt" >&2
  fi
  IFS= read -r -s answer || answer=""
  printf '\n' >&2
  printf '%s' "$answer"
}

env_get() {
  local key="$1" default_value="${2:-}"
  python3 - "$ENV_FILE" "$key" "$default_value" <<'PY'
from pathlib import Path
import re, sys
path, key, default = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
if not path.exists():
    print(default, end="")
    raise SystemExit
value = None
for line in path.read_text(encoding="utf-8").splitlines():
    m = re.match(r"^\s*" + re.escape(key) + r"\s*=\s*(.*)$", line)
    if m:
        value = m.group(1).strip()
if value is None:
    value = default
if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
    value = value[1:-1]
print(value, end="")
PY
}

env_set() {
  local key="$1" value="$2"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || { echo "[awoki] refusing multiline .env value for $key" >&2; exit 2; }
  python3 - "$ENV_FILE" "$key" "$value" <<'PY'
from pathlib import Path
import re, sys
path, key, value = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
out = []
replaced = False
pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
for line in lines:
    if pattern.match(line):
        if not replaced:
            out.append(f"{key}={value}")
            replaced = True
        continue
    out.append(line)
if not replaced:
    if out and out[-1] != "": out.append("")
    out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
}

env_snapshot() {
  local output="$1" source="${2:-$ENV_FILE}"
  python3 - "$source" "$output" <<'PY'
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import hashlib, json, re, sys

env_path, output_path = Path(sys.argv[1]), Path(sys.argv[2])
key_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
values = {}
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        m = key_re.match(line)
        if not m:
            continue
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[m.group(1)] = value

def is_secret(key: str) -> bool:
    if key.endswith("_KEY_ENV"):
        return False
    return key.endswith(("_API_KEY", "_PASSWORD", "_TOKEN", "_SECRET")) or "CREDENTIAL" in key

def display_value(key: str, value: str) -> str:
    if not value:
        return "<empty>"
    if is_secret(key):
        return "<set>"
    if key.endswith("_URL") or key.endswith("_BASE_URL"):
        try:
            parsed = urlsplit(value)
            if parsed.scheme and parsed.hostname:
                host = parsed.hostname
                if parsed.port:
                    host = f"{host}:{parsed.port}"
                return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        except ValueError:
            return "<configured URL>"
    return value

snapshot = {}
for key, value in values.items():
    snapshot[key] = {
        "digest": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "display": display_value(key, value),
        "secret": is_secret(key),
    }
output_path.write_text(json.dumps(snapshot, sort_keys=True), encoding="utf-8")
PY
}

show_env_changes() {
  local baseline="$1"
  python3 - "$baseline" "$ENV_FILE" <<'PY'
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import hashlib, json, re, sys

baseline_path, env_path = Path(sys.argv[1]), Path(sys.argv[2])
before = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else {}
key_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
values = {}
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        m = key_re.match(line)
        if not m:
            continue
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[m.group(1)] = value

def is_secret(key: str) -> bool:
    if key.endswith("_KEY_ENV"):
        return False
    return key.endswith(("_API_KEY", "_PASSWORD", "_TOKEN", "_SECRET")) or "CREDENTIAL" in key

def display_value(key: str, value: str) -> str:
    if not value:
        return "<empty>"
    if is_secret(key):
        return "<set>"
    if key.endswith("_URL") or key.endswith("_BASE_URL"):
        try:
            parsed = urlsplit(value)
            if parsed.scheme and parsed.hostname:
                host = parsed.hostname
                if parsed.port:
                    host = f"{host}:{parsed.port}"
                return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        except ValueError:
            return "<configured URL>"
    return value

current = {}
for key, value in values.items():
    current[key] = {
        "digest": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "display": display_value(key, value),
        "secret": is_secret(key),
    }

changed = []
for key in sorted(set(before) | set(current)):
    old = before.get(key)
    new = current.get(key)
    if old and new and old.get("digest") == new.get("digest"):
        continue
    changed.append((key, old, new))

if not changed:
    print("  .env: no changes since installer start")
    raise SystemExit

print(f"  .env: {len(changed)} changed setting(s)")
for key, old, new in changed:
    if new is None:
        print(f"    {key}: {old.get('display', '<set>')} -> <removed>")
    elif old is None:
        print(f"    {key}: <absent> -> {new.get('display', '<set>')}")
    elif new.get("secret") or old.get("secret"):
        print(f"    {key}: {old.get('display', '<set>')} -> {new.get('display', '<set>')} (value redacted)")
    else:
        print(f"    {key}: {old.get('display', '<empty>')} -> {new.get('display', '<empty>')}")
PY
}

open_editor() {
  local path="$1" editor_cmd
  editor_cmd="${EDITOR:-}"
  if [[ -n "$editor_cmd" ]]; then
    # shellcheck disable=SC2086
    $editor_cmd "$path"
    return
  fi
  if command -v nano >/dev/null 2>&1; then nano "$path"; return; fi
  if command -v vi >/dev/null 2>&1; then vi "$path"; return; fi
  echo "[awoki] no interactive editor found; edit this file in another terminal: $path" >&2
  return 2
}

ensure_opencode_user_config() {
  install -d -m 700 "$(dirname "$OPENCODE_USER_CONFIG")"
  if [[ ! -f "$OPENCODE_USER_CONFIG" ]]; then
    cat >"$OPENCODE_USER_CONFIG" <<'EOF_OPENCODE_USER'
{
  "$schema": "https://opencode.ai/config.json"
}
EOF_OPENCODE_USER
    chmod 600 "$OPENCODE_USER_CONFIG" 2>/dev/null || true
    echo "[awoki] created OpenCode user config: ${OPENCODE_USER_CONFIG#$ROOT/}"
  fi
}

validate_opencode_user_config() {
  [[ -f "$OPENCODE_USER_CONFIG" ]] || return 0
  "$ROOT/.harness/bin/opencode-user-config-check" "$OPENCODE_USER_CONFIG"
}

show_opencode_config_locations() {
  cat <<EOF

OpenCode configuration used by this Awoki installation:
  Custom provider/model config (edit this):
    $OPENCODE_USER_CONFIG
  Container path:
    /home/op/.config/opencode/opencode.jsonc
  Awoki-owned project config (normally do not put personal provider credentials here):
    $ROOT/opencode.jsonc

The user config is under ignored .opencode-state/, is not sent in the Docker build
context, and is bind-mounted into OpenCode. Provider/model changes therefore do
not require an Awoki image rebuild. After an installed runtime is running, use:
  make opencode-config-reload

Prefer OpenCode's credential store (installer provider-login step or
'opencode auth login') for API credentials when the provider supports it.
EOF
}

configure_opencode_user_interactive() {
  local choice
  ensure_opencode_user_config
  echo
  echo "== OpenCode provider/model configuration =="
  show_opencode_config_locations
  while true; do
    echo
    echo "Before Docker build you may paste your custom OpenCode provider/model JSONC now."
    echo "  1) Open/edit the OpenCode user config now (paste custom provider here)"
    echo "  2) Show the config path/instructions again"
    echo "  3) Keep the current OpenCode user config and continue"
    printf 'Choose [1]: '
    IFS= read -r choice || choice=""
    choice="${choice:-1}"
    case "$choice" in
      1)
        open_editor "$OPENCODE_USER_CONFIG" || true
        if validate_opencode_user_config; then
          echo "[awoki] OpenCode user config syntax is valid."
        else
          echo "[awoki] OpenCode user config is invalid; fix it before Docker build/start." >&2
          continue
        fi
        ;;
      2) show_opencode_config_locations ;;
      3) validate_opencode_user_config; return 0 ;;
      *) echo "Choose 1, 2, or 3." ;;
    esac
  done
}

refresh_env_preserving_values() {
  local backup=""
  if [[ -f "$ENV_FILE" ]]; then
    backup="$ROOT/.env.backup.$(date +%Y%m%d-%H%M%S)"
    cp -p "$ENV_FILE" "$backup"
  fi
  python3 - "$ENV_EXAMPLE" "$ENV_FILE" <<'PY'
from pathlib import Path
import re, sys
example, current = Path(sys.argv[1]), Path(sys.argv[2])
old_lines = current.read_text(encoding="utf-8").splitlines() if current.exists() else []
values = {}
unknown_lines = []
key_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
for line in old_lines:
    m = key_re.match(line)
    if m:
        values[m.group(1)] = m.group(2)
example_keys = set()
out = []
for line in example.read_text(encoding="utf-8").splitlines():
    m = key_re.match(line)
    if m:
        key = m.group(1); example_keys.add(key)
        if key in values:
            line = f"{key}={values[key]}"
    out.append(line)
extras = [line for line in old_lines if (m := key_re.match(line)) and m.group(1) not in example_keys]
if extras:
    out.extend(["", "# Preserved local keys not present in the current .env.example"])
    out.extend(extras)
current.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
  if [[ -n "$backup" ]]; then echo "[awoki] previous .env saved as ${backup#$ROOT/}"; fi
}

configure_env_interactive() {
  echo
  echo "== Runtime configuration (.env) =="
  if [[ ! -f "$ENV_FILE" ]]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    echo "[awoki] created .env from .env.example"
  else
    echo "Existing .env detected."
    echo "  1) Refresh template/comments and preserve current values (recommended)"
    echo "  2) Keep .env exactly as-is"
    echo "  3) Reset to .env.example (backs up current .env)"
    local choice
    while true; do
      printf 'Choose [1]: '
      IFS= read -r choice || choice=""
      choice="${choice:-1}"
      case "$choice" in
        1) refresh_env_preserving_values; break ;;
        2) break ;;
        3)
          cp -p "$ENV_FILE" "$ROOT/.env.backup.$(date +%Y%m%d-%H%M%S)"
          cp "$ENV_EXAMPLE" "$ENV_FILE"
          break ;;
        *) echo "Choose 1, 2, or 3." ;;
      esac
    done
  fi

  local value
  value="$(prompt_value "Docker Compose project name" "$(env_get AWOKI_COMPOSE_PROJECT_NAME awoki)")"
  [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || { echo "[awoki] invalid Compose project name" >&2; exit 2; }
  env_set AWOKI_COMPOSE_PROJECT_NAME "$value"

  value="$(prompt_value "SSH port on host loopback" "$(env_get AWOKI_OPENCODE_SSH_PORT 2222)")"
  [[ "$value" =~ ^[0-9]+$ ]] && (( value >= 1 && value <= 65535 )) || { echo "[awoki] invalid SSH port" >&2; exit 2; }
  env_set AWOKI_OPENCODE_SSH_PORT "$value"

  local web_current web_default
  web_current="$(env_get AWOKI_OPENCODE_WEB_ENABLED 1)"
  case "$web_current" in 0|false|FALSE|no|NO|off|OFF) web_default=no ;; *) web_default=yes ;; esac
  if prompt_yes_no "Enable authenticated OpenCode Web on host loopback?" "$web_default"; then
    env_set AWOKI_OPENCODE_WEB_ENABLED 1
    value="$(prompt_value "OpenCode Web port" "$(env_get AWOKI_OPENCODE_WEB_PORT 4096)")"
    [[ "$value" =~ ^[0-9]+$ ]] && (( value >= 1 && value <= 65535 )) || { echo "[awoki] invalid Web port" >&2; exit 2; }
    env_set AWOKI_OPENCODE_WEB_PORT "$value"
    value="$(prompt_value "OpenCode Web username" "$(env_get AWOKI_OPENCODE_WEB_USERNAME opencode)")"
    [[ "$value" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "[awoki] invalid Web username" >&2; exit 2; }
    env_set AWOKI_OPENCODE_WEB_USERNAME "$value"
    if prompt_yes_no "Generate/preserve a random Web password instead of storing one in .env?" yes; then
      env_set AWOKI_OPENCODE_WEB_PASSWORD ""
    else
      local existing secret
      existing="$(env_get AWOKI_OPENCODE_WEB_PASSWORD '')"
      secret="$(prompt_secret "OpenCode Web password" "$([[ -n "$existing" ]] && echo 1 || echo 0)")"
      if [[ -z "$secret" ]]; then secret="$existing"; fi
      if [[ "$secret" == "-" ]]; then secret=""; fi
      [[ -n "$secret" ]] || { echo "[awoki] blank explicit password selected; random generation will be used instead"; }
      env_set AWOKI_OPENCODE_WEB_PASSWORD "$secret"
    fi
  else
    env_set AWOKI_OPENCODE_WEB_ENABLED 0
  fi

  local embedding_current embedding_default
  embedding_current="$(env_get AWOKI_EMBEDDING_BASE_URL '')"
  [[ -n "$embedding_current" ]] && embedding_default=yes || embedding_default=no
  if prompt_yes_no "Configure remote/OpenAI-compatible embeddings now?" "$embedding_default"; then
    value="$(prompt_value "Embedding base URL" "$embedding_current")"
    env_set AWOKI_EMBEDDING_BASE_URL "$value"
    value="$(prompt_value "Embedding deployment/model identity" "$(env_get AWOKI_EMBEDDING_DEPLOYMENT_ID jinaai/jina-embeddings-v2-base-code)")"
    env_set AWOKI_EMBEDDING_DEPLOYMENT_ID "$value"
    value="$(prompt_value "Embedding vector size" "$(env_get AWOKI_VECTOR_SIZE 768)")"
    [[ "$value" =~ ^[0-9]+$ ]] || { echo "[awoki] invalid vector size" >&2; exit 2; }
    env_set AWOKI_VECTOR_SIZE "$value"
    local old_key new_key
    old_key="$(env_get AWOKI_EMBEDDING_API_KEY '')"
    new_key="$(prompt_secret "Embedding API key" "$([[ -n "$old_key" ]] && echo 1 || echo 0)")"
    if [[ "$new_key" == "-" ]]; then old_key=""; elif [[ -n "$new_key" ]]; then old_key="$new_key"; fi
    env_set AWOKI_EMBEDDING_API_KEY "$old_key"
  fi

  local rerank_current rerank_default
  rerank_current="$(env_get AWOKI_RERANK_ENABLED 0)"
  case "$rerank_current" in 1|true|TRUE|yes|YES|on|ON) rerank_default=yes ;; *) rerank_default=no ;; esac
  if prompt_yes_no "Enable/configure the optional reranker?" "$rerank_default"; then
    env_set AWOKI_RERANK_ENABLED 1
    value="$(prompt_value "Reranker URL" "$(env_get AWOKI_RERANK_URL '')")"
    env_set AWOKI_RERANK_URL "$value"
    value="$(prompt_value "Reranker provider" "$(env_get AWOKI_RERANK_PROVIDER tei)")"
    env_set AWOKI_RERANK_PROVIDER "$value"
    local old_rkey new_rkey
    old_rkey="$(env_get AWOKI_RERANK_API_KEY '')"
    new_rkey="$(prompt_secret "Reranker API key" "$([[ -n "$old_rkey" ]] && echo 1 || echo 0)")"
    if [[ "$new_rkey" == "-" ]]; then old_rkey=""; elif [[ -n "$new_rkey" ]]; then old_rkey="$new_rkey"; fi
    env_set AWOKI_RERANK_API_KEY "$old_rkey"
  else
    env_set AWOKI_RERANK_ENABLED 0
  fi

  if prompt_yes_no "Review/edit .env manually before continuing?" no; then
    open_editor "$ENV_FILE"
  fi
}

run_static_preflight() {
  echo
  echo "== Static dependency/development preflight =="
  validate_opencode_user_config
  make dependencies-check
  make dev-preflight
  python3 .harness/validate.py
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose -f "$COMPOSE_FILE" config >/dev/null
    echo "[awoki] Docker Compose configuration parses successfully."
  fi
}

show_prebuild_review() {
  local baseline="$1"
  echo
  echo "== Pre-build configuration review =="
  show_env_changes "$baseline"
  if [[ -f "$OPENCODE_USER_CONFIG" ]]; then
    echo "  OpenCode user/provider config: ${OPENCODE_USER_CONFIG#$ROOT/}"
  else
    echo "  OpenCode user/provider config: <not created>"
  fi
  if git diff --quiet -- opencode.jsonc && git diff --cached --quiet -- opencode.jsonc; then
    echo "  Awoki project opencode.jsonc: unchanged from HEAD"
  else
    echo "  Awoki project opencode.jsonc: MODIFIED (advanced/source-level change)"
  fi
  echo "  Docker build/start: not started by this installer yet"
  echo "  Secrets are never printed in this review."
}

prebuild_review_gate() {
  local baseline="$1" choice
  while true; do
    show_prebuild_review "$baseline"
    echo
    echo "Nothing below builds Docker until you explicitly choose option 5."
    echo "  1) Edit OpenCode user/provider config (safe place to paste custom provider/model config)"
    echo "  2) Edit .env, then re-run static validation"
    echo "  3) Edit Awoki project opencode.jsonc (advanced/source-level config)"
    echo "  4) Re-run static validation without editing"
    echo "  5) BUILD/START Docker now"
    echo "  6) Stop here with configuration saved"
    printf 'Choose [1]: '
    IFS= read -r choice || choice=""
    choice="${choice:-1}"
    case "$choice" in
      1) ensure_opencode_user_config; open_editor "$OPENCODE_USER_CONFIG" || true; run_static_preflight ;;
      2) open_editor "$ENV_FILE"; run_static_preflight ;;
      3)
        echo "[awoki] advanced: this tracked file is baked into the Awoki image."
        echo "[awoki] personal provider/model settings normally belong in ${OPENCODE_USER_CONFIG#$ROOT/}."
        open_editor "$ROOT/opencode.jsonc" || true
        run_static_preflight
        ;;
      4) run_static_preflight ;;
      5)
        if prompt_yes_no "FINAL CONFIRMATION: start Docker build/runtime now?" no; then
          return 0
        fi
        ;;
      6)
        echo "[awoki] configuration saved; Docker build/start was not run."
        echo "[awoki] OpenCode provider/model config: $OPENCODE_USER_CONFIG"
        echo "[awoki] Resume later with: ./install-awoki.sh"
        return 1
        ;;
      *) echo "Choose 1, 2, 3, 4, 5, or 6." ;;
    esac
  done
}

read_runtime_instance_id() {
  python3 - "$LAYOUT_MARKER" <<'PY'
from pathlib import Path
import json, re, sys
value = str(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get("runtime_instance_id", ""))
if not re.fullmatch(r"[0-9a-f]{32}", value):
    raise SystemExit(2)
print(value)
PY
}

reconcile_interactive() {
  command -v docker >/dev/null 2>&1 || return 0
  docker compose version >/dev/null 2>&1 || return 0
  local runtime_id rc new_name
  runtime_id="$(read_runtime_instance_id)"
  while true; do
    set +e
    AWOKI_RUNTIME_INSTANCE_ID="$runtime_id" AWOKI_RUNTIME_CONFLICT_POLICY=ask \
      "$ROOT/.harness/bin/reconcile-opencode-runtime" "$COMPOSE_FILE"
    rc=$?
    set -e
    case "$rc" in
      0) return 0 ;;
      4)
        echo
        echo "Another checkout is using the same Docker Compose project name."
        if ! prompt_yes_no "Use a different Compose project name for this checkout?" yes; then
          return 4
        fi
        new_name="$(prompt_value "New Compose project name" "awoki-$(basename "$ROOT" | tr -cd 'A-Za-z0-9_.-' | tr '[:upper:]' '[:lower:]')")"
        [[ "$new_name" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || { echo "[awoki] invalid Compose project name" >&2; continue; }
        env_set AWOKI_COMPOSE_PROJECT_NAME "$new_name"
        export AWOKI_COMPOSE_PROJECT_NAME="$new_name"
        echo "[awoki] updated .env: AWOKI_COMPOSE_PROJECT_NAME=$new_name"
        ;;
      *) return "$rc" ;;
    esac
  done
}

port_is_bindable_value() {
  local port="$1"
  python3 - "$port" <<'PY_PORT_BIND' >/dev/null 2>&1
import socket
import sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", port))
finally:
    s.close()
PY_PORT_BIND
}

port_is_reserved() {
  local port="$1" reserved=" ${2:-} "
  case "$reserved" in *" $port "*) return 0 ;; *) return 1 ;; esac
}

suggest_free_port() {
  local start="$1" reserved="${2:-}" port
  port=$((start + 1))
  while (( port <= 65535 )); do
    if ! port_is_reserved "$port" "$reserved" && port_is_bindable_value "$port"; then
      printf '%s' "$port"
      return 0
    fi
    port=$((port + 1))
  done
  echo "[awoki] could not find a free loopback port after $start" >&2
  return 3
}

prompt_free_port() {
  local label="$1" current="$2" reserved="$3" suggestion answer
  suggestion="$(suggest_free_port "$current" "$reserved")"
  while true; do
    answer="$(prompt_value "$label" "$suggestion")"
    if [[ ! "$answer" =~ ^[0-9]+$ ]] || (( answer < 1 || answer > 65535 )); then
      echo "[awoki] invalid port: $answer" >&2
      continue
    fi
    if port_is_reserved "$answer" "$reserved"; then
      echo "[awoki] port $answer is already selected for another Awoki listener." >&2
      continue
    fi
    if ! port_is_bindable_value "$answer"; then
      echo "[awoki] port 127.0.0.1:$answer is currently occupied; choose another port." >&2
      continue
    fi
    printf '%s' "$answer"
    return 0
  done
}

paths_equivalent() {
  python3 - "$1" "$2" <<'PY_PATH_EQ' >/dev/null 2>&1
import os
import sys

def variants(value: str):
    out = {os.path.realpath(value)}
    if value.startswith("/host_mnt/"):
        out.add(os.path.realpath(value[len("/host_mnt"):]))
    return out

raise SystemExit(0 if variants(sys.argv[1]) & variants(sys.argv[2]) else 1)
PY_PATH_EQ
}

stop_other_awoki_checkout() {
  local other_root="$1" other_project="$2" service cid actual_root actual_project actual_name
  local ids="" lines=""
  for service in awoki-opencode-ssh qdrant; do
    for cid in $(docker ps -q --filter "label=com.docker.compose.service=$service" 2>/dev/null || true); do
      actual_root="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$cid" 2>/dev/null || true)"
      actual_project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$cid" 2>/dev/null || true)"
      actual_name="$(docker inspect -f '{{ .Name }}' "$cid" 2>/dev/null | sed 's#^/##' || true)"
      case "$actual_root" in '<no value>'|'<nil>'|'') continue ;; esac
      case "$actual_project" in '<no value>'|'<nil>') actual_project="" ;; esac
      paths_equivalent "$actual_root" "$other_root" || continue
      if [[ -n "$other_project" && -n "$actual_project" && "$actual_project" != "$other_project" ]]; then
        continue
      fi
      ids="$ids $cid"
      lines="$lines\n    ${actual_name:-$cid}  service=$service  project=${actual_project:-unknown}"
    done
  done

  if [[ -z "${ids// /}" ]]; then
    echo "[awoki] no running Awoki service containers still match checkout $other_root" >&2
    return 0
  fi

  echo
  echo "The following running containers belong to the other Awoki checkout:"
  printf '%b\n' "$lines"
  echo
  echo "Awoki will STOP these exact containers only. It will not delete containers,"
  echo "named volumes, networks, or host data. The other checkout can be started again later."
  if ! prompt_yes_no "Stop this other Awoki runtime now and continue?" no; then
    echo "[awoki] other runtime left untouched."
    return 10
  fi

  for cid in $ids; do
    echo "[awoki] stopping other-checkout container $cid (no deletion)."
    docker stop "$cid" >/dev/null || {
      echo "[awoki] failed to stop $cid; no container/volume deletion was attempted." >&2
      return 3
    }
  done
  echo "[awoki] other Awoki runtime stopped; its containers and persistent data remain available."
}

configure_parallel_runtime() {
  local other_project="$1" current_project suggested_project value reserved=""
  local ssh_port web_port qhttp_port qgrpc_port lavish_port web_enabled

  current_project="$(env_get AWOKI_COMPOSE_PROJECT_NAME awoki)"
  suggested_project="$current_project"
  if [[ -z "$suggested_project" || "$suggested_project" == "$other_project" ]]; then
    suggested_project="${current_project:-awoki}-2"
  fi
  echo
  echo "== Parallel Awoki runtime settings =="
  echo "The other checkout will stay running. This checkout therefore needs a distinct"
  echo "Compose identity and free loopback ports for every published service."

  while true; do
    value="$(prompt_value "Compose project name for this checkout" "$suggested_project")"
    if [[ ! "$value" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
      echo "[awoki] invalid Compose project name" >&2
      continue
    fi
    if [[ -n "$other_project" && "$value" == "$other_project" ]]; then
      echo "[awoki] choose a project name different from the other checkout ($other_project)." >&2
      continue
    fi
    env_set AWOKI_COMPOSE_PROJECT_NAME "$value"
    export AWOKI_COMPOSE_PROJECT_NAME="$value"
    break
  done

  ssh_port="$(prompt_free_port "SSH port for this checkout" "$(env_get AWOKI_OPENCODE_SSH_PORT 2222)" "$reserved")"
  reserved="$reserved $ssh_port"; env_set AWOKI_OPENCODE_SSH_PORT "$ssh_port"

  web_enabled="$(env_get AWOKI_OPENCODE_WEB_ENABLED 1)"
  case "$web_enabled" in
    1|true|TRUE|yes|YES|on|ON)
      web_port="$(prompt_free_port "OpenCode Web port for this checkout" "$(env_get AWOKI_OPENCODE_WEB_PORT 4096)" "$reserved")"
      reserved="$reserved $web_port"; env_set AWOKI_OPENCODE_WEB_PORT "$web_port"
      ;;
  esac

  qhttp_port="$(prompt_free_port "Qdrant HTTP port for this checkout" "$(env_get AWOKI_QDRANT_HTTP_PORT 6333)" "$reserved")"
  reserved="$reserved $qhttp_port"; env_set AWOKI_QDRANT_HTTP_PORT "$qhttp_port"
  qgrpc_port="$(prompt_free_port "Qdrant gRPC port for this checkout" "$(env_get AWOKI_QDRANT_GRPC_PORT 6334)" "$reserved")"
  reserved="$reserved $qgrpc_port"; env_set AWOKI_QDRANT_GRPC_PORT "$qgrpc_port"
  lavish_port="$(prompt_free_port "Lavish port for this checkout" "$(env_get AWOKI_LAVISH_PORT 4387)" "$reserved")"
  reserved="$reserved $lavish_port"; env_set AWOKI_LAVISH_PORT "$lavish_port"

  run_static_preflight
  show_prebuild_review "$ENV_BASELINE"
  echo "  Parallel runtime ports selected: SSH=$ssh_port Qdrant=$qhttp_port/$qgrpc_port Lavish=$lavish_port"
  [[ -n "${web_port:-}" ]] && echo "  OpenCode Web=$web_port"
  if ! prompt_yes_no "Keep these parallel-runtime settings and continue toward Docker build/start?" yes; then
    echo "[awoki] configuration saved; Docker build/start was not run."
    return 10
  fi
}

check_port_owner_for_installer() {
  local runtime_id="$1" project="$2" label="$3" port="$4" service="$5" report="$6" rc
  : >"$report"
  if AWOKI_RUNTIME_INSTANCE_ID="$runtime_id" \
    AWOKI_COMPOSE_PROJECT_NAME="$project" \
    AWOKI_RUNTIME_CONFLICT_POLICY=ask \
    AWOKI_PORT_OWNER_REPORT_FILE="$report" \
      "$ROOT/.harness/bin/reconcile-opencode-port-owner" "$COMPOSE_FILE" "$port" "$label" "$service" >/dev/null; then
    return 0
  else
    rc=$?
    return "$rc"
  fi
}

resolve_external_port_conflicts_interactive() {
  local runtime_id project report rc record cid name other_project service other_root other_runtime conflict_port action
  local ssh_port web_port qhttp_port qgrpc_port lavish_port web_enabled
  runtime_id="$(read_runtime_instance_id)"

  while true; do
    project="$(env_get AWOKI_COMPOSE_PROJECT_NAME awoki)"
    export AWOKI_COMPOSE_PROJECT_NAME="$project"
    report="$(mktemp "${TMPDIR:-/tmp}/awoki-port-conflict.XXXXXX")"
    chmod 600 "$report" 2>/dev/null || true
    action=""

    ssh_port="$(env_get AWOKI_OPENCODE_SSH_PORT 2222)"
    set +e
    check_port_owner_for_installer "$runtime_id" "$project" SSH "$ssh_port" awoki-opencode-ssh "$report"
    rc=$?
    set -e
    if (( rc != 0 )); then
      if (( rc != 5 )); then rm -f "$report"; return "$rc"; fi
      action="conflict"
    fi

    if [[ -z "$action" ]]; then
      web_enabled="$(env_get AWOKI_OPENCODE_WEB_ENABLED 1)"
      case "$web_enabled" in
        1|true|TRUE|yes|YES|on|ON)
          web_port="$(env_get AWOKI_OPENCODE_WEB_PORT 4096)"
          set +e
          check_port_owner_for_installer "$runtime_id" "$project" "OpenCode Web" "$web_port" awoki-opencode-ssh "$report"
          rc=$?
          set -e
          if (( rc != 0 )); then
            if (( rc != 5 )); then rm -f "$report"; return "$rc"; fi
            action="conflict"
          fi
          ;;
      esac
    fi

    if [[ -z "$action" ]]; then
      lavish_port="$(env_get AWOKI_LAVISH_PORT 4387)"
      set +e
      check_port_owner_for_installer "$runtime_id" "$project" Lavish "$lavish_port" awoki-opencode-ssh "$report"
      rc=$?
      set -e
      if (( rc != 0 )); then
        if (( rc != 5 )); then rm -f "$report"; return "$rc"; fi
        action="conflict"
      fi
    fi

    if [[ -z "$action" ]]; then
      qhttp_port="$(env_get AWOKI_QDRANT_HTTP_PORT 6333)"
      set +e
      check_port_owner_for_installer "$runtime_id" "$project" "Qdrant HTTP" "$qhttp_port" qdrant "$report"
      rc=$?
      set -e
      if (( rc != 0 )); then
        if (( rc != 5 )); then rm -f "$report"; return "$rc"; fi
        action="conflict"
      fi
    fi

    if [[ -z "$action" ]]; then
      qgrpc_port="$(env_get AWOKI_QDRANT_GRPC_PORT 6334)"
      set +e
      check_port_owner_for_installer "$runtime_id" "$project" "Qdrant gRPC" "$qgrpc_port" qdrant "$report"
      rc=$?
      set -e
      if (( rc != 0 )); then
        if (( rc != 5 )); then rm -f "$report"; return "$rc"; fi
        action="conflict"
      fi
    fi

    if [[ -z "$action" ]]; then
      rm -f "$report"
      return 0
    fi

    record="$(head -n 1 "$report" 2>/dev/null || true)"
    rm -f "$report"
    if [[ -z "$record" ]]; then
      echo "[awoki] another-checkout conflict was reported without inspectable owner metadata; refusing to guess." >&2
      return 3
    fi
    IFS=$'\t' read -r cid name other_project service other_root other_runtime conflict_port <<EOF_CONFLICT
$record
EOF_CONFLICT

    echo
    echo "== Another Awoki checkout is using the requested runtime ports =="
    echo "Other checkout: ${other_root:-unknown}"
    echo "Other Compose project: ${other_project:-unknown}"
    echo "Blocking container: ${name:-$cid}"
    echo "Current checkout: $ROOT"
    echo
    echo "The low-level runtime intentionally refuses to stop a different checkout automatically."
    echo "Choose what the interactive installer should do:"
    echo "  1) Stop that other Awoki runtime (containers are preserved) and continue"
    echo "  2) Keep it running; choose different ports/project for this checkout"
    echo "  3) Abort with both installations untouched"
    printf 'Choose [3]: '
    IFS= read -r action || action=""
    action="${action:-3}"
    case "$action" in
      1)
        stop_other_awoki_checkout "$other_root" "$other_project" || {
          rc=$?; [[ "$rc" == "10" ]] && return 10; return "$rc"
        }
        ;;
      2)
        configure_parallel_runtime "$other_project" || {
          rc=$?; [[ "$rc" == "10" ]] && return 10; return "$rc"
        }
        ;;
      3)
        echo "[awoki] install stopped; no other-checkout container was modified."
        return 10
        ;;
      *) echo "Choose 1, 2, or 3." ;;
    esac
  done
}

run_opencode_post_setup() {
  local any_change=0
  if prompt_yes_no "Run OpenCode provider credential login wizard now?" no; then
    docker compose -f "$COMPOSE_FILE" exec -u op -e HOME=/home/op -e OPENCODE_CONFIG_DIR=/awoki/.opencode awoki-opencode-ssh opencode auth login
    any_change=1
  fi
  if prompt_yes_no "Run OpenCode MCP add wizard now?" no; then
    docker compose -f "$COMPOSE_FILE" exec -u op -e HOME=/home/op -e OPENCODE_CONFIG_DIR=/awoki/.opencode awoki-opencode-ssh opencode mcp add
    any_change=1
  fi
  if (( any_change )); then
    echo "[awoki] OpenCode state/config changed. Restarting only the OpenCode SSH service so the Web backend reloads it."
    docker compose -f "$COMPOSE_FILE" restart awoki-opencode-ssh
    AWOKI_RUNTIME_CONFLICT_POLICY=ask make opencode-ssh-up
  fi
}

if [[ -f "$ENV_FILE" ]]; then
  env_snapshot "$ENV_BASELINE" "$ENV_FILE"
else
  # A fresh install starts from .env.example. Treat those template defaults as
  # the baseline so the review shows only operator choices that differ.
  env_snapshot "$ENV_BASELINE" "$ENV_EXAMPLE"
fi

echo "Awoki interactive installer"
echo "Checkout: $ROOT"
echo

if (( INTERACTIVE )); then
  configure_env_interactive
else
  if [[ ! -f "$ENV_FILE" ]]; then cp "$ENV_EXAMPLE" "$ENV_FILE"; fi
fi

echo
echo "== Initialize local Awoki state =="
"$ROOT/init-awoki.sh"

if (( INTERACTIVE )); then
  configure_opencode_user_interactive
fi

run_static_preflight

if (( CONFIGURE_ONLY )); then
  if (( INTERACTIVE )); then show_prebuild_review "$ENV_BASELINE"; fi
  echo
  echo "[awoki] configuration-only install complete; Docker build/start was not run."
  echo "[awoki] Resume with: ./install-awoki.sh"
  exit 0
fi

if (( INTERACTIVE )); then
  if ! prebuild_review_gate "$ENV_BASELINE"; then
    exit 0
  fi

  echo
  echo "== Docker runtime conflict check =="
  before_conflict_compose="$(env_get AWOKI_COMPOSE_PROJECT_NAME awoki)"
  reconcile_interactive
  set +e
  resolve_external_port_conflicts_interactive
  conflict_rc=$?
  set -e
  if (( conflict_rc != 0 )); then
    if (( conflict_rc == 10 )); then
      echo "[awoki] configuration saved; Docker build/start was not run by this checkout."
      exit 0
    fi
    exit "$conflict_rc"
  fi
  after_conflict_compose="$(env_get AWOKI_COMPOSE_PROJECT_NAME awoki)"
  if [[ "$after_conflict_compose" != "$before_conflict_compose" ]]; then
    show_prebuild_review "$ENV_BASELINE"
    if ! prompt_yes_no "The conflict resolution changed .env. Proceed with Docker build/start using this configuration?" yes; then
      echo "[awoki] configuration saved; Docker build/start was not run."
      echo "[awoki] Resume later with: ./install-awoki.sh"
      exit 0
    fi
  fi
fi

# When launched from the interactive wizard, keep conflict handling interactive
# all the way through run-opencode-ssh as a second race-safe check.
if (( INTERACTIVE )); then
  AWOKI_RUNTIME_CONFLICT_POLICY=ask make install-opencode-ssh
else
  make install-opencode-ssh
fi

if (( INTERACTIVE )); then
  echo
  echo "== Optional OpenCode setup =="
  run_opencode_post_setup
fi

if (( ! SKIP_RUNTIME_CHECK )); then
  if (( INTERACTIVE )); then
    if prompt_yes_no "Run Awoki runtime verification now?" yes; then
      make opencode-runtime-check
    fi
  else
    make opencode-runtime-check
  fi
fi

echo
echo "== Final verification =="
make -s opencode-ssh-client-check >/dev/null
echo "[ok] SSH key, container authorization, and public-key login verified."

ssh_port="$(env_get AWOKI_OPENCODE_SSH_PORT 2222)"
web_port="$(env_get AWOKI_OPENCODE_WEB_PORT 4096)"
echo
echo "== Awoki ready =="
printf '%s\n' \
  "OpenCode config: ${OPENCODE_USER_CONFIG#$ROOT/}" \
  "  Reload after edits: make opencode-config-reload" \
  "  Provider login:     make opencode-auth" \
  "" \
  "Web: http://127.0.0.1:${web_port}" \
  "  Password: make opencode-web-password" \
  "" \
  "SSH:" \
  "  ssh -i \"$ROOT/.ssh-container/id_ed25519\" \\" \
  "    -o IdentitiesOnly=yes \\" \
  "    -o UserKnownHostsFile=\"$ROOT/.ssh-container/known_hosts\" \\" \
  "    -o StrictHostKeyChecking=accept-new \\" \
  "    -p $ssh_port op@127.0.0.1" \
  "" \
  "Inside SSH:" \
  "  cd /awoki && tmux new -A -s awoki" \
  "  awoki-opencode"
