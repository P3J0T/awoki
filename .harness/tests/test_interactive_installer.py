from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_interactive_installer_keeps_automation_path_and_guides_runtime_config() -> None:
    installer = (ROOT / "install-awoki.sh").read_text(encoding="utf-8")
    assert "--non-interactive" in installer
    assert "--configure-only" in installer
    assert "AWOKI_RUNTIME_CONFLICT_POLICY=ask" in installer
    for key in (
        "AWOKI_COMPOSE_PROJECT_NAME",
        "AWOKI_OPENCODE_SSH_PORT",
        "AWOKI_OPENCODE_WEB_ENABLED",
        "AWOKI_OPENCODE_WEB_PORT",
        "AWOKI_OPENCODE_WEB_USERNAME",
        "AWOKI_OPENCODE_WEB_PASSWORD",
        "AWOKI_EMBEDDING_BASE_URL",
        "AWOKI_EMBEDDING_DEPLOYMENT_ID",
        "AWOKI_VECTOR_SIZE",
        "AWOKI_RERANK_ENABLED",
        "AWOKI_RERANK_URL",
    ):
        assert key in installer
    assert "make install-opencode-ssh" in installer
    assert "make opencode-runtime-check" in installer
    assert "opencode auth login" in installer
    assert "opencode mcp add" in installer
    assert "== Pre-build configuration review ==" in installer
    assert "Docker build/start: not started by this installer yet" in installer
    assert "Secrets are never printed in this review." in installer
    assert "Edit .env, then re-run static validation" in installer
    assert "Edit OpenCode user/provider config" in installer
    assert "BUILD/START Docker now" in installer
    assert "FINAL CONFIRMATION: start Docker build/runtime now?" in installer
    assert "Stop here with configuration saved" in installer
    assert '.opencode-state/config/opencode.jsonc' in installer
    assert "opencode-user-config-check" in installer
    assert "show_env_changes" in installer
    assert "hashlib.sha256" in installer
    assert 'key.endswith(("_API_KEY", "_PASSWORD", "_TOKEN", "_SECRET"))' in installer
    assert installer.index("prebuild_review_gate") < installer.index('echo "== Docker runtime conflict check =="')
    assert "== Final verification ==" in installer
    assert "make -s opencode-ssh-client-check" in installer
    assert installer.index("make -s opencode-ssh-client-check") < installer.index('echo "== Awoki ready =="')
    assert '"SSH:"' in installer
    assert '.ssh-container/id_ed25519' in installer
    assert '.ssh-container/known_hosts' in installer


def test_interactive_installer_separates_user_provider_config_from_awoki_project_config() -> None:
    installer = (ROOT / "install-awoki.sh").read_text(encoding="utf-8")
    assert 'OPENCODE_USER_CONFIG="$ROOT/.opencode-state/config/opencode.jsonc"' in installer
    assert "personal provider/model settings normally belong" in installer
    assert "Nothing below builds Docker until you explicitly choose option 5." in installer
    gate = installer[installer.index("prebuild_review_gate()") : installer.index("read_runtime_instance_id()")]
    assert '5)\n        if prompt_yes_no "FINAL CONFIRMATION: start Docker build/runtime now?" no; then' in gate
    assert '1) return 0' not in gate


def test_interactive_installer_offers_safe_different_checkout_resolution() -> None:
    installer = (ROOT / "install-awoki.sh").read_text(encoding="utf-8")
    assert "Another Awoki checkout is using the requested runtime ports" in installer
    assert "Stop that other Awoki runtime (containers are preserved) and continue" in installer
    assert "Keep it running; choose different ports/project for this checkout" in installer
    assert "Abort with both installations untouched" in installer
    assert 'docker stop "$cid"' in installer
    assert 'docker rm -f "$cid"' not in installer[installer.index("stop_other_awoki_checkout()") : installer.index("configure_parallel_runtime()") ]
    for key in (
        "AWOKI_OPENCODE_SSH_PORT",
        "AWOKI_OPENCODE_WEB_PORT",
        "AWOKI_QDRANT_HTTP_PORT",
        "AWOKI_QDRANT_GRPC_PORT",
        "AWOKI_LAVISH_PORT",
        "AWOKI_COMPOSE_PROJECT_NAME",
    ):
        assert key in installer
    assert "Keep these parallel-runtime settings and continue toward Docker build/start?" in installer
    main = installer[installer.index('echo "== Docker runtime conflict check =="') :]
    assert main.index("resolve_external_port_conflicts_interactive") < main.index("make install-opencode-ssh")


def test_opencode_user_config_helpers_are_exposed() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "opencode-user-config-check:" in makefile
    assert "opencode-config-reload:" in makefile
    assert "opencode-auth:" in makefile
    checker = ROOT / ".harness" / "bin" / "opencode-user-config-check"
    assert checker.exists()
    assert "invalid OpenCode user JSONC" in checker.read_text(encoding="utf-8")


def test_interactive_installer_avoids_bash4_only_constructs() -> None:
    for name in ("install-awoki.sh", "bootstrap-awoki.sh"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for forbidden in ("declare -A", "mapfile", "readarray", "[[ -v "):
            assert forbidden not in text, f"{name} contains Bash-4-only construct {forbidden!r}"


def test_bootstrap_moves_existing_checkout_instead_of_deleting_it() -> None:
    bootstrap = (ROOT / "bootstrap-awoki.sh").read_text(encoding="utf-8")
    assert 'mv "$TARGET" "$backup"' in bootstrap
    assert "previous checkout moved to" in bootstrap
    assert "running Docker containers are intentionally left alone" in bootstrap
    assert "normal for GitHub Download ZIP" in bootstrap
    assert "git init -q" in bootstrap
    assert "Import Awoki source archive for local runtime" in bootstrap
    assert not re.search(r'rm\s+-rf\s+"?\$TARGET', bootstrap)
    assert "exec ./install-awoki.sh" in bootstrap


def test_makefile_exposes_guided_install_and_validates_scripts() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "install-interactive:" in makefile
    assert "\t./install-awoki.sh" in makefile
    assert "install-awoki.sh \\\n" in makefile
    assert "bootstrap-awoki.sh \\\n" in makefile


def test_launcher_hard_fails_through_shared_ssh_client_verifier() -> None:
    launcher = (ROOT / ".harness" / "bin" / "run-opencode-ssh").read_text(encoding="utf-8")
    helper = ROOT / ".harness" / "bin" / "verify-opencode-ssh-client"
    assert helper.exists()
    assert helper.stat().st_mode & 0o111
    assert '"$ROOT/.harness/bin/verify-opencode-ssh-client"' in launcher
    assert "WARNING: automatic SSH auth probe failed" not in launcher
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "opencode-ssh-client-check:" in makefile
    assert ".harness/bin/verify-opencode-ssh-client" in makefile
