from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".harness"))

from mcp_runtime import MCPRuntimeError, SUPPORTED_REQUIREMENT, validate_mcp_version
from code_search.text_search import scan_files


class RuntimeContractTests(unittest.TestCase):
    def test_awoki_dev_preflight_accepts_writable_top_level_checkout(self) -> None:
        helper = ROOT / ".harness" / "bin" / "awoki-dev-preflight"
        self.assertTrue(helper.stat().st_mode & stat.S_IXUSR)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "awoki"
            (root / ".harness" / "bin").mkdir(parents=True)
            shutil.copy2(helper, root / ".harness" / "bin" / "awoki-dev-preflight")
            (root / ".harness" / "manifest.json").write_text("{}\n", encoding="utf-8")
            (root / "README.md").write_text("# Awoki\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=Awoki Test", "-c", "user.email=awoki@example.invalid", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=Awoki Test", "-c", "user.email=awoki@example.invalid", "commit", "-qm", "fixture"], cwd=root, check=True)
            completed = subprocess.run(
                [str(root / ".harness" / "bin" / "awoki-dev-preflight")],
                cwd=root, text=True, capture_output=True, check=False, timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("awoki_dev_checkout=ok", completed.stdout)
            self.assertIn(f"root={root.resolve()}", completed.stdout)

    def test_awoki_dev_preflight_fails_closed_without_product_git_root(self) -> None:
        helper = ROOT / ".harness" / "bin" / "awoki-dev-preflight"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime-appliance"
            (root / ".harness" / "bin").mkdir(parents=True)
            shutil.copy2(helper, root / ".harness" / "bin" / "awoki-dev-preflight")
            (root / ".harness" / "manifest.json").write_text("{}\n", encoding="utf-8")
            (root / "README.md").write_text("# Awoki runtime\n", encoding="utf-8")
            completed = subprocess.run(
                [str(root / ".harness" / "bin" / "awoki-dev-preflight")],
                cwd=root, text=True, capture_output=True, check=False, timeout=10,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("not a top-level Awoki Git checkout", completed.stderr)
            self.assertIn("do not modify the runtime appliance", completed.stderr)
            text = helper.read_text(encoding="utf-8")
            for forbidden in ("sudo ", " su ", "chown ", "chmod "):
                self.assertNotIn(forbidden, text)
    def _run_ssh_key_bootstrap(self, root: Path) -> subprocess.CompletedProcess[str]:
        helper_src = ROOT / ".harness" / "bin" / "prepare-opencode-ssh-keys"
        helper_dst = root / ".harness" / "bin" / "prepare-opencode-ssh-keys"
        helper_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(helper_src, helper_dst)
        fake_bin = root / ".test-bin"
        fake_bin.mkdir(exist_ok=True)
        fake_ssh_keygen = fake_bin / "ssh-keygen"
        fake_ssh_keygen.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "if [ \"${1:-}\" = \"-y\" ]; then\n"
            "  echo 'ssh-ed25519 AAAATEST awoki-opencode'\n"
            "  exit 0\n"
            "fi\n"
            "out=''\n"
            "while [ $# -gt 0 ]; do\n"
            "  if [ \"$1\" = \"-f\" ]; then shift; out=$1; fi\n"
            "  shift || true\n"
            "done\n"
            "[ -n \"$out\" ]\n"
            "printf '%s\\n' 'FAKE-PRIVATE-KEY' > \"$out\"\n"
            "printf '%s\\n' 'ssh-ed25519 AAAATEST awoki-opencode' > \"$out.pub\"\n",
            encoding="utf-8",
        )
        fake_ssh_keygen.chmod(0o755)
        env = {
            **os.environ,
            "AWOKI_ROOT": str(root),
            "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        }
        return subprocess.run(
            [str(helper_dst)],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def test_opencode_ssh_key_bootstrap_creates_host_only_keypair(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "awoki"
            root.mkdir()
            completed = self._run_ssh_key_bootstrap(root)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            ssh_dir = root / ".ssh-container"
            private_key = ssh_dir / "id_ed25519"
            public_key = ssh_dir / "id_ed25519.pub"
            for path in (private_key, public_key):
                self.assertTrue(path.is_file(), path)
                self.assertFalse(path.is_symlink(), path)
            self.assertFalse((ssh_dir / "authorized_keys").exists())
            self.assertEqual(stat.S_IMODE(ssh_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(private_key.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(public_key.stat().st_mode), 0o644)

    def test_opencode_ssh_key_bootstrap_refuses_symlinked_key_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "awoki"
            root.mkdir()
            first = self._run_ssh_key_bootstrap(root)
            self.assertEqual(first.returncode, 0, first.stderr)
            public_key = root / ".ssh-container" / "id_ed25519.pub"
            public_key.unlink()
            public_key.symlink_to(root / ".ssh-container" / "id_ed25519")
            rejected = self._run_ssh_key_bootstrap(root)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("must be a regular file, not a symlink", rejected.stderr)

    def test_opencode_compose_avoids_docker_desktop_single_file_authorized_keys_bind(self) -> None:
        compose = (ROOT / "docker-compose.opencode.yml").read_text(encoding="utf-8")
        self.assertIn("AWOKI_SSH_AUTHORIZED_KEY: ${AWOKI_SSH_AUTHORIZED_KEY:-}", compose)
        self.assertNotIn("source: ./.ssh-container/authorized_keys", compose)
        self.assertNotIn("target: /awoki-ssh/authorized_keys", compose)

    def test_opencode_ssh_public_key_export_returns_valid_single_line(self) -> None:
        if shutil.which("ssh-keygen") is None:
            self.skipTest("ssh-keygen unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "awoki"
            bin_dir = root / ".harness" / "bin"
            bin_dir.mkdir(parents=True)
            for name in ("prepare-opencode-ssh-keys", "opencode-ssh-public-key"):
                shutil.copy2(ROOT / ".harness" / "bin" / name, bin_dir / name)
            completed = subprocess.run(
                [str(bin_dir / "opencode-ssh-public-key")],
                cwd=root,
                env={**os.environ, "AWOKI_ROOT": str(root)},
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            lines = completed.stdout.splitlines()
            self.assertEqual(len(lines), 1)
            self.assertTrue(lines[0].startswith("ssh-ed25519 "), lines[0])
            self.assertFalse((root / ".ssh-container" / "authorized_keys").exists())


    def test_mcp_version_guard_accepts_v1_and_rejects_v2(self) -> None:
        self.assertEqual(validate_mcp_version("1.29.0"), "1.29.0")
        with self.assertRaisesRegex(MCPRuntimeError, "requires MCP Python SDK 1.x"):
            validate_mcp_version("2.0.0")
        with self.assertRaisesRegex(MCPRuntimeError, "cannot parse"):
            validate_mcp_version("unknown")

    def test_dependency_files_bound_mcp_to_v1(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertEqual(SUPPORTED_REQUIREMENT, "mcp>=1.29,<2")
        self.assertIn(SUPPORTED_REQUIREMENT, requirements)
        self.assertIn(f'"{SUPPORTED_REQUIREMENT}"', pyproject)
        self.assertNotRegex(requirements, r"(?m)^mcp>=1\.13\.0$")

    def test_mcp_launchers_preflight_and_preserve_stderr(self) -> None:
        local = (ROOT / ".harness" / "bin" / "mcp-local").read_text(encoding="utf-8")
        preflight = ROOT / ".harness" / "bin" / "mcp-preflight"
        self.assertTrue(preflight.stat().st_mode & stat.S_IXUSR)
        self.assertIn('mcp-preflight" --quiet', local)
        self.assertIn('tee -a "$LOG" >&2', local)
        self.assertIn("mcp-local.stderr.log", local)

    def test_parser_preflight_honors_selected_python_runtime(self) -> None:
        parser_check = (ROOT / ".harness" / "bin" / "code-parser-check").read_text(encoding="utf-8")
        self.assertIn('"${PYTHON:-python3}"', parser_check)
        self.assertNotIn('}\" python3 -', parser_check)

    def test_exhaustive_code_search_fallback_survives_giant_lines_and_paginates(self) -> None:
        if shutil.which("rg") is None:
            self.skipTest("ripgrep unavailable")
        helper = ROOT / ".harness" / "bin" / "code-search-fallback"
        self.assertTrue(helper.stat().st_mode & stat.S_IXUSR)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            left = root / "left"
            right = root / "right"
            left.mkdir()
            right.mkdir()
            (left / "tree.json").write_text(("a" * 128) + "ProcessTree" + ("b" * 5_000_000) + "\n", encoding="utf-8")
            for index in range(10):
                (left / f"match-left-{index}.txt").write_text(f"ProcessTree {index}\n" * 3, encoding="utf-8")
                (right / f"match-right-{index}.txt").write_text(f"ProcessTree {index}\n" * 2, encoding="utf-8")
            completed = subprocess.run(
                [str(helper), "ProcessTree", str(left), str(right), "--page-size", "7", "--preview-chars", "256"],
                text=True, capture_output=True, check=False, timeout=20,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["mode"], "exhaustive_text_fallback")
            self.assertTrue(payload["universe_complete"])
            self.assertFalse(payload["search_complete"])
            self.assertEqual(payload["match_count"], 51)
            self.assertEqual(payload["matching_file_count"], 21)
            self.assertEqual(payload["returned"], 7)
            self.assertEqual(payload["next_cursor"], "7")
            self.assertIn("Do not pipe through head", payload["note"])
            self.assertLess(len(completed.stdout), 20_000)
            self.assertNotIn("Ripgrep JSON record exceeded", completed.stdout)
            self.assertNotIn("b" * 10_000, completed.stdout)

            last = subprocess.run(
                [str(helper), "ProcessTree", str(left), str(right), "--page-size", "100", "--cursor", payload["next_cursor"], "--preview-chars", "256"],
                text=True, capture_output=True, check=False, timeout=20,
            )
            self.assertEqual(last.returncode, 0, last.stderr)
            last_payload = json.loads(last.stdout)
            self.assertEqual(last_payload["match_count"], 51)
            self.assertTrue(last_payload["search_complete"])
            self.assertEqual(last_payload["returned"], 44)

    def test_cli_forensic_mode_includes_gitignored_files_and_keeps_env_preview_opaque(self) -> None:
        if shutil.which("rg") is None or shutil.which("git") is None:
            self.skipTest("git/ripgrep unavailable")
        helper = ROOT / ".harness" / "bin" / "code-search-fallback"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / ".gitignore").write_text(".env\n", encoding="utf-8")
            (root / "main.go").write_text("// CLI_FORENSIC_NEEDLE\n", encoding="utf-8")
            (root / ".env").write_text("TOKEN=CLI_FORENSIC_NEEDLE-super-secret\n", encoding="utf-8")

            default = subprocess.run(
                [str(helper), "CLI_FORENSIC_NEEDLE", ".", "--fixed-string"],
                cwd=root, text=True, capture_output=True, check=False, timeout=20,
            )
            self.assertEqual(default.returncode, 0, default.stderr)
            self.assertEqual(json.loads(default.stdout)["match_count"], 1)

            forensic = subprocess.run(
                [str(helper), "CLI_FORENSIC_NEEDLE", ".", "--fixed-string", "--include-ignored"],
                cwd=root, text=True, capture_output=True, check=False, timeout=20,
            )
            self.assertEqual(forensic.returncode, 0, forensic.stderr)
            payload = json.loads(forensic.stdout)
            self.assertEqual(payload["match_count"], 2)
            self.assertTrue(payload["include_ignored"])
            env_match = next(row for row in payload["matches"] if row["path"] == ".env")
            self.assertEqual(env_match["match_preview"], "<REDACTED_SENSITIVE_FILE_MATCH>")
            self.assertNotIn("super-secret", json.dumps(payload, sort_keys=True))

    def test_exhaustive_code_search_fallback_splits_to_per_file_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            target = root / "target.txt"
            target.write_text("ProcessTree\n", encoding="utf-8")
            fake_rg = fake_bin / "rg"
            fake_rg.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
            fake_rg.chmod(0o755)
            original_path = os.environ.get("PATH", "")
            os.environ["PATH"] = str(fake_bin) + os.pathsep + original_path
            try:
                result = scan_files(repo_root=root, pattern="ProcessTree", files=[target.name], shard_timeout_seconds=0.2)
            finally:
                os.environ["PATH"] = original_path
            self.assertEqual(result["status"], "partial")
            self.assertFalse(result["universe_complete"])
            self.assertFalse(result["search_complete"])
            self.assertEqual(result["timed_out_files"], [target.name])

    def test_both_images_pin_a_prebuilt_go_semantics_helper_without_shipping_the_compiler(self) -> None:
        for image_name in ("Dockerfile", "Dockerfile.opencode"):
            text = (ROOT / image_name).read_text(encoding="utf-8")
            self.assertIn("FROM golang:1.26.5-bookworm AS go_semantics_builder", text)
            self.assertIn("go build -trimpath", text)
            self.assertIn("COPY --from=go_semantics_builder /out/awoki-go-semantics /usr/local/bin/awoki-go-semantics", text)
            self.assertIn("awoki-go-semantics --version", text)
            self.assertIn("go1\\.26\\.5", text)
            self.assertNotIn("COPY --from=go_semantics_builder /usr/local/go", text)
            self.assertNotIn("PATH=/usr/local/go/bin:${PATH}", text)
            self.assertNotIn("golang-go", text)

    def test_both_images_fail_build_on_incompatible_mcp_runtime(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        opencode = (ROOT / "Dockerfile.opencode").read_text(encoding="utf-8")
        self.assertIn("/awoki/.harness/bin/mcp-preflight --quiet", dockerfile)
        self.assertIn("/awoki/.harness/bin/mcp-preflight --quiet", opencode)

    def test_vendored_oh_my_tmux_snapshot_matches_recorded_hashes(self) -> None:
        vendor = ROOT / ".harness" / "vendor" / "oh-my-tmux"
        upstream = (vendor / "UPSTREAM.md").read_text(encoding="utf-8")
        for name in (".tmux.conf", ".tmux.conf.local", "LICENSE.MIT", "LICENSE.WTFPLv2"):
            path = vendor / name
            self.assertTrue(path.is_file(), name)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertRegex(upstream, rf"(?m)^{re.escape(digest)}\s+{re.escape(name)}$")
        self.assertIn("https://github.com/gpakosz/.tmux", upstream)

    def test_awoki_tmux_local_layer_has_operational_defaults(self) -> None:
        local = (ROOT / ".harness" / "config" / "tmux.conf.local").read_text(encoding="utf-8")
        self.assertIn("tmux_conf_new_window_retain_current_path=true", local)
        self.assertIn("tmux_conf_new_pane_retain_current_path=true", local)
        self.assertIn("tmux_conf_copy_to_os_clipboard=false", local)
        self.assertIn("set -g history-limit 100000", local)
        self.assertIn("set -g mouse on", local)
        self.assertIn("setw -g mode-keys vi", local)

    def test_opencode_image_installs_and_validates_tmux_without_network_bootstrap(self) -> None:
        dockerfile = (ROOT / "Dockerfile.opencode").read_text(encoding="utf-8")
        entrypoint = (ROOT / ".harness" / "bin" / "opencode-ssh-entrypoint").read_text(encoding="utf-8")
        check = ROOT / ".harness" / "bin" / "tmux-check"
        self.assertTrue(check.stat().st_mode & stat.S_IXUSR)
        self.assertIn("/opt/oh-my-tmux/.tmux.conf", dockerfile)
        self.assertIn("ln -s /opt/oh-my-tmux/.tmux.conf /home/op/.tmux.conf", dockerfile)
        self.assertIn("/home/op/.tmux.conf.local", dockerfile)
        self.assertIn("runuser -u op", dockerfile)
        self.assertIn("/awoki/.harness/bin/tmux-check", entrypoint)
        self.assertNotRegex(dockerfile, r"(?i)(curl|wget|git clone).*gpakosz")

    def test_runtime_snapshot_helper_writes_secure_allowlisted_environment(self) -> None:
        entrypoint = (ROOT / ".harness" / "bin" / "opencode-ssh-entrypoint").read_text(encoding="utf-8")
        helper_path = ROOT / ".harness" / "bin" / "awoki-runtime-snapshot"
        helper = helper_path.read_text(encoding="utf-8")
        self.assertTrue(helper_path.stat().st_mode & stat.S_IXUSR)
        self.assertIn("/awoki/.harness/bin/awoki-runtime-snapshot", entrypoint)
        self.assertNotIn("runtime_env_names=(", entrypoint)
        self.assertIn('runtime_env_dir="${AWOKI_RUNTIME_ENV_DIR:-/run/awoki}"', helper)
        self.assertIn('runtime_env_gid="$(id -g op)"', helper)
        self.assertIn('install -d -o root -g "$runtime_env_gid" -m 0750', helper)
        self.assertIn("umask 077", helper)
        self.assertIn("mktemp", helper)
        self.assertIn("printf 'export %s=%q", helper)
        self.assertIn('chmod 0640 "$runtime_env_tmp"', helper)
        self.assertIn('mv -f "$runtime_env_tmp" "$runtime_env_file"', helper)
        self.assertIn("runtime environment handoff validation failed", helper)
        self.assertIn("runuser -u op -- env -i HOME=/home/op /bin/bash", helper)
        self.assertIn("AWOKI_RERANK_API_KEY_ENV names", helper)
        self.assertNotIn('[[ -v ', helper)
        self.assertIn('export AWOKI_RERANK_API_KEY="${!rerank_key_env}"', helper)
        for required in (
            "AWOKI_EMBEDDING_DEPLOYMENT_ID", "AWOKI_EMBEDDING_BASE_URL", "AWOKI_VECTOR_SIZE",
            "AWOKI_QDRANT_COLLECTION", "AWOKI_RERANK_ENABLED", "AWOKI_RERANK_PROVIDER",
            "AWOKI_RERANK_URL", "AWOKI_LAVISH_PORT", "AWOKI_LAVISH_VERSION",
            "LAVISH_AXI_STATE_DIR", "AWOKI_EMBEDDING_WORKER_MAX_RETRIES",
            "AWOKI_EMBEDDING_RETRY_BACKOFF_SECONDS", "AWOKI_EMBEDDING_ADAPTIVE_MIN_BATCH_SIZE",
        ):
            self.assertRegex(helper, rf"\b{required}\b")

    def test_container_opencode_starts_mcp_from_clean_shell_environment(self) -> None:
        config = (ROOT / "opencode.container.jsonc").read_text(encoding="utf-8")
        self.assertIn('"/usr/bin/env", "-i"', config)
        self.assertIn('"/bin/bash", "--noprofile", "--norc", "/awoki/.harness/bin/mcp-auto"', config)
        self.assertNotIn('"environment": {', config)
        self.assertIn("Retrieval credentials", config)
        self.assertIn("validated runtime snapshot", config)

    def test_runtime_snapshot_and_mcp_profile_cover_supported_compose_settings(self) -> None:
        compose = (ROOT / "docker-compose.opencode.yml").read_text(encoding="utf-8")
        service_match = re.search(r"(?ms)^  awoki-opencode-ssh:\n(.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", compose)
        self.assertIsNotNone(service_match)
        env_match = re.search(r"(?ms)^    environment:\n(.*?)(?=^    [A-Za-z_]+:|\Z)", service_match.group(1))
        self.assertIsNotNone(env_match)
        compose_names = {
            match.group(1)
            for match in re.finditer(r"(?m)^      ([A-Z][A-Z0-9_]+):", env_match.group(1))
            if match.group(1).startswith(("AWOKI_", "HARNESS_", "QDRANT_", "LAVISH_"))
        }

        snapshot = (ROOT / ".harness" / "bin" / "awoki-runtime-snapshot").read_text(encoding="utf-8")
        snapshot_match = re.search(r"(?ms)runtime_env_names=\(\n(.*?)\n\)", snapshot)
        self.assertIsNotNone(snapshot_match)
        snapshot_names = set(re.findall(r"\b(?:AWOKI|HARNESS|QDRANT|LAVISH)_[A-Z0-9_]+\b", snapshot_match.group(1)))
        bootstrap_only = {"AWOKI_SSH_AUTHORIZED_KEY"}
        self.assertFalse(
            (compose_names - bootstrap_only) - snapshot_names,
            f"Compose runtime settings missing from snapshot: {sorted((compose_names - bootstrap_only) - snapshot_names)}",
        )
        self.assertFalse(bootstrap_only & snapshot_names, "SSH bootstrap public key must not persist into the runtime snapshot")

        wrapper = (ROOT / ".harness" / "bin" / "awoki-runtime-env").read_text(encoding="utf-8")
        arrays = {}
        for array_name in ("base_names", "qdrant_names", "retrieval_names", "burp_names", "lavish_names"):
            match = re.search(rf"(?ms){array_name}=\(\n(.*?)\n\)", wrapper)
            self.assertIsNotNone(match, array_name)
            arrays[array_name] = set(re.findall(r"\b(?:AWOKI|HARNESS|QDRANT|LAVISH)_[A-Z0-9_]+\b", match.group(1)))
        mcp_names = arrays["base_names"] | arrays["qdrant_names"] | arrays["retrieval_names"] | arrays["burp_names"]
        expected_mcp = {
            name for name in (compose_names - bootstrap_only)
            if not name.startswith(("AWOKI_LAVISH_", "LAVISH_"))
        }
        self.assertFalse(expected_mcp - mcp_names, f"supported MCP settings missing from clean mcp profile: {sorted(expected_mcp - mcp_names)}")
        self.assertFalse(mcp_names & arrays["lavish_names"], "internal mcp profile must not inherit Lavish-only settings")

    def test_mcp_auto_restores_runtime_environment_before_launch(self) -> None:
        launcher = ROOT / ".harness" / "bin" / "mcp-auto"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "awoki"
            bin_dir = root / ".harness" / "bin"
            bin_dir.mkdir(parents=True)
            fake_local = bin_dir / "mcp-local"
            fake_local.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
import json
import os

keys = [
    "AWOKI_ROOT",
    "AWOKI_MODE",
    "AWOKI_EMBEDDING_DEPLOYMENT_ID",
    "AWOKI_EMBEDDING_BASE_URL",
    "AWOKI_VECTOR_SIZE",
    "AWOKI_QDRANT_COLLECTION",
    "AWOKI_RERANK_ENABLED",
    "AWOKI_RERANK_PROVIDER",
    "AWOKI_RERANK_URL",
]
payload = {key: os.environ.get(key) for key in keys}
payload["_runtime_profile"] = os.environ.get("AWOKI_RUNTIME_ENV_PROFILE")
payload["_stale"] = os.environ.get("STALE_MCP_ENV")
payload["_pythonpath"] = os.environ.get("PYTHONPATH")
print(json.dumps(payload, sort_keys=True))
PY
""",
                encoding="utf-8",
            )
            fake_local.chmod(0o755)
            shutil.copy2(ROOT / ".harness" / "bin" / "awoki-runtime-env", bin_dir / "awoki-runtime-env")

            snapshot = {
                "AWOKI_ROOT": str(root),
                "AWOKI_MODE": "container-opencode" if Path("/.dockerenv").exists() else "local",
                "AWOKI_EMBEDDING_DEPLOYMENT_ID": "jinaai/jina-embeddings-v2-base-code",
                "AWOKI_EMBEDDING_BASE_URL": "http://embedding.example.invalid:8000/v1",
                "AWOKI_VECTOR_SIZE": "768",
                "AWOKI_QDRANT_COLLECTION": "awoki_jina_embeddings_v2_base_code_768",
                "AWOKI_RERANK_ENABLED": "1",
                "AWOKI_RERANK_PROVIDER": "tei",
                "AWOKI_RERANK_URL": "http://reranker.example.invalid:8000/rerank",
            }
            runtime_env = Path(tmp) / "runtime.env"
            runtime_env.write_text(
                "\n".join(f"export {key}={shlex.quote(value)}" for key, value in snapshot.items()) + "\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            for key in snapshot:
                env[key] = "stale-value"
            env["AWOKI_RUNTIME_ENV_FILE"] = str(runtime_env)
            env["STALE_MCP_ENV"] = "must-not-leak"
            env["PYTHONPATH"] = "/tmp/untrusted-pythonpath"
            completed = subprocess.run(
                [str(launcher)],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            expected = dict(snapshot)
            if Path("/.dockerenv").exists():
                expected["_runtime_profile"] = "mcp"
                expected["_stale"] = None
                expected["_pythonpath"] = None
            else:
                expected["_runtime_profile"] = None
                expected["_stale"] = "must-not-leak"
                expected["_pythonpath"] = "/tmp/untrusted-pythonpath"
            self.assertEqual(json.loads(completed.stdout), expected)

    def test_mcp_auto_rejects_symlink_runtime_environment(self) -> None:
        launcher = ROOT / ".harness" / "bin" / "mcp-auto"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.env"
            target.write_text("export AWOKI_MODE=local\n", encoding="utf-8")
            link = Path(tmp) / "runtime.env"
            link.symlink_to(target)
            env = os.environ.copy()
            env["AWOKI_RUNTIME_ENV_FILE"] = str(link)
            completed = subprocess.run(
                [str(launcher)],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("refusing symlink runtime environment file", completed.stderr)

    def test_mcp_auto_validates_production_snapshot_trust_before_sourcing(self) -> None:
        launcher = (ROOT / ".harness" / "bin" / "mcp-auto").read_text(encoding="utf-8")
        self.assertIn("runtime environment snapshot trust validation failed", launcher)
        self.assertIn('awoki-runtime-env" --profile mcp --', launcher)
        self.assertIn("file_group_digit", launcher)
        self.assertIn("dir_group_digit", launcher)
        self.assertIn("runtime environment snapshot is writable by an untrusted group/world principal", launcher)

    def test_runtime_check_uses_guarded_qdrant_profile_without_manual_source(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("/awoki/.harness/bin/awoki-runtime-env --profile qdrant -- bash -lc", makefile)
        self.assertIn('test "$${AWOKI_MODE:-}" = container-opencode', makefile)
        self.assertNotIn("cat /run/awoki/runtime.env", makefile)
        self.assertNotIn("source /run/awoki/runtime.env", makefile)

    def test_runtime_env_diagnostic_profiles_are_clean_and_secret_scoped(self) -> None:
        wrapper = ROOT / ".harness" / "bin" / "awoki-runtime-env"
        self.assertTrue(wrapper.stat().st_mode & stat.S_IXUSR)
        wrapper_text = wrapper.read_text(encoding="utf-8")
        self.assertNotIn("[[ -v ", wrapper_text)
        self.assertIn('declare -p "$name" >/dev/null 2>&1', wrapper_text)
        with tempfile.TemporaryDirectory() as tmp:
            runtime_env = Path(tmp) / "runtime.env"
            runtime_env.write_text(
                "\n".join(
                    [
                        "export AWOKI_MODE=container-opencode",
                        "export AWOKI_ROOT=/awoki",
                        "export AWOKI_QDRANT_URL=http://user:pass@qdrant.example.invalid:6333/path?token=hidden",
                        "export AWOKI_EMBEDDING_PROVIDER=openai",
                        "export AWOKI_EMBEDDING_MODEL=text-embeddings-inference",
                        "export AWOKI_EMBEDDING_BASE_URL=http://user:pass@embedding.example.invalid:8000/v1?token=hidden",
                        "export AWOKI_EMBEDDING_API_KEY=super-secret",
                        "export AWOKI_EMBEDDING_BATCH_SIZE=32",
                        "export AWOKI_EMBEDDING_TIMEOUT_SECONDS=30",
                        "export AWOKI_EMBEDDING_QUERY_TIMEOUT_SECONDS=5",
                        "export AWOKI_VECTOR_SIZE=768",
                        "export AWOKI_RERANK_URL=http://reranker.example.invalid:8000/rerank?token=hidden",
                        "export AWOKI_RERANK_API_KEY=rerank-secret",
                        "export AWOKI_BURP_URL=http://host.docker.internal:9876",
                        "export AWOKI_LAVISH_PORT=4444",
                        "export AWOKI_LAVISH_VERSION=1.2.3",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(
                {
                    "AWOKI_RUNTIME_ENV_FILE": str(runtime_env),
                    "AWOKI_EMBEDDING_MODEL": "stale-model",
                    "STALE_DIAGNOSTIC_VALUE": "must-not-leak",
                }
            )

            retrieval = subprocess.run(
                [
                    str(wrapper), "--profile", "retrieval", "--", sys.executable, "-c",
                    (
                        "import json,os; print(json.dumps({"
                        "'model':os.environ.get('AWOKI_EMBEDDING_MODEL'),"
                        "'secret':bool(os.environ.get('AWOKI_EMBEDDING_API_KEY')),"
                        "'stale':os.environ.get('STALE_DIAGNOSTIC_VALUE'),"
                        "'profile':os.environ.get('AWOKI_RUNTIME_ENV_PROFILE'),"
                        "'burp':os.environ.get('AWOKI_BURP_URL')}))"
                    ),
                ],
                cwd=ROOT, env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(retrieval.returncode, 0, retrieval.stderr)
            payload = json.loads(retrieval.stdout)
            self.assertEqual(payload["model"], "text-embeddings-inference")
            self.assertTrue(payload["secret"])
            self.assertIsNone(payload["stale"])
            self.assertEqual(payload["profile"], "retrieval")
            self.assertIsNone(payload["burp"])

            mcp_profile = subprocess.run(
                [
                    str(wrapper), "--profile", "mcp", "--", sys.executable, "-c",
                    (
                        "import json,os; print(json.dumps({"
                        "'secret':bool(os.environ.get('AWOKI_EMBEDDING_API_KEY')),"
                        "'burp':os.environ.get('AWOKI_BURP_URL'),"
                        "'lavish':os.environ.get('AWOKI_LAVISH_PORT'),"
                        "'stale':os.environ.get('STALE_DIAGNOSTIC_VALUE'),"
                        "'profile':os.environ.get('AWOKI_RUNTIME_ENV_PROFILE')}))"
                    ),
                ],
                cwd=ROOT, env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(mcp_profile.returncode, 0, mcp_profile.stderr)
            mcp_payload = json.loads(mcp_profile.stdout)
            self.assertTrue(mcp_payload["secret"])
            self.assertEqual(mcp_payload["burp"], "http://host.docker.internal:9876")
            self.assertIsNone(mcp_payload["lavish"])
            self.assertIsNone(mcp_payload["stale"])
            self.assertEqual(mcp_payload["profile"], "mcp")

            for profile, visible in (
                ("base", "AWOKI_ROOT"),
                ("qdrant", "AWOKI_QDRANT_URL"),
                ("burp", "AWOKI_BURP_URL"),
                ("lavish", "AWOKI_LAVISH_PORT"),
            ):
                completed = subprocess.run(
                    [
                        str(wrapper), "--profile", profile, "--", sys.executable, "-c",
                        (
                            "import json,os; print(json.dumps({"
                            f"'visible':os.environ.get('{visible}'),"
                            "'secret':os.environ.get('AWOKI_EMBEDDING_API_KEY'),"
                            "'profile':os.environ.get('AWOKI_RUNTIME_ENV_PROFILE')}))"
                        ),
                    ],
                    cwd=ROOT, env=env, text=True, capture_output=True, check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                row = json.loads(completed.stdout)
                self.assertIsNotNone(row["visible"], profile)
                self.assertIsNone(row["secret"], profile)
                self.assertEqual(row["profile"], profile)

            config = subprocess.run(
                [str(wrapper), "--print-config"], cwd=ROOT, env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(config.returncode, 0, config.stderr)
            self.assertIn("AWOKI_EMBEDDING_API_KEY=<configured>", config.stdout)
            self.assertIn("AWOKI_RERANK_API_KEY=<configured>", config.stdout)
            self.assertIn("credential_isolation=ambient-environment-minimization-not-sandbox", config.stdout)
            self.assertIn("runtime_user_can_read_snapshot=yes", config.stdout)
            self.assertIn("runtime_env_snapshot_mtime=", config.stdout)
            self.assertIn("AWOKI_QDRANT_URL=http://qdrant.example.invalid:6333/path", config.stdout)
            self.assertIn("AWOKI_EMBEDDING_BASE_URL=http://embedding.example.invalid:8000/v1", config.stdout)
            self.assertNotIn("user:pass", config.stdout)
            self.assertNotIn("token=hidden", config.stdout)
            self.assertNotIn("super-secret", config.stdout)
            self.assertNotIn("rerank-secret", config.stdout)

    def test_runtime_env_help_does_not_require_runtime_snapshot(self) -> None:
        wrapper = ROOT / ".harness" / "bin" / "awoki-runtime-env"
        env = os.environ.copy()
        env["AWOKI_RUNTIME_ENV_FILE"] = "/definitely/missing/awoki-runtime.env"
        completed = subprocess.run(
            [str(wrapper), "--help"], cwd=ROOT, env=env, text=True, capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Usage:", completed.stdout)

    def test_runtime_env_diagnostic_wrapper_rejects_symlink(self) -> None:
        wrapper = ROOT / ".harness" / "bin" / "awoki-runtime-env"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.env"
            target.write_text("export AWOKI_MODE=container-opencode\n", encoding="utf-8")
            link = Path(tmp) / "runtime.env"
            link.symlink_to(target)
            env = os.environ.copy()
            env["AWOKI_RUNTIME_ENV_FILE"] = str(link)
            completed = subprocess.run(
                [str(wrapper), "--check"], cwd=ROOT, env=env, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("refusing symlink runtime environment file", completed.stderr)

    def test_runtime_diagnostics_work_from_host_or_ssh_container(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("docker compose -f docker-compose.opencode.yml exec -T -u op awoki-opencode-ssh", makefile)
        self.assertIn("--profile retrieval -- /awoki/.harness/bin/embedding-benchmark", makefile)
        self.assertIn("--profile retrieval -- /awoki/.harness/bin/reranker-benchmark", makefile)
        self.assertIn("EMBEDDING_BENCHMARK_ARGS", makefile)
        self.assertIn("RERANKER_BENCHMARK_ARGS", makefile)
        phony = next(line for line in makefile.splitlines() if line.startswith(".PHONY:"))
        for target in ("runtime-config", "embedding-benchmark", "reranker-benchmark"):
            self.assertIn(target, phony)

    def test_backend_benchmarks_are_fixed_synthetic_diagnostics(self) -> None:
        for name, marker in (("embedding-benchmark", "AWOKI_EMBEDDING_TIMEOUT_SECONDS"), ("reranker-benchmark", "AWOKI_RERANK_TIMEOUT_SECONDS")):
            benchmark = ROOT / ".harness" / "bin" / name
            self.assertTrue(benchmark.stat().st_mode & stat.S_IXUSR)
            text = benchmark.read_text(encoding="utf-8")
            self.assertIn("synthetic_text_only", text)
            self.assertIn(marker, text)
            self.assertNotIn("workspace/projects", text)
            completed = subprocess.run([str(benchmark), "--help"], cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("synthetic", completed.stdout.lower())

    def test_runtime_benchmarks_use_synthetic_contracts_and_intended_auth_only(self) -> None:
        requests: list[dict[str, object]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                requests.append({
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": body,
                })
                if self.path == "/v1/embeddings":
                    inputs = body.get("input", [])
                    if isinstance(inputs, str):
                        inputs = [inputs]
                    data = [
                        {"object": "embedding", "index": idx, "embedding": [0.1] * 64}
                        for idx, _ in enumerate(inputs)
                    ]
                    payload = {"object": "list", "data": data, "model": "fixture", "usage": {"prompt_tokens": 1, "total_tokens": 1}}
                elif self.path == "/rerank":
                    texts = body.get("texts", body.get("documents", []))
                    payload = [{"index": idx, "score": float(len(texts) - idx)} for idx in range(len(texts))]
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with tempfile.TemporaryDirectory() as tmp:
                runtime_env = Path(tmp) / "runtime.env"
                runtime_env.write_text(
                    "\n".join([
                        "export AWOKI_MODE=container-opencode",
                        "export AWOKI_ROOT=/awoki",
                        "export AWOKI_EMBEDDING_PROVIDER=openai",
                        "export AWOKI_EMBEDDING_MODEL=text-embedding-3-small",
                        f"export AWOKI_EMBEDDING_BASE_URL=http://127.0.0.1:{port}/v1",
                        "export AWOKI_EMBEDDING_API_KEY=embedding-secret",
                        "export AWOKI_EMBEDDING_BATCH_SIZE=3",
                        "export AWOKI_EMBEDDING_TIMEOUT_SECONDS=30",
                        "export AWOKI_EMBEDDING_QUERY_TIMEOUT_SECONDS=5",
                        "export AWOKI_VECTOR_SIZE=4",
                        "export AWOKI_QUERY_PREFIX=query-prefix::",
                        "export AWOKI_DOCUMENT_PREFIX=document-prefix::",
                        "export AWOKI_RERANK_ENABLED=1",
                        "export AWOKI_RERANK_PROVIDER=tei",
                        f"export AWOKI_RERANK_URL=http://127.0.0.1:{port}/rerank",
                        "export AWOKI_RERANK_API_KEY=rerank-secret",
                        "export AWOKI_RERANK_TIMEOUT_SECONDS=20",
                    ]) + "\n",
                    encoding="utf-8",
                )
                env = os.environ.copy()
                env["AWOKI_RUNTIME_ENV_FILE"] = str(runtime_env)
                wrapper = ROOT / ".harness" / "bin" / "awoki-runtime-env"

                embedding = subprocess.run(
                    [str(wrapper), "--profile", "retrieval", "--", str(ROOT / ".harness" / "bin" / "embedding-benchmark"), "--batch-size", "3", "--timeout-seconds", "10", "--json"],
                    cwd=ROOT, env=env, text=True, capture_output=True, check=False,
                )
                self.assertEqual(embedding.returncode, 0, embedding.stderr)
                embedding_payload = json.loads(embedding.stdout)
                self.assertTrue(embedding_payload["synthetic_text_only"])
                self.assertEqual(embedding_payload["configured_vector_size"], 64)
                self.assertTrue(embedding_payload["single"]["contract_ok"])
                self.assertTrue(embedding_payload["bulk"]["contract_ok"])

                reranker = subprocess.run(
                    [str(wrapper), "--profile", "retrieval", "--", str(ROOT / ".harness" / "bin" / "reranker-benchmark"), "--documents", "3", "--timeout-seconds", "10", "--json"],
                    cwd=ROOT, env=env, text=True, capture_output=True, check=False,
                )
                self.assertEqual(reranker.returncode, 0, reranker.stderr)
                rerank_payload = json.loads(reranker.stdout)
                self.assertTrue(rerank_payload["synthetic_text_only"])
                self.assertEqual(rerank_payload["status"], "ok")

            embedding_requests = [row for row in requests if row["path"] == "/v1/embeddings"]
            rerank_requests = [row for row in requests if row["path"] == "/rerank"]
            self.assertEqual(len(embedding_requests), 2)
            self.assertTrue(all(row["authorization"] == "Bearer embedding-secret" for row in embedding_requests))
            self.assertTrue(all(row["body"].get("dimensions") == 64 for row in embedding_requests))
            self.assertTrue(embedding_requests[0]["body"]["input"][0].startswith("query-prefix::"))
            self.assertTrue(embedding_requests[1]["body"]["input"][0].startswith("document-prefix::"))
            self.assertEqual(len(rerank_requests), 1)
            self.assertEqual(rerank_requests[0]["authorization"], "Bearer rerank-secret")
            self.assertNotIn("embedding-secret", embedding.stdout)
            self.assertNotIn("query-prefix::", embedding.stdout)
            self.assertNotIn("document-prefix::", embedding.stdout)
            self.assertNotIn("rerank-secret", reranker.stdout)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_reranker_benchmark_fails_before_network_on_unresolved_key_indirection(self) -> None:
        wrapper = ROOT / ".harness" / "bin" / "awoki-runtime-env"
        benchmark = ROOT / ".harness" / "bin" / "reranker-benchmark"
        with tempfile.TemporaryDirectory() as tmp:
            runtime_env = Path(tmp) / "runtime.env"
            runtime_env.write_text(
                "\n".join([
                    "export AWOKI_MODE=container-opencode",
                    "export AWOKI_ROOT=/awoki",
                    "export AWOKI_RERANK_ENABLED=1",
                    "export AWOKI_RERANK_PROVIDER=tei",
                    "export AWOKI_RERANK_URL=http://reranker.example.invalid:8000/rerank",
                    "export AWOKI_RERANK_API_KEY_ENV=AWOKI_TEST_MISSING_RERANK_SECRET",
                ]) + "\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["AWOKI_RUNTIME_ENV_FILE"] = str(runtime_env)
            completed = subprocess.run(
                [str(wrapper), "--profile", "retrieval", "--", str(benchmark), "--json"],
                cwd=ROOT, env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(completed.returncode, 1, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "failed")
            self.assertIn("unavailable", payload["reason"])
            self.assertFalse(payload["auth_configured"])

    def test_open_lavish_reads_nonsecret_port_from_dotenv_without_sourcing_it(self) -> None:
        script = (ROOT / "open-lavish.sh").read_text(encoding="utf-8")
        self.assertIn("AWOKI_LAVISH_PORT", script)
        self.assertIn('awk -F=', script)
        self.assertNotIn('source "$ROOT/.env"', script)
        self.assertNotIn('. "$ROOT/.env"', script)

    def test_shell_tool_profiles_minimize_retrieval_secret_inheritance_for_burp_and_lavish(self) -> None:
        lavish = (ROOT / ".opencode" / "skills" / "lavish-review" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("awoki-runtime-env --profile lavish", lavish)
        self.assertIn("Do not use `retrieval`/internal `mcp`/`all`", lavish)
        burp = (ROOT / ".opencode" / "skills" / "burp-workflow" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("awoki-runtime-env --profile burp", burp)
        self.assertIn("Live Burp remains the direct `mcp.burp`", burp)
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("not a same-user sandbox", agents)

    def test_repo_aware_mcp_tools_actually_use_repo_parameter(self) -> None:
        import ast

        tree = ast.parse((ROOT / ".harness" / "server.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = [*node.args.args, *node.args.kwonlyargs]
            if not any(arg.arg == "repo" for arg in args):
                continue
            repo_loads = [
                child for child in ast.walk(node)
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id == "repo"
            ]
            self.assertTrue(repo_loads, f"MCP wrapper {node.name} declares repo but never uses it")

    def test_project_refresh_offloads_long_work_from_mcp_event_loop(self) -> None:
        import ast
        tree = ast.parse((ROOT / ".harness" / "server.py").read_text(encoding="utf-8"))
        node = next(
            item for item in tree.body
            if isinstance(item, ast.AsyncFunctionDef) and item.name == "project_refresh"
        )
        calls = [child for child in ast.walk(node) if isinstance(child, ast.Call)]
        self.assertTrue(any(
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "asyncio"
            and call.func.attr == "to_thread"
            for call in calls
        ), "project_refresh must not block the MCP event loop with synchronous indexing")

    def test_opencode_plugin_injects_session_for_every_session_scoped_mcp_tool(self) -> None:
        import ast

        server_path = ROOT / ".harness" / "server.py"
        tree = ast.parse(server_path.read_text(encoding="utf-8"))
        session_scoped: set[str] = set()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_mcp_tool = any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "mcp"
                and decorator.func.attr == "tool"
                for decorator in node.decorator_list
            )
            arguments = [*node.args.args, *node.args.kwonlyargs]
            if is_mcp_tool and any(argument.arg == "session_id" for argument in arguments):
                session_scoped.add(node.name)

        plugin = (ROOT / ".opencode" / "plugins" / "awoki-continuity.ts").read_text(encoding="utf-8")
        match = re.search(
            r"const sessionAwareTools = new Set\(\[(.*?)\]\)",
            plugin,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        registered = set(re.findall(r'"([A-Za-z0-9_]+)"', match.group(1)))
        self.assertEqual(registered, session_scoped)

        maintenance_match = re.search(
            r"const continuityMaintenanceTools = new Set\(\[(.*?)\]\)",
            plugin,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(maintenance_match)
        maintenance = set(re.findall(r'"([A-Za-z0-9_]+)"', maintenance_match.group(1)))
        for tool in {
            "codebase_search",
            "code_index_status",
            "code_definition",
            "code_callers",
            "code_callees",
            "code_path",
            "code_validate_claim",
        }:
            self.assertIn(tool, maintenance)

    def test_agent_guidance_routes_mcp_tools_without_invented_cli(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        skill = (ROOT / ".opencode" / "skills" / "project-continuity" / "SKILL.md").read_text(encoding="utf-8")
        index_command = (ROOT / ".opencode" / "commands" / "project.md").read_text(encoding="utf-8")
        retrieval_command = ROOT / ".opencode" / "commands" / "retrieval-status.md"
        for text in (agents, skill, index_command):
            self.assertIn("MCP", text)
            self.assertIn("awoki_project_refresh", text)
            self.assertIn("Bash", text)
        self.assertTrue(retrieval_command.is_file())
        retrieval_text = retrieval_command.read_text(encoding="utf-8")
        self.assertIn("retrieval_status", retrieval_text)
        self.assertIn("code_index_status", retrieval_text)
        self.assertIn("dedicated code collection", retrieval_text)
        self.assertIn("Never print API keys", retrieval_text)
        self.assertIn("code_validate_claim", agents)
        self.assertIn("cross_project_code_search", agents)
        server = (ROOT / ".harness" / "server.py").read_text(encoding="utf-8")
        self.assertIn('"qdrant_code_collection": code_collection_name()', server)
        root_harness = (ROOT / "HARNESS.md").read_text(encoding="utf-8")
        self.assertIn("content-addressed code-vector collections", root_harness)

    def test_runtime_dependencies_are_locked_and_runtime_check_repairs_snapshot(self) -> None:
        lock = json.loads((ROOT / ".harness" / "runtime-dependencies.lock.json").read_text(encoding="utf-8"))
        critical = lock["critical_runtime"]
        self.assertEqual(critical["opencode_cli"]["default_channel"], "latest")
        self.assertEqual(critical["opencode_plugin_api"]["install_policy"], "match-resolved-opencode-cli")
        self.assertEqual(critical["opencode_sdk_api"]["install_policy"], "match-resolved-opencode-cli")
        self.assertEqual(critical["lavish_axi"]["version"], "0.1.43")
        self.assertEqual(critical["qdrant_server"]["image"], "qdrant/qdrant:v1.18.2")
        dockerfile = (ROOT / "Dockerfile.opencode").read_text(encoding="utf-8")
        self.assertIn("ARG OPENCODE_INSTALL_MODE=latest", dockerfile)
        self.assertIn('latest) opencode_spec="opencode-ai@latest"', dockerfile)
        self.assertIn('opencode_spec="opencode-ai@${OPENCODE_SAFE_VERSION}"', dockerfile)
        self.assertIn("OPENCODE_DISABLE_AUTOUPDATE=1", dockerfile)
        self.assertIn('channel_state="latest_untested"', dockerfile)
        plugin = (ROOT / ".opencode" / "plugins" / "awoki-continuity.ts").read_text(encoding="utf-8")
        self.assertIn("compatibility is resolved at image build", plugin)
        plugin_package = json.loads((ROOT / ".opencode" / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin_package["dependencies"]["@opencode-ai/plugin"], "latest")
        self.assertEqual(plugin_package["dependencies"]["@opencode-ai/sdk"], "latest")
        self.assertTrue((ROOT / ".harness" / "bin" / "opencode-runtime-compat-check").exists())
        entrypoint = (ROOT / ".harness" / "bin" / "opencode-ssh-entrypoint").read_text(encoding="utf-8")
        self.assertIn("/awoki/.harness/bin/opencode-runtime-compat-check", entrypoint)
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("dependencies-check:", makefile)
        self.assertIn("OPENCODE_INSTALL_MODE ?= latest", makefile)
        self.assertIn("OPENCODE_SAFE_VERSION ?=", makefile)
        up_target = makefile[makefile.index("opencode-ssh-up:"):makefile.index("opencode-ssh-down:")]
        self.assertNotIn("opencode-ssh-build", up_target)
        recreate = (ROOT / ".harness" / "bin" / "recreate-opencode-runtime").read_text(encoding="utf-8")
        self.assertIn("--opencode-latest", recreate)
        self.assertIn("--opencode-safe VERSION", recreate)
        target = makefile[makefile.index("opencode-runtime-check:"):makefile.index("runtime-config:")]
        self.assertIn("-u root", target)
        self.assertIn("/awoki/.harness/bin/awoki-runtime-snapshot", target)
        self.assertLess(target.index("awoki-runtime-snapshot"), target.index("awoki-runtime-env"))
        completed = subprocess.run(
            [sys.executable, str(ROOT / ".harness" / "check_runtime_dependencies.py")],
            cwd=ROOT, text=True, capture_output=True, check=False, timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("awoki_runtime_dependencies=ok", completed.stdout)

    def test_repository_readiness_skill_is_guarded_and_parent_job_owned(self) -> None:
        skill = (ROOT / ".opencode" / "skills" / "repository-readiness" / "SKILL.md").read_text(encoding="utf-8")
        project_command = (ROOT / ".opencode" / "commands" / "project.md").read_text(encoding="utf-8")
        project_skill = (ROOT / ".opencode" / "skills" / "project-continuity" / "SKILL.md").read_text(encoding="utf-8")
        for required in (
            "repository_prepare_start", "repository_prepare_status", "repository_prepare_cancel",
            "LOCAL_READY", "FULL_READY", "PREPARATION_RUNNING", "CONFIGURATION_BLOCKED",
            "MANAGED_SCOPE_REQUIRED", 'mode="full"', 'mode="local"',
            "parent job", "best-effort", "Never clone", "Never print API keys",
            "OpenCode TODO", "ad-hoc",
        ):
            self.assertIn(required, skill)
        self.assertIn("repository-readiness", project_command)
        self.assertIn("repository_prepare_start", project_command)
        self.assertIn("repository-readiness", project_skill)
        self.assertIn("repository_prepare", project_skill)

    def test_repository_readiness_continuation_is_optional_best_effort_and_todo_visible(self) -> None:
        skill = (ROOT / ".opencode" / "skills" / "repository-readiness" / "SKILL.md").read_text(encoding="utf-8")
        plugin = (ROOT / ".opencode" / "plugins" / "awoki-continuity.ts").read_text(encoding="utf-8")
        for required in (
            "project_continuation_schedule", "project_continuation_status", "project_continuation_finalize",
            "todowrite", "best-effort", "not a correctness dependency", "unattached",
            "different project", "MANAGED_SCOPE_REQUIRED", "repository_prepare_status",
        ):
            self.assertIn(required, skill)
        for required in (
            "continuation-pending", "continuation-poll", "continuation-claim",
            "client.session.prompt", "session.idle", "scope_conflict", "todowrite",
            "lease_until", 'status === "claimed"', "Best-effort continuation prompt failed",
            "repository_prepare_status",
        ):
            self.assertIn(required, plugin)
        self.assertNotIn("setInterval", plugin)
        for cfg_name in ("opencode.jsonc", "opencode.container.jsonc"):
            config = (ROOT / cfg_name).read_text(encoding="utf-8")
            self.assertIn('"todowrite": "allow"', config)

    def test_compaction_continuity_persists_todos_and_structured_acceptance_without_system_injection(self) -> None:
        plugin = (ROOT / ".opencode" / "plugins" / "awoki-continuity.ts").read_text(encoding="utf-8")
        bridge = (ROOT / ".harness" / "opencode_events.py").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        skill = (ROOT / ".opencode" / "skills" / "project-continuity" / "SKILL.md").read_text(encoding="utf-8")
        manifest = json.loads((ROOT / ".harness" / "manifest.json").read_text(encoding="utf-8"))
        for required in ("todo.updated", "message.updated", "message.part.updated", "session.compacted", "compaction-trigger", "part.auto", "automatic_context_pressure", "explicit_request", "todo-sync", "user-turn", "agent-turn-terminal", "acceptance-tool", "new Blob"):
            self.assertIn(required, plugin)
        self.assertNotIn("experimental.chat.system.transform", plugin)
        self.assertNotIn("output.system.push", plugin)
        self.assertIn("Awoki execution invariants", bridge)
        self.assertIn("work_ledger.compact_context", bridge)
        self.assertIn("acceptance_runs.compact_context", bridge)
        self.assertIn("acceptance-tool", bridge)
        for tool in (
            "session_work_status", "session_runtime_status", "harness_self_check", "reference_describe", "reference_annotate", "reference_resolve",
            "acceptance_run_start", "acceptance_run_status", "acceptance_run_next",
            "acceptance_evidence_get", "acceptance_run_record", "acceptance_run_record_invariant",
            "acceptance_run_finalize",
        ):
            self.assertIn(tool, manifest["tools"]["projects"])
            self.assertIn(tool, plugin)
        self.assertIn("newest user instruction", agents)
        self.assertIn("acceptance_run_status", skill)
        self.assertIn("acceptance_evidence_get", skill)
        self.assertIn("capture_evidence=true", skill)
        self.assertIn("canonical `candidate_ids`", skill)
        self.assertIn("content-addressed", skill)
        self.assertIn("running` and resumable", skill)
        self.assertIn("recorded observations", skill)
        self.assertIn("required_interfaces", skill)
        self.assertIn("pass_requirements", skill)
        self.assertIn("current_acceptance_run", skill)
        self.assertIn("tool_execution_without_followup", skill)
        self.assertIn('clean = clean.replace(/^awoki[.:_-]/i, "")', plugin)
        self.assertIn("acceptanceObservableOrchestrationTools", plugin)
        self.assertIn("acceptanceControlTools", plugin)

    def test_bounded_self_verification_contract_is_exposed_without_rigidifying_semantics(self) -> None:
        manifest = json.loads((ROOT / ".harness" / "manifest.json").read_text(encoding="utf-8"))
        server = (ROOT / ".harness" / "server.py").read_text(encoding="utf-8")
        skill = (ROOT / ".opencode" / "skills" / "reliability-verification" / "SKILL.md").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "RELIABILITY.md").read_text(encoding="utf-8")
        for tool in (
            "reliability_record_assessment",
            "reliability_record_relation",
            "reliability_consume_corrective_budget",
            "reliability_verification_checkpoint",
            "reliability_aggregate_verdict",
        ):
            self.assertIn(tool, manifest["tools"]["reliability"])
            self.assertIn(f"def {tool}", server)
            self.assertIn(tool, skill)
        for semantic_kind in ("claim", "hypothesis", "observation", "question", "contradiction", "gap", "decision", "note"):
            self.assertIn(semantic_kind, docs)
        self.assertIn("structured spine", skill)
        self.assertIn("at most **one**", skill)
        self.assertIn("does **not** turn model inference into machine proof", skill)
        self.assertIn("reranker_complete", skill)
        self.assertIn("single_evidence_scope", docs)
        for result in ("VERIFIED", "VERIFIED_WITH_FINDINGS", "INCOMPLETE", "CONTRADICTED", "BLOCKED", "NOT_APPLICABLE"):
            self.assertIn(result, docs)
        self.assertIn("corrective_budget", server)
        self.assertIn("required_properties", server)

    def test_opencode_recreate_helper_rebuilds_baked_source_without_recreating_qdrant(self) -> None:
        helper = ROOT / ".harness" / "bin" / "recreate-opencode-runtime"
        self.assertTrue(helper.exists())
        self.assertTrue(helper.stat().st_mode & 0o111)
        text = helper.read_text(encoding="utf-8")
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        launcher = (ROOT / ".harness" / "bin" / "run-opencode-ssh").read_text(
            encoding="utf-8"
        )
        self.assertIn("docker compose -f docker-compose.opencode.yml build", text)
        self.assertIn("AWOKI_OPENCODE_FORCE_RECREATE=1", text)
        self.assertIn('"$ROOT/.harness/bin/run-opencode-ssh"', text)
        self.assertIn("opencode_up+=(--force-recreate)", launcher)
        self.assertIn("opencode_up+=(awoki-opencode-ssh)", launcher)
        self.assertNotIn("--force-recreate qdrant", text + launcher)
        self.assertIn("make opencode-runtime-check", text)
        self.assertIn("AWOKI_CODE_RERANK_TIMEOUT_SECONDS=5", text)
        self.assertIn("opencode-recreate:", makefile)
        self.assertIn("recreate-opencode-runtime", makefile)

    def test_public_install_docs_keep_release_identity_out_of_intro_copy(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        install = (ROOT / "INSTALL.txt").read_text(encoding="utf-8")
        readme_intro = "\n".join(readme.splitlines()[:16])
        install_intro = "\n".join(install.splitlines()[:10])
        semver = re.compile(r"\bv\d+\.\d+\.\d+\b")
        self.assertIsNone(semver.search(readme_intro))
        self.assertIsNone(semver.search(install_intro))
        self.assertIn("pyproject.toml", readme_intro)
        self.assertIn(".harness/manifest.json", readme_intro)
        self.assertIn("CHANGELOG.md", readme_intro)
        self.assertIn("pyproject.toml", install_intro)
        self.assertIn(".harness/manifest.json", install_intro)
        self.assertIn("CHANGELOG.md", install_intro)

    def test_code_reranker_timeout_defaults_to_shared_profile_in_new_runtime(self) -> None:
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.opencode.yml").read_text(encoding="utf-8")
        compose_mcp = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("AWOKI_CODE_RERANK_TIMEOUT_SECONDS=\n", env_example)
        self.assertIn("AWOKI_CODE_RERANK_TIMEOUT_SECONDS: ${AWOKI_CODE_RERANK_TIMEOUT_SECONDS:-}", compose)
        self.assertIn("AWOKI_CODE_RERANK_TIMEOUT_SECONDS: ${AWOKI_CODE_RERANK_TIMEOUT_SECONDS:-}", compose_mcp)
        self.assertIn("inherits the shared `AWOKI_RERANK_TIMEOUT_SECONDS`", readme)
        self.assertIn("historical stock 5-second", readme)

    def test_pre_structural_baseline_is_frozen_and_honest(self) -> None:
        baseline_path = (
            ROOT
            / ".harness"
            / "evaluation"
            / "code_search"
            / "baselines"
            / "pre-structural-98f5431.json"
        )
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        self.assertEqual(
            baseline["source_commit_full"],
            "98f543146d3a3e7239488f263497175023874629",
        )
        self.assertEqual(
            baseline["fixture"]["sha256"],
            "8e250f0e305965ce6ee1c2474301122b855bf48d1b3c3225d6ca026360eaec93",
        )
        self.assertEqual(baseline["metrics"]["positive_file_hit_at_1"], 1.0)
        self.assertEqual(baseline["metrics"]["no_answer_accuracy"], 0.0)
        no_answer = next(row for row in baseline["queries"] if row["id"] == "no-answer")
        self.assertTrue(no_answer["expected_no_answer"])
        self.assertEqual(len(no_answer["hits"]), 1)
        self.assertEqual(
            no_answer["hits"][0]["source_path"],
            "workspace/projects/webhook/repo/src/webhook_worker.py",
        )
        docs = (ROOT / "docs" / "CODE_SEARCH_EVALUATION.md").read_text(encoding="utf-8")
        self.assertIn("pre-structural-98f5431.json", docs)

    def test_tmux_check_smokes_configuration_without_brittle_option_assertions(self) -> None:
        check_path = ROOT / ".harness" / "bin" / "tmux-check"
        check = check_path.read_text(encoding="utf-8")
        self.assertIn('new-session -d -s "$session" -c /awoki', check)
        self.assertIn('has-session -t "$session"', check)
        self.assertIn('list-windows -t "$session"', check)
        self.assertNotIn("show-window-options", check)
        self.assertNotIn("show-options -gqv", check)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_tmux = fake_bin / "tmux"
            fake_tmux.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
args=" $* "
case "$args" in
  *" kill-server "*) exit 0 ;;
  *" new-session "*) exit 0 ;;
  *" has-session -t __awoki_config_check "*) exit 0 ;;
  *" list-windows -t __awoki_config_check -F #{window_id} "*) printf '@7\n' ;;
  *" show-options "*|*" show-window-options "*)
    printf 'semantic option assertions must not run during the smoke test\n' >&2
    exit 41
    ;;
  *) printf 'unexpected fake tmux invocation: %s\n' "$*" >&2; exit 1 ;;
esac
""",
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)
            main_conf = root / ".tmux.conf"
            local_conf = root / ".tmux.conf.local"
            main_conf.write_text("# test\n", encoding="utf-8")
            local_conf.write_text("# test\n", encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "HOME": str(root),
                    "TERM": "xterm-256color",
                    "AWOKI_TMUX_CONF": str(main_conf),
                    "AWOKI_TMUX_CONF_LOCAL": str(local_conf),
                }
            )
            completed = subprocess.run(
                [str(check_path)],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("configuration smoke test passed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
