from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import evidence_store
import jsonc
import rag_backend
import reference_catalog
import validate

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".harness/bin/awoki-ai-configure"


class ConfigurationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="awoki-config-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env_path = self.root / ".env"
        self.config_path = self.root / ".opencode-state/config/opencode.jsonc"
        self.ns = runpy.run_path(str(SCRIPT))
        self.globals = self.ns["main"].__globals__

    def invoke(self, *args, environment=None, stdin=""):
        output = io.StringIO()
        env = {"AWOKI_ROOT": str(self.root), **(environment or {})}
        with patch.dict(os.environ, env, clear=True), patch("sys.argv", [str(SCRIPT), "--non-interactive", *args]), patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(output):
            self.assertEqual(self.ns["main"](), 0)
        return output.getvalue()

    def initialize(self, *args):
        self.invoke("--base-url", "https://old.example/v1", *args, environment={"AWOKI_AI_API_KEY": "synthetic-secret"})

    def values(self):
        return self.ns["_read_env"](self.env_path)[1]

    def config(self):
        return json.loads(self.config_path.read_text())

    def test_key_rotation_is_key_only_and_preserves_disabled_reranker(self):
        self.initialize("--no-reranker")
        before_env, before_config = self.env_path.read_text(), self.config_path.read_bytes()
        output = self.invoke("--key-only", "--api-key-stdin", stdin="Bearer rotated-synthetic\n", environment={"AWOKI_AI_BASE_URL": "https://unexpected.example/v1", "AWOKI_AI_AUTH_MODE": "invalid"})
        self.assertEqual(self.env_path.read_text(), before_env.replace("synthetic-secret", "rotated-synthetic"))
        self.assertEqual(self.config_path.read_bytes(), before_config)
        self.assertEqual(self.values()["AWOKI_RERANK_ENABLED"], "0")
        self.assertNotIn("rotated-synthetic", output)

    def test_add_model_preserves_all_retrieval_and_provider_options(self):
        self.initialize("--no-reranker")
        before, provider = self.values(), self.config()["provider"]["custom-openai"]
        self.invoke("--add-chat-model", "--chat-model", "second", "--context", "32000", "--output", "4000", environment={"AWOKI_AI_BASE_URL": "invalid", "AWOKI_AI_AUTH_MODE": "invalid"})
        after = self.values()
        for key, value in before.items():
            if key not in {"AWOKI_OPENCODE_MODEL", "AWOKI_OPENCODE_MODEL_NAME", "AWOKI_OPENCODE_CONTEXT", "AWOKI_OPENCODE_OUTPUT"}:
                self.assertEqual(after[key], value, key)
        actual = self.config()["provider"]["custom-openai"]
        self.assertEqual(actual["options"], provider["options"])
        self.assertEqual(actual["models"]["qwen3.8-27b"], provider["models"]["qwen3.8-27b"])
        self.assertEqual(actual["models"]["second"]["limit"], {"context": 32000, "output": 4000})

    def test_endpoint_change_rederives_linked_reranker_and_embedding_identity(self):
        self.initialize()
        self.invoke("--base-url", "https://new.example/v1", "--embedding-model", "new-embedding")
        values = self.values()
        self.assertEqual(values["AWOKI_RERANK_URL"], "https://new.example/rerank")
        self.assertEqual(values["AWOKI_EMBEDDING_DEPLOYMENT_ID"], "new-embedding")

    def test_fresh_installer_env_uses_custom_http_reranker_not_native_tei(self):
        self.env_path.write_text((ROOT / ".env.example").read_text())
        self.initialize("--enable-reranker")
        self.assertEqual(self.values()["AWOKI_RERANK_PROVIDER"], "http")
        self.assertEqual(self.values()["AWOKI_RERANK_MODEL"], "bge-reranker")

    def test_existing_native_tei_format_is_preserved_by_configuration_and_rotation(self):
        self.initialize("--rerank-provider", "tei")
        self.invoke("--key-only", "--api-key-stdin", stdin="new-synthetic\n")
        self.assertEqual(self.values()["AWOKI_RERANK_PROVIDER"], "tei")
        self.invoke("--context", "200000")
        self.assertEqual(self.values()["AWOKI_RERANK_PROVIDER"], "tei")

    def test_interactive_changes_refresh_linked_defaults_and_accept_raw_authorization(self):
        self.initialize()

        class Terminal(io.StringIO):
            def isatty(self):
                return True

        def answer(prompt):
            for prefix, value in (
                ("Custom provider base URL", "https://interactive.example/v1"),
                ("Embedding model ID", "interactive-embedding"),
                ("Authentication mode", "header"),
                ("Custom API-key header name", "authorization"),
            ):
                if prompt.startswith(prefix):
                    return value
            return ""

        with patch.dict(os.environ, {"AWOKI_ROOT": str(self.root)}, clear=True), patch("sys.argv", [str(SCRIPT)]), patch("sys.stdin", Terminal()), redirect_stdout(Terminal()), patch("builtins.input", side_effect=answer), patch("getpass.getpass", return_value=""):
            self.assertEqual(self.ns["main"](), 0)
        values = self.values()
        self.assertEqual(values["AWOKI_RERANK_URL"], "https://interactive.example/rerank")
        self.assertEqual(values["AWOKI_EMBEDDING_DEPLOYMENT_ID"], "interactive-embedding")
        self.assertEqual(values["AWOKI_AI_AUTH_HEADER"], "Authorization")

    def test_explicit_separate_reranker_is_preserved(self):
        self.initialize("--rerank-url", "https://separate.example/rank")
        self.invoke("--base-url", "https://new.example/v1")
        self.assertEqual(self.values()["AWOKI_RERANK_URL"], "https://separate.example/rank")

    def test_provider_rename_preserves_models_and_default_references(self):
        self.initialize()
        self.invoke("--add-chat-model", "--chat-model", "second")
        config = self.config()
        config["model"] = "custom-openai/second"
        config["agent"] = {"build": {"model": "custom-openai/qwen3.8-27b"}}
        self.config_path.write_text(json.dumps(config))
        self.invoke("--provider-id", "renamed")
        actual = self.config()
        self.assertEqual(set(actual["provider"]), {"renamed"})
        self.assertEqual(set(actual["provider"]["renamed"]["models"]), {"qwen3.8-27b", "second"})
        self.assertEqual(actual["model"], "renamed/second")
        self.assertEqual(actual["agent"]["build"]["model"], "renamed/qwen3.8-27b")

    def test_auth_switch_deduplicates_case_insensitively(self):
        self.initialize()
        config = self.config()
        config["provider"]["custom-openai"]["options"]["headers"]["authorization"] = "old-header"
        self.config_path.write_text(json.dumps(config))
        self.invoke("--auth-mode", "header", "--auth-header", "X-API-Key")
        headers = self.config()["provider"]["custom-openai"]["options"]["headers"]
        self.assertEqual({key.lower() for key in headers}, {"x-api-key", "user-agent"})
        self.invoke("--auth-mode", "header", "--auth-header", "authorization")
        self.assertEqual(self.values()["AWOKI_AI_AUTH_HEADER"], "Authorization")

    def test_second_replace_failure_restores_both_documents(self):
        self.initialize()
        before = (self.env_path.read_bytes(), self.config_path.read_bytes())
        replace = os.replace
        failed = False

        def fail_once(source, target):
            nonlocal failed
            if Path(target) == self.config_path and not failed:
                failed = True
                raise OSError("injected write failure")
            return replace(source, target)

        with patch.object(os, "replace", side_effect=fail_once), self.assertRaisesRegex(ValueError, "previous files restored"):
            self.invoke("--base-url", "https://new.example/v1")
        self.assertEqual((self.env_path.read_bytes(), self.config_path.read_bytes()), before)

    def test_reload_failure_rolls_back_files_and_attempts_previous_runtime(self):
        self.initialize()
        before = (self.env_path.read_bytes(), self.config_path.read_bytes())
        with patch.dict(self.globals, {"_reload": unittest.mock.Mock(side_effect=[RuntimeError("failed"), None])}):
            reload = self.globals["_reload"]
            with self.assertRaisesRegex(ValueError, "previous files restored"):
                self.invoke("--base-url", "https://new.example/v1", "--reload")
            self.assertEqual(reload.call_count, 2)
        self.assertEqual((self.env_path.read_bytes(), self.config_path.read_bytes()), before)

    def test_dry_run_writes_nothing_and_does_not_disclose_key(self):
        output = self.invoke("--dry-run", "--reload", environment={"AWOKI_AI_API_KEY": "synthetic-secret"})
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertNotIn("synthetic-secret", output)
        self.assertIn("dry_run", output)

    def test_config_files_are_private_even_when_old_mode_is_444(self):
        self.initialize()
        self.env_path.chmod(0o444)
        self.config_path.chmod(0o444)
        self.invoke("--base-url", "https://new.example/v1")
        self.assertEqual(self.env_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)

    def test_invalid_limits_do_not_modify_files(self):
        self.initialize()
        before = self.env_path.read_bytes(), self.config_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "output limit"):
            self.invoke("--context", "100", "--output", "101")
        self.assertEqual((self.env_path.read_bytes(), self.config_path.read_bytes()), before)

    def test_all_jsonc_readers_agree_and_do_not_join_tokens(self):
        text = '{/* comment */"url":"https://example.test/a,}","items":[1,2,],}'
        expected = {"url": "https://example.test/a,}", "items": [1, 2]}
        for strip in (jsonc.strip_jsonc, validate._strip_jsonc, self.ns["_strip_jsonc"]):
            self.assertEqual(json.loads(strip(text)), expected)
            with self.assertRaises(ValueError):
                json.loads(strip('{"bad":1/* separator */2}'))
            with self.assertRaises(ValueError):
                strip('{/* unclosed')

    def test_installer_calls_shared_compiler_instead_of_legacy_model_prompt(self):
        installer = (ROOT / "install-awoki.sh").read_text()
        self.assertIn('"$ROOT/.harness/bin/awoki-ai-configure"', installer)
        self.assertNotIn('prompt_value "Embedding deployment/model identity"', installer)

    def test_reload_does_not_report_success_when_docker_state_is_unavailable(self):
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        docker = fake_bin / "docker"
        docker.write_text('#!/bin/sh\nexit "${AWOKI_TEST_DOCKER_EXIT:-0}"\n')
        docker.chmod(0o755)
        for docker_exit, success in (("0", True), ("7", False)):
            completed = subprocess.run(
                ["/usr/bin/make", "-f", str(ROOT / "Makefile"), "-o", "opencode-user-config-check", "opencode-config-reload"],
                cwd=self.root,
                env={"PATH": str(fake_bin) + ":/usr/bin:/bin", "AWOKI_TEST_DOCKER_EXIT": docker_exit},
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(completed.returncode == 0, success, completed.stderr)


class EvidenceChronologyTests(unittest.TestCase):
    def test_capture_order_survives_changed_mtimes_and_legacy_ties_are_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kwargs = {"tool": "codebase_search", "scope_identity": {}}
            hit = {"path": "app.go", "start_line": 1, "symbol_name": "Authenticate"}
            with patch.object(evidence_store, "_now", return_value="2026-09-10T12:00:00.000Z"):
                first = evidence_store.put(root, "demo", kind="one", payload={"hits": [hit]}, **kwargs)
                second = evidence_store.put(root, "demo", kind="two", payload={"hits": [hit]}, **kwargs)
            candidate = first["candidate_index"][0]["candidate_id"]
            os.utime(evidence_store._path(root, "demo", first["evidence_ref"]), ns=(2000000000, 2000000000))
            os.utime(evidence_store._path(root, "demo", second["evidence_ref"]), ns=(1000000000, 1000000000))
            found = reference_catalog._candidate_object(root, "demo", candidate)
            self.assertEqual(found["first_materialized_in"], first["evidence_ref"])
            original = evidence_store.metadata

            def legacy(*args):
                meta = original(*args)
                meta.pop("created_at_ns", None)
                return meta

            with patch.object(evidence_store, "metadata", side_effect=legacy):
                found = reference_catalog._candidate_object(root, "demo", candidate)
            self.assertEqual(found["first_materialized_in"], "")
            self.assertTrue(found["first_materialization_ambiguous"])


class ProviderErrorSafetyTests(unittest.TestCase):
    def setUp(self):
        for name in ("_LAST_EMBEDDING_ERROR", "_LAST_RERANK_ERROR"):
            self.addCleanup(setattr, rag_backend, name, getattr(rag_backend, name))

    def test_provider_error_bodies_never_reach_status_fallback_hits_or_raised_error(self):
        secret = "synthetic-echoed-secret"
        environment = {"AWOKI_EMBEDDING_PROVIDER": "openai", "AWOKI_RERANK_ENABLED": "1", "AWOKI_RERANK_PROVIDER": "http", "AWOKI_RERANK_URL": "https://synthetic.invalid/rerank"}
        with patch.dict(os.environ, environment, clear=True), patch.object(rag_backend, "_openai_embed_texts", side_effect=RuntimeError(secret)), patch.object(rag_backend, "_remote_rerank_scores", side_effect=RuntimeError(secret)):
            with self.assertRaises(RuntimeError) as caught:
                rag_backend.embed_texts(["synthetic"])
            self.assertNotIn(secret, str(caught.exception))
            hits = rag_backend.rerank_hits("synthetic", [{"preview": "synthetic"}])
            self.assertNotIn(secret, str(hits))
            self.assertNotIn(secret, str(rag_backend.retrieval_status_snapshot()))

    @unittest.skipUnless(importlib.util.find_spec("openai"), "OpenAI SDK not installed on this host")
    def test_actual_embedding_sdk_has_exactly_one_raw_authorization_header(self):
        import httpx
        import openai
        requests = []

        def transport(request):
            requests.append(request)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}], "model": "synthetic", "usage": {"prompt_tokens": 1, "total_tokens": 1}})

        original = openai.OpenAI
        def client(**kwargs):
            return original(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(transport)))

        with patch.dict(os.environ, {"AWOKI_EMBEDDING_PROVIDER": "openai", "AWOKI_EMBEDDING_BASE_URL": "https://synthetic.invalid/v1", "AWOKI_EMBEDDING_API_KEY": "synthetic-token", "AWOKI_AI_AUTH_MODE": "header", "AWOKI_AI_AUTH_HEADER": "authorization"}, clear=True), patch.object(openai, "OpenAI", side_effect=client):
            rag_backend.embed_texts(["synthetic"])
        self.assertEqual(requests[0].headers.get_list("authorization"), ["synthetic-token"])


if __name__ == "__main__":
    unittest.main()
