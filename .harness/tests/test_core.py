from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rag_backend
import runtime_safety
from code_search import vector_store

@contextmanager
def patched_env(**updates):
    old = {k: os.environ.get(k) for k in updates}
    try:
        for k, v in updates.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

from harness_core import (
    HarnessPaths,
    approve_promotion,
    classify_memory_text,
    demote_global_memory,
    list_promotion_candidates,
    propose_promotion,
    recall_context,
    save_global_fact,
    save_project_fact,
    save_finding,
    save_hypothesis,
    search_global_memory,
    search_code,
    search_skills,
    open_artifact,
    collect_project_documents,
    index_project,
    index_global,
    project_create,
    search_rag,
)


class CoreTests(unittest.TestCase):

    def test_repository_subprocess_environment_strips_credentials_and_execution_overrides(self):
        source = {
            "HOME": "/home/op",
            "PATH": "/usr/bin:/bin",
            "AWOKI_EMBEDDING_API_KEY": "embed-secret",
            "AWOKI_EMBEDDING_BASE_URL": "https://user:pass@example.invalid/v1?token=x",
            "AWOKI_QDRANT_URL": "http://qdrant:6333",
            "AWOKI_BURP_URL": "http://burp:9876",
            "AWOKI_RERANK_API_KEY": "rerank-secret",
            "AWOKI_RERANK_API_KEY_ENV": "CUSTOM_RERANK_SECRET",
            "CUSTOM_RERANK_SECRET": "indirect-secret",
            "OPENAI_API_KEY": "openai-secret",
            "GITHUB_TOKEN": "gh-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "GIT_SSH_COMMAND": "evil-helper",
            "PYTHONPATH": "/tmp/inject",
            "LD_PRELOAD": "/tmp/inject.so",
            "SAFE_VALUE": "kept",
        }
        cleaned = runtime_safety.credential_free_environment(source)
        self.assertEqual(cleaned["HOME"], "/home/op")
        self.assertEqual(cleaned["PATH"], "/usr/bin:/bin")
        self.assertEqual(cleaned["SAFE_VALUE"], "kept")
        for key in (
            "AWOKI_EMBEDDING_API_KEY", "AWOKI_EMBEDDING_BASE_URL", "AWOKI_QDRANT_URL", "AWOKI_BURP_URL",
            "AWOKI_RERANK_API_KEY", "AWOKI_RERANK_API_KEY_ENV",
            "CUSTOM_RERANK_SECRET", "OPENAI_API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY",
            "SSH_AUTH_SOCK", "GIT_SSH_COMMAND", "PYTHONPATH", "LD_PRELOAD",
        ):
            self.assertNotIn(key, cleaned)

    def test_opencode_compose_does_not_mount_noexec_tmpfs_over_tmp(self):
        root = Path(__file__).resolve().parents[2]
        compose = (root / "docker-compose.opencode.yml").read_text(encoding="utf-8")
        self.assertNotRegex(compose, r"(?m)^\s*-\s*/tmp(?:\s|:|$)")

    def test_opencode_entrypoint_probes_tmp_execution(self):
        root = Path(__file__).resolve().parents[2]
        entrypoint = (root / ".harness/bin/opencode-ssh-entrypoint").read_text(encoding="utf-8")
        self.assertIn('/tmp/.awoki-exec-probe', entrypoint)
        self.assertIn('OpenTUI cannot load its native render library', entrypoint)

    def test_opencode_account_has_no_placeholder_password(self):
        root = Path(__file__).resolve().parents[2]
        dockerfile = (root / "Dockerfile.opencode").read_text(encoding="utf-8")
        self.assertNotIn('chpasswd', dockerfile)
        self.assertNotIn('awoki-op-disabled', dockerfile)
        self.assertIn('passwd -d op', dockerfile)
        self.assertNotIn('sudo ', dockerfile)

    def test_embedding_defaults_to_remote_openai_compatible(self):
        with patched_env(AWOKI_EMBEDDING_PROVIDER=None, AWOKI_EMBEDDING_MODEL=None, AWOKI_QDRANT_COLLECTION=None):
            profile = rag_backend.embedding_profile()
            self.assertEqual(profile["provider"], "openai")
            self.assertEqual(profile["model"], "text-embeddings-inference")
            self.assertEqual(rag_backend.qdrant_collection_name(), "awoki_openai_text_embeddings_inference")

    def test_embedding_profile_reports_endpoint_without_exposing_key(self):
        with patched_env(
            AWOKI_EMBEDDING_PROVIDER="openai",
            AWOKI_EMBEDDING_MODEL="text-embeddings-inference",
            AWOKI_EMBEDDING_DEPLOYMENT_ID="jinaai/jina-embeddings-v2-base-code",
            AWOKI_EMBEDDING_BASE_URL="http://embedding.example.invalid:8000/v1",
            AWOKI_EMBEDDING_API_KEY=None,
            AWOKI_VECTOR_SIZE="768",
        ):
            profile = rag_backend.embedding_profile()
        self.assertTrue(profile["configuration_ready"])
        self.assertTrue(profile["endpoint_configured"])
        self.assertFalse(profile["auth_configured"])
        self.assertEqual(profile["base_url"], "http://embedding.example.invalid:8000/v1")
        self.assertEqual(profile["deployment_id"], "jinaai/jina-embeddings-v2-base-code")
        self.assertEqual(profile["explicit_vector_size"], 768)
        self.assertNotIn("api_key", profile)

    def test_openai_compatible_embedding_allows_empty_key_with_base_url(self):
        class EmbeddingItem:
            embedding = [3.0, 4.0]

        class Embeddings:
            def create(self, **kwargs):
                self.kwargs = kwargs
                return type("Response", (), {"data": [EmbeddingItem()]})()

        clients = []

        class FakeOpenAI:
            def __init__(self, *, api_key, base_url=None, timeout=None, max_retries=None):
                self.api_key = api_key
                self.base_url = base_url
                self.timeout = timeout
                self.max_retries = max_retries
                self.embeddings = Embeddings()
                clients.append(self)

        fake_openai = type("FakeOpenAIModule", (), {"OpenAI": FakeOpenAI})()
        cfg = rag_backend.EmbeddingConfig(
            provider="openai",
            model="text-embeddings-inference",
            batch_size=32,
            normalize=True,
            query_prefix="",
            document_prefix="",
            explicit_vector_size=768,
        )
        with patched_env(
            AWOKI_EMBEDDING_BASE_URL="http://embedding.example.invalid:8000/v1",
            AWOKI_EMBEDDING_API_KEY=None,
            AWOKI_OPENAI_API_KEY=None,
            OPENAI_API_KEY=None,
        ), mock.patch.dict(sys.modules, {"openai": fake_openai}):
            vectors = rag_backend._openai_embed_texts(["test"], cfg)
        self.assertEqual(clients[0].api_key, "awoki-local-endpoint")
        self.assertEqual(clients[0].base_url, "http://embedding.example.invalid:8000/v1")
        self.assertEqual(clients[0].timeout, 30.0)
        self.assertEqual(clients[0].max_retries, 1)
        self.assertEqual(vectors, [[0.6, 0.8]])

    def test_query_embedding_uses_short_no_retry_budget(self):
        class EmbeddingItem:
            embedding = [1.0, 0.0]

        class Embeddings:
            def create(self, **kwargs):
                return type("Response", (), {"data": [EmbeddingItem()]})()

        clients = []

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.embeddings = Embeddings()
                clients.append(self)

        fake_openai = type("FakeOpenAIModule", (), {"OpenAI": FakeOpenAI})()
        with patched_env(
            AWOKI_EMBEDDING_BASE_URL="http://embedding.example.invalid:8000/v1",
            AWOKI_EMBEDDING_API_KEY=None,
            AWOKI_OPENAI_API_KEY=None,
            OPENAI_API_KEY=None,
            AWOKI_EMBEDDING_QUERY_TIMEOUT_SECONDS=None,
            AWOKI_EMBEDDING_QUERY_MAX_RETRIES=None,
        ), mock.patch.dict(sys.modules, {"openai": fake_openai}):
            vector = rag_backend.embed_query("decision handler")
        self.assertEqual(vector, [1.0, 0.0])
        self.assertEqual(clients[0].kwargs["timeout"], 5.0)
        self.assertEqual(clients[0].kwargs["max_retries"], 0)

    def test_retrieval_status_snapshot_is_passive_and_does_not_probe_qdrant(self):
        original = dict(rag_backend._LAST_QDRANT_PROBE)
        try:
            rag_backend._LAST_QDRANT_PROBE = {
                "status": "not_probed",
                "available": None,
                "checked_at": "",
                "elapsed_ms": 0,
                "reason": "not probed",
            }
            with mock.patch.object(
                rag_backend, "qdrant_client", side_effect=AssertionError("passive status must not probe Qdrant")
            ):
                status = rag_backend.retrieval_status_snapshot()
            self.assertFalse(status["network_probe_performed"])
            self.assertEqual(status["qdrant_client"], "not_probed")
            self.assertIsNone(status["qdrant_available"])
            self.assertIn("use retrieval_probe", status["status_contract"])
        finally:
            rag_backend._LAST_QDRANT_PROBE = original

    def test_explicit_retrieval_probe_uses_bounded_qdrant_probe(self):
        captured = []
        def fake_client(timeout=5.0, verify=True):
            captured.append((timeout, verify))
            rag_backend._record_qdrant_probe(
                available=True, status="ok", reason="fixture", elapsed_ms=1
            )
            return object(), "ok"
        with mock.patch.object(rag_backend, "qdrant_client", side_effect=fake_client):
            result = rag_backend.probe_retrieval(
                probe_qdrant=True,
                probe_embedding=False,
                probe_reranker=False,
                timeout_seconds=1.5,
            )
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0][1])
        self.assertGreater(captured[0][0], 0.0)
        self.assertLessEqual(captured[0][0], 1.5)
        self.assertEqual(result["qdrant"]["status"], "ok")
        self.assertTrue(result["network_probe"])

    def test_native_tei_rerank_profile_allows_server_selected_model(self):
        with patched_env(
            AWOKI_RERANK_ENABLED="1",
            AWOKI_RERANK_PROVIDER="tei",
            AWOKI_RERANK_URL="http://reranker.example.invalid:8000/rerank",
            AWOKI_RERANK_MODEL="",
            AWOKI_RERANK_API_KEY=None,
            AWOKI_RERANK_API_KEY_ENV=None,
        ):
            profile = rag_backend.rerank_profile()
        self.assertTrue(profile["enabled"])
        self.assertTrue(profile["configuration_ready"])
        self.assertTrue(profile["endpoint_configured"])
        self.assertTrue(profile["server_selects_model"])
        self.assertEqual(profile["model"], "")
        self.assertFalse(profile["auth_configured"])

    def test_rerank_profile_fails_closed_when_indirect_key_is_missing(self):
        with patched_env(
            AWOKI_RERANK_ENABLED="1",
            AWOKI_RERANK_PROVIDER="tei",
            AWOKI_RERANK_URL="http://reranker.example.invalid:8000/rerank",
            AWOKI_RERANK_API_KEY=None,
            AWOKI_RERANK_API_KEY_ENV="AWOKI_TEST_MISSING_RERANK_SECRET",
            AWOKI_TEST_MISSING_RERANK_SECRET=None,
        ):
            profile = rag_backend.rerank_profile()
        self.assertFalse(profile["configuration_ready"])
        self.assertFalse(profile["auth_configured"])
        self.assertEqual(profile["auth_key_source"], "none")
        self.assertIn("unavailable", profile["auth_configuration_error"])

    def test_rerank_profile_resolves_indirect_key_when_present(self):
        with patched_env(
            AWOKI_RERANK_ENABLED="1",
            AWOKI_RERANK_PROVIDER="tei",
            AWOKI_RERANK_URL="http://reranker.example.invalid:8000/rerank",
            AWOKI_RERANK_API_KEY=None,
            AWOKI_RERANK_API_KEY_ENV="AWOKI_TEST_RERANK_SECRET",
            AWOKI_TEST_RERANK_SECRET="secret-value",
        ):
            profile = rag_backend.rerank_profile()
        self.assertTrue(profile["configuration_ready"])
        self.assertTrue(profile["auth_configured"])
        self.assertEqual(profile["auth_key_source"], "indirect")
        self.assertEqual(profile["auth_configuration_error"], "")

    def test_rerank_profile_rejects_invalid_indirect_key_name(self):
        with patched_env(
            AWOKI_RERANK_ENABLED="1",
            AWOKI_RERANK_PROVIDER="tei",
            AWOKI_RERANK_URL="http://reranker.example.invalid:8000/rerank",
            AWOKI_RERANK_API_KEY=None,
            AWOKI_RERANK_API_KEY_ENV="bad-name;echo",
        ):
            profile = rag_backend.rerank_profile()
        self.assertFalse(profile["configuration_ready"])
        self.assertIn("valid environment variable name", profile["auth_configuration_error"])

    def test_remote_rerank_does_not_call_network_with_unresolved_indirect_key(self):
        with patched_env(
            AWOKI_RERANK_ENABLED="1",
            AWOKI_RERANK_PROVIDER="tei",
            AWOKI_RERANK_URL="http://reranker.example.invalid:8000/rerank",
            AWOKI_RERANK_API_KEY=None,
            AWOKI_RERANK_API_KEY_ENV="AWOKI_TEST_MISSING_RERANK_SECRET",
            AWOKI_TEST_MISSING_RERANK_SECRET=None,
        ):
            profile = rag_backend.rerank_profile()
            fake_httpx = mock.Mock()
            with mock.patch.dict(sys.modules, {"httpx": fake_httpx}):
                with self.assertRaisesRegex(RuntimeError, "unavailable"):
                    rag_backend._remote_rerank_scores("query", ["document"], profile)
            fake_httpx.post.assert_not_called()

    def test_hash_embedding_is_explicit_fallback(self):
        with patched_env(AWOKI_EMBEDDING_PROVIDER="hash", AWOKI_VECTOR_SIZE="128"):
            vectors = rag_backend.embed_texts(["hello world"], is_query=False)
            self.assertEqual(len(vectors), 1)
            self.assertEqual(len(vectors[0]), 128)

    def test_merge_hits_uses_rrf_and_tracks_backends(self):
        fts = [{"retrieval_backend":"sqlite_fts", "id":"a", "title":"A", "score":0.1}]
        vec = [{"retrieval_backend":"qdrant", "id":"a", "title":"A", "score":0.9}]
        merged = rag_backend.merge_hits(fts, vec, limit=5)
        self.assertEqual(len(merged), 1)
        self.assertIn("sqlite_fts", merged[0]["retrieval_backends"])
        self.assertIn("qdrant", merged[0]["retrieval_backends"])
        self.assertIn("rrf_score", merged[0])

    def test_rerank_disabled_is_noop(self):
        with patched_env(AWOKI_RERANK_ENABLED="0"):
            hits = [{"id":"a", "title":"A", "score":1.0}, {"id":"b", "title":"B", "score":0.5}]
            self.assertEqual(rag_backend.rerank_hits("query", hits, limit=1), hits[:1])

    def test_remote_rerank_uses_http_result_and_falls_back(self):
        hits = [{"id":"a", "title":"A", "score":1.0}, {"id":"b", "title":"B", "score":0.5}]
        with patched_env(AWOKI_RERANK_ENABLED="1", AWOKI_RERANK_PROVIDER="http", AWOKI_RERANK_URL="http://rerank.test"):
            from unittest import mock
            with mock.patch.object(rag_backend, "_remote_rerank_scores", return_value=[(1, 0.9), (0, 0.1)]):
                ranked = rag_backend.rerank_hits("query", hits, limit=2)
                self.assertEqual([row["id"] for row in ranked], ["b", "a"])
                self.assertEqual(ranked[0]["rerank_backend"], "remote_http")
            with mock.patch.object(rag_backend, "_remote_rerank_scores", side_effect=RuntimeError("down")):
                fallback = rag_backend.rerank_hits("query", hits, limit=2)
                self.assertEqual([row["id"] for row in fallback], ["a", "b"])
                self.assertTrue(fallback[0]["rerank_fallback"])

    def test_rerank_timeout_override_caps_interactive_request(self):
        hits = [{"id": "a", "title": "A", "score": 1.0}]
        captured = []
        def scores(query, documents, profile):
            captured.append(profile["timeout_seconds"])
            return [(0, 0.9)]
        with patched_env(
            AWOKI_RERANK_ENABLED="1",
            AWOKI_RERANK_PROVIDER="http",
            AWOKI_RERANK_URL="http://rerank.test",
            AWOKI_RERANK_TIMEOUT_SECONDS="20",
        ), mock.patch.object(rag_backend, "_remote_rerank_scores", side_effect=scores):
            ranked = rag_backend.rerank_hits("query", hits, limit=1, timeout_override=5)
        self.assertEqual(captured, [5.0])
        self.assertEqual(ranked[0]["id"], "a")

    def test_rerank_top_n_override_requests_full_internal_window(self):
        hits = [
            {"id": f"h{i}", "title": f"H{i}", "score": 1.0 - i / 10.0}
            for i in range(4)
        ]
        captured = []

        def scores(query, documents, profile):
            captured.append((len(documents), profile["top_n"]))
            return [(idx, 1.0 - idx / 10.0) for idx in range(len(documents))]

        with patched_env(
            AWOKI_RERANK_ENABLED="1",
            AWOKI_RERANK_PROVIDER="http",
            AWOKI_RERANK_URL="http://rerank.test",
            AWOKI_RERANK_TOP_N="1",
        ), mock.patch.object(rag_backend, "_remote_rerank_scores", side_effect=scores):
            ranked = rag_backend.rerank_hits("query", hits, limit=4, top_n_override=4)
        self.assertEqual(captured, [(4, 4)])
        self.assertEqual(sum(1 for row in ranked if row.get("rerank_score") is not None), 4)

    def test_tei_rerank_uses_native_texts_contract(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return [
                    {"index": 1, "score": 0.9},
                    {"index": 0, "score": 0.2},
                ]

        profile = {
            "provider": "tei",
            "url": "http://tei.test/rerank",
            "model": "ignored-by-native-tei",
            "top_n": 1,
            "timeout_seconds": 20,
        }
        fake_httpx = mock.Mock()
        fake_httpx.post.return_value = Response()
        # The host-side lightweight validation suite must not require optional
        # runtime dependencies to be installed. Inject the imported module at
        # the boundary used by _remote_rerank_scores instead of importing it.
        with mock.patch.dict(sys.modules, {"httpx": fake_httpx}):
            scores = rag_backend._remote_rerank_scores("jwt validation", ["a", "b"], profile)
        self.assertEqual(scores, [(1, 0.9)])
        payload = fake_httpx.post.call_args.kwargs["json"]
        self.assertEqual(payload["texts"], ["a", "b"])
        self.assertNotIn("documents", payload)
        self.assertFalse(payload["raw_scores"])

    def test_classify_secret_value_recommends_redaction_without_censoring_analysis(self):
        c = classify_memory_text("password=super-secret-token")
        self.assertNotEqual(c["sensitivity"], "sensitive")
        self.assertTrue(c["redaction_recommended"])
        self.assertIn("<REDACTED_SECRET>", c["redacted_preview"])

    def test_classify_global_candidate(self):
        c = classify_memory_text("When auditing auth tests, always check negative cases first.")
        self.assertIn(c["decision"], {"global_candidate", "hybrid_candidate"})

    def test_save_project_fact_redacts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".harness" / "memory").mkdir(parents=True)
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"x"}', encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("x", paths=paths)
            saved = save_project_fact("api_key=abc123", paths=paths)
            self.assertIn("<REDACTED_SECRET>", saved["text"])


    def test_promotion_requires_resolution(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".harness" / "memory").mkdir(parents=True)
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"x"}', encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            cand = propose_promotion("Project uses auth negative test first", "When testing auth, check negative cases early.", paths=paths)
            self.assertEqual(cand["status"], "pending_review")
            pending = list_promotion_candidates(paths)
            self.assertEqual(len(pending), 1)
            result = approve_promotion(candidate_line=pending[0]["_line"], paths=paths)
            self.assertEqual(result["status"], "approved")
            self.assertEqual(list_promotion_candidates(paths), [])

    def test_demote_hides_global_and_saves_project_copy(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".harness" / "memory").mkdir(parents=True)
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"x"}', encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("x", paths=paths)
            save_global_fact("Use demo-api staging fixture command only here.", reason="test", reviewed=True, paths=paths)
            hits = search_global_memory("demo-api", paths=paths)
            self.assertTrue(hits)
            result = demote_global_memory(global_line=hits[0]["_line"], reason="project-specific", paths=paths)
            self.assertEqual(result["status"], "demoted")
            self.assertEqual(search_global_memory("demo-api", paths=paths), [])
            ctx = recall_context("demo-api", include_global=True, paths=paths)
            self.assertTrue(ctx["project_hits"])

    def test_sensitive_value_promotion_is_redacted_but_not_censored(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".harness" / "memory").mkdir(parents=True)
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"x"}', encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            result = propose_promotion("password=super-secret-token", paths=paths)
            self.assertEqual(result["status"], "pending_review")
            self.assertTrue(result["redaction_applied"])
            self.assertIn("<REDACTED_SECRET>", result["source_text"])
            self.assertEqual(len(list_promotion_candidates(paths)), 1)

    def test_direct_global_save_queues_review(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".harness" / "memory").mkdir(parents=True)
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"x"}', encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            result = save_global_fact("When testing auth, check negative cases early.", reason="test", paths=paths)
            self.assertEqual(result["status"], "needs_review")
            self.assertEqual(len(list_promotion_candidates(paths)), 1)

    def test_findings_and_hypotheses_redact(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".harness" / "memory").mkdir(parents=True)
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"x"}', encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("x", paths=paths)
            finding = save_finding("token=abc123", evidence="password=hunter2", paths=paths)
            hypothesis = save_hypothesis("secret=abc123", paths=paths)
            self.assertIn("<REDACTED_SECRET>", finding["title"])
            self.assertIn("<REDACTED_SECRET>", finding["evidence"])
            self.assertIn("<REDACTED_SECRET>", hypothesis["hypothesis"])

    def test_search_code_finds_project_source(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".harness" / "memory").mkdir(parents=True)
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"x"}', encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "auth.py").write_text("def negative_auth_case(): pass\n", encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            hits = search_code("negative_auth_case", paths=paths)
            self.assertTrue(hits)
            self.assertEqual(hits[0]["path"], "src/auth.py")

    def test_legacy_search_code_preserves_security_code_semantics(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".harness" / "memory").mkdir(parents=True)
            (root / ".harness" / "manifest.json").write_text(
                '{"harness_version":"test","active_project_id":"x"}', encoding="utf-8"
            )
            (root / "src").mkdir()
            (root / "src" / "auth.go").write_text(
                "func authenticate(r Request) {\n    token := helper.BearerTokenFromRequest(r)\n}\n",
                encoding="utf-8",
            )
            paths = HarnessPaths(root=root, global_root=root / "global")
            hits = search_code("BearerTokenFromRequest", paths=paths)
            self.assertTrue(hits)
            self.assertIn("token := helper.BearerTokenFromRequest", hits[0]["preview"])

    def test_open_artifact_uses_source_aware_redaction_for_project_repo(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".harness" / "memory").mkdir(parents=True)
            (root / ".harness" / "manifest.json").write_text(
                '{"harness_version":"test","active_project_id":"demo"}', encoding="utf-8"
            )
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            repo = root / "workspace" / "projects" / "demo" / "repo"
            source = repo / "auth.go"
            source.write_text(
                'func authenticate(r Request) {\n    token := helper.BearerTokenFromRequest(r)\n    api_key := "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"\n}\n',
                encoding="utf-8",
            )
            opened = open_artifact(
                "workspace/projects/demo/repo/auth.go", paths=paths
            )
            self.assertIn("token := helper.BearerTokenFromRequest", opened["text"])
            self.assertNotIn("sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456", opened["text"])
            self.assertIn("<REDACTED_", opened["text"])

    def test_search_code_and_open_artifact_accept_canonicalized_root(self):
        with tempfile.TemporaryDirectory() as d:
            real_root = Path(d) / "real"
            real_root.mkdir()
            alias_root = Path(d) / "alias"
            alias_root.symlink_to(real_root, target_is_directory=True)
            (real_root / ".harness" / "memory").mkdir(parents=True)
            (real_root / ".harness" / "manifest.json").write_text(
                '{"harness_version":"test","active_project_id":"x"}',
                encoding="utf-8",
            )
            (real_root / "src").mkdir()
            (real_root / "src" / "auth.py").write_text(
                "def canonical_path_symbol(): pass\n",
                encoding="utf-8",
            )
            (real_root / "notes.txt").write_text("password=hunter2", encoding="utf-8")
            paths = HarnessPaths(root=alias_root, global_root=real_root / "global")
            hits = search_code("canonical_path_symbol", paths=paths)
            self.assertTrue(hits)
            opened = open_artifact("notes.txt", paths=paths)
            self.assertTrue(opened["redacted"])

    def test_skill_search(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            skill = root / ".opencode" / "skills" / "demo" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text('---\ndescription: Demo reverse engineering triage skill\ntags: [re, triage]\n---\n# Demo\n', encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            hits = search_skills("triage", paths=paths)
            self.assertTrue(hits)
            self.assertEqual(hits[0]["name"], "demo")

    def test_sqlite_fts_indexes_project_memory(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".harness" / "memory").mkdir(parents=True)
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"x"}', encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("x", paths=paths)
            save_project_fact("The auth module has a negative_auth_case regression test.", paths=paths)
            indexed = index_project(include_qdrant=False, paths=paths)
            self.assertEqual(indexed["fts"]["backend"], "sqlite_fts")
            hits = search_rag("negative_auth_case", paths=paths)["project_hits"]
            self.assertTrue(hits)
            self.assertIn("sqlite_fts", hits[0].get("retrieval_backends", [hits[0].get("retrieval_backend")]))

    def test_sqlite_fts_indexes_global_memory(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".harness" / "memory").mkdir(parents=True)
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"x"}', encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            save_global_fact("When testing authorization, verify negative cases first.", reviewed=True, paths=paths)
            indexed = index_global(include_qdrant=False, paths=paths)
            self.assertEqual(indexed["fts"]["backend"], "sqlite_fts")
            hits = search_rag("authorization negative", scope="global", include_global=True, paths=paths)["global_hits"]
            self.assertTrue(hits)


    def test_project_fact_redacts_evidence_too(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".harness" / "memory").mkdir(parents=True)
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"x"}', encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("x", paths=paths)
            saved = save_project_fact("normal fact", evidence="Authorization: Bearer abc123", paths=paths)
            self.assertIn("<REDACTED>", saved["evidence"])
            self.assertEqual(saved["sensitivity"], "normal")
            self.assertEqual(saved["index_policy"], "safe")

    def test_open_artifact_redacts_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".harness" / "memory").mkdir(parents=True)
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"x"}', encoding="utf-8")
            target = root / "notes.txt"
            target.write_text("password=hunter2", encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            opened = open_artifact("notes.txt", paths=paths)
            self.assertTrue(opened["redacted"])
            self.assertNotIn("hunter2", opened["text"])

    def test_general_project_rag_never_builds_fixed_window_code_documents(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".harness" / "memory").mkdir(parents=True)
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"x"}', encoding="utf-8")
            (root / ".harness" / "internal.py").write_text("def awoki_internal_only(): pass", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("def app_symbol(): pass", encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            docs = collect_project_documents(paths, include_code=True)
            self.assertFalse(any(doc.kind == "code" for doc in docs))

    def test_active_project_id_auto_uses_root_name(self):
        from harness_core import active_project_id
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "real-project"
            root.mkdir()
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"__auto__"}', encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            self.assertEqual(active_project_id(paths), "real-project")

    def test_open_artifact_raw_read_requires_env_gate(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".harness" / "memory").mkdir(parents=True)
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"x"}', encoding="utf-8")
            (root / "notes.txt").write_text("hello", encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            opened = open_artifact("notes.txt", redact_secrets=False, paths=paths)
            self.assertIn("error", opened)

    def test_artifact_index_still_includes_harness_artifacts_dir(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".harness" / "memory").mkdir(parents=True)
            (root / ".harness" / "artifacts").mkdir(parents=True)
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"x"}', encoding="utf-8")
            (root / ".harness" / "artifacts" / "strings.txt").write_text("unique_artifact_symbol", encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            docs = collect_project_documents(paths, include_artifacts=True, include_code=False)
            self.assertIn(".harness/artifacts/strings.txt", {d.source_path for d in docs})

    def test_active_project_id_env_auto_does_not_override_root_name(self):
        from harness_core import active_project_id
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "real-project"
            root.mkdir()
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"__auto__"}', encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=root / "global")
            old = os.environ.get("AWOKI_PROJECT_ID")
            os.environ["AWOKI_PROJECT_ID"] = "__auto__"
            try:
                self.assertEqual(active_project_id(paths), "real-project")
            finally:
                if old is None:
                    os.environ.pop("AWOKI_PROJECT_ID", None)
                else:
                    os.environ["AWOKI_PROJECT_ID"] = old



    def test_code_vector_sync_embeds_and_persists_incrementally(self):
        class FakeClient:
            def __init__(self):
                self.upserts = []
            def collection_exists(self, name):
                return False
            def upsert(self, *, collection_name, points, wait):
                self.upserts.append(list(points))
            def set_payload(self, **kwargs):
                raise AssertionError("no payload-only update expected")
            def delete(self, **kwargs):
                raise AssertionError("no delete expected")

        client = FakeClient()
        rows = [
            {
                "embedding_key": f"key-{i}", "content_hash": f"hash-{i}", "text": f"text {i}",
                "repo_id": "demo:repo", "path": f"f{i}.py", "chunk_id": f"c{i}",
                "start_line": 1, "end_line": 1, "language": "python",
            }
            for i in range(3)
        ]
        calls = []
        def fake_embed(texts, *, is_query=False):
            calls.append(list(texts))
            return [[float(len(calls)), 0.0] for _ in texts]
        with mock.patch.object(vector_store.rag_backend, "qdrant_client", return_value=(client, "ok")), \
             mock.patch.object(vector_store.rag_backend, "embedding_profile", return_value={"batch_size": 2, "provider": "test"}), \
             mock.patch.object(vector_store.rag_backend, "embed_texts", side_effect=fake_embed), \
             mock.patch.object(vector_store.rag_backend, "ensure_qdrant_collection") as ensure_collection:
            result = vector_store.sync_branch_memberships(
                project_id="demo", branch_key="repo:repo|branch:main", old_rows=[], new_rows=rows
            )
        self.assertEqual(result["status"], "indexed", result)
        self.assertEqual(result["new_vectors"], 3)
        self.assertEqual([len(call) for call in calls], [2, 1])
        self.assertEqual([len(batch) for batch in client.upserts], [2, 1])
        ensure_collection.assert_called_once()


class AwokiAuditRegressionTests(unittest.TestCase):
    def test_propose_skill_update_is_review_only(self):
        from harness_core import propose_skill_update
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "proj"
            root.mkdir()
            (root / ".harness" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
            (root / ".harness" / "manifest.json").write_text('{"harness_version":"test","active_project_id":"x"}', encoding="utf-8")
            paths = HarnessPaths(root=root, global_root=Path(d) / "global")
            result = propose_skill_update(
                "reliability-verification",
                "Add code example: token := helper.BearerTokenFromRequest(r); api_key=abcdef123456",
                reason="audit", paths=paths,
            )
            self.assertEqual(result["kind"], "skill_update_candidate")
            self.assertEqual(result["status"], "pending_review")
            self.assertIn("token := helper.BearerTokenFromRequest(r)", result["proposed_change"])
            self.assertIn("<REDACTED_SECRET>", result["proposed_change"])
            self.assertTrue(result["redaction_applied"])
            self.assertTrue((root / ".harness" / "memory" / "skill_update_candidates.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
