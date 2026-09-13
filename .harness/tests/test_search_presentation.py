from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

import evidence_store
import harness_core as core
import opencode_events
import project_workspace
from code_search import engine
from code_search.presentation import compact_search_response


def json_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


class SearchPresentationTests(unittest.TestCase):
    def sample(self):
        return {
            "status": "ambiguous", "project_id": "demo", "query": "auth flow",
            "scope": {"repo_id": "service", "revision_key": "rev", "repository_assurance": "WORKING_TREE_BOUND"},
            "hits": [{
                "symbol_id": "s1", "source_id": "service", "revision_key": "rev",
                "path": "auth.py", "symbol": "check", "qualified_name": "Service.check",
                "start_line": 1, "end_line": 3, "preview": "source", "truncated": True,
                "dirty": False, "score": 0.5, "final_rank": 1, "fts_rank": 1,
                "rank_fusion_components": {"fts": 0.5}, "authority_class": "production",
                "source_role": "implementation", "retrieval_backends": ["code_fts"],
                "parse_status": "partial", "parse_diagnostics": ["incomplete parse"],
                "resolution_status": "unresolved", "candidate_count": 0,
                "confidence": 0, "edge": {"resolved": False}, "control_context": ["if token"],
                "evidence_locator": {"source_id": "service", "revision_key": "rev", "path": "auth.py"},
                "new_safety_warning": {"reason": "future unknown field"},
            }],
            "details": {
                "ambiguity": True, "unresolved": ["dynamic call"],
                "retrieval": {
                    "stage_top": {"fused": [{"path": "auth.py"}] * 10},
                    "qdrant_requested": True, "qdrant_used": False, "embedding_status": "degraded",
                    "rerank_requested": True, "rerank_eligible": True, "rerank_applied": False,
                    "rerank_future_warning": "not an alias",
                    "reranker": {"attempted": True, "applied": False, "degraded": True,
                                 "failure_class": "timeout", "retryable": True, "reason": "timed out"},
                },
            },
            "index": {"freshness": {"lexical_current": False, "view_drift": {"changed": True}}},
            "freshness": {"vector_current": False, "vector_query_reason": "unavailable"},
            "analysis_policy": {"recommended_semantics_operations": ["path.Join"]},
            "evidence_capture": {"status": "rejected", "reason": "scope drift"},
        }

    def test_identity_order_source_and_safety_survive_without_mutation(self):
        original = self.sample()
        original["hits"].append({**deepcopy(original["hits"][0]), "symbol_id": "s2"})
        before = deepcopy(original)
        result = compact_search_response(original)
        self.assertEqual(original, before)
        self.assertEqual([h["symbol_id"] for h in result["hits"]], ["s1", "s2"])
        for hit, source in zip(result["hits"], original["hits"]):
            self.assertNotIn("final_rank", hit)
            for key in ("source_id", "revision_key", "path", "symbol", "qualified_name", "preview", "truncated", "dirty", "score", "authority_class", "source_role", "retrieval_backends", "parse_status", "parse_diagnostics", "resolution_status", "candidate_count", "confidence", "edge", "control_context", "evidence_locator", "new_safety_warning"):
                self.assertEqual(hit[key], source[key], key)
        for key in ("status", "scope", "index", "freshness", "analysis_policy", "evidence_capture"):
            self.assertEqual(result[key], original[key], key)
        self.assertEqual(result["details"]["ambiguity"], True)
        self.assertEqual(result["details"]["unresolved"], ["dynamic call"])
        result["hits"][0]["edge"]["resolved"] = True
        self.assertEqual(original, before, "presentation must not alias canonical nested evidence")

    def test_backend_failures_and_unknown_fields_survive(self):
        original = self.sample()
        result = compact_search_response(original)
        retrieval = result["details"]["retrieval"]
        self.assertNotIn("stage_top", retrieval)
        self.assertNotIn("rerank_applied", retrieval)
        self.assertEqual(retrieval["reranker"], original["details"]["retrieval"]["reranker"])
        self.assertFalse(retrieval["reranker"]["applied"])
        self.assertEqual(retrieval["embedding_status"], "degraded")
        self.assertTrue(retrieval["rerank_requested"])
        self.assertEqual(retrieval["rerank_future_warning"], "not an alias")

    def test_diagnostics_full_and_failures_are_exact_passthrough(self):
        original = self.sample()
        for view in ("full", "diagnostics", "FULL"):
            self.assertIs(compact_search_response(original, view=view), original)
        for status in ("rejected", "not_found", "stale", "refresh_started", "backend_unavailable"):
            error = {**original, "status": status, "reason": "do not discard diagnostics"}
            self.assertIs(compact_search_response(error), error)
        empty = {"status": "ok", "reason": "no search performed"}
        self.assertIs(compact_search_response(empty), empty)
        for view in ("peek", "context", "PEEK", "unknown", ""):
            self.assertEqual(compact_search_response(original, view=view)["presentation"]["detail"], "compact")

    def test_multi_source_partial_scope_and_failures_survive(self):
        for group, identity in (("repositories", "repo_id"), ("sources", "source_id")):
            result = self.sample()
            result["status"] = "partial"
            result[group] = [
                {identity: "one", "status": "ok", "revision": {"revision_key": "r1"}, "retrieval": result["details"]["retrieval"]},
                {identity: "two", "status": "backend_unavailable", "reason": "required backend failed", "routing": {"selected_mode": "conceptual"}},
            ]
            compact = compact_search_response(result)
            self.assertEqual(compact[group][1], result[group][1])
            self.assertEqual(compact[group][0]["revision"], {"revision_key": "r1"})
            self.assertEqual(compact[group][0][identity], "one")
            self.assertNotIn("stage_top", compact[group][0]["retrieval"])

    def fixture(self, root, repos=("one",)):
        paths = core.HarnessPaths(root=root, global_root=root / "global")
        core.project_create("demo", paths=paths)
        for rid in repos:
            repo = project_workspace.paths_for(root, "demo").project_dir / "repo" / rid
            repo.mkdir(parents=True)
            for number in range(12):
                (repo / f"auth{number}.py").write_text(
                    f"def validate_token_{number}(token):\n"
                    "    '''Validate authentication token.'''\n"
                    "    return token is not None\n", encoding="utf-8",
                )
            for args in (["init", "-b", "main"], ["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture"]):
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
            project_workspace.project_repo_add(root, "demo", rid, f"repo/{rid}", default=rid == "one")
            self.assertEqual(engine.index_project_code(paths, "demo", repo=rid, include_qdrant=False)["status"], "indexed")
        return paths

    def test_real_search_keeps_hits_and_captured_ranking_available(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}):
            root = Path(td)
            paths = self.fixture(root)
            for view in ("peek", "context"):
                original = core.codebase_search(
                    "authentication token", name="demo", repo="one", mode="lexical", view=view,
                    limit=10, max_chars=600, use_qdrant=False, use_reranker=False,
                    capture_evidence=True, paths=paths,
                )
                before = deepcopy(original)
                compact = compact_search_response(original, view=view)
                self.assertEqual(len(compact["hits"]), 10)
                self.assertEqual([h["symbol_id"] for h in compact["hits"]], [h["symbol_id"] for h in original["hits"]])
                self.assertEqual([h["preview"] for h in compact["hits"]], [h["preview"] for h in original["hits"]])
                self.assertLess(json_size(compact), json_size(original) * 0.75)
                capture = compact["evidence_capture"]
                self.assertEqual(capture["status"], "stored")
                stored = evidence_store.get(root, "demo", capture["evidence_ref"], selector="payload.hits", limit=10)
                self.assertEqual(stored["status"], "ok")
                self.assertEqual(stored["value"], original["hits"])
                self.assertIn("final_rank", stored["value"][0])
                self.assertNotIn("final_rank", compact["hits"][0])
                self.assertEqual(original, before)

    def test_real_multi_repo_results_keep_separate_identities(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}):
            paths = self.fixture(Path(td), repos=("one", "two"))
            original = core.codebase_search("authentication token", name="demo", mode="lexical", view="peek", limit=30, use_qdrant=False, use_reranker=False, paths=paths)
            compact = compact_search_response(original, view="peek")
            self.assertEqual(compact["status"], "ok")
            self.assertEqual(compact["scope"], original["scope"])
            self.assertEqual({h["source_id"] for h in compact["hits"]}, {"one", "two"})
            self.assertEqual([h["evidence_locator"] for h in compact["hits"]], [h["evidence_locator"] for h in original["hits"]])

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP SDK integration runs in runtime image")
    def test_registered_mcp_tool_projects_after_real_evidence_capture(self):
        import asyncio
        import server

        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}):
            root = Path(td)
            paths = self.fixture(root)
            with mock.patch.object(core.HarnessPaths, "from_env", return_value=paths):
                response = asyncio.run(server.mcp.call_tool("codebase_search", {
                    "query": "authentication token", "name": "demo", "repo": "one",
                    "mode": "lexical", "view": "peek", "limit": 10,
                    "use_qdrant": False, "use_reranker": False, "capture_evidence": True,
                }))
            # FastMCP returns content plus structured output for annotated dict tools.
            content, structured = response
            self.assertEqual(structured["presentation"]["detail"], "compact")
            self.assertEqual(len(structured["hits"]), 10)
            self.assertNotIn("fts_rank", structured["hits"][0])
            self.assertEqual(json.loads(content[0].text), structured)
            saved = evidence_store.get(root, "demo", structured["evidence_capture"]["evidence_ref"], selector="payload.hits")
            self.assertIn("fts_rank", saved["value"][0])
            with mock.patch.object(core.HarnessPaths, "from_env", return_value=paths):
                _, window = asyncio.run(server.mcp.call_tool("code_source_window", {
                    "path": "auth0.py", "name": "demo", "repo": "one", "start_line": 1, "end_line": 3,
                }))
                _, verified = asyncio.run(server.mcp.call_tool("code_evidence_verify", {
                    "evidence_id": window["evidence_ref"], "name": "demo", "repo": "one",
                }))
            self.assertEqual(window["citation"], "one/auth0.py:1-3")
            self.assertEqual(verified["verdict"], "CURRENT_VERIFIED_SNAPSHOT")
            self.assertEqual(verified["evidence_ref"], window["evidence_ref"])
            self.assertIn("not verification of a behavioral claim", verified["verification_boundary"])


class SharedPolicyTests(unittest.TestCase):
    def test_compaction_uses_exact_startup_core_without_acceptance_boilerplate(self):
        policy = Path(opencode_events.__file__).with_name("AGENT_CORE.md").read_text(encoding="utf-8").strip()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = opencode_events.compaction_context(root, "ordinary-session", max_chars=4_000)
            self.assertTrue(result["context"].startswith(policy))
            self.assertNotIn("Active acceptance contract", result["context"])
            self.assertNotIn("acceptance_run_record", result["context"])
            self.assertNotIn("native rg through Bash", result["context"])
            self.assertLessEqual(len(result["context"]), 4_000)
