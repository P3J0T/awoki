from __future__ import annotations

import ast
import json
import os
import re
import py_compile
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate_json(path: Path) -> None:
    json.loads(path.read_text(encoding="utf-8"))


def _strip_jsonc(text: str) -> str:
    out = []
    i = 0
    in_str = False
    esc = False
    while i < len(text):
        c = text[i]
        n = text[i + 1] if i + 1 < len(text) else ""
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == "\"":
                in_str = False
            i += 1
            continue
        if c == "\"":
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and n == "/":
            i += 2
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and n == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def validate_jsonc(path: Path) -> None:
    json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))


def validate_jsonl(path: Path) -> None:
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            json.loads(line)



def validate_compose_shape(path: Path, expected_services: list[str] | None = None) -> None:
    try:
        import yaml
    except Exception:
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "docker-compose.yml must parse to a mapping"
    services = data.get("services")
    assert isinstance(services, dict), "docker-compose.yml missing services"
    expected_services = expected_services or ["qdrant", "awoki-mcp"]
    for name in expected_services:
        assert name in services, f"{path.name} missing {name} service"
    networks = data.get("networks", {})
    assert "awoki-data" in networks and "awoki-egress" in networks, f"{path.name} missing separated data/egress networks"
    assert networks["awoki-data"].get("internal") is True, f"{path.name} Qdrant data network must be internal"
    assert services["qdrant"].get("networks") == ["awoki-data"], f"{path.name} Qdrant must join only the internal data network"
    qdrant_ports = services["qdrant"].get("ports", [])
    assert qdrant_ports and all(str(port).startswith("127.0.0.1:") for port in qdrant_ports), (
        f"{path.name} must publish Qdrant only on host loopback"
    )
    service_name = "awoki-mcp" if "awoki-mcp" in services else "awoki-opencode-ssh" if "awoki-opencode-ssh" in services else ""
    if service_name:
        env = services[service_name].get("environment", {})
        assert env.get("AWOKI_PROJECT_ID") != "awoki", "AWOKI_PROJECT_ID must not default every project to awoki"
        assert env.get("AWOKI_QDRANT_URL") == "http://qdrant:6333", "Docker service must use internal qdrant hostname"
        for key in (
            "AWOKI_CODE_QDRANT_COLLECTION",
            "AWOKI_CODE_PEEK_MAX_CHARS",
            "AWOKI_CODE_CONTEXT_MAX_CHARS",
            "AWOKI_CODE_FULL_MAX_CHARS",
            "AWOKI_CODE_RRF_K",
        ):
            assert key in env, f"{path.name} must pass through {key}"
    if "awoki-mcp" in services:
        env = services["awoki-mcp"].get("environment", {})
        volumes = services["awoki-mcp"].get("volumes", [])
        assert not any(str(v).startswith("./:/") or str(v).startswith("./:/awoki") for v in volumes), "Awoki source must be baked into the image, not repo-root bind-mounted"
        assert any("./workspace:/awoki/workspace:rw" in str(v) for v in volumes), "runtime workspace mount missing"
        assert not any("/models" in str(v) or "data/models" in str(v) for v in volumes), "local model mounts must be absent"
        assert not any("CREDENTIAL" in str(k) for k in env), "built-in credential environment must be absent"
        assert set(services["awoki-mcp"].get("networks", [])) == {"awoki-data", "awoki-egress"}, "Awoki MCP must separate Qdrant data from remote/host egress"
    if "awoki-opencode-ssh" in services:
        svc = services["awoki-opencode-ssh"]
        env = svc.get("environment", {})
        assert env.get("AWOKI_QDRANT_URL") == "http://qdrant:6333", "OpenCode container must use internal qdrant hostname"
        assert env.get("AWOKI_QDRANT_CONTAINER_URL") == "http://qdrant:6333", "OpenCode container must expose the canonical internal qdrant URL"
        assert env.get("AWOKI_BURP_URL") == "${AWOKI_BURP_CONTAINER_URL:-http://host.docker.internal:9876}", "OpenCode container must use container Burp URL alias"
        assert env.get("AWOKI_BURP_MCP_URL") == "${AWOKI_BURP_CONTAINER_URL:-http://host.docker.internal:9876}", "Compose should still expose container Burp URL alias for scripts"
        volumes = svc.get("volumes", [])
        assert not any(str(v).startswith("./:/awoki") or str(v).startswith("./:/") for v in volumes), "OpenCode container must not bind-mount the repository root"
        assert any("./workspace:/awoki/workspace:rw" in str(v) for v in volumes), "OpenCode runtime workspace mount missing"
        for required in ("/.harness/state", "/.harness/index", "/.harness/artifacts", "/.harness/memory"):
            assert any(required in str(v) for v in volumes), f"OpenCode runtime mount missing {required}"
        assert not any("/models" in str(v) or "data/models" in str(v) for v in volumes), "OpenCode container must not mount local models"
        assert not any("CREDENTIAL" in str(k) for k in env), "built-in credential environment must be absent"
        assert set(svc.get("networks", [])) == {"awoki-data", "awoki-egress"}, "OpenCode must separate Qdrant data from remote/host egress"
        assert any("awoki_ssh_host_keys:/etc/awoki-ssh-host-keys" in str(v) for v in volumes), "SSH server host keys must persist separately"
        assert env.get("AWOKI_SSH_AUTHORIZED_KEY") == "${AWOKI_SSH_AUTHORIZED_KEY:-}", (
            "OpenCode SSH must receive only the launcher-derived public key through environment"
        )
        assert env.get("AWOKI_OPENCODE_WEB_ENABLED") == "${AWOKI_OPENCODE_WEB_ENABLED:-1}", "OpenCode Web must be enabled by default"
        assert env.get("AWOKI_OPENCODE_WEB_PORT") == "${AWOKI_OPENCODE_WEB_PORT:-4096}", "OpenCode Web port must be configurable"
        assert env.get("AWOKI_OPENCODE_WEB_USERNAME") == "${AWOKI_OPENCODE_WEB_USERNAME:-opencode}", "OpenCode Web username must be configurable"
        assert "AWOKI_OPENCODE_WEB_PASSWORD" not in env and "OPENCODE_SERVER_PASSWORD" not in env, "OpenCode Web password must not be stored in Compose service environment"
        assert not any(
            isinstance(v, dict) and v.get("source") == "./.ssh-container/authorized_keys"
            for v in volumes
        ), "Docker Desktop-portable SSH bootstrap must not use a single-file authorized_keys bind"
        assert any(".opencode-state/share:/home/op/.local/share/opencode:rw" in str(v) for v in volumes), "OpenCode share state must persist"
        assert any(".opencode-state/local-state:/home/op/.local/state/opencode:rw" in str(v) for v in volumes), "OpenCode local state must persist"
        assert any(".opencode-state/cache:/home/op/.cache:rw" in str(v) for v in volumes), "OpenCode plugin/package cache must persist"
        assert any(".opencode-state/web-auth:/awoki-web-auth:ro" in str(v) for v in volumes), "OpenCode Web auth directory must be mounted read-only"
        assert not any(".ssh-container/authorized_keys" in str(v) for v in volumes), "authorized_keys host-file binds must be absent"
        ports = svc.get("ports", [])
        assert any(str(p).startswith("127.0.0.1:") and "${AWOKI_OPENCODE_SSH_PORT:-2222}:22" in str(p) for p in ports), "OpenCode SSH host port must be configurable and loopback-only"
        assert any(str(p).startswith("127.0.0.1:") and "${AWOKI_OPENCODE_WEB_PORT:-4096}" in str(p) for p in ports), "OpenCode Web must be published only on host loopback"
        assert any(str(p).startswith("127.0.0.1:") and "${AWOKI_LAVISH_PORT:-4387}" in str(p) for p in ports), "Lavish must be published only on host loopback"
        assert env.get("LAVISH_AXI_NO_OPEN") == "1", "Lavish must not try to open a browser inside the container"
        assert env.get("AWOKI_LAVISH_VERSION") == "${AWOKI_LAVISH_VERSION:-0.1.43}", "Lavish version must be pinned by default"


def validate_launcher() -> None:
    script = (ROOT / ".harness" / "bin" / "run-opencode-ssh").read_text(encoding="utf-8")
    assert "AWOKI_OPENCODE_SSH_PORT" in script, "SSH launcher must honor AWOKI_OPENCODE_SSH_PORT"
    assert "AWOKI_OPENCODE_WEB_ENABLED" in script and "AWOKI_OPENCODE_WEB_PORT" in script, "SSH launcher must configure OpenCode Web"
    assert 'prepare-opencode-web-auth"' in script, "SSH launcher must materialize the host-only OpenCode Web secret before Compose"
    assert 'opencode-web-health"' in script, "SSH launcher must health-check the authenticated OpenCode Web endpoint"
    assert 'check_published_port "SSH" "$SSH_PORT"' in script, "SSH launcher must preflight SSH port conflicts"
    assert 'check_published_port "OpenCode Web" "$WEB_PORT"' in script, "SSH launcher must preflight Web port conflicts"
    assert 'opencode-ssh-public-key")"' in script, "SSH launcher must derive and export the validated public key before Compose starts"
    assert 'export AWOKI_SSH_AUTHORIZED_KEY' in script, "SSH launcher must export only the public key to Compose"
    helper = (ROOT / ".harness" / "bin" / "prepare-opencode-ssh-keys").read_text(encoding="utf-8")
    public_key_helper = (ROOT / ".harness" / "bin" / "opencode-ssh-public-key").read_text(encoding="utf-8")
    assert "ssh-keygen -y -f" in helper, "SSH key bootstrap must keep the public key synchronized with the private key"
    assert "must be a regular file, not a symlink" in helper, "SSH key bootstrap must reject symlinked key inputs"
    assert "ssh-keygen -lf" in public_key_helper, "SSH public-key export must validate the derived key"
    assert "exactly one line" in public_key_helper, "SSH public-key export must reject multi-line injection"
    wait_qdrant = (ROOT / ".harness" / "bin" / "wait-qdrant").read_text(encoding="utf-8")
    assert 'AWOKI_QDRANT_WAIT_COMPOSE_FILE="$COMPOSE_FILE"' in script, "SSH launcher must pass the active Compose file to Qdrant readiness"
    assert "AWOKI_QDRANT_WAIT_SERVICE=awoki-opencode-ssh" in script, "SSH launcher must probe Qdrant from the OpenCode Docker network"
    assert '"$ROOT/.harness/bin/wait-qdrant"' in script, "SSH launcher must gate startup through wait-qdrant"
    assert 'PROBE_SERVICE="${AWOKI_QDRANT_WAIT_SERVICE:-}"' in wait_qdrant, "Qdrant readiness helper must support an explicit Docker-network probe service"
    assert 'docker compose -f "$COMPOSE_FILE" run' in wait_qdrant, "Qdrant readiness helper must probe through Compose networking"
    assert "    -T" in wait_qdrant, "Qdrant readiness Compose probe must disable TTY allocation when Python is supplied over stdin"
    assert "http://qdrant:6333" in wait_qdrant, "Qdrant readiness helper must use the internal Docker service endpoint"
    assert "AWOKI_QDRANT_HOST_URL" in wait_qdrant, "Qdrant readiness helper must retain host-mode fallback"
    assert 'wait-qdrant" >/dev/null || true' not in script, "SSH launcher must not ignore Qdrant readiness failures"


def validate_layout_files() -> None:
    # Runtime workspaces are intentionally absent from Git. init-layout creates
    # them locally after clone; validation only requires the tool-owned sources
    # that define and document that layout.
    required = [
        ROOT / "init-awoki.sh",
        ROOT / ".harness" / "bin" / "init-layout",
        ROOT / ".harness" / "bin" / "prepare-opencode-ssh-keys",
        ROOT / ".harness" / "bin" / "opencode-ssh-public-key",
        ROOT / ".harness" / "bin" / "prepare-opencode-web-auth",
        ROOT / ".harness" / "bin" / "opencode-web-password",
        ROOT / ".harness" / "bin" / "opencode-web-health",
        ROOT / ".harness" / "bin" / "awoki-opencode",
        ROOT / ".harness" / "index" / "README.md",
        ROOT / ".harness" / "state" / "README.md",
        ROOT / ".harness" / "artifacts" / "burp" / "README.md",
        ROOT / ".harness" / "artifacts" / "code" / "README.md",
        ROOT / ".harness" / "artifacts" / "docs" / "README.md",
        ROOT / ".harness" / "artifacts" / "evidence" / "README.md",
        ROOT / ".harness" / "artifacts" / "reports" / "README.md",
        ROOT / "docs" / "FILESYSTEM.md",
    ]
    missing = [str(p.relative_to(ROOT)) for p in required if not p.exists()]
    assert not missing, f"missing filesystem scaffold files: {missing}"


def validate_repository_privacy_contract() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    dockerignore_path = ROOT / ".dockerignore"
    ignore_lines = gitignore.splitlines()
    assert "/workspace/" in ignore_lines, "the entire runtime workspace must be ignored by Git"
    assert "/.harness/" not in ignore_lines, "Awoki source under .harness must remain trackable"
    assert "/.harness/memory/" in ignore_lines, "mutable legacy harness memory must not be tracked"
    assert "/.harness/notes.md" in ignore_lines, "mutable harness notes must not be tracked"
    assert dockerignore_path.exists(), "missing .dockerignore privacy boundary"
    dockerignore = dockerignore_path.read_text(encoding="utf-8")
    dockerignore_lines = dockerignore.splitlines()
    assert "workspace/" in dockerignore_lines, "Docker build context must exclude runtime workspace data"
    assert ".harness/memory/" in dockerignore_lines, "Docker build context must exclude mutable harness memory"
    assert ".harness/notes.md" in dockerignore_lines, "Docker build context must exclude mutable harness notes"
    assert ".env" in dockerignore_lines and ".env.*" in dockerignore_lines, "Docker build context must exclude dotenv runtime secrets"

    tracked = subprocess.run(
        ["git", "ls-files", "workspace"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    assert not tracked, f"runtime workspace files must not be tracked: {tracked}"

    tracked_runtime_memory = subprocess.run(
        ["git", "ls-files", ".harness/memory/**", ".harness/notes.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert not tracked_runtime_memory, (
        "mutable harness memory/notes must not be tracked: "
        f"{tracked_runtime_memory}"
    )

    private_examples = [
        "workspace/projects/demo/repo/src/main.py",
        "workspace/projects/demo/memory/continuity.jsonl",
        "workspace/projects/demo/artifacts/burp/raw/request.txt",
        "workspace/notes/private.md",
        "workspace/corpora/code/target/source.c",
        ".harness/memory/project.jsonl",
        ".harness/memory/skill_update_candidates.jsonl",
        ".harness/notes.md",
        "id_ed25519",
        "runtime.pid",
        "runtime.sock",
    ]
    for relative in private_examples:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", relative],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 0, f"private runtime path is not ignored: {relative}"

    init_layout = (ROOT / ".harness" / "bin" / "init-layout").read_text(encoding="utf-8")
    assert '"$ROOT/.harness/memory"' in init_layout, (
        "init-layout must create the Git-ignored runtime memory directory"
    )
    assert '"$ROOT/data/qdrant/collections"' in init_layout, (
        "init-layout must create the bind-mounted Qdrant collections parent"
    )
    assert 'prepare-opencode-ssh-keys"' in init_layout, (
        "init-layout must prepare the host SSH key pair before the runtime can start"
    )
    run_opencode_ssh = (ROOT / ".harness" / "bin" / "run-opencode-ssh").read_text(encoding="utf-8")
    assert '"$ROOT/data/qdrant/collections"' in run_opencode_ssh, (
        "OpenCode launcher must reject an incomplete Qdrant collections layout"
    )

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "git archive" in makefile, "release archives must be built from tracked Git content only"


def validate_continuity_contract() -> None:
    spec = ROOT / "docs" / "CONTINUITY.md"
    reliability_spec = ROOT / "docs" / "RELIABILITY.md"
    plugin = ROOT / ".opencode" / "plugins" / "awoki-continuity.ts"
    skill = ROOT / ".opencode" / "skills" / "project-continuity" / "SKILL.md"
    reliability_skill = ROOT / ".opencode" / "skills" / "reliability-verification" / "SKILL.md"
    repository_readiness_skill = ROOT / ".opencode" / "skills" / "repository-readiness" / "SKILL.md"
    for path in (
        spec, reliability_spec, plugin, skill, reliability_skill, repository_readiness_skill,
        ROOT / ".harness" / "continuity.py",
        ROOT / ".harness" / "continuations.py",
        ROOT / ".harness" / "safety.py",
        ROOT / ".harness" / "indexing_policy.py",
        ROOT / ".harness" / "opencode_events.py",
        ROOT / ".harness" / "work_ledger.py",
        ROOT / ".harness" / "acceptance_runs.py",
        ROOT / ".harness" / "evidence_store.py",
        ROOT / ".harness" / "reliability.py",
        ROOT / ".harness" / "awoki.py",
        ROOT / ".harness" / "bin" / "awoki",
        ROOT / ".harness" / "run_tests.py",
        ROOT / ".harness" / "validate_opencode_plugin.py",
    ):
        assert path.exists(), f"missing continuity component: {path.relative_to(ROOT)}"
    plugin_text = plugin.read_text(encoding="utf-8")
    for hook in ("tool.execute.before", "tool.execute.after", "experimental.session.compacting", "todo.updated", "message.updated", "message.part.updated", "session.compacted", "session.idle", "session.deleted"):
        assert hook in plugin_text, f"continuity plugin missing hook {hook}"
    assert "output.args.session_id" in plugin_text, "continuity plugin must inject OpenCode sessionID into Awoki MCP args"
    assert "--session-id" in plugin_text, "continuity plugin must forward session identity to the sanitized bridge"
    assert "output.context.push" in plugin_text, "continuity plugin must preserve generated context across compaction"
    assert "todo-sync" in plugin_text and "user-turn" in plugin_text and "agent-turn-terminal" in plugin_text, "continuity plugin must mirror TODO state and record structural terminal-turn metadata"
    assert "new Blob" in plugin_text, "TODO projection must be transported to the sanitized bridge over stdin, not argv"
    assert "experimental.chat.system.transform" not in plugin_text, "dynamic system-message injection breaks strict Qwen/llama.cpp chat templates"
    assert "output.system.push" not in plugin_text, "plugins must not append extra system messages"
    assert '"codebase_search"' in plugin_text, "codebase search must receive OpenCode session identity"
    assert '"code_text_search"' in plugin_text, "text fallback must receive OpenCode session identity"
    assert "continuityMaintenanceTools" in plugin_text, "continuity plugin must not checkpoint its own maintenance calls"
    assert "acceptance-tool" in plugin_text, "continuity plugin must record bounded structural tool provenance for acceptance runs"
    assert "compaction-trigger" in plugin_text and "part.auto" in plugin_text, "continuity plugin must persist structural auto-vs-explicit compaction trigger identity"
    assert 'clean = clean.replace(/^awoki[.:_-]/i, "")' in plugin_text, "continuity plugin must canonicalize Awoki MCP namespace prefixes"
    assert "acceptanceObservableOrchestrationTools" in plugin_text and "acceptanceControlTools" in plugin_text, "acceptance orchestration provenance must be distinct from execution and control/self-proof"
    for required in ("continuation-pending", "continuation-poll", "continuation-claim", "client.session.prompt", "session.idle", "lease_until"):
        assert required in plugin_text, f"continuity plugin missing durable continuation behavior: {required}"
    assert "setInterval" not in plugin_text, "continuity plugin must use bounded one-shot timers rather than interval polling"
    bridge_text = (ROOT / ".harness" / "opencode_events.py").read_text(encoding="utf-8")
    assert "Awoki execution invariants" in bridge_text, "compaction context must reinject MCP/execution invariants"
    assert "Awoki reliability invariants" in bridge_text, "compaction context must reinject reliability invariants"
    assert "work_ledger.compact_context" in bridge_text, "compaction context must include durable operational TODO state"
    assert "acceptance_runs.compact_context" in bridge_text, "compaction context must include active structured acceptance state"
    assert "acceptance-tool" in bridge_text, "sanitized bridge must accept bounded acceptance tool provenance"
    assert "compaction-trigger" in bridge_text, "sanitized bridge must accept structural compaction trigger metadata"
    manifest_text = (ROOT / ".harness" / "manifest.json").read_text(encoding="utf-8")
    for tool in ("session_work_status", "session_runtime_status", "harness_self_check", "reference_describe", "reference_annotate", "reference_resolve", "acceptance_run_start", "acceptance_run_status", "acceptance_run_next", "acceptance_evidence_get", "acceptance_run_record", "acceptance_run_record_invariant", "acceptance_run_finalize"):
        assert f'"{tool}"' in manifest_text, f"manifest missing continuity/acceptance tool {tool}"
    for tool in ("reliability_record_assessment", "reliability_verification_checkpoint"):
        assert f'"{tool}"' in manifest_text, f"manifest missing self-verification tool {tool}"
    for command in ("explore", "verify", "reliability-check", "ship-check"):
        command_path = ROOT / ".opencode" / "commands" / f"{command}.md"
        assert command_path.exists(), f"missing reliability command {command}"
    assert "reliability_start" in (ROOT / ".opencode" / "commands" / "reliability-check.md").read_text(encoding="utf-8"), "reliability command must use the deterministic ledger"
    reliability_command = (ROOT / ".opencode" / "commands" / "reliability-check.md").read_text(encoding="utf-8")
    ship_command = (ROOT / ".opencode" / "commands" / "ship-check.md").read_text(encoding="utf-8")
    reliability_skill_text = reliability_skill.read_text(encoding="utf-8")
    repository_readiness_text = repository_readiness_skill.read_text(encoding="utf-8")
    for required in (
        "repository_prepare_start", "repository_prepare_status", "repository_prepare_cancel",
        "code_index_refresh_start/status/cancel", "code_vector_refresh_start/status/cancel",
        "LOCAL_READY", "FULL_READY", "CONFIGURATION_BLOCKED", "MANAGED_SCOPE_REQUIRED",
        "parent job", "best effort", "not a correctness dependency",
        "project_continuation_schedule", "project_continuation_finalize", "todowrite",
        "Transient HTTP/transport retry", "Never publish incomplete vector membership",
    ):
        assert required in repository_readiness_text, f"repository-readiness skill missing safety/readiness contract: {required}"
    assert "do not modify `.env`" in repository_readiness_text and "Never print API keys" in repository_readiness_text, "repository-readiness skill must preserve runtime configuration/secrets boundary"
    for required in ("reliability_verify_code_claim", "reliability_verify_semantics_claim"):
        assert required in reliability_command, f"reliability command missing structured verifier route: {required}"
        assert required in ship_command, f"ship command missing structured verifier route: {required}"
    for required in ("reliability_record_assessment", "reliability_verification_checkpoint"):
        assert required in reliability_skill_text, f"reliability skill missing bounded assessment route: {required}"
        assert required in reliability_skill_text, f"reliability skill missing structured verifier route: {required}"
    assert 'reliability_start(mode="ship")' in ship_command, "ship command must activate the fail-closed ship claim gate"
    assert (ROOT / ".opencode" / "commands" / "lavish.md").exists(), "missing ad-hoc Lavish command"
    assert (ROOT / ".opencode" / "commands" / "codebase.md").exists(), "missing semantic codebase command"
    fallback = ROOT / ".harness" / "bin" / "code-search-fallback"
    assert fallback.exists(), "missing exhaustive raw code-search fallback"
    assert fallback.stat().st_mode & 0o100, "exhaustive raw code-search fallback must be executable"
    assert (ROOT / ".opencode" / "commands" / "backup.md").exists(), "missing backup command"
    expected_commands = {
        "backup", "burp", "burp-intruder", "burp-repeater", "burp-send",
        "burp-status", "burp-validate", "callees", "callers", "code-across",
        "code-index-status", "code-path", "code-validate-claim", "codebase",
        "definition", "demote-memory", "explore", "harness-boot", "lavish",
        "project", "project-status", "recall", "reliability-check",
        "retrieval-status", "review-promotions", "ship-check",
        "verify",
    }
    actual_commands = {path.stem for path in (ROOT / ".opencode" / "commands").glob("*.md")}
    assert actual_commands == expected_commands, f"slash command surface drifted: {sorted(actual_commands ^ expected_commands)}"
    for command_path in (ROOT / ".opencode" / "commands").glob("*.md"):
        command_text = command_path.read_text(encoding="utf-8")
        assert command_text.startswith("---\n") and "\ndescription:" in command_text.split("---", 2)[1], f"command missing frontmatter description: {command_path.name}"
    assert (ROOT / "docs" / "COMMANDS.md").exists(), "missing command surface documentation"
    agents_text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    codebase_text = (ROOT / ".opencode" / "commands" / "codebase.md").read_text(encoding="utf-8")
    for required in (
        "Deterministic repository-analysis default",
        "code_flow_graph",
        "code_source_window",
        "code_text_search",
        "code-search-fallback",
    ):
        assert required in agents_text, f"AGENTS.md missing repository-analysis invariant: {required}"
    for required in (
        "project_repo_add",
        "repository_index_advice",
        "verify your findings before answering",
        "strict_backends=true",
        "Awoki self-development boundary",
        "awoki-dev-preflight",
    ):
        assert required in agents_text, f"AGENTS.md missing multi-repo/reliability guidance: {required}"
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
    operator_reference_path = ROOT / "docs" / "OPERATOR_REFERENCE.md"
    identity_path = ROOT / "docs" / "AWOKI_IDENTITY.md"
    usefulness_path = ROOT / "docs" / "USEFULNESS_EVALUATION.md"
    for required_path in (operator_reference_path, identity_path, usefulness_path):
        assert required_path.exists(), f"missing public/stabilization documentation: {required_path.relative_to(ROOT)}"
    operator_reference_text = operator_reference_path.read_text(encoding="utf-8")
    architecture_text = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    continuity_text = (ROOT / "docs" / "CONTINUITY.md").read_text(encoding="utf-8")
    for required in (
        "Why Awoki exists",
        "What using it should feel like",
        "Natural security/code-review workflow",
        "Current development phase: prove usefulness, then simplify",
        "docs/AWOKI_IDENTITY.md",
        "docs/USEFULNESS_EVALUATION.md",
        "docs/OPERATOR_REFERENCE.md",
        "repository_prepare_start",
        "FULL_READY",
        "LOCAL_READY",
        "active working set",
        "code_exact_search",
        "slim orientation view",
        "How a normal investigation flows",
        "```mermaid",
    ):
        assert required in readme_text, f"README.md missing public onboarding guidance: {required}"
    for required in ("Component and data-flow view", "```mermaid", "Session work ledger"):
        assert required in architecture_text, f"ARCHITECTURE.md missing visual architecture guidance: {required}"
    for required in ("Long-session / compaction execution", "sequenceDiagram", "automatic_context_pressure"):
        assert required in continuity_text, f"CONTINUITY.md missing visual compaction guidance: {required}"
    for required in (
        "Multi-repository projects and semantic readiness",
        "project_repo_add",
        "repository_index_advice",
        "code_index_refresh_start",
        "code_index_refresh_status",
        "code_vector_refresh_start",
        "repository_prepare_start",
        "parent job",
        "bounded worker-local retry/backoff",
        "data/qdrant/collections/",
        "mode=lexical",
        "strict_backends=true",
        "published_vector_collection",
        "dev-preflight",
        "repository-readiness",
        "FULL_READY",
        "LOCAL_READY",
    ):
        assert required in operator_reference_text, f"OPERATOR_REFERENCE.md missing detailed multi-repo/index guidance: {required}"
    identity_text = identity_path.read_text(encoding="utf-8")
    for required in (
        "One-sentence identity",
        "Core mental model",
        "Current stabilization rule",
        "Current evaluation agenda",
        "R9.1.6.17",
        "current-session working set",
    ):
        assert required in identity_text, f"AWOKI_IDENTITY.md missing dense maintainer identity: {required}"
    usefulness_text = usefulness_path.read_text(encoding="utf-8")
    for required in (
        "Primary question",
        "Recommended first 12 journeys",
        "Reflection evaluation",
        "Feature usefulness audit",
        "Complexity budget for new mechanisms",
        "Observed journey evidence",
    ):
        assert required in usefulness_text, f"USEFULNESS_EVALUATION.md missing stabilization criteria: {required}"
    project_command_text = (ROOT / ".opencode" / "commands" / "project.md").read_text(encoding="utf-8")
    project_skill_text = (ROOT / ".opencode" / "skills" / "project-continuity" / "SKILL.md").read_text(encoding="utf-8")
    assert "repository-readiness" in project_command_text, "project command must route explicit repository priming to repository-readiness skill"
    assert "repository-readiness" in project_skill_text, "project-continuity must route explicit repository priming to repository-readiness skill"
    for path_name, text in (("project command", project_command_text), ("project-continuity skill", project_skill_text), ("AGENTS.md", agents_text)):
        assert ("autonomously poll" in text or "without model polling" in text), f"{path_name} must forbid model-driven rapid polling of readiness/vector jobs"
    machine_harness = (ROOT / ".harness" / "HARNESS.md").read_text(encoding="utf-8")
    for required in (
        "project_repo_add", "repository_index_advice", "repo/<repo-id>/",
        "strict_backends=true", "Awoki self-development boundary", "awoki-dev-preflight",
    ):
        assert required in machine_harness, f".harness/HARNESS.md missing runtime guidance: {required}"
    dev_preflight = ROOT / ".harness" / "bin" / "awoki-dev-preflight"
    assert dev_preflight.exists(), "missing Awoki self-development preflight"
    assert os.access(dev_preflight, os.X_OK), "Awoki self-development preflight must be executable"
    code_search_docs = (ROOT / "docs" / "CODE_SEARCH.md").read_text(encoding="utf-8")
    for required in (
        "Authority-aware conceptual retrieval",
        "mode=lexical",
        "strict_backends=true",
        "published_vector_collection",
        "Structural expansion is candidate generation only",
        "code_exact_search",
    ):
        assert required in code_search_docs, f"CODE_SEARCH.md missing R9 retrieval contract: {required}"
    filesystem_text = (ROOT / "docs" / "FILESYSTEM.md").read_text(encoding="utf-8")
    assert "registered multi-repository mode" in filesystem_text, (
        "filesystem repository-identity documentation must describe registered child roots"
    )
    for required in ("code_flow_graph", "code_source_window", "code_text_search", "SOURCE-CONFIRMED", "code-search-fallback"):
        assert required in codebase_text, f"/codebase orchestration missing: {required}"
    assert (ROOT / ".opencode" / "skills" / "lavish-review" / "SKILL.md").exists(), "missing Lavish skill"
    for cfg_name in ("opencode.jsonc", "opencode.container.jsonc"):
        cfg = json.loads(_strip_jsonc((ROOT / cfg_name).read_text(encoding="utf-8")))
        assert "docs/RELIABILITY.md" in cfg.get("instructions", []), f"{cfg_name} must always load reliability rules"
        assert cfg.get("permission", {}).get("skill", {}).get("*") == "allow", f"{cfg_name} must allow project skills"
    runtime_snapshot = (ROOT / ".harness" / "bin" / "awoki-runtime-snapshot").read_text(encoding="utf-8")
    assert "AWOKI_OPENCODE_WEB_ENABLED" in runtime_snapshot and "AWOKI_OPENCODE_WEB_PORT" in runtime_snapshot, "runtime snapshot must carry non-secret Web routing config"
    assert "AWOKI_OPENCODE_WEB_PASSWORD" not in runtime_snapshot and "OPENCODE_SERVER_PASSWORD" not in runtime_snapshot, "runtime snapshot must never persist the Web password"
    web_client = (ROOT / ".harness" / "bin" / "awoki-opencode").read_text(encoding="utf-8")
    assert "opencode attach" in web_client, "Awoki TUI wrapper must attach to the shared OpenCode Web backend"
    assert '--password' not in web_client and '-p ' not in web_client, "Awoki TUI wrapper must not put the Web password in process arguments"

    manifest = json.loads((ROOT / ".harness" / "manifest.json").read_text(encoding="utf-8"))
    preferred = manifest.get("projects", {}).get("preferred_tools", [])
    assert preferred == ["project_open", "project_capture", "project_search", "project_refresh", "code_index_refresh_status", "code_index_refresh_start", "code_vector_refresh_status", "code_vector_refresh_start", "project_pause", "project_status", "repository_prepare_start", "repository_prepare_status", "repository_prepare_cancel"], "preferred continuity surface drifted"
    assert manifest.get("memory_policy", {}).get("private_chain_of_thought") == "never_store"
    assert manifest.get("code_analysis", {}).get("default_policy") == "evidence_backed_deterministic_investigation", "manifest code-analysis policy drifted"



def validate_backup_contract() -> None:
    backup_module = ROOT / ".harness" / "backup.py"
    backup_wrapper = ROOT / ".harness" / "bin" / "awoki-backup"
    docs = ROOT / "docs" / "BACKUP_RESTORE.md"
    for path in (backup_module, backup_wrapper, docs):
        assert path.exists(), f"missing backup/restore component: {path.relative_to(ROOT)}"
    text = backup_module.read_text(encoding="utf-8")
    for required in (
        "portable", "full", "SHA-256", "AWOKI_QDRANT_COLLECTION",
        "AWOKI_CODE_QDRANT_COLLECTION", "AWOKI_EMBEDDING_DEPLOYMENT_ID", "AWOKI_EMBEDDING_NORMALIZE",
        "contains_explicit_secrets", "named_docker_volumes_included",
        "Qdrant is running", "unsafe archive path",
    ):
        assert required in text, f"backup implementation missing required safeguard: {required}"
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    for target in ("backup-portable:", "backup-full:", "backup-verify:", "backup-inspect:", "restore:"):
        assert target in makefile, f"Makefile missing runtime migration target {target}"
    assert "BACKUP_INCLUDE_SECRETS" in makefile, "secret inclusion must remain explicit"
    assert "RESTORE_REINDEX" in makefile, "restore reindex policy must be configurable"
    assert "RESTORE_ALLOW_LIVE" not in makefile, "restore must not expose a live-data override"
    assert "maintenance-check:" in makefile, "runtime Make targets must honour the backup/restore lock"
    for relative in (
        ".harness/bin/mcp-docker",
        ".harness/bin/mcp-local",
        ".harness/bin/run-opencode-ssh",
    ):
        launcher = (ROOT / relative).read_text(encoding="utf-8")
        assert "awoki-backup\" lock-check" in launcher, (
            f"{relative} must use the stale-lock-aware backup/restore guard"
        )
    manifest = json.loads((ROOT / ".harness" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("backup_restore", {}).get("archive_format") == "awoki-runtime-backup/v1", "manifest backup contract drifted"

    # Every host bind mount is an explicit migration decision. New writable
    # runtime mounts must not appear without being classified here and in the
    # backup documentation/implementation.
    bind_policy = {
        "workspace": "portable",
        ".harness/state": "portable",
        ".harness/index": "full_only",
        ".harness/artifacts": "portable",
        ".harness/memory": "portable",
        ".harness/notes.md": "portable",
        "data/qdrant": "full_only",
        ".awoki-global": "portable",
        ".opencode-state/share": "sensitive_opt_in",
        ".opencode-state/local-state": "sensitive_opt_in",
        ".opencode-state/config": "sensitive_opt_in",
        ".opencode-state/cache": "excluded_cache",
        ".opencode-state/npm": "excluded_cache",
        ".opencode-state/web-auth": "sensitive_opt_in",
    }
    discovered: set[str] = set()
    for compose_name in ("docker-compose.yml", "docker-compose.opencode.yml"):
        compose_text = (ROOT / compose_name).read_text(encoding="utf-8")
        for raw in compose_text.splitlines():
            match = re.match(r'^\s*-\s+["\']?(\.\.?\/[^:"\']+):', raw)
            if not match:
                # Long-form bind mounts use a dedicated source: field. Keep
                # backup/restore classification aware of those host paths too.
                match = re.match(r'^\s*source:\s*["\']?(\.\.?/[^\s"\']+)', raw)
            if match:
                source = match.group(1)
                discovered.add(source[2:] if source.startswith("./") else source)
    unclassified = sorted(discovered - set(bind_policy))
    assert not unclassified, f"Compose bind mounts lack backup/restore classification: {unclassified}"
    missing = sorted(set(bind_policy) - discovered)
    assert not missing, f"backup mount policy contains stale paths not mounted by Compose: {missing}"

def validate_manifest_tools() -> None:
    manifest = json.loads((ROOT / ".harness" / "manifest.json").read_text(encoding="utf-8"))
    advertised = set()
    for tools in manifest.get("tools", {}).values():
        if isinstance(tools, list):
            advertised.update(str(t) for t in tools)

    server_ast = ast.parse((ROOT / ".harness" / "server.py").read_text(encoding="utf-8"))
    server_funcs = {
        node.name
        for node in server_ast.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    exposed: set[str] = set()
    for node in server_ast.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                exposed.add(node.name)
                break

    missing_implementation = sorted(advertised - server_funcs)
    assert not missing_implementation, (
        "manifest advertises MCP tools not implemented in server.py: "
        f"{missing_implementation}"
    )
    missing_manifest = sorted(exposed - advertised)
    assert not missing_manifest, (
        "MCP tools are exposed by server.py but absent from the machine-readable manifest: "
        f"{missing_manifest}"
    )
    stale_manifest = sorted(advertised - exposed)
    assert not stale_manifest, (
        "manifest lists tools that are not exposed with @mcp.tool(): "
        f"{stale_manifest}"
    )
    assert "search_code" not in exposed, (
        "legacy bounded search_code must not be exposed as an MCP tool; use code_text_search"
    )

def main() -> None:
    validate_json(ROOT / ".harness" / "manifest.json")
    for p in (ROOT / ".harness" / "memory").glob("*.jsonl"):
        validate_jsonl(p)
    validate_compose_shape(ROOT / "docker-compose.yml", ["qdrant", "awoki-mcp"])
    validate_compose_shape(ROOT / "docker-compose.opencode.yml", ["qdrant", "awoki-opencode-ssh"])
    validate_layout_files()
    validate_repository_privacy_contract()
    validate_launcher()
    validate_jsonc(ROOT / "opencode.jsonc")
    validate_jsonc(ROOT / "opencode.container.jsonc")
    for cfg_name in ("opencode.jsonc", "opencode.container.jsonc"):
        cfg = json.loads(_strip_jsonc((ROOT / cfg_name).read_text(encoding="utf-8")))
        burp = cfg.get("mcp", {}).get("burp", {})
        assert burp.get("type") == "remote", f"{cfg_name} must expose direct remote Burp MCP"
        assert burp.get("url") == "http://host.docker.internal:9876", f"{cfg_name} Burp MCP URL must be concrete; OpenCode does not expand env placeholders here"
        assert burp.get("enabled") is True, f"{cfg_name} Burp MCP must be enabled"
        assert burp.get("oauth") is False, f"{cfg_name} Burp MCP must disable OAuth for local Burp MCP"
        assert burp.get("timeout") == 30000, f"{cfg_name} Burp MCP timeout must match local Burp MCP expectations"
        assert burp.get("headers", {}).get("Host") == "127.0.0.1:9876", f"{cfg_name} Burp MCP must set Host header"
        assert burp.get("headers", {}).get("Origin") == "http://127.0.0.1:9876", f"{cfg_name} Burp MCP must set Origin header"
        assert "{env:" not in json.dumps(burp), f"{cfg_name} Burp MCP config must not use env placeholders"
        awoki_cfg = cfg.get("mcp", {}).get("awoki", {})
        awoki_cmd = awoki_cfg.get("command")
        if cfg_name == "opencode.container.jsonc":
            assert isinstance(awoki_cmd, list) and awoki_cmd[:2] == ["/usr/bin/env", "-i"], "container Awoki MCP must clear the inherited OpenCode environment before launch"
            assert "/bin/bash" in awoki_cmd and "--noprofile" in awoki_cmd and "--norc" in awoki_cmd, "container Awoki MCP launcher must bypass shell startup files"
            assert awoki_cmd[-1] == "/awoki/.harness/bin/mcp-auto", "container Awoki MCP must end at mcp-auto"
            assert "environment" not in awoki_cfg, "container Awoki MCP must not duplicate runtime values in OpenCode config"
        else:
            assert awoki_cmd == [".harness/bin/mcp-auto"], f"{cfg_name} Awoki MCP must use mcp-auto"
    validate_manifest_tools()
    validate_continuity_contract()
    validate_backup_contract()
    dockerfile = (ROOT / "Dockerfile.opencode").read_text(encoding="utf-8")
    assert "sentence-transformers" not in dockerfile.lower() and "flagembedding" not in dockerfile.lower(), "OpenCode image must not install local retrieval models"
    assert "NOPASSWD" not in dockerfile, "OpenCode image must not grant passwordless sudo"
    assert "neovim" in dockerfile and "tmux" in dockerfile, "OpenCode image must include Neovim and tmux"
    assert "FROM node:22-bookworm-slim AS node_runtime" in dockerfile, "OpenCode/Lavish image must pin Node 22"
    assert "COPY --from=node_runtime /usr/local/bin/npm /usr/local/bin/npm" not in dockerfile, "npm symlink must not be flattened during multi-stage copy"
    assert "ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm" in dockerfile, "Dockerfile must recreate the canonical npm symlink"
    assert "ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx" in dockerfile, "Dockerfile must recreate the canonical npx symlink"
    assert "test -f /usr/local/lib/node_modules/npm/lib/cli.js" in dockerfile, "Dockerfile must verify the copied npm installation before use"
    assert "install -d -m 1777 /tmp" in dockerfile, "OpenCode image must provide a writable executable /tmp for OpenTUI"
    assert "chpasswd" not in dockerfile and "awoki-op-disabled" not in dockerfile, "OpenCode account must not contain a placeholder password"
    assert "passwd -d op" in dockerfile, "OpenCode account must be passwordless while SSH password authentication remains disabled"
    opencode_compose = (ROOT / "docker-compose.opencode.yml").read_text(encoding="utf-8")
    assert not re.search(r"(?m)^\s*-\s*/tmp(?:\s|:|$)", opencode_compose), "OpenCode /tmp must not be replaced by Docker's default noexec tmpfs"
    ssh_entrypoint = (ROOT / ".harness" / "bin" / "opencode-ssh-entrypoint").read_text(encoding="utf-8")
    assert "/tmp/.awoki-exec-probe" in ssh_entrypoint, "SSH entrypoint must verify executable /tmp before starting OpenCode sessions"
    assert "failed to map segment from shared object" in ssh_entrypoint, "OpenTUI /tmp failure must have an actionable diagnostic"
    runtime_snapshot = (ROOT / ".harness" / "bin" / "awoki-runtime-snapshot").read_text(encoding="utf-8")
    assert "/awoki/.harness/bin/awoki-runtime-snapshot" in ssh_entrypoint, "SSH entrypoint must refresh the shared runtime snapshot through the dedicated helper"
    assert "AWOKI_EMBEDDING_DEPLOYMENT_ID" in runtime_snapshot and "AWOKI_RERANK_ENABLED" in runtime_snapshot, "runtime environment handoff must include retrieval settings"
    assert "AWOKI_CODE_QDRANT_COLLECTION" in runtime_snapshot and "AWOKI_CODE_CONTEXT_MAX_CHARS" in runtime_snapshot, "runtime environment handoff must include structural code-search settings"
    assert "AWOKI_LAVISH_PORT" in runtime_snapshot and "LAVISH_AXI_STATE_DIR" in runtime_snapshot, "runtime environment handoff must include non-secret Lavish settings used by SSH-side tooling"
    assert "AWOKI_RERANK_API_KEY_ENV names" in runtime_snapshot and 'export AWOKI_RERANK_API_KEY="${!rerank_key_env}"' in runtime_snapshot, "SSH runtime must resolve or fail closed on reranker key indirection"
    assert "printf 'export %s=%q" in runtime_snapshot and "chmod 0640" in runtime_snapshot, "runtime environment snapshot must be shell-escaped and non-world-readable"
    assert "runuser -u op -- env -i HOME=/home/op /bin/bash" in runtime_snapshot, "runtime snapshot helper must validate readability with a clean op environment"
    assert "runuser -u op -- env -i" in ssh_entrypoint and "mcp-preflight --quiet" in ssh_entrypoint, "SSH startup MCP preflight must run from an explicit clean environment"
    assert "tmux-check" in ssh_entrypoint and "LANG=C.UTF-8 LC_ALL=C.UTF-8" in ssh_entrypoint, "SSH startup tmux/runtime preflights must use deterministic minimal environment"
    mcp_auto = (ROOT / ".harness" / "bin" / "mcp-auto").read_text(encoding="utf-8")
    assert "/run/awoki/runtime.env" in mcp_auto and 'source "$runtime_env_file"' in mcp_auto, "mcp-auto must restore the SSH runtime environment snapshot"
    assert "refusing symlink runtime environment file" in mcp_auto, "mcp-auto must reject a symlink runtime environment snapshot"
    assert "runtime environment snapshot trust validation failed" in mcp_auto and "file_group_digit" in mcp_auto, "mcp-auto must validate production snapshot ownership/writeability before sourcing"
    assert 'awoki-runtime-env" --profile mcp --' in mcp_auto, "container MCP must relaunch through the clean profile-filtered environment wrapper"
    runtime_env = (ROOT / ".harness" / "bin" / "awoki-runtime-env").read_text(encoding="utf-8")
    assert "env -i" in runtime_env and "AWOKI_RUNTIME_ENV_PROFILE" in runtime_env, "runtime diagnostic wrapper must isolate stale SSH state and expose the selected profile"
    for profile in ("base", "qdrant", "retrieval", "burp", "lavish", "mcp", "all"):
        assert profile in runtime_env, f"runtime diagnostic profile missing: {profile}"
    assert "refusing symlink runtime environment file" in runtime_env and "must be root-owned" in runtime_env, "runtime diagnostic wrapper must validate snapshot trust"
    assert "print_url" in runtime_env and "<configured>" in runtime_env, "runtime config printing must sanitize URLs and redact secret values"
    assert "ambient-environment-minimization-not-sandbox" in runtime_env and "runtime_user_can_read_snapshot" in runtime_env, "runtime diagnostics must not overclaim same-user credential isolation"
    rag_backend_text = (ROOT / ".harness" / "rag_backend.py").read_text(encoding="utf-8")
    assert "def _rerank_api_key_state" in rag_backend_text, "reranker auth indirection must be resolved centrally"
    assert "auth_configuration_error" in rag_backend_text and "auth_key_source" in rag_backend_text, "reranker profile must expose auth configuration state without exposing the key"
    assert "configured reranker credential environment variable" in rag_backend_text, "reranker key indirection must fail closed when its named variable is unavailable"
    embedding_benchmark = (ROOT / ".harness" / "bin" / "embedding-benchmark").read_text(encoding="utf-8")
    reranker_benchmark = (ROOT / ".harness" / "bin" / "reranker-benchmark").read_text(encoding="utf-8")
    assert "synthetic_text_only" in embedding_benchmark and "workspace/projects" not in embedding_benchmark, "embedding benchmark must use fixed synthetic content only"
    assert "synthetic_text_only" in reranker_benchmark and "workspace/projects" not in reranker_benchmark, "reranker benchmark must use fixed synthetic content only"
    assert "auth_configuration_error" in reranker_benchmark and "valid environment variable name" in reranker_benchmark, "reranker benchmark must fail closed on broken API-key indirection before network use"
    assert "http.client" in embedding_benchmark and "from openai import" not in embedding_benchmark, "embedding benchmark must remain self-contained and SDK-independent"
    assert "http.client" in reranker_benchmark and "import httpx" not in reranker_benchmark, "reranker benchmark must remain self-contained and SDK-independent"
    runtime_safety_text = (ROOT / ".harness" / "runtime_safety.py").read_text(encoding="utf-8")
    assert "def credential_free_environment" in runtime_safety_text and "SSH_AUTH_SOCK" in runtime_safety_text and "AWOKI_EMBEDDING_API_KEY" in runtime_safety_text, "repository subprocess environment must strip retrieval credentials and ambient execution helpers"
    provenance_text = (ROOT / ".harness" / "code_search" / "provenance.py").read_text(encoding="utf-8")
    project_workspace_text = (ROOT / ".harness" / "project_workspace.py").read_text(encoding="utf-8")
    text_search_text = (ROOT / ".harness" / "code_search" / "text_search.py").read_text(encoding="utf-8")
    assert "runtime_safety.credential_free_environment()" in provenance_text, "passive Git provenance reads must start from the credential-free subprocess environment"
    assert "runtime_safety.credential_free_environment()" in project_workspace_text, "project Git status/registration reads must start from the credential-free subprocess environment"
    assert "env=provenance.sanitized_git_environment()" in text_search_text, "exhaustive ripgrep scans must not inherit MCP retrieval credentials"
    code_index_jobs_text = (ROOT / ".harness" / "code_index_jobs.py").read_text(encoding="utf-8")
    assert "env=runtime_safety.credential_free_environment()" in code_index_jobs_text, "detached local structural indexing worker must not inherit retrieval credentials or ambient execution helpers"
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "runtime-config:" in makefile and "embedding-benchmark:" in makefile and "reranker-benchmark:" in makefile, "runtime diagnostic Make targets must remain exposed"
    phony_line = next(line for line in makefile.splitlines() if line.startswith(".PHONY:"))
    for target in ("runtime-config", "embedding-benchmark", "reranker-benchmark"):
        assert target in phony_line, f"runtime diagnostic target must be phony: {target}"
    assert "docker compose -f docker-compose.opencode.yml exec -T -u op awoki-opencode-ssh" in makefile, "runtime diagnostics must work from the host by entering the running SSH container"
    open_lavish = (ROOT / "open-lavish.sh").read_text(encoding="utf-8")
    assert "AWOKI_LAVISH_PORT" in open_lavish and "awk -F=" in open_lavish, "open-lavish must resolve custom Compose .env port without requiring ambient shell export"
    assert 'source "$ROOT/.env"' not in open_lavish and '. "$ROOT/.env"' not in open_lavish, "open-lavish must not execute arbitrary .env shell content"
    assert "StrictModes no" not in dockerfile and "StrictModes yes" in dockerfile, "SSH strict mode must remain enabled"
    assert not (ROOT / ".harness" / "credentials.py").exists(), "built-in credential module must be removed"
    server_text = (ROOT / ".harness" / "server.py").read_text(encoding="utf-8")
    for removed_tool in ("credential_add", "credential_update", "credential_resolve", "credential_render_env", "credential_audit", "save_secret_ref", "get_secret_ref"):
        assert removed_tool not in server_text, f"removed credential MCP tool still present: {removed_tool}"
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for forbidden_package in ("sentence-transformers", "flagembedding", "transformers", "torch"):
        assert forbidden_package not in requirements, f"local model dependency remains: {forbidden_package}"
    assert "mcp>=1.29,<2" in requirements, "Awoki FastMCP v1 code must reject the breaking MCP SDK 2.x line"
    pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"mcp>=1.29,<2"' in pyproject_text, "pyproject MCP constraint must match requirements.txt"
    parser_pins = (
        "tree-sitter-language-pack==0.10.0",
        "tree-sitter==0.25.2",
        "tree-sitter-c-sharp==0.23.1",
        "tree-sitter-embedded-template==0.25.0",
        "tree-sitter-yaml==0.7.2",
    )
    for parser_pin in parser_pins:
        assert parser_pin in requirements, f"structural parser dependency must be pinned: {parser_pin}"
        assert f'"{parser_pin}"' in pyproject_text, f"pyproject structural parser pin must match requirements.txt: {parser_pin}"
    mcp_local = (ROOT / ".harness" / "bin" / "mcp-local").read_text(encoding="utf-8")
    assert "mcp-preflight" in mcp_local and 'tee -a "$LOG" >&2' in mcp_local, "MCP launcher must preflight and preserve stderr diagnostics"
    for image_name in ("Dockerfile", "Dockerfile.opencode"):
        image_text = (ROOT / image_name).read_text(encoding="utf-8")
        assert "/awoki/.harness/bin/mcp-preflight --quiet" in image_text, f"{image_name} must fail build on an incompatible MCP runtime"
        assert "/awoki/.harness/bin/code-parser-check" in image_text, f"{image_name} must validate every curated structural parser at build time"
        assert "/awoki/.harness/bin/code-search-eval-check" in image_text, f"{image_name} must run the structural search golden gate at build time"
        assert "code-parser-check >/tmp" not in image_text, f"{image_name} must expose parser diagnostics in build logs"
        assert "code-search-eval-check >/tmp" not in image_text, f"{image_name} must expose evaluation diagnostics in build logs"
        assert "FROM golang:1.26.5-bookworm AS go_semantics_builder" in image_text, f"{image_name} must pin the deterministic Go semantics builder"
        assert "COPY --from=go_semantics_builder /out/awoki-go-semantics /usr/local/bin/awoki-go-semantics" in image_text, f"{image_name} must ship the pinned prebuilt semantics helper"
        assert "COPY --from=go_semantics_builder /usr/local/go" not in image_text and "COPY --from=go_semantics_builder /usr/local/go /usr/local/go" not in image_text, f"{image_name} must not ship the Go compiler/toolchain just to evaluate fixed semantics"
        assert "awoki-go-semantics --version" in image_text and "go1\\.26\\.5" in image_text, f"{image_name} must validate the pinned semantics helper at build time"
        assert "golang-go" not in image_text, f"{image_name} must not depend on Debian's drifting/older Go package for semantics proof"

    tmux_vendor = ROOT / ".harness" / "vendor" / "oh-my-tmux"
    for name in (".tmux.conf", ".tmux.conf.local", "LICENSE.MIT", "LICENSE.WTFPLv2", "UPSTREAM.md"):
        assert (tmux_vendor / name).is_file(), f"missing vendored Oh my tmux! file: {name}"
    tmux_local = (ROOT / ".harness" / "config" / "tmux.conf.local").read_text(encoding="utf-8")
    assert "set -g history-limit 100000" in tmux_local and "set -g mouse on" in tmux_local, "Awoki tmux local defaults drifted"
    assert "/opt/oh-my-tmux/.tmux.conf" in dockerfile, "OpenCode image must install the vendored tmux main config"
    assert "ln -s /opt/oh-my-tmux/.tmux.conf /home/op/.tmux.conf" in dockerfile, "tmux main config must remain immutable and linked"
    assert "/awoki/.harness/bin/tmux-check" in dockerfile and "/awoki/.harness/bin/tmux-check" in ssh_entrypoint, "tmux config must be smoke-tested at build and startup"
    rag_text = (ROOT / ".harness" / "rag_backend.py").read_text(encoding="utf-8")
    assert "AWOKI_RERANK_URL" in rag_text and "AWOKI_RERANK_FAIL_MODE" in rag_text, "remote reranker configuration missing"
    assert "AWOKI_EMBEDDING_BASE_URL" in rag_text, "remote embedding endpoint configuration missing"
    for skill_path in (ROOT / ".opencode" / "skills").glob("*/SKILL.md"):
        text = skill_path.read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"skill frontmatter missing: {skill_path}"
        end = text.find("\n---", 4)
        assert end > 0, f"skill frontmatter terminator missing: {skill_path}"
        header = text[4:end]
        allowed = {"name", "description", "license", "compatibility", "metadata"}
        top_keys = {line.split(":", 1)[0].strip() for line in header.splitlines() if line and not line.startswith(" ") and ":" in line}
        unknown = top_keys - allowed
        assert not unknown, f"unsupported OpenCode skill frontmatter in {skill_path}: {sorted(unknown)}"
    for relative in [
        ".harness/continuity.py", ".harness/continuations.py", ".harness/continuity_migration.py", ".harness/safety.py", ".harness/mcp_runtime.py",
        ".harness/indexing_policy.py", ".harness/opencode_events.py", ".harness/agent_runtime.py", ".harness/work_ledger.py", ".harness/acceptance_runs.py", ".harness/evidence_store.py", ".harness/awoki.py", ".harness/backup.py",
        ".harness/harness_core.py", ".harness/rag_backend.py", ".harness/reliability.py",
        ".harness/code_search/text_search.py",
        ".harness/burp.py", ".harness/server.py", ".harness/project_workspace.py",
        ".harness/project.py", ".harness/run_tests.py", ".harness/validate_opencode_plugin.py", ".harness/integrations/burp/awoki_burp.py",
        ".harness/bin/code-search-fallback",
    ]:
        py_compile.compile(str(ROOT / relative), doraise=True)
    print("harness validation ok")


if __name__ == "__main__":
    main()
