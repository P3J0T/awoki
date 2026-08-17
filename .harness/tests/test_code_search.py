from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import replace
from contextlib import closing
from pathlib import Path
from unittest import mock

import project_workspace
import indexing_policy
from harness_core import (
    HarnessPaths,
    code_callers,
    code_callees,
    code_definition,
    code_flow_graph,
    code_path,
    code_source_window,
    code_evidence_verify,
    code_semantics_check,
    code_text_search,
    code_validate_claim,
    codebase_search,
    code_diagnostics_trace,
    cross_project_code_search,
    code_index_status,
    code_index_verify,
    index_project,
    project_create,
    project_index_preview,
    project_refresh,
    project_source_add,
    project_source_list,
    project_status,
)
from code_search import engine, evaluation, languages, parser, provenance, semantics, store, text_search
from code_search.models import BranchIdentity, CodeReference, ParsedFile


class StructuralCodeSearchTests(unittest.TestCase):
    def make_project(self, root: Path, name: str = "demo") -> tuple[HarnessPaths, Path]:
        paths = HarnessPaths(root=root, global_root=root / "global")
        project_create(name, paths=paths)
        pp = project_workspace.paths_for(root, name)
        project_workspace.enable_code_index(root, name)
        return paths, pp.project_dir / "repo"

    def write_fixture(self, repo: Path) -> None:
        source = repo / "src" / "webhook_worker.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "from __future__ import annotations\n\n"
            "import sqlite3\n\n"
            "def should_process_delivery(connection: sqlite3.Connection, delivery_id: str) -> bool:\n"
            "    row = connection.execute(\n"
            "        \"SELECT 1 FROM processed_deliveries WHERE delivery_id = ?\",\n"
            "        (delivery_id,),\n"
            "    ).fetchone()\n"
            "    return row is None\n\n"
            "def mark_delivery_processed(connection: sqlite3.Connection, delivery_id: str) -> None:\n"
            "    connection.execute(\n"
            "        \"INSERT INTO processed_deliveries(delivery_id) VALUES (?)\",\n"
            "        (delivery_id,),\n"
            "    )\n"
            "    connection.commit()\n\n"
            "def handle_delivery(connection: sqlite3.Connection, delivery_id: str) -> bool:\n"
            "    if not should_process_delivery(connection, delivery_id):\n"
            "        return False\n"
            "    mark_delivery_processed(connection, delivery_id)\n"
            "    return True\n",
            encoding="utf-8",
        )

    def test_router_distinguishes_callers_callees_paths_and_exact_queries(self):
        cases = {
            "Who calls validate_claims?": "callers",
            "What does validate_claims call?": "callees",
            "Can request_body reach subprocess.run?": "path",
            "Where is validate_claims defined?": "definition",
            "Find all uses of EXPECTED_ISSUER": "exact",
            "How is issuer validation enforced?": "conceptual",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(engine.route_query(query)["mode"], expected)

    def test_smali_source_supports_structural_search_call_graph_and_manifest_evidence(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            corpus = pp.sources_dir / "smali"
            corpus.mkdir(parents=True)
            auth = corpus / "Auth.smali"
            auth.write_text(
                ".class public Lcom/foo/Auth;\n"
                ".super Ljava/lang/Object;\n"
                ".method public authenticate(Ljava/lang/String;)Z\n"
                "    invoke-static {p1}, Lcom/foo/Token;->verify(Ljava/lang/String;)Z\n"
                "    move-result v0\n"
                "    if-eqz v0, :bad\n"
                "    const/4 v0, 0x1\n"
                "    return v0\n"
                ":bad\n"
                "    const/4 v0, 0x0\n"
                "    return v0\n"
                ".end method\n",
                encoding="utf-8",
            )
            token = corpus / "Token.smali"
            token.write_text(
                ".class public Lcom/foo/Token;\n"
                ".super Ljava/lang/Object;\n"
                ".method public static verify(Ljava/lang/String;)Z\n"
                "    const/4 v0, 0x1\n"
                "    return v0\n"
                ".end method\n",
                encoding="utf-8",
            )

            registered = project_source_add(
                "smali", name="demo", source_type="smali", paths=paths
            )
            self.assertEqual(registered["status"], "registered", registered)
            listed = project_source_list(name="demo", paths=paths)
            self.assertTrue(any(row["source_id"] == "smali" for row in listed["sources"]))

            result = codebase_search(
                "authenticate",
                name="demo",
                source_id="smali",
                mode="exact",
                use_qdrant=False,
                use_reranker=False,
                paths=paths,
            )
            self.assertEqual(result["status"], "ok", result)
            hit = result["hits"][0]
            self.assertEqual(hit["qualified_name"], "Lcom/foo/Auth;->authenticate(Ljava/lang/String;)Z")
            self.assertEqual(hit["source_type"], "smali")
            self.assertEqual(hit["evidence_locator"]["source_id"], "smali")

            with mock.patch.object(
                project_workspace,
                "source_manifest_identity",
                side_effect=AssertionError("passive source status must not hash the corpus"),
            ):
                passive = code_index_status(name="demo", source_id="smali", paths=paths)
            self.assertEqual(passive["status"], "unverified", passive)
            self.assertFalse(passive["freshness"]["lexical_current"])
            self.assertFalse(passive["verification"]["source_scan"])
            self.assertEqual(passive["verification"]["source_freshness"], "requires_explicit_verify")

            callees = code_callees(
                "Lcom/foo/Auth;->authenticate(Ljava/lang/String;)Z",
                name="demo",
                source_id="smali",
                paths=paths,
            )
            self.assertEqual(callees["hits"][0]["qualified_name"], "Lcom/foo/Token;->verify(Ljava/lang/String;)Z")
            self.assertEqual(callees["hits"][0]["resolution_method"], "qualified_exact")

            window = code_source_window(
                "Auth.smali", name="demo", source_id="smali", start_line=3, end_line=12, paths=paths
            )
            self.assertEqual(window["evidence"]["assurance"], "CONTENT_MANIFEST_BOUND")
            self.assertEqual(window["evidence_locator"]["revision_key"], hit["revision_key"])
            verified = code_evidence_verify(
                window["evidence"]["evidence_id"], name="demo", source_id="smali", paths=paths
            )
            self.assertEqual(verified["verdict"], "CURRENT_SOURCE_CONTENT_MANIFEST_BOUND", verified)

            # Changing another file keeps the exact Auth bytes current but must
            # invalidate the corpus revision the evidence was bound to.
            token.write_text(token.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
            stale = code_evidence_verify(
                window["evidence"]["evidence_id"], name="demo", source_id="smali", paths=paths
            )
            self.assertEqual(stale["verdict"], "SOURCE_CURRENT_REVISION_CHANGED", stale)

    def test_structural_declarator_name_does_not_use_parameter_identifier(self):
        class FakeNode:
            def __init__(self, node_type, start, end, *, fields=None, children=None):
                self.type = node_type
                self.start_byte = start
                self.end_byte = end
                self._fields = fields or {}
                self.named_children = children or []

            def child_by_field_name(self, name):
                return self._fields.get(name)

        data = b"int process_delivery(int delivery_id)"
        owner_start = data.index(b"process_delivery")
        owner = FakeNode("identifier", owner_start, owner_start + len(b"process_delivery"))
        parameter_start = data.index(b"delivery_id")
        parameter = FakeNode("identifier", parameter_start, parameter_start + len(b"delivery_id"))
        parameter_list = FakeNode("parameter_list", data.index(b"("), len(data), children=[parameter])
        declarator = FakeNode(
            "function_declarator", owner_start, len(data),
            fields={"declarator": owner}, children=[owner, parameter_list],
        )
        definition = FakeNode(
            "function_definition", 0, len(data), fields={"declarator": declarator},
        )
        self.assertEqual(parser._symbol_name(data, definition), "process_delivery")

    def test_r916_named_declaration_wrapper_preserves_declared_type_name(self):
        class FakeNode:
            def __init__(self, node_type, start, end, *, fields=None, children=None):
                self.type = node_type
                self.start_byte = start
                self.end_byte = end
                self._fields = fields or {}
                self.named_children = children or []

            def child_by_field_name(self, name):
                return self._fields.get(name)

        data = b"type MatchingEngine interface { IsMatching(string, string) bool }"
        start = data.index(b"MatchingEngine")
        owner = FakeNode("type_identifier", start, start + len(b"MatchingEngine"))
        spec = FakeNode(
            "type_spec",
            start,
            len(data),
            fields={"name": owner},
            children=[owner],
        )
        declaration = FakeNode("type_declaration", 0, len(data), children=[spec])
        self.assertEqual(parser._symbol_name(data, declaration), "MatchingEngine")

    def test_definition_call_graph_and_natural_language_router(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            paths, repo = self.make_project(Path(td))
            self.write_fixture(repo)
            result = codebase_search(
                "How does this repository decide whether an incoming delivery has already been processed?",
                name="demo",
                paths=paths,
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["routing"]["selected_mode"], "conceptual")
            self.assertTrue(any(hit["symbol"] == "should_process_delivery" for hit in result["hits"]))
            definition = code_definition("should_process_delivery", name="demo", paths=paths)
            self.assertEqual(definition["hits"][0]["path"], "src/webhook_worker.py")
            callers = code_callers("mark_delivery_processed", name="demo", paths=paths)
            self.assertEqual(callers["hits"][0]["symbol"], "handle_delivery")
            path = code_path("handle_delivery", "mark_delivery_processed", name="demo", paths=paths)
            self.assertEqual(path["details"]["graph"]["status"], "found")

    def test_symlinked_workspace_root_keeps_canonicalized_repository_sources_visible(self):
        # macOS exposes /var through /private/var. Multi-repo resolution
        # canonicalizes repository roots, so scans must canonicalize the
        # workspace root before computing project-relative paths as well.
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            base = Path(td)
            canonical_root = base / "canonical"
            canonical_root.mkdir()
            alias_root = base / "alias"
            alias_root.symlink_to(canonical_root, target_is_directory=True)
            paths, repo = self.make_project(alias_root)
            self.write_fixture(repo)

            preview = engine.preview_project_code(paths, "demo")
            self.assertTrue(
                any(item.get("repo_relative") == "src/webhook_worker.py" for item in preview["included"]),
                preview,
            )
            result = codebase_search(
                "How is incoming delivery processing decided?", name="demo", paths=paths
            )
            self.assertEqual(result["status"], "ok", result)
            self.assertTrue(any(hit.get("symbol") == "should_process_delivery" for hit in result["hits"]), result)

    def test_codebase_search_reuses_clean_index_without_repository_rescan(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            self.write_fixture(repo)
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            engine.index_project_code(paths, "demo", include_qdrant=False)
            with mock.patch("code_search.engine._scan_repository", side_effect=AssertionError("search must reuse clean index")):
                result = codebase_search(
                    "How is incoming delivery processing decided?", name="demo", paths=paths
                )
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["index"]["freshness"]["lexical_current"])

    def test_code_index_status_is_passive_and_does_not_scan_or_probe_qdrant(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            self.write_fixture(repo)
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            engine.index_project_code(paths, "demo", include_qdrant=False)
            with mock.patch.object(
                engine, "_scan_repository", side_effect=AssertionError("status must not scan repository")
            ), mock.patch.object(
                engine.vector_store, "collection_available", side_effect=AssertionError("status must not probe Qdrant")
            ):
                status = code_index_status(name="demo", paths=paths)
            self.assertEqual(status["verification"]["mode"], "passive")
            self.assertFalse(status["verification"]["repository_scan"])
            self.assertFalse(status["verification"]["network"])
            self.assertTrue(status["freshness"]["lexical_current"])
            self.assertEqual(status["freshness"]["source_probe_verified_by"], "clean_git_content_identity")

    def test_index_metadata_drift_is_visible_without_masquerading_as_source_staleness(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            self.write_fixture(repo)
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            indexed_probe = indexed["source_probe_hash"]
            indexed_view = indexed["repository_evidence"]["view_fingerprint"]
            indexed_content_view = indexed["repository_evidence"]["content_view_fingerprint"]

            index_path = repo / ".git" / "index"
            before = index_path.stat()
            os.utime(index_path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))

            passive = code_index_status(name="demo", paths=paths)
            self.assertTrue(passive["freshness"]["lexical_current"], passive)
            self.assertTrue(passive["freshness"]["content_view_current"])
            self.assertFalse(passive["freshness"]["repository_view_current"])
            self.assertEqual(passive["freshness"]["view_drift_reasons"], ["repository_view_metadata"])
            self.assertEqual(passive["freshness"]["current_source_probe_hash"], indexed_probe)
            self.assertEqual(passive["freshness"]["source_probe_verified_by"], "clean_git_content_identity")
            self.assertNotEqual(passive["freshness"]["current_repository_view_fingerprint"], indexed_view)
            self.assertEqual(passive["freshness"]["current_content_view_fingerprint"], indexed_content_view)

            deep = code_index_verify(name="demo", include_qdrant=False, paths=paths)
            self.assertTrue(deep["freshness"]["lexical_current"], deep)
            self.assertTrue(deep["freshness"]["content_view_current"])
            self.assertFalse(deep["freshness"]["repository_view_current"])
            self.assertEqual(deep["freshness"]["view_drift_reasons"], ["repository_view_metadata"])

    def test_code_index_verify_explicitly_performs_deep_source_scan(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            self.write_fixture(repo)
            engine.index_project_code(paths, "demo", include_qdrant=False)
            real_scan = engine._scan_repository
            with mock.patch.object(engine, "_scan_repository", wraps=real_scan) as scan:
                status = code_index_verify(name="demo", include_qdrant=False, paths=paths)
            scan.assert_called_once()
            self.assertEqual(status["verification"]["mode"], "deep")
            self.assertTrue(status["verification"]["repository_scan"])
            self.assertFalse(status["verification"]["network"])
            self.assertTrue(status["current_excluded_count_verified"])

    def test_r9161_parser_extraction_profile_change_invalidates_and_reparses_index(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            self.write_fixture(repo)
            old_profile = {
                **engine.parser_runtime_profile(),
                "extraction_profile": "awoki-symbol-extraction-v1",
            }
            current_profile = {
                **engine.parser_runtime_profile(),
                "extraction_profile": "awoki-symbol-extraction-v2",
            }
            with mock.patch.object(engine, "parser_runtime_profile", return_value=old_profile):
                first = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertIn(first["status"], {"indexed", "current"})

            with mock.patch.object(engine, "parser_runtime_profile", return_value=current_profile):
                stale = engine.index_status(paths, "demo", deep_verify=True, verify_qdrant=False)
            self.assertFalse(stale["freshness"]["lexical_current"], stale)
            self.assertFalse(stale["freshness"]["lexical_checks"]["parser_profile"])

            real_parse = engine.parse_source
            with mock.patch.object(engine, "parser_runtime_profile", return_value=current_profile), \
                 mock.patch.object(engine, "parse_source", wraps=real_parse) as parse:
                refreshed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertEqual(refreshed["status"], "indexed", refreshed)
            self.assertGreater(parse.call_count, 0, "parser semantic profile changes must reparse unchanged files")
            with mock.patch.object(engine, "parser_runtime_profile", return_value=current_profile):
                current = engine.index_status(paths, "demo", deep_verify=True, verify_qdrant=False)
            self.assertTrue(current["freshness"]["lexical_current"], current)

    def test_r9161_local_index_progress_is_bounded_metadata_only(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            self.write_fixture(repo)
            updates = []
            result = engine.index_project_code(
                paths, "demo", include_qdrant=False, progress_callback=updates.append
            )
            self.assertIn(result["status"], {"indexed", "current"})
            phases = [row.get("phase") for row in updates]
            self.assertIn("source_scanned", phases)
            self.assertIn("structural_index", phases)
            self.assertIn("publishing_index", phases)
            self.assertIn("structural_complete", phases)
            for row in updates:
                self.assertNotIn("source", row)
                self.assertNotIn("text", row)
                self.assertNotIn("content", row)
                self.assertIn("files_total", row)
                self.assertIn("files_processed", row)

    def test_r9161_engine_version_bump_invalidates_prior_structural_snapshot(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            self.write_fixture(repo)
            engine.index_project_code(paths, "demo", include_qdrant=False)
            pp = project_workspace.paths_for(paths.root, "demo")
            db = store.db_path(pp.project_dir)
            branch = engine.branch_identity("demo", repo)
            state = store.read_state(db, branch.branch_key)
            store.write_state(
                db,
                branch=branch,
                project_id="demo",
                source_probe_hash=state["source_probe_hash"],
                parser_profile_hash=state["parser_profile_hash"],
                embedding_profile_hash=state["embedding_profile_hash"],
                document_set_hash=state["document_set_hash"],
                qdrant_membership_hash=state["qdrant_membership_hash"],
                indexed_at=state["indexed_at"],
                vector_status=state["vector_status"],
                vector_reason=state["vector_reason"],
                engine_version="awoki-structural-code-v7",
            )
            stale = engine.index_status(paths, "demo")
            self.assertFalse(stale["freshness"]["lexical_current"], stale)
            self.assertFalse(stale["freshness"]["lexical_checks"]["engine"])
            self.assertEqual(engine.ENGINE_VERSION, "awoki-structural-code-v8")

    def test_first_codebase_search_builds_lexical_index_without_remote_vectors(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "0"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            self.write_fixture(repo)
            with mock.patch.object(
                engine.vector_store, "sync_branch_memberships",
                side_effect=AssertionError("interactive search must not build remote vectors"),
            ), mock.patch.object(
                engine.vector_store, "search_with_status",
                side_effect=AssertionError("stale/unavailable vectors must be skipped"),
            ):
                result = codebase_search(
                    "How is incoming delivery processing decided?", name="demo", paths=paths
                )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["details"]["vector_search"]["status"], "skipped")

    def test_semantic_backend_failure_degrades_without_failing_structural_search(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            self.write_fixture(repo)
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            engine.index_project_code(paths, "demo", include_qdrant=False)
            pp = project_workspace.paths_for(paths.root, "demo")
            db = store.db_path(pp.project_dir)
            branch = engine.branch_identity("demo", repo)
            state = store.read_state(db, branch.branch_key)
            store.write_state(
                db,
                branch=branch,
                project_id="demo",
                source_probe_hash=state["source_probe_hash"],
                parser_profile_hash=state["parser_profile_hash"],
                embedding_profile_hash=state["embedding_profile_hash"],
                document_set_hash=state["document_set_hash"],
                qdrant_membership_hash="materialized-membership",
                indexed_at=state["indexed_at"],
                vector_status="indexed",
                vector_reason="",
                engine_version=engine.ENGINE_VERSION,
            )
            manifest_path = store.manifest_path(pp.project_dir)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["published_vector_collection"] = engine.vector_store.code_collection_name()
            manifest["vector"] = {
                **dict(manifest.get("vector") or {}),
                "status": "indexed",
                "collection": engine.vector_store.code_collection_name(),
            }
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            with mock.patch.object(engine.vector_store, "search_with_status", return_value={
                "status": "degraded", "backend": "qdrant", "collection": "test",
                "reason": "embedding query timed out", "hits": [],
            }):
                result = codebase_search(
                    "How is incoming delivery processing decided?", name="demo", paths=paths
                )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["details"]["vector_search"]["status"], "degraded")
            self.assertTrue(result["hits"])

    def test_codebase_flow_question_advertises_evidence_backed_followup_policy(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            self.write_fixture(repo)
            result = codebase_search(
                "Explain the flow of incoming delivery processing in detail",
                name="demo",
                paths=paths,
            )
            policy = result["analysis_policy"]
            self.assertTrue(policy["evidence_backed_default"])
            self.assertTrue(policy["flow_oriented"])
            self.assertTrue(policy["semantic_is_discovery_only"])
            self.assertIn("code_flow_graph", policy["recommended_followup_tools"])
            self.assertIn("code_source_window", policy["recommended_followup_tools"])
            self.assertEqual(policy["strict_claim_validation"], "selective_atomic_proof")

    def test_text_search_is_exhaustive_paginated_multi_path_and_policy_gated(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            for directory in ("src", "tests"):
                (repo / directory).mkdir(parents=True, exist_ok=True)
            (repo / "src" / "one.py").write_text("NEEDLE first\nsecond NEEDLE\n", encoding="utf-8")
            (repo / "tests" / "two.py").write_text("third NEEDLE\n", encoding="utf-8")
            (repo / ".env").write_text("TOKEN=NEEDLE-secret\n", encoding="utf-8")

            first = code_text_search(
                "NEEDLE", name="demo", paths_filter=["src", "tests", ".env"],
                page_size=2, paths=paths,
            )
            self.assertEqual(first["status"], "ok")
            self.assertTrue(first["eligible_universe_complete"])
            self.assertTrue(first["repository_universe_complete"])
            self.assertTrue(first["universe_complete"])
            self.assertFalse(first["search_complete"])
            self.assertEqual(first["match_count"], 4)
            self.assertEqual(first["matching_file_count"], 3)
            self.assertEqual(first["policy_excluded_file_count"], 0)
            self.assertNotEqual(first["next_cursor"], "")
            env_match = next(match for match in first["matches"] if match["path"] == ".env")
            self.assertTrue(env_match["match_redacted"])
            self.assertEqual(env_match["match_preview"], "<REDACTED_SENSITIVE_FILE_MATCH>")
            self.assertNotIn("NEEDLE-secret", env_match["context_preview"])

            second = code_text_search(
                "NEEDLE", name="demo", paths_filter=["src", "tests", ".env"],
                page_size=2, cursor=first["next_cursor"], paths=paths,
            )
            self.assertEqual(second["status"], "ok")
            self.assertTrue(second["search_complete"])
            self.assertTrue(second["repository_search_complete"])
            self.assertEqual(second["match_count"], 4)
            self.assertEqual(
                {match["path"] for match in first["matches"] + second["matches"]},
                {".env", "src/one.py", "tests/two.py"},
            )

    def test_authentication_source_is_not_excluded_by_generic_secret_detection(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            counts = {
                "pipeline/authn/authenticator.go": 1,
                "pipeline/authn/authenticator_anonymous.go": 1,
                "pipeline/authn/authenticator_bearer_token.go": 1,
                "pipeline/authn/authenticator_bearer_token_test.go": 3,
                "pipeline/authn/authenticator_cookie_session.go": 1,
                "pipeline/authn/authenticator_cookie_session_test.go": 2,
                "pipeline/authn/authenticator_jwt.go": 2,
                "pipeline/authn/authenticator_jwt_test.go": 5,
                "pipeline/authn/authenticator_oauth2_client_credentials.go": 1,
                "pipeline/authn/authenticator_oauth2_client_credentials_test.go": 1,
                "pipeline/authn/authenticator_oauth2_introspection.go": 1,
                "pipeline/authn/authenticator_oauth2_introspection_test.go": 5,
                "proxy/request_handler.go": 1,
            }
            for rel, count in counts.items():
                path = repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                prelude = (
                    "package authn\n\n"
                    "func authenticate(r any) error {\n"
                    "    token := helper.BearerTokenFromRequest(r)\n"
                    "    clientSecret := config.ClientSecret\n"
                    "    password := config.Password\n"
                    "    _ = token\n    _ = clientSecret\n    _ = password\n"
                )
                matches = "".join(
                    f"    _ = ErrAuthenticatorNotResponsible // occurrence {index}\n"
                    for index in range(count)
                )
                path.write_text(prelude + matches + "    return nil\n}\n", encoding="utf-8")

            raw = subprocess.run(
                ["rg", "-n", "-F", "ErrAuthenticatorNotResponsible", "."],
                cwd=repo, text=True, capture_output=True, check=True,
            )
            raw_matches = [line for line in raw.stdout.splitlines() if line.strip()]
            self.assertEqual(len(raw_matches), 25)

            result = code_text_search(
                "ErrAuthenticatorNotResponsible", name="demo", fixed_string=True,
                page_size=100, paths=paths,
            )
            self.assertEqual(result["match_count"], 25)
            self.assertEqual(result["matching_file_count"], 13)
            self.assertTrue(result["eligible_universe_complete"])
            self.assertTrue(result["repository_universe_complete"])
            self.assertTrue(result["universe_complete"])
            self.assertEqual(result["policy_excluded_source_file_count"], 0)
            self.assertEqual(
                {row["path"] for row in result["matches"]},
                set(counts),
            )

    def test_source_secret_literals_are_redacted_without_excluding_source(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            source = repo / "credentials" / "provider.go"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(
                'package credentials\n\nfunc Token() string {\n'
                '    token := "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"\n'
                '    return token\n}\n',
                encoding="utf-8",
            )
            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertIn(indexed["status"], {"indexed", "current"})
            self.assertNotIn("credentials/provider.go", {
                str(row.get("repo_relative"))
                for row in __import__("indexing_policy").read_index_manifest(
                    store.manifest_path(project_workspace.paths_for(paths.root, "demo").project_dir)
                ).get("excluded", [])
            })
            window = code_source_window(
                "credentials/provider.go", name="demo", start_line=1, end_line=6, paths=paths
            )
            rendered = "\n".join(str(row["text"]) for row in window["lines"])
            self.assertIn("<REDACTED_SECRET>", rendered)
            self.assertNotIn("sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456", rendered)
            self.assertTrue(window["redacted"])
            definition = code_definition("Token", name="demo", paths=paths)
            self.assertEqual(definition["status"], "ok")
            # Keep the lexical redaction regression hermetic: host-side validation is
            # intentionally allowed to run without ripgrep installed.  Simulate the
            # scanner's exact-match record, then let code_text_search exercise its real
            # materialization/context-preview/redaction path.
            source_bytes = source.read_bytes()
            match_offset = source_bytes.index(b"token :=")
            with mock.patch.object(text_search.shutil, "which", return_value="/awoki-test/rg"), \
                    mock.patch.object(
                        text_search,
                        "_run_rg_shard",
                        return_value=([("credentials/provider.go", 4, 5, match_offset, b"token :=")], False, ""),
                    ):
                lexical = code_text_search("token :=", name="demo", fixed_string=True, paths=paths)
            lexical_rendered = json.dumps(lexical, sort_keys=True)
            self.assertEqual(lexical["status"], "ok")
            self.assertEqual(lexical["match_count"], 1)
            self.assertNotIn("sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456", lexical_rendered)
            self.assertIn("<REDACTED_SECRET>", lexical_rendered)

    def test_known_high_confidence_source_secret_is_best_effort_redacted_from_sqlite_and_vector_rows(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "0"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            canary = "sk-proj-AWOKICANARYABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
            (repo / "auth.py").write_text(
                f'def token():\n    api_key = "{canary}"\n    return api_key\n',
                encoding="utf-8",
            )
            captured_rows = []
            def fake_sync(**kwargs):
                captured_rows.extend(kwargs.get("new_rows") or [])
                return {"status": "indexed", "membership_hash": "fixture", "new_vectors": len(kwargs.get("new_rows") or [])}
            with mock.patch.object(engine.vector_store, "sync_branch_memberships", side_effect=fake_sync), \
                 mock.patch.object(engine.vector_store, "collection_available", return_value=(True, "ok")):
                indexed = engine.index_project_code(paths, "demo", include_qdrant=True)
            self.assertIn(indexed["status"], {"indexed", "current"})
            pp = project_workspace.paths_for(paths.root, "demo")
            db_bytes = store.db_path(pp.project_dir).read_bytes()
            self.assertNotIn(canary.encode(), db_bytes)
            self.assertTrue(captured_rows)
            self.assertNotIn(canary, json.dumps(captured_rows, sort_keys=True))
            self.assertIn("<REDACTED_", json.dumps(captured_rows, sort_keys=True))

    def test_repository_completeness_reports_policy_excluded_source(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            src = repo / "src"
            src.mkdir(parents=True, exist_ok=True)
            (src / "visible.go").write_text("package src\n// NEEDLE\n", encoding="utf-8")
            (src / "blocked.go").write_text("// awoki:no-rag\npackage src\n// NEEDLE\n", encoding="utf-8")
            result = code_text_search("NEEDLE", name="demo", fixed_string=True, paths=paths)
            self.assertEqual(result["match_count"], 1)
            self.assertTrue(result["eligible_universe_complete"])
            self.assertFalse(result["repository_universe_complete"])
            self.assertFalse(result["universe_complete"])
            self.assertTrue(result["search_complete"])
            self.assertFalse(result["repository_search_complete"])
            self.assertEqual(result["policy_excluded_source_file_count"], 1)
            self.assertEqual(result["policy_excluded_source_reasons"], {"no_rag_marker": 1})
            self.assertEqual(result["status"], "partial")

    def test_security_source_values_are_best_effort_redacted_without_hiding_semantics(self):
        samples = {
            'token := helper.BearerTokenFromRequest(r)': 'token := helper.BearerTokenFromRequest(r)',
            'password = config.password': 'password = config.password',
            'credential := "hunter2-super-secret"': 'credential := "<REDACTED_SECRET>"',
            'dsn := "postgres://admin:password123@db.internal/prod"': 'dsn := "postgres://admin:<REDACTED_SECRET>@db.internal/prod"',
            'os.Setenv("TOKEN", "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456")': 'os.Setenv("TOKEN", "<REDACTED_SECRET>")',
        }
        import safety
        for raw, expected in samples.items():
            with self.subTest(raw=raw):
                safe, _ = safety.redact_source_text(raw)
                self.assertEqual(safe, expected)

    def test_repository_paths_named_harness_or_opencode_are_not_silently_dropped(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            fixtures = {
                ".harness/auth_engine.py": "def AUTH_INTERNAL_NEEDLE(): pass\n",
                ".opencode/plugins/auth.ts": "export const AUTH_INTERNAL_NEEDLE = true\n",
            }
            for rel, text in fixtures.items():
                f = repo / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text, encoding="utf-8")
            result = code_text_search("AUTH_INTERNAL_NEEDLE", name="demo", fixed_string=True, paths=paths)
            self.assertEqual(result["match_count"], 2)
            self.assertEqual({m["path"] for m in result["matches"]}, set(fixtures))
            self.assertTrue(result["repository_universe_complete"])

    def test_generated_or_vendor_named_text_is_lexical_only_not_a_blind_spot(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            fixtures = {
                "build/generated.go": "package build\n// GENERATED_AUTH_NEEDLE\n",
                "dist/auth.js": "export const GENERATED_AUTH_NEEDLE = true;\n",
                "target/generated.rs": "// GENERATED_AUTH_NEEDLE\n",
                "node_modules/local-auth/index.js": "// GENERATED_AUTH_NEEDLE\n",
            }
            for rel, text in fixtures.items():
                f = repo / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text, encoding="utf-8")
            result = code_text_search(
                "GENERATED_AUTH_NEEDLE", name="demo", fixed_string=True, page_size=50, paths=paths
            )
            self.assertEqual(result["match_count"], len(fixtures))
            self.assertEqual({m["path"] for m in result["matches"]}, set(fixtures))
            self.assertTrue(result["repository_universe_complete"])

            engine.index_project_code(paths, "demo", include_qdrant=False)
            manifest = indexing_policy.read_index_manifest(
                store.manifest_path(project_workspace.paths_for(paths.root, "demo").project_dir)
            )
            excluded = {str(row.get("repo_relative")): row for row in manifest.get("excluded", [])}
            for rel in fixtures:
                self.assertEqual(excluded[rel]["reason"], "lexical_only_policy")
                self.assertEqual(excluded[rel]["policy_reason"], "generated_text_lexical_only")
                self.assertTrue(excluded[rel]["lexical_included"])

    def test_unknown_textual_source_formats_remain_in_exhaustive_lexical_universe(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            fixtures = {
                "api/service.proto": 'rpc Authenticate(Request) returns (Reply); // CROSS_NEEDLE\n',
                "policy/auth.rego": 'allow if { input.token != "" } # CROSS_NEEDLE\n',
                "schema/auth.graphql": 'type AuthPayload { token: String } # CROSS_NEEDLE\n',
                "scripts/auth.ps1": '$token = $env:TOKEN # CROSS_NEEDLE\n',
                "src/Auth.hs": 'authenticate token = token -- CROSS_NEEDLE\n',
                "policies/custom.authzlang": 'permit(user) when token != "" // CROSS_NEEDLE\n',
            }
            for rel, text in fixtures.items():
                f = repo / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text, encoding="utf-8")
            raw = subprocess.run(
                ["rg", "-n", "-F", "CROSS_NEEDLE", "."], cwd=repo,
                text=True, capture_output=True, check=True,
            )
            self.assertEqual(len([line for line in raw.stdout.splitlines() if line.strip()]), len(fixtures))
            result = code_text_search("CROSS_NEEDLE", name="demo", fixed_string=True, page_size=50, paths=paths)
            self.assertEqual(result["match_count"], len(fixtures))
            self.assertEqual(result["matching_file_count"], len(fixtures))
            self.assertTrue(result["repository_universe_complete"])
            self.assertEqual({m["path"] for m in result["matches"]}, set(fixtures))
            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertIn(indexed["status"], {"indexed", "current"})
            manifest = indexing_policy.read_index_manifest(
                store.manifest_path(project_workspace.paths_for(paths.root, "demo").project_dir)
            )
            structural = {str(row.get("repo_relative")): row for row in manifest.get("included", [])}
            for rel in fixtures:
                self.assertIn(rel, structural)
                self.assertEqual(structural[rel]["structural_parser"], "text_fallback")
                self.assertTrue(structural[rel]["lexical_included"])
            conceptual = codebase_search("CROSS_NEEDLE", name="demo", limit=20, paths=paths)
            self.assertEqual(conceptual["status"], "ok")
            self.assertTrue(any(hit["path"] in fixtures for hit in conceptual["hits"]))

    def test_security_vocabulary_never_reduces_code_coverage(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            fixtures = {
                "auth/token_handler.go": "package auth\n// AWOKI_SECURITY_COVERAGE_CANARY token password secret JWT OAuth credentials Authorization\n",
                "credentials/provider.authzlang": "permit when token != \"\" // AWOKI_SECURITY_COVERAGE_CANARY\n",
                "secrets/rotation.custom": "rotate(client_secret) # AWOKI_SECURITY_COVERAGE_CANARY\n",
                "oauth/jwt.policyx": "allow bearer token # AWOKI_SECURITY_COVERAGE_CANARY\n",
            }
            for rel, text in fixtures.items():
                target = repo / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")

            raw = subprocess.run(
                ["rg", "-n", "-F", "AWOKI_SECURITY_COVERAGE_CANARY", "."], cwd=repo,
                text=True, capture_output=True, check=True,
            )
            raw_paths = {line.split(":", 1)[0].removeprefix("./") for line in raw.stdout.splitlines() if line.strip()}
            self.assertEqual(raw_paths, set(fixtures))

            lexical = code_text_search(
                "AWOKI_SECURITY_COVERAGE_CANARY", name="demo", fixed_string=True, page_size=50, paths=paths
            )
            self.assertEqual(lexical["match_count"], len(fixtures))
            self.assertEqual({row["path"] for row in lexical["matches"]}, set(fixtures))
            self.assertTrue(lexical["repository_universe_complete"])

            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertIn(indexed["status"], {"indexed", "current"})
            manifest = indexing_policy.read_index_manifest(
                store.manifest_path(project_workspace.paths_for(paths.root, "demo").project_dir)
            )
            structural = {str(row.get("repo_relative")): row for row in manifest.get("included", [])}
            self.assertEqual(set(fixtures), set(fixtures) & set(structural))
            for rel in fixtures:
                if engine.detect_language(repo / rel) is None:
                    self.assertEqual(structural[rel]["structural_parser"], "text_fallback")

            conceptual = codebase_search("AWOKI_SECURITY_COVERAGE_CANARY", name="demo", limit=20, paths=paths)
            self.assertEqual(conceptual["status"], "ok")
            self.assertEqual({hit["path"] for hit in conceptual["hits"]} & set(fixtures), set(fixtures))

    def test_oathkeeper_shaped_production_tests_fixtures_and_security_packages_keep_roles(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            fixtures = {
                "pipeline/authn/authenticator_jwt.go": "package authn\n// OATH_PROD_AUTHENTICATOR behavior\n",
                "pipeline/authn/authenticator_jwt_test.go": "package authn\n// OATH_TEST_TOKEN_LOCATION edge case\n",
                "test/e2e/e2e-rules.json": '{"note":"OATH_E2E_RULE_FIXTURE"}\n',
                "credentials/fetcher.go": "package credentials\n// OATH_CREDENTIALS_PRODUCTION source\n",
            }
            for rel, text in fixtures.items():
                target = repo / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertIn(indexed["status"], {"indexed", "current"})
            expectations = {
                "OATH_PROD_AUTHENTICATOR": "production",
                "OATH_TEST_TOKEN_LOCATION": "test",
                "OATH_E2E_RULE_FIXTURE": "test",
                "OATH_CREDENTIALS_PRODUCTION": "production",
            }
            for query, role in expectations.items():
                result = codebase_search(query, name="demo", limit=10, paths=paths)
                self.assertEqual(result["status"], "ok")
                hit = next((row for row in result["hits"] if query in row.get("preview", "")), None)
                self.assertIsNotNone(hit, query)
                self.assertEqual(hit["source_role"], role)

    def test_large_textual_source_remains_in_exhaustive_lexical_universe(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            target = repo / "api" / "large.proto"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(("message Padding {}\n" * 110000) + "// LARGE_TEXT_NEEDLE\n", encoding="utf-8")
            self.assertGreater(target.stat().st_size, 2_000_000)
            raw = subprocess.run(
                ["rg", "-n", "-F", "LARGE_TEXT_NEEDLE", "."], cwd=repo,
                text=True, capture_output=True, check=True,
            )
            self.assertEqual(len([line for line in raw.stdout.splitlines() if line.strip()]), 1)

            result = code_text_search(
                "LARGE_TEXT_NEEDLE", name="demo", fixed_string=True, page_size=50, paths=paths
            )
            self.assertEqual(result["match_count"], 1)
            self.assertEqual(result["matching_file_count"], 1)
            self.assertEqual(result["matches"][0]["path"], "api/large.proto")
            self.assertTrue(result["eligible_universe_complete"])
            self.assertTrue(result["repository_universe_complete"])

            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertIn(indexed["status"], {"indexed", "current"})
            manifest = indexing_policy.read_index_manifest(
                store.manifest_path(project_workspace.paths_for(paths.root, "demo").project_dir)
            )
            row = next(item for item in manifest["excluded"] if item.get("repo_relative") == "api/large.proto")
            self.assertEqual(row["reason"], "lexical_only_policy")
            self.assertEqual(row["policy_reason"], "large_text_lexical_only")
            self.assertTrue(row["lexical_included"])
            self.assertFalse(row["included"])

    def test_tracked_source_symlink_is_accounted_as_repository_incompleteness(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            (repo / "real.go").write_text("package demo\n// LINK_NEEDLE\n", encoding="utf-8")
            try:
                (repo / "linked.go").symlink_to("real.go")
            except OSError:
                self.skipTest("symlink unavailable")
            result = code_text_search("LINK_NEEDLE", name="demo", fixed_string=True, paths=paths)
            self.assertFalse(result["repository_universe_complete"])
            self.assertFalse(result["universe_complete"])
            self.assertGreaterEqual(result["policy_excluded_source_file_count"], 1)
            self.assertGreaterEqual(result["policy_excluded_source_reasons"].get("symlink_not_allowed", 0), 1)

    def test_nested_git_worktree_is_rejected_instead_of_silently_indexed(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            nested = repo / "oathkeeper"
            nested.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=nested, check=True)
            (nested / "main.go").write_text("package main\n", encoding="utf-8")
            status = code_index_status(name="demo", paths=paths)
            self.assertEqual(status["status"], "invalid_repo_root")
            self.assertIn("oathkeeper", status.get("nested_git_roots", []))
            search = code_text_search("package", name="demo", fixed_string=True, paths=paths)
            self.assertEqual(search["status"], "invalid_repo_root")

    def test_git_repository_scope_explicitly_honors_gitignore(self):
        if not shutil.which("git") or shutil.which("rg") is None:
            self.skipTest("git/ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "awoki@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Awoki Test"], cwd=repo, check=True)
            (repo / ".gitignore").write_text("ignored.env\n", encoding="utf-8")
            (repo / "main.go").write_text("package main\n// VISIBLE_SCOPE_NEEDLE\n", encoding="utf-8")
            (repo / "ignored.env").write_text("TOKEN=VISIBLE_SCOPE_NEEDLE-secret\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore", "main.go"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
            result = code_text_search("VISIBLE_SCOPE_NEEDLE", name="demo", fixed_string=True, paths=paths)
            self.assertEqual(result["match_count"], 1)
            self.assertEqual(result["repository_scope"], "git_tracked_and_visible_untracked")
            self.assertFalse(result["git_ignored_paths_scanned"])
            self.assertTrue(result["repository_universe_complete"])

    def test_forensic_text_search_can_explicitly_include_gitignored_files_without_exposing_env_values(self):
        if not shutil.which("git") or shutil.which("rg") is None:
            self.skipTest("git/ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "awoki@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Awoki Test"], cwd=repo, check=True)
            (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
            (repo / "main.go").write_text("package main\n// FORENSIC_SCOPE_NEEDLE\n", encoding="utf-8")
            (repo / ".env").write_text("TOKEN=FORENSIC_SCOPE_NEEDLE-secret-value\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore", "main.go"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)

            default = code_text_search("FORENSIC_SCOPE_NEEDLE", name="demo", fixed_string=True, paths=paths)
            self.assertEqual(default["match_count"], 1)
            self.assertFalse(default["git_ignored_paths_scanned"])

            forensic = code_text_search(
                "FORENSIC_SCOPE_NEEDLE", name="demo", fixed_string=True, include_ignored=True, paths=paths
            )
            self.assertEqual(forensic["match_count"], 2)
            self.assertEqual(forensic["matching_file_count"], 2)
            self.assertTrue(forensic["git_ignored_paths_scanned"])
            self.assertTrue(forensic["include_ignored"])
            self.assertEqual(forensic["repository_scope"], "git_tracked_visible_untracked_and_ignored")
            env_match = next(row for row in forensic["matches"] if row["path"] == ".env")
            self.assertEqual(env_match["match_preview"], "<REDACTED_SENSITIVE_FILE_MATCH>")
            self.assertEqual(env_match["context_preview"], "<REDACTED_SENSITIVE_FILE_CONTEXT>")
            rendered = json.dumps(forensic, sort_keys=True)
            self.assertNotIn("secret-value", rendered)

    def test_forensic_ignored_file_change_invalidates_materialized_cursor(self):
        if not shutil.which("git") or shutil.which("rg") is None:
            self.skipTest("git/ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "awoki@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Awoki Test"], cwd=repo, check=True)
            (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            (repo / "main.go").write_text("package main\n// CURSOR_FORENSIC_NEEDLE\n", encoding="utf-8")
            ignored = repo / "ignored.txt"
            ignored.write_text("CURSOR_FORENSIC_NEEDLE first\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore", "main.go"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)

            first = code_text_search(
                "CURSOR_FORENSIC_NEEDLE", name="demo", fixed_string=True, include_ignored=True, page_size=1, paths=paths
            )
            self.assertTrue(first["next_cursor"])
            ignored.write_text("CURSOR_FORENSIC_NEEDLE second\n", encoding="utf-8")
            stale = code_text_search(
                "CURSOR_FORENSIC_NEEDLE", name="demo", fixed_string=True, include_ignored=True,
                page_size=1, cursor=first["next_cursor"], paths=paths
            )
            self.assertEqual(stale["status"], "stale_cursor")

    def test_project_repo_inside_parent_git_worktree_is_rejected(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            root = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            paths, repo = self.make_project(root)
            (repo / "main.py").write_text("def f(): return True\n", encoding="utf-8")
            status = code_index_status(name="demo", paths=paths)
            self.assertEqual(status["status"], "invalid_repo_root")
            self.assertEqual(status["active_branch"]["source"], "git_root_mismatch")

    def test_claim_validation_never_returns_raw_secret_literal(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            (repo / "secret_flow.py").write_text(
                "def sink(value):\n    return value\n\n"
                "def caller():\n    return sink(\"sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456\")\n",
                encoding="utf-8",
            )
            result = code_validate_claim("caller calls sink", name="demo", paths=paths)
            self.assertEqual(result["verdict"], "VERIFIED")
            rendered = json.dumps(result, sort_keys=True)
            self.assertNotIn("sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456", rendered)
            self.assertIn("<REDACTED_", rendered)

    def test_text_search_giant_line_returns_all_locations_without_serializing_line(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            huge = repo / "tree.json"
            huge.write_text(
                ("a" * 90_000) + "NEEDLE" + ("b" * 90_000) + "NEEDLE" + ("c" * 90_000) + "\n",
                encoding="utf-8",
            )
            result = code_text_search("NEEDLE", name="demo", page_size=10, preview_chars=256, paths=paths)
            self.assertEqual(result["match_count"], 2)
            self.assertTrue(result["search_complete"])
            self.assertTrue(result["universe_complete"])
            self.assertEqual([m["line"] for m in result["matches"]], [1, 1])
            self.assertGreater(result["matches"][0]["line_bytes"], 250_000)
            self.assertLess(len(result["matches"][0]["context_preview"]), 300)
            self.assertLess(len(json.dumps(result)), 20_000)
            self.assertNotIn("a" * 10_000, json.dumps(result))

    def test_text_search_cursor_is_rejected_after_source_changes(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            source = repo / "worker.py"
            source.write_text("NEEDLE\nNEEDLE\n", encoding="utf-8")
            first = code_text_search("NEEDLE", name="demo", page_size=1, paths=paths)
            self.assertFalse(first["search_complete"])
            source.write_text("NEEDLE\nNEEDLE\nNEEDLE\n", encoding="utf-8")
            stale = code_text_search(
                "NEEDLE", name="demo", page_size=1, cursor=first["next_cursor"], paths=paths,
            )
            self.assertEqual(stale["status"], "stale_cursor")
            self.assertIn("restart", stale["reason"])

    def test_text_search_large_dirty_same_size_edit_invalidates_cursor(self):
        if shutil.which("rg") is None or shutil.which("git") is None:
            self.skipTest("ripgrep and git are required")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            source = repo / "large.txt"
            payload = ("A" * 2_050_000) + "\nNEEDLE one\nNEEDLE two\n"
            source.write_text(payload, encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "awoki@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Awoki Test"], cwd=repo, check=True)
            subprocess.run(["git", "add", "large.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)

            raw = bytearray(source.read_bytes())
            raw[100] = ord("B")
            source.write_bytes(raw)
            first = code_text_search("NEEDLE", name="demo", page_size=1, paths=paths)
            self.assertFalse(first["search_complete"])
            size_before = source.stat().st_size

            raw = bytearray(source.read_bytes())
            raw[100] = ord("C")
            source.write_bytes(raw)
            self.assertEqual(source.stat().st_size, size_before)
            stale = code_text_search(
                "NEEDLE", name="demo", page_size=1, cursor=first["next_cursor"], paths=paths
            )
            self.assertEqual(stale["status"], "stale_cursor")

    def test_text_search_clean_index_manifest_avoids_repository_rescan_and_pages_from_materialized_cache(self):
        if shutil.which("rg") is None or shutil.which("git") is None:
            self.skipTest("ripgrep and git are required")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            source = repo / "worker.py"
            source.write_text("NEEDLE one\nNEEDLE two\nNEEDLE three\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "awoki@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Awoki Test"], cwd=repo, check=True)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertIn(indexed["status"], {"indexed", "current"})

            with mock.patch.object(engine, "_scan_repository", side_effect=AssertionError("manifest fast path should be used")):
                first = code_text_search("NEEDLE", name="demo", page_size=1, paths=paths)
            self.assertEqual(first["eligibility_source"], "index_manifest")
            self.assertTrue(first["scan_complete"])
            self.assertEqual(first["match_count"], 3)
            self.assertEqual(first["returned"], 1)
            self.assertFalse(first["search_complete"])

            with mock.patch.object(engine, "_scan_repository", side_effect=AssertionError("continuation must not rescan policy")), \
                    mock.patch.object(text_search, "_run_rg_shard", side_effect=AssertionError("continuation must not rerun ripgrep")):
                second = code_text_search(
                    "NEEDLE", name="demo", page_size=2, cursor=first["next_cursor"], paths=paths,
                )
            self.assertTrue(second["cache_hit"])
            self.assertTrue(second["search_complete"])
            self.assertEqual(second["match_count"], 3)
            self.assertEqual([row["index"] for row in second["matches"]], [1, 2])

    def test_text_search_operation_budget_returns_resumable_state_instead_of_hanging(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            (repo / "worker.py").write_text("NEEDLE\nNEEDLE\n", encoding="utf-8")
            first = code_text_search(
                "NEEDLE", name="demo", page_size=10, operation_timeout_seconds=0.01, paths=paths,
            )
            self.assertEqual(first["status"], "partial")
            self.assertFalse(first["scan_complete"])
            self.assertFalse(first["universe_complete"])
            self.assertTrue(first["resume_required"])
            self.assertTrue(first["operation_budget_exhausted"])
            self.assertFalse(first["match_count_final"])
            self.assertNotEqual(first["next_cursor"], "")

            second = code_text_search(
                "NEEDLE", name="demo", page_size=10, cursor=first["next_cursor"],
                operation_timeout_seconds=20, paths=paths,
            )
            self.assertEqual(second["status"], "ok")
            self.assertTrue(second["scan_complete"])
            self.assertTrue(second["universe_complete"])
            self.assertTrue(second["match_count_final"])
            self.assertEqual(second["match_count"], 2)
            self.assertTrue(second["search_complete"])

    def test_text_search_missing_rg_returns_schema_stable_resumable_state(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            (repo / "worker.py").write_text("NEEDLE\n", encoding="utf-8")
            with mock.patch.object(text_search.shutil, "which", return_value=None):
                result = code_text_search("NEEDLE", name="demo", paths=paths)

            self.assertEqual(result["status"], "error")
            self.assertIn("ripgrep", result["reason"])
            self.assertFalse(result["scanner_available"])
            self.assertFalse(result["scan_complete"])
            self.assertFalse(result["universe_complete"])
            self.assertTrue(result["resume_required"])
            self.assertEqual(result["match_count"], 0)
            self.assertFalse(result["match_count_final"])
            self.assertFalse(result["search_complete"])
            self.assertNotEqual(result["next_cursor"], "")

    def test_materialized_text_search_pages_without_rg_after_scan_completion(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            (repo / "worker.py").write_text("NEEDLE one\nNEEDLE two\n", encoding="utf-8")
            first = code_text_search("NEEDLE", name="demo", page_size=1, paths=paths)
            self.assertTrue(first["scan_complete"])
            self.assertFalse(first["search_complete"])

            with mock.patch.object(text_search.shutil, "which", return_value=None), \
                    mock.patch.object(text_search, "_run_rg_shard", side_effect=AssertionError("paging completed search must not scan")):
                second = code_text_search(
                    "NEEDLE", name="demo", page_size=1, cursor=first["next_cursor"], paths=paths,
                )
            self.assertEqual(second["status"], "ok")
            self.assertTrue(second["scan_complete"])
            self.assertTrue(second["search_complete"])
            self.assertEqual(second["match_count"], 2)
            self.assertEqual(second["matches"][0]["index"], 1)

    def test_all_project_scoped_code_tools_resolve_the_attached_session(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            session_id = "opencode-code-session"
            project_create("demo", session_id=session_id, paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            project_workspace.enable_code_index(root, "demo")
            self.write_fixture(pp.project_dir / "repo")

            searches = [
                codebase_search(
                    "How are repeated deliveries suppressed?",
                    session_id=session_id,
                    paths=paths,
                ),
                code_index_status(session_id=session_id, paths=paths),
                code_definition(
                    "should_process_delivery", session_id=session_id, paths=paths
                ),
                code_callers(
                    "mark_delivery_processed", session_id=session_id, paths=paths
                ),
                code_callees("handle_delivery", session_id=session_id, paths=paths),
                code_path(
                    "handle_delivery",
                    "mark_delivery_processed",
                    session_id=session_id,
                    paths=paths,
                ),
                code_flow_graph(
                    "handle_delivery",
                    session_id=session_id,
                    paths=paths,
                ),
                code_source_window(
                    "src/webhook_worker.py",
                    start_line=1,
                    end_line=40,
                    session_id=session_id,
                    paths=paths,
                ),
                code_text_search(
                    "mark_delivery_processed",
                    session_id=session_id,
                    paths=paths,
                ),
                code_validate_claim(
                    "handle_delivery calls mark_delivery_processed",
                    session_id=session_id,
                    paths=paths,
                ),
            ]
            for result in searches:
                self.assertNotEqual(result["status"], "rejected", result)
                self.assertEqual(result.get("project_id"), "demo", result)

            self.assertEqual(searches[2]["hits"][0]["symbol"], "should_process_delivery")
            self.assertEqual(searches[3]["hits"][0]["symbol"], "handle_delivery")
            self.assertTrue(
                any(hit["symbol"] == "mark_delivery_processed" for hit in searches[4]["hits"])
            )
            self.assertEqual(searches[5]["details"]["graph"]["status"], "found")
            self.assertEqual(searches[6]["graph"]["status"], "ok")
            self.assertEqual(searches[7]["status"], "ok")
            # This test verifies attached-session routing for every project-scoped tool.
            # Text-search execution itself is covered by rg-gated tests above; host-side
            # validation must not accidentally require ripgrep because the production
            # MCP Docker images provide that runtime dependency.
            if searches[8]["status"] in {"ok", "partial"}:
                self.assertEqual(searches[8]["match_count"], 2)
                self.assertTrue(searches[8]["search_complete"])
            else:
                self.assertEqual(searches[8]["status"], "error")
                self.assertIn("reason", searches[8])
                self.assertFalse(searches[8]["search_complete"])
                self.assertFalse(searches[8]["match_count_final"])
            self.assertEqual(searches[9]["verdict"], "VERIFIED")

    def test_flow_graph_traverses_only_resolved_edges_and_keeps_boundaries(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            self.write_fixture(repo)
            result = code_flow_graph(
                "handle_delivery",
                name="demo",
                max_depth=4,
                paths=paths,
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["root"]["symbol"], "handle_delivery")
            graph = result["graph"]
            names = {row["name"] for row in graph["nodes"]}
            self.assertIn("handle_delivery", names)
            self.assertIn("should_process_delivery", names)
            self.assertIn("mark_delivery_processed", names)
            self.assertGreaterEqual(graph["boundaries"]["resolved"], 2)
            self.assertTrue(all(
                edge["resolution_status"] == "resolved" or not edge.get("target_symbol_id")
                for edge in graph["edges"]
            ))
            self.assertIn("Only resolved edges are traversed.", graph["rules"])

    def test_source_window_hash_checks_and_clips_giant_source_lines(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            long_line = 'PAYLOAD = "' + ('x' * 120_000) + '"\n'
            (repo / "long_source.py").write_text(
                long_line + "def endpoint():\n    return PAYLOAD\n",
                encoding="utf-8",
            )
            codebase_search("Where is endpoint defined?", name="demo", paths=paths)
            result = code_source_window(
                "long_source.py",
                name="demo",
                start_line=1,
                end_line=3,
                max_chars=2000,
                max_line_chars=256,
                paths=paths,
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["source_sha256"], result["indexed_source_sha256"])
            self.assertTrue(result["truncated"])
            self.assertTrue(result["truncation"]["truncated"])
            self.assertIn("max_line_chars", result["truncation"]["reasons"])
            self.assertIn(1, result["truncation"]["line_truncated_lines"])
            self.assertFalse(result["truncation"]["complete_requested_range"])
            self.assertTrue(result["lines"][0]["truncated"])
            self.assertLessEqual(len(result["lines"][0]["text"]), 256)
            self.assertGreater(result["lines"][0]["source_chars"], 100_000)

            (repo / "long_source.py").write_text(
                long_line + "def endpoint():\n    return 'changed'\n",
                encoding="utf-8",
            )
            stale = code_source_window(
                "long_source.py",
                name="demo",
                start_line=2,
                end_line=3,
                paths=paths,
            )
            self.assertEqual(stale["status"], "stale_source")
            self.assertEqual(stale["verdict"], "STALE_SOURCE")

    def test_source_window_reports_explicit_continuation_when_total_budget_cuts_range(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            (repo / "many.py").write_text(
                "\n".join(f"VALUE_{i} = '{'x' * 220}'" for i in range(1, 31)) + "\n",
                encoding="utf-8",
            )
            codebase_search("Where are VALUE constants?", name="demo", paths=paths)
            result = code_source_window(
                "many.py", name="demo", start_line=1, end_line=30,
                max_chars=1000, max_line_chars=512, paths=paths,
            )
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["truncated"])
            self.assertIn("max_chars", result["truncation"]["reasons"])
            self.assertLess(result["returned"]["end_line"], 30)
            self.assertEqual(result["truncation"]["continue_from_line"], result["returned"]["end_line"] + 1)
            self.assertIn("Continue with start_line=", result["truncation"]["suggested_action"])

    def test_structural_index_is_separate_from_general_project_rag(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            self.write_fixture(repo)
            preview = project_index_preview("demo", include_code=True, paths=paths)
            self.assertTrue(any(item.get("path", "").endswith("webhook_worker.py") for item in preview["included"]))
            self.assertFalse(any(getattr(doc, "kind", "") == "code" for doc in preview.get("documents", [])))

    def test_incremental_index_reuses_unchanged_files_and_removes_deleted_files(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            self.write_fixture(repo)
            first = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertIn("src/webhook_worker.py", first["changed_files"])
            second = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertEqual(second["status"], "current")
            self.assertEqual(second["changed_files"], [])
            (repo / "src" / "webhook_worker.py").unlink()
            third = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertIn("src/webhook_worker.py", third["removed_files"])
            db = store.db_path(project_workspace.paths_for(paths.root, "demo").project_dir)
            branch = engine.branch_identity("demo", repo)
            self.assertFalse(store.definitions(db, "demo", branch.branch_key, "should_process_delivery"))

    def test_branch_membership_prevents_cross_branch_leakage(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            (repo / "main_only.py").write_text("def main_only_symbol():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "main"], check=True, capture_output=True)
            engine.index_project_code(paths, "demo", include_qdrant=False)
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feature"], check=True, capture_output=True)
            (repo / "main_only.py").unlink()
            (repo / "feature_only.py").write_text("def feature_only_symbol():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "feature"], check=True, capture_output=True)
            engine.index_project_code(paths, "demo", include_qdrant=False)
            feature = engine.search_project_code(paths, "demo", "main_only_symbol", mode="exact", include_qdrant=False)
            self.assertEqual(feature["hits"], [])
            self.assertTrue(engine.search_project_code(paths, "demo", "feature_only_symbol", mode="exact", include_qdrant=False)["hits"])
            subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True)
            main = engine.search_project_code(paths, "demo", "main_only_symbol", mode="exact", include_qdrant=False)
            self.assertTrue(main["hits"])
            self.assertEqual(main["scope"]["branch_name"], "main")

    def test_identical_symbols_can_exist_on_multiple_branches_without_id_collision(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            (repo / "shared.py").write_text("def shared_symbol():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "main"], check=True, capture_output=True)
            engine.index_project_code(paths, "demo", include_qdrant=False)
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feature"], check=True, capture_output=True)
            engine.index_project_code(paths, "demo", include_qdrant=False)

            db = store.db_path(project_workspace.paths_for(paths.root, "demo").project_dir)
            branches = {row["branch_name"]: row["branch_key"] for row in store.all_branches(db)}
            self.assertEqual(set(branches), {"main", "feature"})
            main_defs = store.definitions(db, "demo", branches["main"], "shared_symbol")
            feature_defs = store.definitions(db, "demo", branches["feature"], "shared_symbol")
            self.assertEqual(len(main_defs), 1)
            self.assertEqual(len(feature_defs), 1)
            self.assertNotEqual(main_defs[0]["symbol_id"], feature_defs[0]["symbol_id"])

    def test_duplicate_parser_references_are_deduplicated_during_refresh_and_claim_validation(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            (repo / "flow.py").write_text(
                "def target():\n"
                "    return True\n\n"
                "def entry():\n"
                "    return target()\n",
                encoding="utf-8",
            )
            real_parse = engine.parse_source

            def duplicate_reference_parse(path, data, embedding_profile_hash):
                parsed = real_parse(path, data, embedding_profile_hash)
                self.assertTrue(parsed.references)
                return replace(parsed, references=parsed.references + (parsed.references[0],))

            with mock.patch("code_search.engine.parse_source", side_effect=duplicate_reference_parse):
                refreshed = engine.index_project_code(
                    paths, "demo", include_qdrant=False, force=True
                )
                self.assertEqual(refreshed["status"], "indexed")
                self.assertEqual(
                    refreshed["reference_integrity"]["duplicate_references_deduplicated"], 1
                )
                result = code_validate_claim(
                    "entry calls target",
                    name="demo",
                    refresh_index=True,
                    paths=paths,
                )
            self.assertEqual(result["verdict"], "VERIFIED")
            db = store.db_path(project_workspace.paths_for(paths.root, "demo").project_dir)
            branch = engine.branch_identity("demo", repo)
            counts = store.counts(db, branch.branch_key)
            original = real_parse(
                "flow.py",
                (repo / "flow.py").read_bytes(),
                engine.vector_store.embedding_profile_hash(),
            )
            self.assertEqual(counts["references"], len(original.references))

    def test_project_refresh_deduplicates_duplicate_parser_references_without_sqlite_error(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            (repo / "refresh_flow.py").write_text(
                "def target():\n"
                "    return True\n\n"
                "def entry():\n"
                "    return target()\n",
                encoding="utf-8",
            )
            real_parse = engine.parse_source

            def duplicate_reference_parse(path, data, embedding_profile_hash):
                parsed = real_parse(path, data, embedding_profile_hash)
                return replace(parsed, references=parsed.references + (parsed.references[0],))

            with mock.patch("code_search.engine.parse_source", side_effect=duplicate_reference_parse):
                result = project_refresh(
                    name="demo",
                    reason="duplicate reference regression",
                    include_code=True,
                    include_qdrant=False,
                    paths=paths,
                )
            code_index = result["index"]["code_index"]
            self.assertEqual(code_index["status"], "indexed")
            self.assertEqual(
                code_index["reference_integrity"]["duplicate_references_deduplicated"], 1
            )
            self.assertEqual(
                code_index_status(name="demo", paths=paths)["reference_integrity"]
                ["duplicate_references_deduplicated"],
                1,
            )

    def test_conflicting_parser_reference_identity_returns_structured_rejection_not_sqlite_error(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            (repo / "conflict_flow.py").write_text(
                "def target():\n"
                "    return True\n\n"
                "def entry():\n"
                "    return target()\n",
                encoding="utf-8",
            )
            real_parse = engine.parse_source

            def conflicting_reference_parse(path, data, embedding_profile_hash):
                parsed = real_parse(path, data, embedding_profile_hash)
                first = parsed.references[0]
                conflict = replace(first, source_text=first.source_text + " # conflicting")
                return replace(parsed, references=(first, conflict, *parsed.references[1:]))

            with mock.patch("code_search.engine.parse_source", side_effect=conflicting_reference_parse):
                result = engine.index_project_code(
                    paths, "demo", include_qdrant=False, force=True
                )
            self.assertEqual(result["status"], "rejected")
            self.assertIn("reference identity conflict", result["reason"])
            self.assertNotIn("UNIQUE constraint", result.get("error", ""))
            self.assertEqual(result["reference_integrity"]["identity_conflicts"], 1)

    def test_conflicting_reference_identity_is_rejected_before_existing_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "code.sqlite"
            branch = BranchIdentity(
                repo_id="demo:repo",
                branch_key="working-tree:test",
                branch_name="test",
                commit_sha="abc",
                dirty=False,
                source="test",
            )
            initial = ParsedFile(
                language="python",
                parser_id="test",
                parse_mode="test",
                parse_status="ok",
                references=(
                    CodeReference(
                        reference_id="stable-ref",
                        source_symbol_id=None,
                        reference_kind="branch",
                        target_name="If",
                        target_qualified_hint="flag",
                        line=1,
                        column=0,
                        source_text="if flag:",
                        control_context=(),
                    ),
                ),
            )
            store.replace_file(
                db,
                file_id="old-file",
                project_id="demo",
                branch=branch,
                rel_path="flow.py",
                content_hash="old-hash",
                size_bytes=1,
                parsed=initial,
                indexed_at="2026-08-07T00:00:00Z",
            )
            conflict = replace(
                initial,
                references=(
                    initial.references[0],
                    replace(initial.references[0], source_text="if different_flag:"),
                ),
            )
            with self.assertRaises(store.ReferenceIdentityConflict):
                store.replace_file(
                    db,
                    file_id="new-file",
                    project_id="demo",
                    branch=branch,
                    rel_path="flow.py",
                    content_hash="new-hash",
                    size_bytes=2,
                    parsed=conflict,
                    indexed_at="2026-08-07T00:00:01Z",
                )
            row = store.file_record(db, "demo", branch.branch_key, "flow.py")
            self.assertIsNotNone(row)
            self.assertEqual(row["file_id"], "old-file")
            self.assertEqual(row["content_hash"], "old-hash")

    def test_deterministic_claim_validation_uses_no_semantic_backends(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            source = repo / "auth.py"
            source.write_text(
                "EXPECTED_ISSUER = 'trusted'\n\n"
                "def validate_claims(payload):\n"
                "    if payload.get('iss') != EXPECTED_ISSUER:\n"
                "        raise ValueError('invalid issuer')\n"
                "    return True\n\n"
                "def handler(payload):\n"
                "    return validate_claims(payload)\n",
                encoding="utf-8",
            )
            with mock.patch("rag_backend.embed_query", side_effect=AssertionError("embedding must not run")), \
                 mock.patch("rag_backend.rerank_hits", side_effect=AssertionError("reranker must not run")):
                call = code_validate_claim("handler calls validate_claims", name="demo", paths=paths)
                behavior = code_validate_claim(
                    "validate_claims raises ValueError('invalid issuer') when payload.get('iss') != EXPECTED_ISSUER",
                    name="demo",
                    paths=paths,
                )
            self.assertEqual(call["verdict"], "VERIFIED")
            self.assertEqual(behavior["verdict"], "VERIFIED")
            wrong = code_validate_claim(
                "validate_claims raises ValueError('invalid issuer') when payload.get('iss') != OTHER_ISSUER",
                name="demo",
                paths=paths,
            )
            self.assertEqual(wrong["verdict"], "INCONCLUSIVE")

    def test_atomic_validator_remains_strict_for_broad_natural_language_request(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "flow.py").write_text(
                "def entry(flag):\n"
                "    if flag:\n"
                "        return 'yes'\n"
                "    return 'no'\n",
                encoding="utf-8",
            )
            result = code_validate_claim(
                "validate the flow tree execution", name="demo", paths=paths
            )
            self.assertEqual(result["verdict"], "INCONCLUSIVE")
            self.assertIn("supported deterministic proof obligation", result["reason"])
            self.assertTrue(result.get("supported_claims"))

    def test_parameter_shadowing_never_validates_a_global_call_claim(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "shadowed.py").write_text(
                "def target():\n"
                "    return 'global'\n\n"
                "def entry(target):\n"
                "    return target()\n",
                encoding="utf-8",
            )
            result = code_validate_claim("entry calls target", name="demo", paths=paths)
            self.assertEqual(result["verdict"], "INCONCLUSIVE")
            self.assertIn("parameter", result["reason"])
            # The broad static graph may contain a same-name candidate, but the
            # strict validator must not turn that navigation edge into proof.
            self.assertTrue(result.get("graph_evidence"))

    def test_conditional_module_rebinding_blocks_direct_call_verification(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "rebound.py").write_text(
                "def target():\n"
                "    return True\n\n"
                "if ENABLE_OVERRIDE:\n"
                "    target = lambda: False\n\n"
                "def entry():\n"
                "    return target()\n",
                encoding="utf-8",
            )
            result = code_validate_claim("entry calls target", name="demo", paths=paths)
            self.assertEqual(result["verdict"], "INCONCLUSIVE")
            self.assertIn("rebinds", result["reason"])

    def test_explicit_global_rebinding_blocks_direct_call_verification(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "global_rebound.py").write_text(
                "def target():\n"
                "    return True\n\n"
                "def replace(value):\n"
                "    global target\n"
                "    target = value\n\n"
                "def entry():\n"
                "    return target()\n",
                encoding="utf-8",
            )
            result = code_validate_claim("entry calls target", name="demo", paths=paths)
            self.assertEqual(result["verdict"], "INCONCLUSIVE")
            self.assertIn("rebinds", result["reason"])

    def test_bare_method_name_is_not_validated_as_same_class_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "method_shadow.py").write_text(
                "class Service:\n"
                "    def target(self):\n"
                "        return True\n\n"
                "    def entry(self):\n"
                "        return target()\n",
                encoding="utf-8",
            )
            result = code_validate_claim("Service.entry calls Service.target", name="demo", paths=paths)
            self.assertEqual(result["verdict"], "INCONCLUSIVE")
            self.assertIn("module-level", result["reason"])

    def test_reach_claim_reproves_every_python_edge_from_fresh_source(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "flow.py").write_text(
                "def sink():\n"
                "    return True\n\n"
                "def middle():\n"
                "    return sink()\n\n"
                "def entry():\n"
                "    return middle()\n",
                encoding="utf-8",
            )
            result = code_validate_claim("entry can reach sink", name="demo", paths=paths)
            self.assertEqual(result["verdict"], "VERIFIED")
            self.assertEqual(len(result["edge_proofs"]), 2)
            self.assertTrue(all(edge["verdict"] == "VERIFIED" for edge in result["edge_proofs"]))

    def test_cross_file_reach_remains_inconclusive_without_binding_proof(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "target.py").write_text("def sink():\n    return True\n", encoding="utf-8")
            (repo / "entry.py").write_text(
                "from target import sink\n\n"
                "def entry():\n"
                "    return sink()\n",
                encoding="utf-8",
            )
            result = code_validate_claim("entry can reach sink", name="demo", paths=paths)
            self.assertEqual(result["verdict"], "INCONCLUSIVE")

    def test_decorated_function_behavior_claim_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "decorated.py").write_text(
                "def wrapper(fn):\n"
                "    return fn\n\n"
                "@wrapper\n"
                "def validate(value):\n"
                "    if value != 1:\n"
                "        raise ValueError('bad')\n",
                encoding="utf-8",
            )
            result = code_validate_claim(
                "validate raises ValueError('bad') when value != 1", name="demo", paths=paths
            )
            self.assertEqual(result["verdict"], "INCONCLUSIVE")
            self.assertTrue(any(
                "decorators may replace or wrap" in blocker
                for candidate in result.get("candidates", [])
                for blocker in candidate.get("blockers", [])
            ))

    def test_nested_conditional_is_not_claimed_as_unconditional_outcome(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "nested.py").write_text(
                "def guarded(x, enabled):\n"
                "    if x != 1:\n"
                "        if enabled:\n"
                "            raise ValueError('bad')\n",
                encoding="utf-8",
            )
            result = code_validate_claim(
                "guarded raises ValueError('bad') when x != 1", name="demo", paths=paths
            )
            self.assertEqual(result["verdict"], "INCONCLUSIVE")

    def test_cross_project_scope_is_explicit_and_labeled(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths, repo_a = self.make_project(root, "alpha")
            _, repo_b = self.make_project(root, "beta")
            (repo_a / "a.py").write_text("def alpha_guard():\n    return True\n", encoding="utf-8")
            (repo_b / "b.py").write_text("def beta_guard():\n    return True\n", encoding="utf-8")
            rejected = cross_project_code_search("guard", projects=[], paths=paths)
            self.assertEqual(rejected["status"], "rejected")
            result = cross_project_code_search(
                "guard", projects=["alpha", "beta"], view="peek", refresh_stale=True, paths=paths
            )
            self.assertEqual(result["scope"]["projects"], ["alpha", "beta"])
            self.assertEqual({hit["project_id"] for hit in result["hits"]}, {"alpha", "beta"})

    def test_cross_project_search_does_not_refresh_without_explicit_permission(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths, repo_a = self.make_project(root, "alpha")
            _, repo_b = self.make_project(root, "beta")
            (repo_a / "a.py").write_text("def alpha_guard():\n    return True\n", encoding="utf-8")
            (repo_b / "b.py").write_text("def beta_guard():\n    return True\n", encoding="utf-8")
            engine.index_project_code(paths, "alpha", include_qdrant=False)
            result = cross_project_code_search(
                "guard", projects=["alpha", "beta"], view="peek", paths=paths
            )
            self.assertEqual({hit["project_id"] for hit in result["hits"]}, {"alpha"})
            statuses = {row["project_id"]: row["status"] for row in result["projects"]}
            self.assertEqual(statuses["beta"], "not_indexed")

    def test_git_ignored_untracked_source_is_not_enumerated(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            (repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
            (repo / "ignored.py").write_text("def ignored_symbol():\n    return True\n", encoding="utf-8")
            (repo / "visible.py").write_text("def visible_symbol():\n    return True\n", encoding="utf-8")
            result = engine.index_project_code(paths, "demo", include_qdrant=False)
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            enumerated = {
                row["repo_relative"]
                for row in [*manifest["included"], *manifest["excluded"]]
            }
            self.assertIn("visible.py", enumerated)
            self.assertNotIn("ignored.py", enumerated)

    def test_commit_metadata_updates_without_reembedding_unchanged_source(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            (repo / "stable.py").write_text("def stable_symbol():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "first"], check=True, capture_output=True)
            first = engine.index_project_code(paths, "demo", include_qdrant=False)
            first_commit = first["branch"]["commit_sha"]
            subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-m", "metadata"], check=True, capture_output=True)
            second = engine.index_project_code(paths, "demo", include_qdrant=False)
            second_commit = second["branch"]["commit_sha"]
            self.assertNotEqual(first_commit, second_commit)
            self.assertGreaterEqual(second["metadata_updated_files"], 1)
            hit = engine.search_project_code(
                paths, "demo", "stable_symbol", mode="exact", include_qdrant=False
            )["hits"][0]
            self.assertEqual(hit["commit_sha"], second_commit)

    def test_failed_vector_sync_retains_previous_membership_snapshot_for_retry(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            source = repo / "guard.py"
            source.write_text("def guard():\n    return True\n", encoding="utf-8")

            def successful_sync(**kwargs):
                return {
                    "status": "indexed",
                    "membership_hash": engine.vector_store.membership_hash(
                        kwargs["new_rows"], kwargs["project_id"], kwargs["branch_key"]
                    ),
                    "new_vectors": len(kwargs["new_rows"]),
                    "reused_vectors": 0,
                    "removed_memberships": len(kwargs["old_rows"]),
                }

            with mock.patch.object(engine.vector_store, "sync_branch_memberships", side_effect=successful_sync):
                engine.index_project_code(paths, "demo", include_qdrant=True)
            db = store.db_path(project_workspace.paths_for(paths.root, "demo").project_dir)
            branch = engine.branch_identity("demo", repo)
            first_snapshot = store.synced_vector_memberships(db, "demo", branch.branch_key)
            self.assertTrue(first_snapshot)
            first_state = store.read_state(db, branch.branch_key)
            first_vector_hash = first_state["qdrant_membership_hash"]

            source.unlink()
            with mock.patch.object(engine.vector_store, "sync_branch_memberships", return_value={
                "status": "degraded",
                "reason": "qdrant unavailable",
                "membership_hash": engine.vector_store.membership_hash([], "demo", branch.branch_key),
            }):
                engine.index_project_code(paths, "demo", include_qdrant=True)
            self.assertEqual(
                store.synced_vector_memberships(db, "demo", branch.branch_key),
                first_snapshot,
            )
            failed_state = store.read_state(db, branch.branch_key)
            self.assertEqual(failed_state["qdrant_membership_hash"], first_vector_hash)
            self.assertEqual(failed_state["vector_status"], "degraded")

            retry_calls: list[tuple[list[dict], list[dict]]] = []

            def retry_sync(**kwargs):
                retry_calls.append((list(kwargs["old_rows"]), list(kwargs["new_rows"])))
                return {
                    "status": "indexed",
                    "membership_hash": engine.vector_store.membership_hash(
                        kwargs["new_rows"], kwargs["project_id"], kwargs["branch_key"]
                    ),
                    "new_vectors": 0,
                    "reused_vectors": 0,
                    "removed_memberships": len(kwargs["old_rows"]),
                }

            with mock.patch.object(engine.vector_store, "sync_branch_memberships", side_effect=retry_sync):
                engine.index_project_code(paths, "demo", include_qdrant=True)
            self.assertEqual(retry_calls[0][0], first_snapshot)
            self.assertEqual(retry_calls[0][1], [])
            self.assertEqual(store.synced_vector_memberships(db, "demo", branch.branch_key), [])

    def test_code_specific_text_allowlist_includes_supported_source_names(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "Guard.cs").write_text("class Guard { bool Check() { return true; } }\n", encoding="utf-8")
            (repo / "Makefile").write_text("all:\n\t@echo ok\n", encoding="utf-8")
            result = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertEqual(result["counts"]["files"], 2)
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual({row["repo_relative"] for row in manifest["included"]}, {"Guard.cs", "Makefile"})

    def test_sensitive_and_non_source_files_are_not_indexed(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "safe.py").write_text("def safe_symbol():\n    return True\n", encoding="utf-8")
            (repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            (repo / "README.md").write_text("secret semantic decoy\n", encoding="utf-8")
            result = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertEqual(result["counts"]["files"], 1)
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            reasons = {item.get("reason") for item in manifest["excluded"]}
            self.assertIn("unsupported_code_extension", reasons)
            env_row = next(item for item in manifest["excluded"] if item.get("repo_relative") == ".env")
            self.assertTrue(env_row["lexical_included"])
            self.assertEqual(env_row["policy_reason"], "sensitive_text_lexical_only")
            self.assertEqual(env_row["reason"], "lexical_only_policy")
            self.assertFalse(env_row["included"])

    def test_import_alias_call_resolves_without_receiver_guessing(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            package = repo / "pkg"
            package.mkdir()
            (package / "mod.py").write_text("def target():\n    return True\n", encoding="utf-8")
            (repo / "entry.py").write_text(
                "import pkg.mod as pm\n\n"
                "def entry():\n"
                "    return pm.target()\n",
                encoding="utf-8",
            )
            result = code_path("entry", "target", name="demo", paths=paths)
            self.assertEqual(result["details"]["graph"]["status"], "found")
            self.assertEqual(result["details"]["graph"]["path"][0]["resolution_method"], "import_qualified")

    def test_tree_sitter_import_binding_parser_preserves_alias_targets(self):
        self.assertEqual(
            parser._import_bindings("python", "import pkg.mod as pm"),
            [("pm", "pkg.mod")],
        )
        self.assertEqual(
            parser._import_bindings("python", "from pkg.mod import target as run, other"),
            [("run", "pkg.mod.target"), ("other", "pkg.mod.other")],
        )
        self.assertEqual(
            parser._import_bindings("typescript", "import { target as run } from './pkg/mod'"),
            [("run", "pkg.mod.target")],
        )

    def test_structural_chunks_do_not_duplicate_nested_symbol_bodies(self):
        source = (
            "MODULE_TOKEN = True\n\n"
            "class Service:\n"
            "    class_value = 1\n\n"
            "    def run(self):\n"
            "        nested_unique_token = 42\n"
            "        return nested_unique_token\n\n"
            "@decorate\n"
            "def decorated_entry():\n"
            "    return Service().run()\n\n"
            "AFTER_TOKEN = True\n"
        ).encode("utf-8")
        parsed = parser.parse_source("src/service.py", source, "embedding-profile")
        by_symbol: dict[str, list[str]] = {}
        for chunk in parsed.chunks:
            by_symbol.setdefault(chunk.symbol_name, []).append(chunk.text)
        self.assertIn("nested_unique_token", "\n".join(by_symbol["run"]))
        self.assertNotIn("nested_unique_token", "\n".join(by_symbol["Service"]))
        module_text = "\n".join(by_symbol["service.py"])
        self.assertIn("MODULE_TOKEN", module_text)
        self.assertIn("AFTER_TOKEN", module_text)
        self.assertNotIn("nested_unique_token", module_text)
        decorated = next(symbol for symbol in parsed.symbols if symbol.name == "decorated_entry")
        self.assertEqual(decorated.start_line, 11)
        self.assertIn("@decorate", "\n".join(by_symbol["decorated_entry"]))

    def test_exact_search_treats_like_wildcards_as_literals(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "percent.py").write_text("VALUE = '100%'\n", encoding="utf-8")
            (repo / "plain.py").write_text("VALUE = '100x'\n", encoding="utf-8")
            result = engine.search_project_code(
                paths, "demo", "%", mode="exact", include_qdrant=False
            )
            self.assertEqual({hit["path"] for hit in result["hits"]}, {"percent.py"})

    def test_definition_lookup_uses_strongest_exact_tier(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "names.py").write_text(
                "def entry():\n    return True\n\n"
                "def entrypoint():\n    return False\n",
                encoding="utf-8",
            )
            result = code_definition("entry", name="demo", paths=paths)
            self.assertEqual(result["status"], "ok")
            self.assertEqual([hit["symbol"] for hit in result["hits"]], ["entry"])

    def test_results_expose_parser_mode_and_status(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "mode.py").write_text("def parser_mode_symbol():\n    return True\n", encoding="utf-8")
            hit = code_definition("parser_mode_symbol", name="demo", paths=paths)["hits"][0]
            self.assertIn(hit["parse_mode"], {"tree_sitter", "python_ast_fallback"})
            self.assertIn(hit["parse_status"], {"ok", "partial"})
            self.assertIsInstance(hit["parse_diagnostics"], list)

    def test_bounded_views_have_distinct_source_budgets(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            source = "def bounded_symbol():\n" + "".join(
                f"    value_{index} = {index}\n" for index in range(240)
            ) + "    return value_239\n"
            (repo / "bounded.py").write_text(source, encoding="utf-8")
            peek = code_definition("bounded_symbol", name="demo", view="peek", paths=paths)["hits"][0]
            context = code_definition("bounded_symbol", name="demo", view="context", paths=paths)["hits"][0]
            full = code_definition("bounded_symbol", name="demo", view="full", paths=paths)["hits"][0]
            self.assertEqual(peek["preview"], "")
            self.assertLess(len(context["preview"]), len(full["preview"]))
            self.assertIn("return value_239", full["preview"])

    def test_arbitrary_receiver_call_is_not_guessed_from_unique_method_name(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "receiver.py").write_text(
                "class Service:\n"
                "    def target(self):\n"
                "        return True\n\n"
                "def entry(Service):\n"
                "    return Service.target()\n",
                encoding="utf-8",
            )
            claim = code_validate_claim("entry calls target", name="demo", paths=paths)
            self.assertEqual(claim["verdict"], "INCONCLUSIVE")
            callees = engine.callees_lookup(paths, "demo", "entry")
            self.assertEqual(callees["hits"][0]["resolution_status"], "unresolved")

    def test_module_scope_calls_participate_in_static_paths(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "top.py").write_text(
                "def sink():\n"
                "    return True\n\n"
                "sink()\n",
                encoding="utf-8",
            )
            result = code_path("module:top", "sink", name="demo", paths=paths)
            self.assertEqual(result["details"]["graph"]["status"], "found")
            self.assertEqual(result["details"]["graph"]["path"][0]["call_source"], "sink()")

    def test_graph_path_reports_static_control_context(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "flow.py").write_text(
                "def sink():\n"
                "    return True\n\n"
                "def entry(enabled):\n"
                "    if enabled:\n"
                "        return sink()\n"
                "    return False\n",
                encoding="utf-8",
            )
            result = code_path("entry", "sink", name="demo", paths=paths)
            self.assertEqual(result["details"]["graph"]["status"], "found")
            edge = result["details"]["graph"]["path"][0]
            self.assertIn("if: enabled", edge["control_context"])
            self.assertIn("sink()", edge["call_source"])

    def test_claim_with_additional_enclosing_condition_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "nested_guard.py").write_text(
                "def guarded(x, enabled):\n"
                "    if enabled:\n"
                "        if x != 1:\n"
                "            raise ValueError('bad')\n",
                encoding="utf-8",
            )
            result = code_validate_claim(
                "guarded raises ValueError('bad') when x != 1", name="demo", paths=paths
            )
            self.assertEqual(result["verdict"], "INCONCLUSIVE")
            self.assertTrue(any(
                "additional enclosing control dependency" in blocker
                for candidate in result.get("candidates", [])
                for blocker in candidate.get("blockers", [])
            ))

    def test_general_project_rag_freshness_is_not_coupled_to_code_changes(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            source = repo / "fresh.py"
            source.write_text("def fresh():\n    return True\n", encoding="utf-8")
            index_project(
                include_artifacts=False,
                include_code=True,
                include_qdrant=False,
                project_id="demo",
                paths=paths,
            )
            source.write_text("def fresh():\n    return False\n", encoding="utf-8")
            general = project_workspace.project_status(paths.root, "demo")
            self.assertTrue(general["index_freshness"]["fresh"])
            code = code_index_verify(name="demo", include_qdrant=False, paths=paths)
            self.assertEqual(code["status"], "stale")
            self.assertIn("source_probe", code["freshness"]["stale_reasons"])

    def test_golden_evaluation_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths, repo = self.make_project(root)
            self.write_fixture(repo)
            suite = root / "suite.jsonl"
            suite.write_text(
                json.dumps({
                    "id": "definition",
                    "projects": ["demo"],
                    "query": "Where is should_process_delivery defined?",
                    "mode": "definition",
                    "expected": [{"path": "src/webhook_worker.py", "symbol": "should_process_delivery", "grade": 3}],
                    "forbidden_paths": [".env"],
                    "include_qdrant": False,
                }) + "\n",
                encoding="utf-8",
            )
            report = root / "report.json"
            result = evaluation.run_suite(paths, suite, report_path=report)
            self.assertEqual(result["summary"]["hit_at_1"], 1.0)
            self.assertEqual(result["summary"]["forbidden_path_leakage_count"], 0)
            self.assertTrue(report.exists())

    def test_golden_evaluation_can_materialize_isolated_fixture_projects(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            suite = root / "fixture-suite.jsonl"
            fixture = {
                "type": "fixture",
                "projects": {
                    "fixture-project": {
                        "files": {
                            "src/example.py": "def exact_fixture_symbol():\n    return True\n",
                            ".env": "SECRET=must-not-be-indexed\n",
                        }
                    }
                },
            }
            query = {
                "id": "fixture-definition",
                "projects": ["fixture-project"],
                "query": "Where is exact_fixture_symbol defined?",
                "mode": "definition",
                "expected": [
                    {"path": "src/example.py", "symbol": "exact_fixture_symbol", "grade": 3}
                ],
                "forbidden_paths": [".env"],
                "include_qdrant": False,
            }
            suite.write_text(
                json.dumps(fixture) + "\n" + json.dumps(query) + "\n", encoding="utf-8"
            )
            result = evaluation.run_suite(paths, suite)
            self.assertTrue(result["fixture"]["isolated"])
            self.assertEqual(result["summary"]["hit_at_1"], 1.0)
            self.assertEqual(result["summary"]["forbidden_path_leakage_count"], 0)
            self.assertFalse((root / "workspace" / "projects" / "fixture-project").exists())

    def test_bundled_offline_golden_suites_pass_release_acceptance(self):
        suites_root = Path(__file__).resolve().parents[1] / "evaluation" / "code_search" / "suites"
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            for suite_name in ("smoke", "graph", "security", "cross-project", "branches"):
                with self.subTest(suite=suite_name):
                    result = evaluation.run_suite(paths, suites_root / f"{suite_name}.jsonl")
                    self.assertTrue(result["acceptance"]["passed"], result["results"])
                    self.assertEqual(result["summary"]["forbidden_path_leakage_count"], 0)
                    self.assertEqual(result["summary"]["cross_branch_leakage_count"], 0)

    def test_parser_runtime_profile_reports_dependency_and_abi_identity(self):
        profile = languages.parser_runtime_profile()
        self.assertEqual(
            set(profile["dependency_versions"]),
            {
                "tree-sitter-language-pack",
                "tree-sitter",
                "tree-sitter-c-sharp",
                "tree-sitter-embedded-template",
                "tree-sitter-yaml",
            },
        )
        self.assertEqual(set(profile["language_abi"]), {"maximum", "minimum"})

    def test_multilingual_golden_suite_requires_real_tree_sitter_parsers(self):
        if not languages.parser_runtime_profile()["available"]:
            self.skipTest("tree-sitter-language-pack is not installed in this test environment")
        suites_root = Path(__file__).resolve().parents[1] / "evaluation" / "code_search" / "suites"
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            result = evaluation.run_suite(paths, suites_root / "multilingual.jsonl")
            self.assertTrue(result["acceptance"]["passed"], result["results"])
            observed = result["fixture"]["parse_modes_by_language"]
            for language in ("python", "typescript", "go"):
                self.assertGreaterEqual(observed[language]["tree_sitter"], 1)

    def test_fixture_qdrant_mode_is_explicit_and_cleans_memberships(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            suite = root / "qdrant-fixture.jsonl"
            suite.write_text(
                json.dumps({
                    "type": "fixture",
                    "include_qdrant": True,
                    "projects": {
                        "fixture-project": {
                            "files": {"src/example.py": "def exact_fixture_symbol():\n    return True\n"}
                        }
                    },
                })
                + "\n"
                + json.dumps({
                    "id": "fixture-definition",
                    "projects": ["fixture-project"],
                    "query": "Where is exact_fixture_symbol defined?",
                    "mode": "definition",
                    "expected": [
                        {"path": "src/example.py", "symbol": "exact_fixture_symbol", "grade": 3}
                    ],
                    "include_qdrant": False,
                })
                + "\n",
                encoding="utf-8",
            )
            calls: list[dict[str, object]] = []

            def fake_sync(**kwargs):
                calls.append(kwargs)
                return {
                    "status": "indexed",
                    "membership_hash": "fixture",
                    "new_vectors": len(kwargs.get("new_rows") or []),
                    "reused_vectors": 0,
                    "removed_memberships": len(kwargs.get("old_rows") or [])
                    if not kwargs.get("new_rows")
                    else 0,
                    "deleted_points": 1 if kwargs.get("old_rows") and not kwargs.get("new_rows") else 0,
                }

            with mock.patch.object(evaluation.vector_store, "sync_branch_memberships", side_effect=fake_sync):
                result = evaluation.run_suite(paths, suite)
            self.assertTrue(result["acceptance"]["passed"])
            self.assertEqual(result["fixture"]["qdrant_cleanup"]["status"], "cleaned")
            self.assertTrue(any(call.get("new_rows") for call in calls))
            self.assertTrue(any(call.get("old_rows") and not call.get("new_rows") for call in calls))

    def test_fixture_query_cannot_request_qdrant_without_vector_indexing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            suite = root / "invalid-vector-suite.jsonl"
            suite.write_text(
                json.dumps({
                    "type": "fixture",
                    "projects": {"fixture-project": {"files": {"src/example.py": "def x():\n    pass\n"}}},
                })
                + "\n"
                + json.dumps({
                    "id": "invalid",
                    "projects": ["fixture-project"],
                    "query": "x",
                    "mode": "exact",
                    "include_qdrant": True,
                    "expected": [{"path": "src/example.py", "symbol": "x", "grade": 3}],
                })
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "fixture was not indexed with include_qdrant=true"):
                evaluation.run_suite(paths, suite)

    def test_golden_evaluation_rejects_unsafe_fixture_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            suite = root / "unsafe-suite.jsonl"
            suite.write_text(
                json.dumps({
                    "type": "fixture",
                    "projects": {
                        "fixture-project": {"files": {"../escape.py": "pass\n"}}
                    },
                }) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unsafe fixture repository path"):
                evaluation.run_suite(paths, suite)

    def test_empty_source_file_is_not_mistaken_for_a_changed_source(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            package = repo / "tests" / "example"
            package.mkdir(parents=True, exist_ok=True)
            (package / "__init__.py").write_bytes(b"")
            (package / "worker.py").write_text(
                "def run():\n    return True\n", encoding="utf-8"
            )

            result = engine.index_project_code(paths, "demo", include_qdrant=False)

            self.assertNotEqual(result["status"], "stale_source", result)
            self.assertIn(result["status"], {"indexed", "degraded"}, result)
            status = engine.index_status(paths, "demo", deep_verify=True, verify_qdrant=False)
            self.assertTrue(status["freshness"]["lexical_current"], status)
            self.assertGreaterEqual(status["counts"]["files"], 2, status)

    def test_source_changed_after_policy_scan_is_never_parsed_or_embedded(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            source = repo / "race.py"
            source.write_text("def current():\n    return True\n", encoding="utf-8")
            stale_entry = {
                "included": True,
                "reason": "safe_allowlist",
                "repo_relative": "race.py",
                "absolute_path": str(source),
                "content_hash": "0" * 64,
                "size_bytes": source.stat().st_size,
            }
            with mock.patch.object(engine, "_scan_repository", return_value=([stale_entry], [], "stale-probe")), \
                 mock.patch.object(engine, "parse_source") as parse_mock:
                result = engine.ensure_current(paths, "demo", include_qdrant=False)
            self.assertEqual(result["status"], "stale_source")
            self.assertEqual(result["path"], "race.py")
            parse_mock.assert_not_called()

    def test_index_status_checks_that_confirmed_vector_collection_still_exists(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            self.write_fixture(repo)
            engine.index_project_code(paths, "demo", include_qdrant=False)
            pp = project_workspace.paths_for(paths.root, "demo")
            db = store.db_path(pp.project_dir)
            branch = engine.branch_identity("demo", repo)
            membership = engine.vector_store.membership_hash(
                store.branch_embedding_memberships(db, "demo", branch.branch_key),
                "demo",
                branch.branch_key,
            )
            with closing(sqlite3.connect(db)) as conn:
                conn.execute(
                    "UPDATE code_index_state SET vector_status='indexed', vector_reason='', qdrant_membership_hash=?",
                    (membership,),
                )
                conn.commit()
            manifest_path = store.manifest_path(pp.project_dir)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["published_vector_collection"] = engine.vector_store.code_collection_name()
            manifest["vector"] = {
                **dict(manifest.get("vector") or {}),
                "status": "indexed",
                "collection": engine.vector_store.code_collection_name(),
            }
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            with mock.patch.object(engine.vector_store, "collection_available", return_value=(False, "missing collection")):
                status = engine.index_status(paths, "demo", deep_verify=True, verify_qdrant=True)
            self.assertEqual(status["status"], "degraded")
            self.assertFalse(status["freshness"]["vector_current"] )
            self.assertIn("vector_collection", status["freshness"]["stale_reasons"] )
            self.assertEqual(status["freshness"]["vector_reason"], "missing collection")

    def test_python_claim_refuses_source_changed_after_indexing(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            source = repo / "claim.py"
            source.write_text(
                "def validate(value):\n"
                "    if value != 1:\n"
                "        raise ValueError('bad')\n",
                encoding="utf-8",
            )
            engine.index_project_code(paths, "demo", include_qdrant=False)
            pp = project_workspace.paths_for(paths.root, "demo")
            branch = engine.branch_identity("demo", repo)
            db = store.db_path(pp.project_dir)
            definitions = store.definitions(db, "demo", branch.branch_key, "validate")
            self.assertEqual(len(definitions), 1)
            source.write_text(
                "def validate(value):\n"
                "    return True\n",
                encoding="utf-8",
            )
            result = engine._validate_python_condition_claim(
                repo, definitions[0],
                "validate raises ValueError('bad') when value != 1",
            )
            self.assertEqual(result["verdict"], "STALE_SOURCE")
            self.assertNotEqual(result["source_sha256"], result["indexed_sha256"] )

    def test_source_roles_keep_tests_visible_without_confusing_production(self):
        self.assertEqual(indexing_policy.source_role("pipeline/authn/authenticator.go"), "production")
        self.assertEqual(indexing_policy.source_role("credentials/fetcher.go"), "production")
        self.assertEqual(indexing_policy.source_role("pipeline/authn/authenticator_test.go"), "test")
        self.assertEqual(indexing_policy.source_role("test/e2e/config.yml"), "test")
        self.assertEqual(indexing_policy.source_role("rule/testdata/rules.json"), "test_fixture")
        self.assertEqual(indexing_policy.source_role("vendor/example/generated.go"), "generated_or_vendor")

        rows = [
            {"path": "pipeline/authn/authenticator_test.go", "score": 1.0},
            {"path": "pipeline/authn/authenticator.go", "score": 1.0},
        ]
        normal = engine._rank_source_roles("how does authentication work", rows)
        self.assertEqual(normal[0]["source_role"], "production")
        tests = engine._rank_source_roles("authentication edge case tests", rows)
        self.assertEqual(tests[0]["source_role"], "test")
        self.assertEqual({r["source_role"] for r in normal}, {"production", "test"})


    def test_repository_evidence_binds_clean_git_snapshot_and_treats_author_as_claim(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            paths, repo = self.make_project(Path(td))
            (repo / "main.go").write_text("package demo\nfunc Main() {}\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "claimed@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Claimed Author"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)

            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            evidence = indexed["repository_evidence"]
            self.assertEqual(evidence["assurance"], "VERIFIED_SNAPSHOT")
            self.assertEqual(evidence["head_sha"], subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, text=True, capture_output=True
            ).stdout.strip())
            self.assertTrue(evidence["raw_tree_sha"])
            self.assertEqual(evidence["raw_tree_sha"], evidence["effective_tree_sha"])
            self.assertEqual(evidence["author_claim"]["name"], "Claimed Author")
            self.assertFalse(evidence["author_claim"]["verified"])
            self.assertIn(evidence["commit_signature"]["verification_status"], {"unsigned", "present_not_verified_external_verifier_not_executed"})

            passive = code_index_status(name="demo", paths=paths)
            self.assertTrue(passive["freshness"]["lexical_current"])
            self.assertEqual(passive["repository_assurance"], "VERIFIED_SNAPSHOT")
            self.assertEqual(passive["indexed_repository_evidence"]["raw_tree_sha"], evidence["raw_tree_sha"])

    def test_replace_ref_changes_git_view_without_changing_head_and_invalidates_index(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            paths, repo = self.make_project(Path(td))
            (repo / "main.py").write_text("def value():\n    return 1\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, text=True, capture_output=True).stdout.strip()
            engine.index_project_code(paths, "demo", include_qdrant=False)
            before = code_index_status(name="demo", paths=paths)
            self.assertTrue(before["freshness"]["lexical_current"])

            raw_tree = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], check=True, text=True, capture_output=True).stdout.strip()
            # Build a replacement commit with a genuinely different tree while
            # keeping the working tree itself clean at the original HEAD. This
            # proves that `git replace` can change the effective Git view without
            # changing the ref/HEAD SHA Awoki was initially given.
            (repo / "main.py").write_text("def value():\n    return 2\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "main.py"], check=True)
            replacement_tree = subprocess.run(
                ["git", "-C", str(repo), "write-tree"], check=True, text=True, capture_output=True
            ).stdout.strip()
            self.assertNotEqual(replacement_tree, raw_tree)
            subprocess.run(["git", "-C", str(repo), "reset", "--hard", head], check=True, capture_output=True)
            replacement = subprocess.run(
                ["git", "-C", str(repo), "commit-tree", replacement_tree, "-p", head],
                input="replacement view\n", text=True, check=True, capture_output=True,
            ).stdout.strip()
            subprocess.run(["git", "-C", str(repo), "replace", head, replacement], check=True)
            self.assertEqual(subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, text=True, capture_output=True
            ).stdout.strip(), head)

            after = code_index_status(name="demo", paths=paths)
            self.assertFalse(after["freshness"]["lexical_current"])
            self.assertIn("content_view", after["freshness"]["stale_reasons"])
            deep = provenance.collect_repository_evidence(repo, deep=True)
            self.assertTrue(deep["replace_refs_present"])
            self.assertIn("replace_refs_active", deep["anomalies"])
            self.assertIn("effective_tree_differs_from_raw_commit_tree", deep["anomalies"])
            self.assertNotEqual(deep["raw_tree_sha"], deep["effective_tree_sha"])
            self.assertEqual(deep["assurance"], "WORKING_TREE_BOUND")

    def test_source_evidence_id_detects_source_and_snapshot_drift(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            paths, repo = self.make_project(Path(td))
            source = repo / "main.py"
            source.write_text("def stable():\n    return 1\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            engine.index_project_code(paths, "demo", include_qdrant=False)

            window = code_source_window("main.py", name="demo", start_line=1, end_line=2, paths=paths)
            self.assertEqual(window["status"], "ok")
            token = window["evidence"]["evidence_id"]
            self.assertEqual(window["evidence"]["assurance"], "VERIFIED_SNAPSHOT")
            self.assertEqual(window["evidence"]["authenticity"], "self_contained_checksum_not_signature")
            current = code_evidence_verify(token, name="demo", paths=paths)
            self.assertEqual(current["verdict"], "CURRENT_VERIFIED_SNAPSHOT")
            self.assertTrue(current["current"])

            (repo / "other.txt").write_text("unrelated\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "other.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "unrelated"], check=True, capture_output=True)
            moved = code_evidence_verify(token, name="demo", paths=paths)
            self.assertEqual(moved["verdict"], "SOURCE_CURRENT_SNAPSHOT_CHANGED")
            self.assertTrue(moved["source_current"])
            self.assertFalse(moved["snapshot_current"])

            source.write_text("def stable():\n    return 2\n", encoding="utf-8")
            stale = code_evidence_verify(token, name="demo", paths=paths)
            self.assertEqual(stale["verdict"], "STALE_SOURCE")
            self.assertFalse(stale["source_current"])

    def test_non_git_source_remains_searchable_with_filesystem_bound_assurance(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            paths, repo = self.make_project(Path(td))
            (repo / "main.py").write_text("def filesystem_only():\n    return True\n", encoding="utf-8")
            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertEqual(indexed["repository_evidence"]["assurance"], "FILESYSTEM_BOUND")
            result = codebase_search("filesystem_only", name="demo", paths=paths)
            self.assertTrue(result["hits"])
            window = code_source_window("main.py", name="demo", start_line=1, end_line=2, paths=paths)
            self.assertEqual(window["evidence"]["assurance"], "FILESYSTEM_BOUND")
            verified = code_evidence_verify(window["evidence"]["evidence_id"], name="demo", paths=paths)
            self.assertEqual(verified["verdict"], "CURRENT_SOURCE_FILESYSTEM_BOUND")
            self.assertTrue(verified["current"])
            self.assertIsNone(verified["snapshot_current"])

    def test_sparse_checkout_never_claims_full_repository_text_universe(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            paths, repo = self.make_project(Path(td))
            (repo / "src").mkdir(parents=True, exist_ok=True)
            (repo / "hidden").mkdir(parents=True, exist_ok=True)
            (repo / "src" / "visible.go").write_text("package demo\nconst Visible = \"VISIBLE_NEEDLE\"\n", encoding="utf-8")
            (repo / "hidden" / "hidden.go").write_text("package demo\nconst Hidden = \"HIDDEN_NEEDLE\"\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "sparse-checkout", "init", "--cone"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "sparse-checkout", "set", "src"], check=True, capture_output=True)
            self.assertFalse((repo / "hidden" / "hidden.go").exists())

            constraints = provenance.repository_scope_constraints(repo)
            self.assertTrue(constraints["sparse_checkout"])
            self.assertGreaterEqual(constraints["unmaterialized_tracked_file_count"], 1)
            result = code_text_search("VISIBLE_NEEDLE", name="demo", fixed_string=True, paths=paths)
            self.assertFalse(result["repository_universe_complete"])
            self.assertGreaterEqual(result["repository_scope_constraints"]["unmaterialized_tracked_file_count"], 1)

    def test_submodules_are_explicit_scope_boundary_not_silent_nested_source(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "main.py").write_text("def root_symbol():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, text=True, capture_output=True).stdout.strip()
            subprocess.run(["git", "-C", str(repo), "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/lib"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "gitlink"], check=True, capture_output=True)
            constraints = provenance.repository_scope_constraints(repo)
            self.assertEqual(constraints["submodule_gitlink_count"], 1)
            self.assertFalse(constraints["submodule_repositories_scanned"])
            deep = provenance.collect_repository_evidence(repo, deep=True)
            self.assertIn("submodules_present", deep["anomalies"])
            self.assertEqual(deep["assurance"], "WORKING_TREE_BOUND")

    def test_content_filter_reduces_repository_assurance_without_hiding_source(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            paths, repo = self.make_project(Path(td))
            (repo / ".gitattributes").write_text("*.go filter=awoki-test\n", encoding="utf-8")
            (repo / "auth.go").write_text("package demo\nfunc AuthToken() string { return \"token-name-not-a-secret\" }\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "filter.awoki-test.clean", "cat"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "filter.awoki-test.smudge", "cat"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertEqual(indexed["repository_evidence"]["assurance"], "WORKING_TREE_BOUND")
            self.assertIn("worktree_content_filters_referenced", indexed["repository_evidence"]["anomalies"])
            found = codebase_search("AuthToken", name="demo", paths=paths)
            self.assertTrue(any(hit["path"] == "auth.go" for hit in found["hits"]))

    def test_passive_git_inspection_does_not_execute_configured_content_filter(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            root = Path(td)
            paths, repo = self.make_project(root)
            marker = root / "filter-executed"
            (repo / ".gitattributes").write_text("*.go filter=evil\n", encoding="utf-8")
            (repo / "auth.go").write_text("package demo\nfunc VisibleAuth() bool { return true }\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            # Commit while the attribute driver is undefined; Git treats the
            # non-required missing driver as pass-through and executes nothing.
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            command = f"sh -c 'touch {marker}; cat'"
            subprocess.run(["git", "-C", str(repo), "config", "filter.evil.clean", command], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "filter.evil.smudge", command], check=True)

            evidence = provenance.collect_repository_evidence(repo, deep=True)
            self.assertFalse(marker.exists())
            self.assertIn("worktree_content_filters_referenced", evidence["anomalies"])
            self.assertEqual(evidence["assurance"], "WORKING_TREE_BOUND")

            branch = engine.branch_identity("demo", repo)
            self.assertFalse(marker.exists())
            self.assertFalse(branch.dirty, "literal pass-through content should still prove clean without running filter")

            state = project_workspace._repository_state(project_workspace.paths_for(root, "demo"))
            self.assertFalse(marker.exists())
            self.assertEqual(state["cleanliness_proof"], "literal_worktree_index_no_filters")
            self.assertFalse(state["dirty"])

            # Search coverage remains intact even though provenance assurance
            # is lower because a helper-backed representation was configured.
            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertFalse(marker.exists())
            self.assertEqual(indexed["repository_evidence"]["assurance"], "WORKING_TREE_BOUND")
            found = codebase_search("VisibleAuth", name="demo", paths=paths)
            self.assertTrue(any(hit["path"] == "auth.go" for hit in found["hits"]))

    def test_git_repository_environment_overrides_cannot_rebind_project_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths, repo = self.make_project(root)
            (repo / "main.py").write_text("def real_repo():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "real"], check=True, capture_output=True)
            real_head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, text=True, capture_output=True
            ).stdout.strip()

            other = root / "other-repo"
            other.mkdir()
            (other / "other.py").write_text("def other_repo():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(other)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(other), "config", "user.email", "other@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(other), "config", "user.name", "Other"], check=True)
            subprocess.run(["git", "-C", str(other), "add", "."], check=True)
            subprocess.run(["git", "-C", str(other), "commit", "-m", "other"], check=True, capture_output=True)

            poisoned = {
                "GIT_DIR": str(other / ".git"),
                "GIT_WORK_TREE": str(other),
                "GIT_INDEX_FILE": str(other / ".git" / "index"),
            }
            with mock.patch.dict(os.environ, poisoned, clear=False):
                branch = engine.branch_identity("demo", repo)
                evidence = provenance.collect_repository_evidence(repo, deep=True)
                indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertEqual(branch.commit_sha, real_head)
            self.assertEqual(evidence["head_sha"], real_head)
            self.assertEqual(indexed["branch"]["commit_sha"], real_head)
            self.assertCountEqual(
                evidence["ignored_git_repository_environment_overrides"],
                ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"],
            )

    def test_signature_presence_does_not_execute_configured_verifier(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, repo = self.make_project(root)
            marker = root / "signature-verifier-executed"
            (repo / "main.py").write_text("def signed_looking():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True)
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, text=True, capture_output=True).stdout.strip()
            tree = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], check=True, text=True, capture_output=True).stdout.strip()
            commit_text = (
                f"tree {tree}\nparent {head}\n"
                "author Claimed Signer <claimed@example.invalid> 1700000000 +0000\n"
                "committer Claimed Signer <claimed@example.invalid> 1700000000 +0000\n"
                "gpgsig -----BEGIN PGP SIGNATURE-----\n"
                " fake-signature-material\n"
                " -----END PGP SIGNATURE-----\n\n"
                "signed-looking metadata only\n"
            )
            new_head = subprocess.run(
                ["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"],
                input=commit_text,
                text=True,
                check=True,
                capture_output=True,
            ).stdout.strip()
            subprocess.run(["git", "-C", str(repo), "update-ref", "HEAD", new_head], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "gpg.program", f"sh -c 'touch {marker}; exit 1'"],
                check=True,
            )
            evidence = provenance.collect_repository_evidence(repo, deep=True)
            self.assertFalse(marker.exists())
            self.assertTrue(evidence["commit_signature"]["present"])
            self.assertFalse(evidence["commit_signature"]["verified"])
            self.assertEqual(
                evidence["commit_signature"]["verification_status"],
                "present_not_verified_external_verifier_not_executed",
            )

    def test_sparse_pattern_change_changes_repository_view_fingerprint(self):
        with tempfile.TemporaryDirectory() as td:
            _, repo = self.make_project(Path(td))
            (repo / "one").mkdir()
            (repo / "two").mkdir()
            (repo / "one" / "a.go").write_text("package one\n", encoding="utf-8")
            (repo / "two" / "b.go").write_text("package two\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "sparse-checkout", "init", "--cone"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "sparse-checkout", "set", "one"], check=True, capture_output=True)
            first = provenance.light_view_state(repo)
            subprocess.run(["git", "-C", str(repo), "sparse-checkout", "set", "two"], check=True, capture_output=True)
            second = provenance.light_view_state(repo)
            self.assertEqual(first["head_sha"], second["head_sha"])
            self.assertNotEqual(first["sparse_patterns_sha256"], second["sparse_patterns_sha256"])
            self.assertNotEqual(first["content_view_fingerprint"], second["content_view_fingerprint"])
            self.assertNotEqual(first["view_fingerprint"], second["view_fingerprint"])

    def test_allowlisted_go_semantics_checks_observe_runtime_not_model_arithmetic(self):
        if shutil.which("go") is None and not Path("/usr/local/bin/awoki-go-semantics").is_file():
            self.skipTest("prebuilt semantics helper and Go fallback unavailable")
        duration = code_semantics_check(
            "go", "duration_multiply", {"duration": "500ms", "unit": "Millisecond"}
        )
        self.assertEqual(duration["status"], "ok")
        self.assertEqual(duration["observed"]["duration_numeric_nanoseconds"], 500_000_000)
        self.assertEqual(duration["observed"]["unit_numeric_nanoseconds"], 1_000_000)
        self.assertEqual(duration["observed"]["product_nanoseconds"], 500_000_000_000_000)
        self.assertEqual(duration["observed"]["product_seconds"], 500_000)

        assertion = code_semantics_check("go", "failed_error_type_assertion", {})
        self.assertEqual(assertion["observed"], {"branch": "err_nil", "err_is_nil": True, "ok": False})

        joined = code_semantics_check("go", "path_join", {"parts": ["/base", "/api/v1/users"]})
        self.assertEqual(joined["observed"]["result"], "/base/api/v1/users")
        self.assertFalse(joined["network"])
        self.assertIn("repository code was not executed", joined["proof_scope"])

    def test_go_semantics_reports_project_toolchain_alignment_without_executing_project(self):
        if shutil.which("go") is None and not Path("/usr/local/bin/awoki-go-semantics").is_file():
            self.skipTest("prebuilt semantics helper and Go fallback unavailable")
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "go.mod").write_text("module example.invalid/demo\n\ngo 1.17\n", encoding="utf-8")
            result = code_semantics_check(
                "go", "path_join", {"parts": ["/base", "/api"]}, name="demo", paths=paths
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["project_go"]["go_version"], "1.17")
            self.assertIn(result["toolchain_alignment"], {"major_minor_match", "major_minor_mismatch", "unknown"})
            self.assertIn("repository code was not executed", result["proof_scope"])
            if result["toolchain_alignment"] == "major_minor_mismatch":
                self.assertIn("not proof", result["applicability"])

    def test_semantics_check_rejects_unimplemented_language_instead_of_guessing(self):
        result = code_semantics_check("rust", "path_join", {"parts": ["a", "b"]})
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["supported_languages"], ["go"])

    def test_source_window_recommends_mechanical_semantics_for_observed_go_primitives(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            paths, repo = self.make_project(Path(td))
            (repo / "main.go").write_text(
                "package demo\n"
                "import \"time\"\n"
                "func timeout() time.Duration {\n"
                "    duration, _ := time.ParseDuration(\"500ms\")\n"
                "    return time.Millisecond * duration\n"
                "}\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            engine.index_project_code(paths, "demo", include_qdrant=False)
            window = code_source_window("main.go", name="demo", start_line=1, end_line=6, paths=paths)
            self.assertEqual(window["status"], "ok")
            semantics = window["deterministic_semantics"]
            self.assertTrue(semantics["recommended"])
            self.assertEqual(semantics["tool"], "code_semantics_check")
            self.assertEqual(semantics["operations"], ["parse_duration", "duration_multiply"])

    def test_evidence_id_is_compact_tamper_detecting_and_project_scoped(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            root = Path(td)
            paths, repo = self.make_project(root, "demo")
            source = repo / "main.py"
            source.write_text("def evidence_target():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            engine.index_project_code(paths, "demo", include_qdrant=False)
            window = code_source_window("main.py", name="demo", start_line=1, end_line=2, paths=paths)
            token = window["evidence"]["evidence_id"]
            self.assertLess(len(token), 512, token)
            payload = provenance.decode_evidence(token)
            self.assertEqual(payload["project_id"], "demo")
            self.assertEqual(payload["path"], "main.py")

            # One-character mutation must fail the checksum/decompression contract.
            pivot = max(token.find(".") + 2, len(token) // 2)
            replacement = "A" if token[pivot] != "A" else "B"
            tampered = token[:pivot] + replacement + token[pivot + 1:]
            rejected = code_evidence_verify(tampered, name="demo", paths=paths)
            self.assertEqual(rejected["status"], "rejected")
            self.assertEqual(rejected["verdict"], "INVALID_EVIDENCE")

            project_create("other", paths=paths)
            mismatch = code_evidence_verify(token, name="other", paths=paths)
            self.assertEqual(mismatch["status"], "rejected")
            self.assertEqual(mismatch["verdict"], "PROJECT_MISMATCH")

    def test_crafted_evidence_id_cannot_force_unbounded_file_read(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths, repo = self.make_project(root)
            huge = repo / "huge.bin"
            with huge.open("wb") as handle:
                handle.truncate(provenance.MAX_EVIDENCE_VERIFY_FILE_BYTES + 1)
            token = provenance.encode_evidence({
                "assurance": "FILESYSTEM_BOUND",
                "project_id": "demo",
                "commit_sha": "",
                "raw_tree_sha": "",
                "view_fingerprint": "",
                "path": "huge.bin",
                "source_sha256": "0" * 64,
                "start_line": 1,
                "end_line": 1,
            })
            # Defense in depth: even if the source-evidence policy were later
            # broadened, a crafted token still cannot force an unbounded read.
            with mock.patch.object(indexing_policy, "source_evidence_path_allowed", return_value=(True, "test_override")):
                result = code_evidence_verify(token, name="demo", paths=paths)
            self.assertEqual(result["verdict"], "VERIFICATION_BUDGET_EXCEEDED")
            self.assertEqual(result["status"], "incomplete")

    def test_unrelated_dirty_file_prevents_new_source_window_from_claiming_verified_snapshot(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            paths, repo = self.make_project(Path(td))
            source = repo / "main.py"
            other = repo / "other.py"
            source.write_text("def stable_target():\n    return 1\n", encoding="utf-8")
            other.write_text("def other():\n    return 1\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertEqual(indexed["repository_evidence"]["assurance"], "VERIFIED_SNAPSHOT")

            # The requested file remains byte-identical, but the repository as
            # a whole is no longer the verified clean snapshot captured at index time.
            other.write_text("def other():\n    return 2\n", encoding="utf-8")
            window = code_source_window("main.py", name="demo", start_line=1, end_line=2, paths=paths)
            self.assertEqual(window["status"], "ok")
            self.assertTrue(window["dirty"])
            self.assertEqual(window["evidence"]["assurance"], "WORKING_TREE_BOUND")
            verified = code_evidence_verify(window["evidence"]["evidence_id"], name="demo", paths=paths)
            self.assertEqual(verified["verdict"], "CURRENT_SOURCE_WORKING_TREE_BOUND")

    def test_dirty_source_evidence_remains_current_but_never_claims_verified_snapshot(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            paths, repo = self.make_project(Path(td))
            source = repo / "main.py"
            source.write_text("def dirty_target():\n    return 1\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            source.write_text("def dirty_target():\n    return 2\n", encoding="utf-8")
            engine.index_project_code(paths, "demo", include_qdrant=False)
            window = code_source_window("main.py", name="demo", start_line=1, end_line=2, paths=paths)
            self.assertEqual(window["evidence"]["assurance"], "WORKING_TREE_BOUND")
            verified = code_evidence_verify(window["evidence"]["evidence_id"], name="demo", paths=paths)
            self.assertEqual(verified["verdict"], "CURRENT_SOURCE_WORKING_TREE_BOUND")
            self.assertTrue(verified["source_current"])
            self.assertFalse(verified["snapshot_current"])

    def test_project_status_surfaces_concise_repository_assurance_only_when_code_enabled(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("plain", paths=paths)
            plain = project_status("plain", paths=paths)
            self.assertNotIn("code_repository", plain)

            pp = project_workspace.paths_for(root, "plain")
            repo = pp.project_dir / "repo"
            (repo / "main.py").write_text("def status_target():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            project_workspace.enable_code_index(root, "plain")
            engine.index_project_code(paths, "plain", include_qdrant=False)
            status = project_status("plain", paths=paths)
            code_repo = status["code_repository"]
            self.assertEqual(code_repo["assurance"], "VERIFIED_SNAPSHOT")
            self.assertTrue(code_repo["head_sha"])
            self.assertTrue(code_repo["tree_sha"])
            self.assertEqual(code_repo["verification"], "passive; use code_index_verify for deep repository/source verification")
            # Keep generic project status compact: no complete provenance blob here.
            self.assertNotIn("author_claim", code_repo)
            self.assertNotIn("configured_filter_keys", code_repo)

    def test_go_primitive_queries_recommend_semantics_check_without_forcing_it_everywhere(self):
        self.assertTrue(engine._needs_go_semantics_check("what does path.Join do here?"))
        self.assertTrue(engine._needs_go_semantics_check("failed type assertion to error"))
        self.assertTrue(engine._needs_go_semantics_check("time.ParseDuration then time.Millisecond multiplication"))
        self.assertTrue(engine._needs_go_semantics_check("what does httputil.ReverseProxy do with forwarded headers?"))
        self.assertEqual(
            engine._recommended_go_semantics_operations("what does httputil.ReverseProxy do with forwarded headers?"),
            ["reverse_proxy_rewrite_headers"],
        )
        self.assertEqual(
            engine._recommended_go_semantics_operations("time.ParseDuration multiplied by time.Millisecond"),
            ["parse_duration", "duration_multiply"],
        )
        self.assertFalse(engine._needs_go_semantics_check("how does JWT authentication work?"))

    def test_git_reads_disable_lazy_promisor_fetch_and_transient_config_poisoning(self):
        with mock.patch.dict(
            os.environ,
            {
                "GIT_NO_LAZY_FETCH": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.fsmonitor",
                "GIT_CONFIG_VALUE_0": "/tmp/should-not-run",
                "GIT_EXTERNAL_DIFF": "/tmp/should-not-run-diff",
                "GIT_CEILING_DIRECTORIES": "/",
                "GIT_DISCOVERY_ACROSS_FILESYSTEM": "0",
                "GIT_TRACE2": "/tmp/should-not-write-trace",
                "GIT_EXEC_PATH": "/tmp/should-not-exec-git-helper",
                "GIT_ASKPASS": "/tmp/should-not-run-askpass",
            },
            clear=False,
        ):
            env = provenance.sanitized_git_environment()
        self.assertEqual(env["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertNotIn("GIT_CONFIG_COUNT", env)
        self.assertNotIn("GIT_CONFIG_KEY_0", env)
        self.assertNotIn("GIT_CONFIG_VALUE_0", env)
        self.assertNotIn("GIT_EXTERNAL_DIFF", env)
        self.assertNotIn("GIT_CEILING_DIRECTORIES", env)
        self.assertNotIn("GIT_DISCOVERY_ACROSS_FILESYSTEM", env)
        self.assertNotIn("GIT_TRACE2", env)
        self.assertNotIn("GIT_EXEC_PATH", env)
        self.assertNotIn("GIT_ASKPASS", env)
        self.assertEqual(env["GIT_PAGER"], "")

    def test_shallow_history_is_disclosed_without_hiding_current_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            origin = root / "origin"
            origin.mkdir()
            (origin / "main.py").write_text("def history_target():\n    return 1\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(origin)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(origin), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(origin), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(origin), "add", "."], check=True)
            subprocess.run(["git", "-C", str(origin), "commit", "-m", "one"], check=True, capture_output=True)
            (origin / "main.py").write_text("def history_target():\n    return 2\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(origin), "commit", "-am", "two"], check=True, capture_output=True)
            clone = root / "shallow"
            subprocess.run(["git", "clone", "--depth=1", f"file://{origin}", str(clone)], check=True, capture_output=True)
            evidence = provenance.collect_repository_evidence(clone, deep=True)
            self.assertTrue(evidence["history_view"]["shallow_repository"])
            self.assertEqual(evidence["history_view"]["history_assurance"], "LIMITED_OR_REWRITTEN_LOCAL_VIEW")
            self.assertIn("shallow_history", evidence["anomalies"])
            # Shallow ancestry limits history claims but does not make the current clean tree disappear.
            self.assertEqual(evidence["assurance"], "VERIFIED_SNAPSHOT")

    def test_reverse_proxy_rewrite_semantics_resolve_forwarded_header_boundary_without_network(self):
        result = code_semantics_check("go", "reverse_proxy_rewrite_headers", {})
        if result.get("status") == "unavailable":
            self.skipTest(result.get("reason") or "Go unavailable")
        self.assertEqual(result["status"], "ok", result)
        self.assertFalse(result["network"])
        self.assertFalse(result["repository_code_executed"])
        observed = result["observed"]
        inbound = observed["in_at_rewrite"]
        outbound = observed["out_at_rewrite"]
        for name in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto"):
            self.assertIn(name, inbound)
            self.assertNotIn(name, outbound)
        # ReverseProxy's Rewrite pre-processing does not blanket-delete every
        # header whose name begins with X-Forwarded-. Oathkeeper's own policy
        # still has to decide what to do with these surviving values.
        self.assertEqual(outbound["X-Forwarded-Port"], ["443"])
        self.assertEqual(outbound["X-Forwarded-Uri"], ["/original"])
        self.assertEqual(outbound["X-Custom"], ["kept"])
        self.assertEqual(observed["response_status"], 200)

    def test_semantics_probe_does_not_inherit_go_tool_execution_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            marker = root / "go-helper-executed"
            helper = root / "evil-toolexec"
            helper.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 97\n", encoding="utf-8")
            helper.chmod(0o755)
            goenv = root / "goenv"
            goenv.write_text(f"GOFLAGS=-toolexec={helper}\n", encoding="utf-8")
            poisoned = {
                "GOFLAGS": f"-toolexec={helper}",
                "GOENV": str(goenv),
                "CC": str(helper),
                "CXX": str(helper),
                "GOCACHEPROG": str(helper),
            }
            with mock.patch.dict(os.environ, poisoned, clear=False):
                result = code_semantics_check("go", "path_join", {"parts": ["/base", "/api"]})
            if result.get("status") == "unavailable":
                self.skipTest(result.get("reason") or "Go unavailable")
            self.assertEqual(result["status"], "ok", result)
            self.assertFalse(marker.exists(), "semantics probe inherited arbitrary Go/compiler helper execution")
            self.assertFalse(result["inherited_go_configuration"])
            self.assertEqual(result["observed"]["result"], "/base/api")

    def test_passive_git_inspection_disables_configured_fsmonitor_hook(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            root = Path(td)
            paths, repo = self.make_project(root)
            marker = root / "fsmonitor-executed"
            hook = root / "evil-fsmonitor"
            hook.write_text(f"#!/bin/sh\ntouch '{marker}'\necho\nexit 0\n", encoding="utf-8")
            hook.chmod(0o755)
            (repo / "main.py").write_text("def fsmonitor_target():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", str(hook)], check=True)

            branch = engine.branch_identity("demo", repo)
            evidence = provenance.collect_repository_evidence(repo, deep=True)
            state = project_workspace._repository_state(project_workspace.paths_for(root, "demo"))
            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertFalse(marker.exists(), "passive repository inspection executed core.fsmonitor")
            self.assertFalse(branch.dirty)
            self.assertEqual(evidence["assurance"], "VERIFIED_SNAPSHOT")
            self.assertFalse(state["dirty"])
            self.assertEqual(indexed["repository_evidence"]["assurance"], "VERIFIED_SNAPSHOT")

    def test_git_stat_trust_weakening_config_lowers_assurance_and_changes_view_identity(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            paths, repo = self.make_project(Path(td))
            (repo / "main.py").write_text("def stat_target():\n    return 1\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertEqual(indexed["repository_evidence"]["assurance"], "VERIFIED_SNAPSHOT")
            before = indexed["repository_evidence"]["view_fingerprint"]

            subprocess.run(["git", "-C", str(repo), "config", "core.trustctime", "false"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "core.checkStat", "minimal"], check=True)
            deep = provenance.collect_repository_evidence(repo, deep=True)
            self.assertEqual(deep["assurance"], "WORKING_TREE_BOUND")
            self.assertIn("git_ctime_trust_disabled", deep["anomalies"])
            self.assertIn("git_checkstat_minimal", deep["anomalies"])
            self.assertNotEqual(deep["view_fingerprint"], before)
            passive = code_index_status(name="demo", paths=paths)
            self.assertFalse(passive["freshness"]["lexical_current"])

    def test_preexisting_assume_unchanged_never_enables_clean_snapshot_or_text_cursor_reuse(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep unavailable")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            paths, repo = self.make_project(Path(td))
            source = repo / "main.py"
            source.write_text("def hidden_change():\n    return 1  # CURSOR_NEEDLE\n    # CURSOR_NEEDLE\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "update-index", "--assume-unchanged", "main.py"], check=True)

            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertEqual(indexed["repository_evidence"]["assurance"], "WORKING_TREE_BOUND")
            passive = code_index_status(name="demo", paths=paths)
            self.assertFalse(passive["freshness"]["lexical_current"])

            first = code_text_search("CURSOR_NEEDLE", name="demo", fixed_string=True, page_size=1, paths=paths)
            self.assertEqual(first["match_count"], 2)
            self.assertTrue(first["next_cursor"])
            self.assertEqual(first["eligibility_source"], "live_policy_scan")

            source.write_text("def hidden_change():\n    return 2  # CHANGED\n", encoding="utf-8")
            self.assertEqual(subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"], check=True, text=True, capture_output=True
            ).stdout.strip(), "")
            stale = code_text_search(
                "CURSOR_NEEDLE", name="demo", fixed_string=True, page_size=1, cursor=first["next_cursor"], paths=paths
            )
            self.assertEqual(stale["status"], "stale_cursor")

    def test_assume_unchanged_and_manual_skip_worktree_cannot_fake_clean_index_reuse(self):
        for flag, anomaly in (("--assume-unchanged", "assume_unchanged_index_entries"), ("--skip-worktree", "manual_skip_worktree_index_entries")):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
                paths, repo = self.make_project(Path(td))
                source = repo / "main.py"
                source.write_text("def hidden_change():\n    return 1\n", encoding="utf-8")
                subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
                subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
                subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
                subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
                engine.index_project_code(paths, "demo", include_qdrant=False)
                before = code_index_status(name="demo", paths=paths)
                self.assertTrue(before["freshness"]["lexical_current"])

                subprocess.run(["git", "-C", str(repo), "update-index", flag, "main.py"], check=True)
                source.write_text("def hidden_change():\n    return 2\n", encoding="utf-8")
                porcelain = subprocess.run(
                    ["git", "-C", str(repo), "status", "--porcelain"], check=True, text=True, capture_output=True
                ).stdout.strip()
                self.assertEqual(porcelain, "", "fixture must demonstrate Git porcelain hiding the worktree edit")

                # Awoki binds index representation into the repository view, so
                # the old structural index becomes stale even though status says clean.
                after = code_index_status(name="demo", paths=paths)
                self.assertFalse(after["freshness"]["lexical_current"])
                self.assertIn("source_probe", after["freshness"]["stale_reasons"])
                self.assertIn(anomaly, after["freshness"]["assurance_reasons"])
                deep = provenance.collect_repository_evidence(repo, deep=True)
                self.assertIn(anomaly, deep["anomalies"])
                self.assertEqual(deep["assurance"], "WORKING_TREE_BOUND")
                stale = code_source_window("main.py", name="demo", start_line=1, end_line=2, paths=paths)
                self.assertEqual(stale["status"], "stale_source")

    def test_grafts_limit_history_assurance_without_hiding_current_clean_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, repo = self.make_project(root)
            (repo / "main.py").write_text("def graft_target():\n    return 1\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "one"], check=True, capture_output=True)
            (repo / "main.py").write_text("def graft_target():\n    return 2\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "commit", "-am", "two"], check=True, capture_output=True)
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, text=True, capture_output=True
            ).stdout.strip()
            grafts = repo / ".git" / "info" / "grafts"
            grafts.parent.mkdir(parents=True, exist_ok=True)
            grafts.write_text(head + "\n", encoding="utf-8")

            evidence = provenance.collect_repository_evidence(repo, deep=True)
            self.assertTrue(evidence["history_view"]["grafts_present"])
            self.assertTrue(evidence["history_view"]["grafts_sha256"])
            self.assertEqual(evidence["history_view"]["history_assurance"], "LIMITED_OR_REWRITTEN_LOCAL_VIEW")
            self.assertIn("grafts_active", evidence["anomalies"])
            self.assertEqual(evidence["assurance"], "VERIFIED_SNAPSHOT")

    def test_promisor_partial_clone_configuration_is_disclosed_and_never_contacts_remote(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, repo = self.make_project(root)
            (repo / "main.py").write_text("def promisor_target():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "remote.origin.url", "https://invalid.example/never-contact.git"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "remote.origin.promisor", "true"], check=True)

            evidence = provenance.collect_repository_evidence(repo, deep=True)
            history = evidence["history_view"]
            self.assertTrue(history["partial_clone_configured"])
            self.assertTrue(history["promisor_remote_configured"])
            self.assertTrue(history["lazy_fetch_disabled"])
            self.assertFalse(history["remote_contacted"])
            self.assertEqual(history["history_assurance"], "LIMITED_OR_REWRITTEN_LOCAL_VIEW")
            self.assertIn("partial_clone_configured", evidence["anomalies"])
            # Current fully-materialized clean source can still be bound even
            # though claims about complete local ancestry are weaker.
            self.assertEqual(evidence["assurance"], "VERIFIED_SNAPSHOT")


    def test_prebuilt_semantics_helper_path_works_without_runtime_go_compiler(self):
        go = shutil.which("go")
        if go is None:
            self.skipTest("Go unavailable to build a temporary simulation of the release helper")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            helper = root / "awoki-go-semantics"
            subprocess.run(
                [go, "build", "-trimpath", "-o", str(helper), str(semantics.BUNDLED_PROBE_SOURCE)],
                check=True,
                capture_output=True,
                env={**os.environ, "GO111MODULE": "off", "GOTOOLCHAIN": "local", "CGO_ENABLED": "0"},
            )
            with mock.patch.object(semantics, "PREBUILT_HELPER", helper), mock.patch.object(
                semantics, "_trusted_go_binary", return_value=""
            ), mock.patch.dict(os.environ, {"GODEBUG": "this_setting_must_not_control_the_probe=1"}, clear=False):
                result = code_semantics_check(
                    "go", "duration_multiply", {"duration": "500ms", "unit": "Millisecond"}
                )
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["execution_backend"], "prebuilt_pinned_helper")
            self.assertEqual(result["observed"]["product_seconds"], 500_000)
            self.assertFalse(result["repository_code_executed"])
            self.assertFalse(result["network"])

    def test_stable_sparse_view_can_reuse_materialized_index_without_claiming_verified_snapshot(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            paths, repo = self.make_project(Path(td))
            (repo / "one").mkdir()
            (repo / "two").mkdir()
            (repo / "one" / "main.py").write_text("def sparse_visible():\n    return True\n", encoding="utf-8")
            (repo / "two" / "other.py").write_text("def sparse_hidden():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "sparse-checkout", "init", "--cone"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "sparse-checkout", "set", "one"], check=True, capture_output=True)

            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertEqual(indexed["repository_evidence"]["assurance"], "WORKING_TREE_BOUND")
            self.assertIn("sparse_checkout_active", indexed["repository_evidence"]["anomalies"])
            self.assertTrue(provenance.passive_index_reuse_safe(indexed["repository_evidence"]))

            with mock.patch.object(engine, "_scan_repository", side_effect=AssertionError("stable sparse view should reuse materialized index")):
                result = codebase_search("sparse_visible", name="demo", mode="exact", paths=paths)
            self.assertIn(result["status"], {"ok", "ambiguous"}, result)
            self.assertEqual(result["scope"]["repository_assurance"], "WORKING_TREE_BOUND")
            self.assertTrue(result["freshness"]["lexical_current"])

    def test_repository_view_mutation_during_index_never_publishes_verified_snapshot(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            paths, repo = self.make_project(Path(td))
            (repo / "main.py").write_text("def stable():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            original = store.branch_embedding_memberships

            def mutate_after_source_index(*args, **kwargs):
                rows = original(*args, **kwargs)
                (repo / "late-untracked.py").write_text("def appeared_late():\n    return True\n", encoding="utf-8")
                return rows

            with mock.patch.object(store, "branch_embedding_memberships", side_effect=mutate_after_source_index):
                result = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertEqual(result["status"], "stale_source", result)
            self.assertIn("changed during indexing", result["reason"])
            self.assertEqual(result["initial_repository_assurance"], "VERIFIED_SNAPSHOT")
            self.assertEqual(result["final_repository_assurance"], "WORKING_TREE_BOUND")
            self.assertFalse(store.manifest_path(project_workspace.paths_for(paths.root, "demo").project_dir).exists())

    def test_text_search_rejects_pre_10_8_cursor_version(self):
        raw = json.dumps(
            {"v": 3, "offset": 1, "fingerprint": "f" * 64, "search_id": "s" * 64},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        with self.assertRaisesRegex(ValueError, "unsupported cursor version"):
            text_search._decode_cursor(cursor)

    def test_forged_evidence_id_cannot_hash_explicit_sensitive_or_no_rag_files(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            env_file = repo / ".env"
            env_file.write_text("CLIENT_SECRET=guessable-secret-value\n", encoding="utf-8")
            no_rag = repo / "private.py"
            no_rag.write_text("# awoki:no-rag\nAPI_VALUE = 'private-analysis-value'\n", encoding="utf-8")
            readme = repo / "README.md"
            readme.write_text("ordinary prose that source-window would not structurally expose\n", encoding="utf-8")
            binary = repo / "blob.bin"
            binary.write_bytes(b"\x00\x01\x02private-binary")

            def forged(rel: str) -> str:
                return provenance.encode_evidence({
                    "assurance": "FILESYSTEM_BOUND",
                    "project_id": "demo",
                    "commit_sha": "",
                    "raw_tree_sha": "",
                    "view_fingerprint": "forged",
                    "path": rel,
                    "source_sha256": "0" * 64,
                    "git_blob_oid": "",
                    "start_line": 1,
                    "end_line": 1,
                })

            for rel, reason in (
                (".env", "explicit_sensitive_path"),
                ("private.py", "no_rag_marker"),
                ("README.md", "prose_lexical_only"),
                ("blob.bin", "nontext_not_source_evidence"),
            ):
                with self.subTest(rel=rel):
                    result = code_evidence_verify(forged(rel), name="demo", paths=paths)
                    self.assertEqual(result["status"], "rejected", result)
                    self.assertEqual(result["verdict"], "INVALID_EVIDENCE_POLICY")
                    self.assertIn(reason, result["reason"])
                    rendered = json.dumps(result, sort_keys=True)
                    self.assertNotIn("current_source_sha256", rendered)
                    self.assertNotIn("guessable-secret-value", rendered)
                    self.assertNotIn("private-analysis-value", rendered)

    def test_git_file_identity_uses_literal_pathspec_for_metacharacter_filename(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            weird = repo / "src" / "[x].py"
            other = repo / "src" / "x.py"
            weird.parent.mkdir(parents=True)
            weird.write_text("def weird_literal():\n    return 11\n", encoding="utf-8")
            other.write_text("def ordinary():\n    return 22\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            engine.index_project_code(paths, "demo", include_qdrant=False)

            window = code_source_window("src/[x].py", name="demo", start_line=1, end_line=2, paths=paths)
            self.assertEqual(window["status"], "ok", window)
            expected = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD:src/[x].py"],
                check=True, text=True, capture_output=True,
            ).stdout.strip()
            self.assertEqual(window["evidence"]["git_blob_oid"], expected)
            verified = code_evidence_verify(window["evidence"]["evidence_id"], name="demo", paths=paths)
            self.assertEqual(verified["verdict"], "CURRENT_VERIFIED_SNAPSHOT", verified)
            self.assertEqual(verified["current_git_blob_oid"], expected)

    def test_replace_ref_source_evidence_uses_raw_head_blob_identity(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            source = repo / "main.py"
            source.write_text("def raw_value():\n    return 1\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "raw"], check=True, capture_output=True)
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, text=True, capture_output=True).stdout.strip()
            raw_blob = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD:main.py"], check=True, text=True, capture_output=True).stdout.strip()

            source.write_text("def raw_value():\n    return 2\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "main.py"], check=True)
            replacement_tree = subprocess.run(["git", "-C", str(repo), "write-tree"], check=True, text=True, capture_output=True).stdout.strip()
            subprocess.run(["git", "-C", str(repo), "reset", "--hard", head], check=True, capture_output=True)
            replacement = subprocess.run(
                ["git", "-C", str(repo), "commit-tree", replacement_tree, "-p", head],
                input="replacement\n", text=True, check=True, capture_output=True,
            ).stdout.strip()
            subprocess.run(["git", "-C", str(repo), "replace", head, replacement], check=True)

            sha = indexing_policy.content_hash_file(source)
            evidence = provenance.build_source_evidence(
                repo_root=repo,
                project_id="demo",
                repo_id="demo:repo",
                branch_key="main",
                commit_sha=head,
                rel_path="main.py",
                source_sha256=sha,
                indexed_sha256=sha,
                start_line=1,
                end_line=2,
                assurance_hint="WORKING_TREE_BOUND",
            )
            self.assertEqual(evidence["git_blob_oid"], raw_blob)

    def test_extended_git_environment_poisoning_is_removed(self):
        poisoned_keys = {
            "GIT_CONFIG_GLOBAL": "/tmp/poison-global",
            "GIT_CONFIG_SYSTEM": "/tmp/poison-system",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_SHALLOW_FILE": "/tmp/poison-shallow",
            "GIT_QUARANTINE_PATH": "/tmp/poison-quarantine",
            "GIT_GRAFT_FILE": "/tmp/poison-grafts",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_GLOB_PATHSPECS": "1",
            "GIT_NOGLOB_PATHSPECS": "1",
            "GIT_ICASE_PATHSPECS": "1",
            "GIT_ATTR_NOSYSTEM": "1",
        }
        with mock.patch.dict(os.environ, poisoned_keys, clear=False):
            env = provenance.sanitized_git_environment()
            workspace_env = project_workspace._passive_git_env()
        for key in poisoned_keys:
            self.assertNotIn(key, env)
            self.assertNotIn(key, workspace_env)

    def test_incompatible_derived_code_database_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "awoki_code.sqlite"
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("CREATE TABLE code_files(file_id TEXT PRIMARY KEY)")
                conn.execute("CREATE TABLE obsolete_marker(value TEXT)")
                conn.execute("PRAGMA user_version=2")
                conn.commit()
            store.init_db(db)
            with closing(sqlite3.connect(db)) as conn:
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                obsolete = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='obsolete_marker'"
                ).fetchone()
                vector_state = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_vector_memberships'"
                ).fetchone()
            self.assertEqual(version, store.SCHEMA_VERSION)
            self.assertIsNone(obsolete)
            self.assertIsNotNone(vector_state)

    def test_registered_multi_repo_search_scope_evidence_and_branch_identity(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            repos = {}
            for rid, marker in (("one", "alpha_only"), ("two", "beta_only")):
                repo = pp.project_dir / "repo" / rid
                repo.mkdir(parents=True, exist_ok=True)
                (repo / "main.py").write_text(
                    f"def shared_marker():\n    return '{marker}'\n\ndef {marker}():\n    return True\n",
                    encoding="utf-8",
                )
                subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
                subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
                subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
                subprocess.run(["git", "-C", str(repo), "commit", "-m", rid], check=True, capture_output=True)
                project_workspace.project_repo_add(root, "demo", rid, f"repo/{rid}", default=(rid == "one"))
                repos[rid] = repo

            broad = codebase_search("shared_marker", name="demo", mode="exact", limit=10, paths=paths)
            self.assertEqual(broad["status"], "ok", broad)
            self.assertEqual({hit.get("repo_id") for hit in broad["hits"]}, {"demo:one", "demo:two"})

            ambiguous = code_source_window("main.py", name="demo", start_line=1, end_line=2, paths=paths)
            self.assertEqual(ambiguous["status"], "ambiguous_repository")

            window = code_source_window("main.py", name="demo", repo="two", start_line=1, end_line=4, paths=paths)
            self.assertEqual(window["status"], "ok", window)
            payload = provenance.decode_evidence(window["evidence"]["evidence_id"])
            self.assertEqual(payload["repo_id"], "demo:two")
            verified = code_evidence_verify(window["evidence"]["evidence_id"], name="demo", repo="two", paths=paths)
            self.assertEqual(verified["status"], "ok", verified)
            mismatch = code_evidence_verify(window["evidence"]["evidence_id"], name="demo", repo="one", paths=paths)
            self.assertEqual(mismatch["verdict"], "REPOSITORY_MISMATCH")

            exact = code_text_search("beta_only", name="demo", repo="two", paths=paths)
            if shutil.which("rg") is None:
                self.assertEqual(exact["status"], "error", exact)
                self.assertFalse(exact["scanner_available"], exact)
                self.assertFalse(exact["repository_universe_complete"], exact)
                self.assertTrue(exact["resume_required"], exact)
                self.assertIn("ripgrep", exact.get("reason", "").lower())
            else:
                self.assertEqual(exact["status"], "ok", exact)
                self.assertEqual(exact["match_count"], 2)
                self.assertTrue(exact["repository_universe_complete"])

            status = code_index_status(name="demo", paths=paths)
            self.assertTrue(status.get("multi_repo"), status)
            verified_status = code_index_verify(name="demo", include_qdrant=False, paths=paths)
            self.assertTrue(verified_status.get("multi_repo"), verified_status)
            self.assertEqual(len(verified_status.get("repositories") or []), 2, verified_status)
            project_workspace.enable_code_index(root, "demo")
            overall = project_status("demo", paths=paths)
            self.assertTrue((overall.get("code_repository") or {}).get("multi_repo"), overall)
            self.assertEqual(len((overall.get("code_repository") or {}).get("repositories") or []), 2, overall)
            keys = []
            for row in status.get("repositories") or []:
                branch = row.get("active_branch") or {}
                keys.append(branch.get("branch_key"))
            self.assertEqual(len(set(keys)), 2, status)

            refreshed = project_refresh("demo", include_code=True, include_qdrant=False, paths=paths)
            self.assertEqual(refreshed["index"]["code_index"]["status"], "indexed", refreshed)
            self.assertEqual(len(refreshed["index"]["code_index"]["repositories"]), 2)
            situation = (pp.project_dir / "SITUATION.md").read_text(encoding="utf-8")
            self.assertIn("registered repositories: 2", situation)
            self.assertIn("`one`", situation)
            self.assertIn("`two`", situation)

    def test_legacy_v3_evidence_fails_closed_after_multi_repo_migration(self):
        import zlib
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            _, legacy_repo = self.make_project(root, "demo")
            (legacy_repo / "main.py").write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(legacy_repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(legacy_repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(legacy_repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(legacy_repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(legacy_repo), "commit", "-m", "legacy"], check=True, capture_output=True)
            codebase_search("value", name="demo", mode="exact", paths=paths)
            window = code_source_window("main.py", name="demo", start_line=1, end_line=1, paths=paths)
            self.assertEqual(window["status"], "ok", window)
            payload = provenance.decode_evidence(window["evidence"]["evidence_id"])
            wire = [
                3, {"VERIFIED_SNAPSHOT":"V", "WORKING_TREE_BOUND":"W", "FILESYSTEM_BOUND":"F"}[payload["assurance"]],
                payload["project_id"], payload["commit_sha"], payload["raw_tree_sha"], payload["view_fingerprint"],
                payload["path"], payload["source_sha256"], payload["git_blob_oid"], payload["start_line"], payload["end_line"],
            ]
            raw = json.dumps(wire, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            encoded = base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii").rstrip("=")
            token = f"ev3z.{encoded}.{provenance._sha(raw)[:16]}"
            self.assertEqual(code_evidence_verify(token, name="demo", paths=paths)["status"], "ok")

            # Convert the project to registered mode with two exact child Git roots.
            container = legacy_repo.parent
            moved = container / "one"
            legacy_repo.rename(root / "legacy-moved")
            (root / "legacy-moved").rename(moved)
            two = container / "two"
            two.mkdir(parents=True)
            (two / "main.py").write_text("value = 2\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(two)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(two), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(two), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(two), "add", "."], check=True)
            subprocess.run(["git", "-C", str(two), "commit", "-m", "two"], check=True, capture_output=True)
            project_workspace.project_repo_add(root, "demo", "one", "repo/one", default=True)
            project_workspace.project_repo_add(root, "demo", "two", "repo/two")
            rejected = code_evidence_verify(token, name="demo", paths=paths)
            self.assertEqual(rejected["verdict"], "AMBIGUOUS_LEGACY_EVIDENCE_REPOSITORY")


if __name__ == "__main__":
    unittest.main()

# R9 retrieval-quality regressions. Kept outside the main class declaration so
# unittest discovery still sees them through a dedicated class and the fixture
# remains independent of Oathkeeper-specific names.
class RetrievalQualityR9Tests(unittest.TestCase):
    def make_project(self, root: Path, name: str = "demo") -> tuple[HarnessPaths, Path]:
        paths = HarnessPaths(root=root, global_root=root / "global")
        project_create(name, paths=paths)
        pp = project_workspace.paths_for(root, name)
        project_workspace.enable_code_index(root, name)
        return paths, pp.project_dir / "repo"
    def test_r9_lexical_mode_is_real_and_unknown_mode_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "auth.py").write_text("def reject_credentials():\n    return False\n", encoding="utf-8")
            with mock.patch.object(engine.vector_store, "search_with_status", side_effect=AssertionError("qdrant must not run")), \
                 mock.patch("rag_backend.rerank_hits", side_effect=AssertionError("reranker must not run")):
                result = codebase_search("reject credentials", name="demo", mode="lexical", paths=paths)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["routing"]["selected_mode"], "lexical")
            self.assertEqual(result["details"]["retrieval"]["qdrant_candidates"], 0)
            self.assertTrue(result["details"]["retrieval"]["qdrant_requested"])
            self.assertFalse(result["details"]["retrieval"]["qdrant_eligible"])
            self.assertFalse(result["details"]["retrieval"]["reranker"]["attempted"])
            self.assertTrue(result["details"]["retrieval"]["stage_top"]["fused"])
            rejected = codebase_search("reject credentials", name="demo", mode="definitely-not-a-mode", paths=paths)
            self.assertEqual(rejected["status"], "rejected")

    def test_r9_result_focus_does_not_misread_ignore_tests_as_test_intent(self):
        self.assertEqual(
            engine._result_focus("ignore tests and find the production implementation", "auto")["focus"],
            "implementation",
        )
        self.assertEqual(
            engine._result_focus("find the tests that demonstrate rejection", "auto")["focus"],
            "tests",
        )

    def test_r9_bounded_context_preserves_requested_top_k_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "code.sqlite"
            rows = []
            for idx in range(10):
                rows.append({
                    "project_id": "demo", "repo_id": "demo:default", "branch_key": "b",
                    "path": f"src/f{idx}.py", "start_line": 1, "end_line": 10,
                    "symbol_name": f"f{idx}", "symbol_kind": "function",
                    "text": "x" * 5000, "score": 1.0 - idx / 100.0,
                    "retrieval_backend": "code_fts", "final_rank": idx + 1,
                })
            hits = engine._bounded_rows(db, rows, "context", 10, max_chars=1200)
            self.assertEqual(len(hits), 10)
            self.assertEqual([hit["final_rank"] for hit in hits], list(range(1, 11)))
            self.assertLessEqual(sum(len(hit["preview"]) for hit in hits), 1200)

    def test_r102_hundred_candidate_primary_diagnostics_does_not_inline_full_trace(self):
        rows = []
        for idx in range(100):
            rows.append({
                "path": f"pipeline/authn/authenticator_{idx}.go",
                "qualified_name": f"(*Authenticator{idx}).Authenticate",
                "symbol_kind": "method",
                "authority_class": "production_implementation",
                "retrieval_backends": ["code_fts", "code_qdrant"],
                "fts_rank": idx + 1,
                "qdrant_rank": idx + 2,
                "fused_rank": idx + 1,
                "pre_rerank_rank": idx + 1,
                "rerank_focus_lane_eligible": idx >= 18,
                "rerank_focus_lane_signals": ["query_overlap"] if idx >= 18 else [],
                "rerank_focus_selection_order": idx - 17 if idx >= 18 else None,
                "rerank_structural_lane_eligible": idx >= 26,
                "rerank_structural_selection_order": idx - 25 if idx >= 26 else None,
                "rerank_selection_lane": "general" if idx < 18 else ("focus" if idx < 26 else ("structural" if idx < 30 else "not_selected")),
                "rerank_selected": idx < 30,
                "rerank_score_returned": idx < 30,
                "rerank_score": 0.9 - idx / 1000.0 if idx < 30 else None,
                "rerank_rank": idx + 1 if idx < 30 else None,
                "final_rank": idx + 1,
                "final_score": 0.5,
                "text": "x" * 5000,
            })
        details = engine._diagnostic_details(
            {"retrieval": {"fts_candidates": 100, "qdrant_candidates": 50, "stage_top": {}}},
            rows,
            ["(*Authenticator90).Authenticate"],
        )
        descriptor = details["candidate_trace"]
        self.assertEqual(descriptor["pool_size"], 100)
        self.assertNotIn("rows", descriptor)
        self.assertEqual(details["rerank_selected_candidates"]["selected"], 30)
        target = details["diagnostic_targets"]["items"][0]
        self.assertTrue(target["found"])
        self.assertEqual(target["matches"][0]["rank"], 91)
        self.assertLess(len(json.dumps(details, separators=(",", ":"))), 25_000)
        full_trace = engine._diagnostic_candidate_trace(rows)
        self.assertEqual(full_trace["pool_size"], 100)
        self.assertEqual(len(full_trace["rows"]), 100)
        self.assertNotIn("text", full_trace["columns"])

    def test_r102_diagnostics_primary_response_uses_trace_handle_and_target_records(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            for idx in range(40):
                (repo / f"auth_{idx}.py").write_text(
                    f"def authenticate_{idx}(request):\n    return bool(request.get('credentials'))\n",
                    encoding="utf-8",
                )
            result = codebase_search(
                "where can credentials be rejected before authorization",
                name="demo",
                mode="conceptual",
                view="diagnostics",
                use_qdrant=False,
                use_reranker=False,
                diagnostic_targets=["authenticate_39"],
                limit=20,
                paths=paths,
            )
            self.assertEqual(result["status"], "ok")
            self.assertNotIn("_diagnostic_trace", result)
            details = result["details"]
            self.assertLessEqual(len(result["hits"]), 10)
            stage_targets = details["diagnostic_target_stages"]
            stage_item = stage_targets["items"][0]
            self.assertEqual(stage_item["target"], "authenticate_39")
            self.assertTrue(stage_item["stages"]["fts"]["found"])
            self.assertTrue(stage_item["stages"]["fused"]["found"])
            self.assertTrue(stage_item["stages"]["composed_pool"]["found"])
            descriptor = details["candidate_trace"]
            self.assertEqual(descriptor["encoding"], "stored-columns+rows")
            self.assertTrue(descriptor["stored"])
            self.assertEqual(descriptor["rows_inline"], 0)
            self.assertRegex(descriptor["trace_id"], r"^diag_[0-9a-f]{24}$")
            self.assertNotIn("rows", descriptor)
            self.assertEqual(descriptor["retrieval_tool"], "code_diagnostics_trace")
            self.assertTrue(all("preview" not in hit for hit in result["hits"]))

            detail_keys = list(details)
            self.assertLess(detail_keys.index("diagnostic_targets"), detail_keys.index("rerank_selected_candidates"))
            targets = details["diagnostic_targets"]
            self.assertEqual(targets["applied"], 1)
            target = targets["items"][0]
            self.assertTrue(target["found"])
            self.assertTrue(any("authenticate_39" in str(match.get("symbol")) for match in target["matches"]))

            payload_size = len(json.dumps(result, separators=(",", ":")))
            self.assertLess(payload_size, 40_000)

            target_page = code_diagnostics_trace(
                descriptor["trace_id"], name="demo", target="authenticate_39", paths=paths
            )
            self.assertEqual(target_page["status"], "ok")
            self.assertGreaterEqual(target_page["matched_total"], 1)
            symbol_index = target_page["columns"].index("symbol")
            self.assertTrue(any("authenticate_39" in str(row[symbol_index]) for row in target_page["rows"]))

            page = code_diagnostics_trace(
                descriptor["trace_id"], name="demo", offset=0, limit=10, paths=paths
            )
            self.assertEqual(page["status"], "ok")
            self.assertLessEqual(page["returned"], 10)
            self.assertEqual(page["pool_size"], descriptor["pool_size"])
            self.assertNotIn("preview", json.dumps(page))

    def test_r102_diagnostics_response_serializes_telemetry_before_hits(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            for idx in range(30):
                (repo / f"auth_{idx}.py").write_text(
                    f"def authenticate_{idx}(request):\n    return bool(request.get('credentials'))\n",
                    encoding="utf-8",
                )
            result = codebase_search(
                "where can credentials be rejected before authorization",
                name="demo",
                mode="conceptual",
                view="diagnostics",
                use_qdrant=False,
                use_reranker=False,
                limit=20,
                paths=paths,
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["view"], "diagnostics")
            keys = list(result)
            self.assertLess(keys.index("details"), keys.index("hits"))
            self.assertTrue(all("preview" not in hit for hit in result["hits"]))
            retrieval = result["details"]["retrieval"]
            self.assertIn("fts", retrieval)
            self.assertIn("reranker", retrieval)
            self.assertIn("stage_top", retrieval)
            trace = result["details"]["candidate_trace"]
            self.assertEqual(trace["encoding"], "stored-columns+rows")
            self.assertEqual(trace["rows_inline"], 0)
            self.assertIn("trace_id", trace)
            self.assertNotIn("rows", trace)
            self.assertNotIn("rerank_request_documents", retrieval)
            self.assertIn("request_documents", retrieval["reranker"])

    def test_r102_diagnostic_trace_is_project_scoped(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            root = Path(td)
            paths, repo = self.make_project(root, "demo")
            (repo / "auth.py").write_text("def authenticate():\n    return True\n", encoding="utf-8")
            project_create("other", paths=paths)
            result = codebase_search(
                "authenticate",
                name="demo",
                mode="conceptual",
                view="diagnostics",
                use_qdrant=False,
                use_reranker=False,
                paths=paths,
            )
            trace_id = result["details"]["candidate_trace"]["trace_id"]
            denied = code_diagnostics_trace(trace_id, name="other", paths=paths)
            self.assertEqual(denied["status"], "rejected")
            self.assertIn("different project", denied["reason"])

    def test_r102_rerank_diagnostics_explain_reserved_lane_exclusion(self):
        rows = []
        for idx in range(45):
            rows.append({
                "chunk_id": f"c{idx}",
                "path": f"pipeline/authn/auth_{idx}.go",
                "symbol_name": f"Authenticate{idx}",
                "symbol_kind": "method",
                "authority_class": "production_implementation",
                "score": 1.0 - idx / 100.0,
                "fused_rank": idx + 1,
                "pre_rerank_rank": idx + 1,
                "text": "credentials rejected before authorization",
                "retrieval_backends": ["code_fts"],
                "refinement_requalified": True,
                "refinement_parent_fused_rank": 6 + idx,
                "refinement_parent_path": f"pipeline/authn/parent_{idx}.go",
                "refinement_query_overlap": 0.30,
            })
        selected, tail, telemetry = engine._select_rerank_window(
            "where can credentials be rejected before authorization",
            rows,
            30,
            focus="implementation",
        )
        self.assertEqual(len(selected), 30)
        self.assertEqual(telemetry["focus_budget"], 8)
        excluded = next(row for row in tail if row.get("rerank_focus_lane_eligible"))
        self.assertTrue(excluded["rerank_focus_lane_eligible"])
        self.assertIn("query_overlap", excluded["rerank_focus_lane_signals"])
        self.assertGreater(excluded["rerank_focus_selection_order"], telemetry["focus_budget"])
        self.assertIn("focus budget exhausted", excluded["rerank_selection_exclusion"])

    def test_r9_source_roles_distinguish_schema_docs_and_generated(self):
        self.assertEqual(indexing_policy.source_role(".schemas/auth.schema.json"), "config_schema")
        self.assertEqual(indexing_policy.source_role("docs/security.md"), "documentation")
        self.assertEqual(indexing_policy.source_role("internal/stub/generated.go"), "generated_or_vendor")
        self.assertEqual(indexing_policy.source_role("pipeline/authn/auth.go"), "production")

    def test_r9_structural_promotion_is_candidate_generation_not_authority_proof(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "auth.py").write_text(
                "def reject_credentials(value):\n"
                "    return value is None\n",
                encoding="utf-8",
            )
            (repo / "test_auth.py").write_text(
                "from auth import reject_credentials\n\n"
                "def test_rejection():\n"
                "    assert reject_credentials(None)\n",
                encoding="utf-8",
            )
            engine.index_project_code(paths, "demo", include_qdrant=False)
            pp = project_workspace.paths_for(paths.root, "demo")
            db = store.db_path(pp.project_dir)
            branch = engine.branch_identity("demo", repo)
            test_defs = store.definitions(db, "demo", branch.branch_key, "test_rejection")
            self.assertEqual(len(test_defs), 1)
            source = dict(test_defs[0])
            source.update({"score": 0.9, "fused_rank": 1, "retrieval_backend": "code_fts"})
            promoted = engine._structural_promotions(
                db, branch.branch_key, [source], "where are credentials rejected"
            )
            self.assertTrue(any(row.get("symbol_name") == "reject_credentials" for row in promoted))
            target = next(row for row in promoted if row.get("symbol_name") == "reject_credentials")
            self.assertTrue(target["promotion_candidate_only"])
            self.assertEqual(target["promotion_edge"], "resolved_call")
            self.assertEqual(target["promotion_graph_distance"], 1)
            self.assertIn("promotion_query_overlap", target)

    def test_r9_promoted_candidate_does_not_gain_authority_without_query_relevance(self):
        rows = [
            {
                "path": "src/relevant_test.py", "symbol_kind": "function", "score": 0.50,
                "authority_class": "test", "fused_rank": 1,
            },
            {
                "path": "src/new_proxy.py", "symbol_kind": "function", "score": 0.05,
                "authority_class": "production_implementation", "fused_rank": 99,
                "promotion_candidate_only": True, "promotion_query_overlap": 0.0,
            },
        ]
        ranked = engine._apply_authority_prior(
            "where can credentials be rejected before authorization", rows, "implementation"
        )
        promoted = next(row for row in ranked if row["path"] == "src/new_proxy.py")
        self.assertAlmostEqual(promoted["authority_adjustment"], 0.0)
        self.assertNotEqual(ranked[0]["path"], "src/new_proxy.py")

    def test_r9_structural_promotions_receive_bounded_rerank_slots_without_displacing_raw_top(self):
        raw = [
            {"path": f"src/raw_{i}.py", "symbol_id": f"raw-{i}", "score": 1.0 - i / 100.0}
            for i in range(30)
        ]
        promotions = [
            {"path": f"src/promoted_{i}.py", "symbol_id": f"prom-{i}", "score": 0.001}
            for i in range(8)
        ]
        candidates = engine._compose_rerank_candidates(raw, promotions, 30)
        self.assertEqual(len(candidates), 30)
        self.assertEqual(candidates[0]["symbol_id"], "raw-0")
        self.assertGreater(sum(1 for row in candidates if str(row["symbol_id"]).startswith("prom-")), 0)
        self.assertGreater(sum(1 for row in candidates if str(row["symbol_id"]).startswith("raw-")), 20)

    def test_r9_authority_prior_prefers_relevant_implementation_but_not_hard_filters_tests(self):
        rows = [
            {"path": ".schemas/auth.schema.json", "symbol_kind": "file", "score": 0.12, "fused_rank": 1, "retrieval_backends": ["code_fts", "code_qdrant"]},
            {"path": "src/auth.py", "symbol_kind": "function", "score": 0.10, "fused_rank": 2, "retrieval_backends": ["code_fts", "code_qdrant"], "text": "def reject_credentials(): pass"},
            {"path": "tests/test_auth.py", "symbol_kind": "function", "score": 0.11, "fused_rank": 3, "retrieval_backends": ["code_qdrant"]},
            {"path": "src/migrate.py", "symbol_kind": "function", "score": 0.02, "fused_rank": 4, "retrieval_backends": ["code_fts"], "text": "def migrate(): pass"},
        ]
        implementation = engine._apply_authority_prior("where is credential rejection enforced", rows, "implementation")
        self.assertEqual(implementation[0]["path"], "src/auth.py")
        self.assertLess(
            next(row for row in implementation if row["path"] == "src/migrate.py")["score"],
            next(row for row in implementation if row["path"] == "tests/test_auth.py")["score"],
        )
        tests = engine._apply_authority_prior("find the credential rejection tests", rows, "tests")
        self.assertEqual(tests[0]["path"], "tests/test_auth.py")
        self.assertEqual({row["path"] for row in tests}, {row["path"] for row in rows})

    def test_r9_authority_scoring_rescues_dual_backend_implementation_without_promoting_unrelated_production(self):
        query = "Several credential-handling strategies are tried and one can continue before authentication fails"
        rows = [
            {"path": "stub/.oathkeeper.schema.json", "symbol_kind": "file", "score": 0.06143, "fused_rank": 1, "retrieval_backends": ["code_fts", "code_qdrant"]},
            {"path": "oryx/decoderx/http.go", "symbol_kind": "function", "score": 0.05455, "fused_rank": 4, "retrieval_backends": ["code_fts"], "text": "HTTPDecoderSetIgnoreParseErrorsStrategy"},
            {"path": "pipeline/authn/authenticator_oauth2_introspection.go", "symbol_kind": "method", "score": 0.05296, "fused_rank": 7, "retrieval_backends": ["code_fts", "code_qdrant"], "text": "Authenticate ErrAuthenticatorNotResponsible"},
        ]
        ranked = engine._apply_authority_prior(query, rows, "implementation")
        self.assertEqual(ranked[0]["path"], "pipeline/authn/authenticator_oauth2_introspection.go")
        unrelated = next(row for row in ranked if row["path"] == "oryx/decoderx/http.go")
        relevant = ranked[0]
        self.assertLess(unrelated["authority_relevance_signal"], relevant["authority_relevance_signal"])
        self.assertLess(unrelated["authority_adjustment"], relevant["authority_adjustment"])

        q1 = [
            {"path": ".schemas/auth.schema.json", "symbol_kind": "file", "score": 0.14828, "fused_rank": 1, "retrieval_backends": ["code_fts", "code_qdrant"]},
            {"path": "proxy/proxy_test.go", "symbol_kind": "function", "score": 0.09879, "fused_rank": 2, "retrieval_backends": ["code_qdrant"]},
            {"path": "oryx/popx/cmd.go", "symbol_kind": "function", "score": 0.05595, "fused_rank": 4, "retrieval_backends": ["code_fts"], "text": "NewMigrateSQLUpCmd"},
        ]
        q1_ranked = engine._apply_authority_prior(
            "Where can a request carrying credentials be rejected before it reaches authorization?",
            q1,
            "implementation",
        )
        self.assertNotEqual(q1_ranked[0]["path"], "oryx/popx/cmd.go")
        self.assertGreater(
            next(row for row in q1_ranked if row["path"] == "proxy/proxy_test.go")["score"],
            next(row for row in q1_ranked if row["path"] == "oryx/popx/cmd.go")["score"],
        )

    def test_r9_guarded_production_representation_requires_independent_support(self):
        rows = [
            {"path": "schema/a.json", "score": 1.00, "authority_class": "config_schema", "pre_authority_score": 1.00, "authority_relevance_signal": 1.0},
            {"path": "tests/a_test.py", "score": 0.90, "authority_class": "test", "pre_authority_score": 0.90, "authority_relevance_signal": 0.2},
            {"path": "schema/b.json", "score": 0.80, "authority_class": "config_schema", "pre_authority_score": 0.80, "authority_relevance_signal": 1.0},
            {"path": "src/relevant.py", "score": 0.70, "authority_class": "production_implementation", "pre_authority_score": 0.70, "authority_relevance_signal": 1.0},
            {"path": "src/unrelated.py", "score": 0.69, "authority_class": "production_implementation", "pre_authority_score": 0.69, "authority_relevance_signal": 0.0},
        ]
        ranked = engine._diversify_results(rows, "implementation")
        self.assertEqual(ranked[2]["path"], "src/relevant.py")
        self.assertTrue(ranked[2]["authority_representation_reserved"])
        self.assertFalse(bool(next(row for row in ranked if row["path"] == "src/unrelated.py").get("authority_representation_reserved")))

    def test_r9_diversity_reduces_schema_crowding_without_hard_filtering(self):
        rows = [
            {"path": ".schemas/a.schema.json", "score": 0.80, "authority_class": "config_schema", "fused_rank": 1},
            {"path": ".schemas/b.schema.json", "score": 0.79, "authority_class": "config_schema", "fused_rank": 2},
            {"path": ".schemas/c.schema.json", "score": 0.78, "authority_class": "config_schema", "fused_rank": 3},
            {"path": "src/auth.py", "score": 0.77, "authority_class": "production_implementation", "fused_rank": 4},
            {"path": "tests/test_auth.py", "score": 0.76, "authority_class": "test", "fused_rank": 5},
        ]
        ranked = engine._diversify_results(rows, "implementation")
        self.assertLess(
            next(i for i, row in enumerate(ranked) if row["path"] == "src/auth.py"),
            next(i for i, row in enumerate(ranked) if row["path"] == ".schemas/c.schema.json"),
        )
        self.assertEqual({row["path"] for row in ranked}, {row["path"] for row in rows})
        self.assertTrue(any(float(row.get("diversity_adjustment") or 0.0) < 0 for row in ranked))

    def test_r9_reranker_telemetry_is_explicit_and_scores_survive(self):
        rows = [{
            "chunk_id": "c1", "path": "src/auth.py", "symbol_kind": "function",
            "text": "def reject_credentials(): pass", "score": 0.2, "fused_rank": 1,
        }]
        profile = {
            "enabled": True, "provider": "tei", "model": "", "candidate_limit": 30,
            "top_n": 10, "timeout_seconds": 20, "max_document_chars": 4000,
        }
        def fake_rerank(query, payload, limit, timeout_override=None, top_n_override=None):
            self.assertNotIn("authority=", str(payload[0].get("kind") or ""))
            item = dict(payload[0])
            item["rerank_score"] = 0.91
            item["rerank_backend"] = "remote_http"
            return [item]
        with mock.patch("rag_backend.rerank_profile", return_value=profile), \
             mock.patch("rag_backend.rerank_enabled", return_value=True), \
             mock.patch("rag_backend.rerank_hits", side_effect=fake_rerank):
            ranked, telemetry = engine._rerank("credential rejection", rows, 10, enabled=True)
        self.assertTrue(telemetry["attempted"])
        self.assertTrue(telemetry["applied"])
        self.assertEqual(telemetry["backend"], "tei")
        self.assertEqual(ranked[0]["rerank_score"], 0.91)
        self.assertEqual(ranked[0]["pre_rerank_score"], 0.2)
        self.assertEqual(ranked[0]["rerank_rank"], 1)
        self.assertNotEqual(ranked[0]["score"], ranked[0]["rerank_score"])
        self.assertEqual(telemetry["rerank_candidates_scored"] if "rerank_candidates_scored" in telemetry else telemetry["candidates_scored"], 1)
        self.assertEqual(telemetry["results_out"], 1)
        self.assertEqual(telemetry["post_rerank_pool_size"], 1)

    def test_reranker_timeout_inherits_shared_profile_when_code_override_is_empty(self):
        rows = [{
            "chunk_id": "c1", "path": "src/auth.py", "symbol_kind": "function",
            "text": "def authenticate(): pass", "score": 0.2, "fused_rank": 1,
        }]
        profile = {
            "enabled": True, "provider": "tei", "model": "", "candidate_limit": 30,
            "top_n": 10, "timeout_seconds": 20, "max_document_chars": 4000,
        }
        def fake_rerank(query, payload, limit, timeout_override=None, top_n_override=None):
            self.assertEqual(timeout_override, 20.0)
            item = dict(payload[0])
            item["rerank_score"] = 0.8
            return [item]
        with mock.patch.dict(os.environ, {"AWOKI_CODE_RERANK_TIMEOUT_SECONDS": ""}, clear=False), \
             mock.patch("rag_backend.rerank_profile", return_value=profile), \
             mock.patch("rag_backend.rerank_enabled", return_value=True), \
             mock.patch("rag_backend.rerank_hits", side_effect=fake_rerank):
            _, telemetry = engine._rerank("auth", rows, 10, enabled=True)
        self.assertEqual(telemetry["timeout_seconds"], 20.0)
        self.assertEqual(telemetry["timeout_source"], "AWOKI_RERANK_TIMEOUT_SECONDS")

    def test_reranker_timeout_override_and_transient_failure_are_explicit(self):
        rows = [{
            "chunk_id": "c1", "path": "src/auth.py", "symbol_kind": "function",
            "text": "def authenticate(): pass", "score": 0.2, "fused_rank": 1,
        }]
        profile = {
            "enabled": True, "provider": "tei", "model": "", "candidate_limit": 30,
            "top_n": 10, "timeout_seconds": 20, "max_document_chars": 4000,
        }
        def fake_rerank(query, payload, limit, timeout_override=None, top_n_override=None):
            self.assertEqual(timeout_override, 7.0)
            item = dict(payload[0])
            item["rerank_error"] = "Request timed out."
            item["rerank_fallback"] = True
            return [item]
        with mock.patch.dict(os.environ, {"AWOKI_CODE_RERANK_TIMEOUT_SECONDS": "7"}, clear=False), \
             mock.patch("rag_backend.rerank_profile", return_value=profile), \
             mock.patch("rag_backend.rerank_enabled", return_value=True), \
             mock.patch("rag_backend.rerank_hits", side_effect=fake_rerank):
            _, telemetry = engine._rerank("auth", rows, 10, enabled=True)
        self.assertEqual(telemetry["timeout_seconds"], 7.0)
        self.assertEqual(telemetry["timeout_source"], "AWOKI_CODE_RERANK_TIMEOUT_SECONDS<=AWOKI_RERANK_TIMEOUT_SECONDS")
        self.assertEqual(telemetry["failure_class"], "timeout")
        self.assertTrue(telemetry["retryable"])
        self.assertTrue(telemetry["degraded"])

    def test_reranker_code_timeout_cannot_exceed_shared_transport_timeout(self):
        rows = [{"chunk_id": "c1", "path": "src/auth.py", "symbol_kind": "function", "text": "x", "score": 0.2, "fused_rank": 1}]
        profile = {"enabled": True, "provider": "tei", "model": "", "candidate_limit": 30, "top_n": 10, "timeout_seconds": 20, "max_document_chars": 4000}
        def fake_rerank(query, payload, limit, timeout_override=None, top_n_override=None):
            self.assertEqual(timeout_override, 20.0)
            item = dict(payload[0]); item["rerank_score"] = 0.8; return [item]
        with mock.patch.dict(os.environ, {"AWOKI_CODE_RERANK_TIMEOUT_SECONDS": "60"}, clear=False), \
             mock.patch("rag_backend.rerank_profile", return_value=profile), \
             mock.patch("rag_backend.rerank_enabled", return_value=True), \
             mock.patch("rag_backend.rerank_hits", side_effect=fake_rerank):
            _, telemetry = engine._rerank("auth", rows, 10, enabled=True)
        self.assertEqual(telemetry["timeout_seconds"], 20.0)

    def test_r91_symbol_refinement_descends_from_module_to_concrete_method(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "auth.py").write_text(
                "class Authenticator:\n"
                "    def validate(self, request):\n"
                "        return True\n\n"
                "    def authenticate(self, request):\n"
                "        if not request.get('credentials'):\n"
                "            raise ValueError('credentials rejected before authorization')\n"
                "        return True\n",
                encoding="utf-8",
            )
            engine.index_project_code(paths, "demo", include_qdrant=False)
            pp = project_workspace.paths_for(paths.root, "demo")
            db = store.db_path(pp.project_dir)
            branch = engine.branch_identity("demo", repo)
            module = next(
                row for row in store.definitions(db, "demo", branch.branch_key, "auth.py", limit=20)
                if row.get("symbol_kind") == "module"
            )
            module.update({
                "score": 0.04,
                "fused_rank": 4,
                "retrieval_backends": ["code_fts", "code_qdrant"],
            })
            refined = engine._symbol_refinements(
                db,
                [module],
                "where can credentials be rejected before authorization",
                focus="implementation",
            )
            names = {row.get("symbol_name") for row in refined}
            self.assertIn("authenticate", names)
            target = next(row for row in refined if row.get("symbol_name") == "authenticate")
            self.assertTrue(target["refinement_candidate_only"])
            self.assertEqual(target["refinement_parent_symbol_id"], module["symbol_id"])
            self.assertEqual(target["refinement_parent_fused_rank"], 4)
            self.assertEqual(target["symbol_kind"], "method")
            self.assertIn("credentials rejected", target["text"])
            self.assertLess(target["score"], module["score"] + 0.1)

    def test_r911_module_refinement_falls_back_to_exact_file_scope_when_parent_edges_are_absent(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "auth.py").write_text(
                "def authenticate(request):\n"
                "    if request.get('credentials') == 'bad':\n"
                "        raise ValueError('rejected before authorization')\n"
                "    return True\n",
                encoding="utf-8",
            )
            engine.index_project_code(paths, "demo", include_qdrant=False)
            pp = project_workspace.paths_for(paths.root, "demo")
            db = store.db_path(pp.project_dir)
            branch = engine.branch_identity("demo", repo)
            module = next(
                row for row in store.definitions(db, "demo", branch.branch_key, "auth.py", limit=20)
                if row.get("symbol_kind") == "module"
            )
            module.update({
                "score": 0.04,
                "fused_rank": 6,
                "retrieval_backends": ["code_fts", "code_qdrant"],
            })
            diagnostics = {}
            with mock.patch("code_search.store.child_symbols", return_value=[]):
                refined = engine._symbol_refinements(
                    db,
                    [module],
                    "where can credentials be rejected before authorization",
                    focus="implementation",
                    diagnostics=diagnostics,
                )
            target = next(row for row in refined if row.get("symbol_name") == "authenticate")
            self.assertEqual(target["refinement_enumeration"], "file_scope")
            self.assertEqual(target["refinement_parent_fused_rank"], 6)
            self.assertEqual(diagnostics["parents_refined"], 1)
            self.assertGreaterEqual(diagnostics["children_generated"], 1)
            self.assertIn("file_scope", diagnostics["parents"][0]["enumeration"])

    def test_r91_coarse_modules_and_interfaces_are_not_implementation_authority(self):
        self.assertEqual(
            engine._authority_class({"path": "credentials/signer.go", "symbol_kind": "module"}),
            "production_module",
        )
        self.assertEqual(
            engine._authority_class({"path": "credentials/signer.go", "symbol_kind": "interface"}),
            "production_contract",
        )
        self.assertEqual(
            engine._authority_class({"path": "pipeline/authn/authenticator.go", "symbol_kind": "method"}),
            "production_implementation",
        )

    def test_r91_refinements_are_inside_actual_reranker_evaluation_window(self):
        raw = [
            {"path": f"src/raw_{i}.py", "symbol_id": f"raw-{i}", "score": 1.0 - i / 100.0}
            for i in range(50)
        ]
        refinements = [
            {"path": f"src/refined_{i}.py", "symbol_id": f"ref-{i}", "score": 0.001, "refinement_candidate_only": True}
            for i in range(8)
        ]
        candidates = engine._compose_rerank_candidates(
            raw,
            [],
            50,
            refinements=refinements,
            evaluation_limit=30,
        )
        self.assertEqual(len(candidates), 50)
        first_eval_window = candidates[:30]
        self.assertTrue(any(str(row["symbol_id"]).startswith("ref-") for row in first_eval_window))
        self.assertEqual(candidates[0]["symbol_id"], "raw-0")
        self.assertGreater(sum(1 for row in first_eval_window if str(row["symbol_id"]).startswith("raw-")), 15)

    def test_r91_unscored_refinement_cannot_inherit_parent_rank_into_top_results(self):
        rows = [
            {"path": "schema.json", "score": 0.04, "fused_rank": 1, "authority_class": "config_schema"},
            {
                "path": "src/auth.py", "symbol_name": "authenticate", "symbol_kind": "method",
                "score": 0.001, "refinement_candidate_only": True,
                "refinement_parent_fused_rank": 2, "authority_class": "production_implementation",
            },
        ]
        fused = engine._rank_fuse_after_rerank(rows)
        refined = next(row for row in fused if row["path"] == "src/auth.py")
        self.assertNotIn("parent_discovery", refined["rank_fusion_components"])
        self.assertLess(refined["score"], next(row for row in fused if row["path"] == "schema.json")["score"])

    def test_r91_scored_refinement_can_earn_top_three_without_hard_filtering_test_or_schema(self):
        query = "where can credentials be rejected before authorization"
        rows = [
            {
                "path": ".schemas/auth.schema.json", "symbol_kind": "file", "fused_rank": 3,
                "rerank_rank": 1, "rerank_score": 0.105, "score": 0.105,
                "retrieval_backends": ["code_fts", "code_qdrant"],
            },
            {
                "path": "proxy/proxy_test.go", "symbol_kind": "function", "fused_rank": 23,
                "rerank_rank": 2, "rerank_score": 0.055, "score": 0.055,
                "retrieval_backends": ["code_qdrant"],
            },
            {
                "path": "pipeline/authn/authenticator.go", "symbol_kind": "method",
                "symbol_name": "Authenticate", "text": "reject credentials before authorization",
                "refinement_candidate_only": True, "refinement_parent_fused_rank": 6,
                "rerank_rank": 3, "rerank_score": 0.052, "score": 0.052,
                "retrieval_backends": ["symbol_refinement"],
            },
        ]
        fused = engine._rank_fuse_after_rerank(rows)
        authority = engine._apply_authority_prior(query, fused, "implementation")
        final = engine._diversify_results(authority, "implementation")
        self.assertEqual({row["path"] for row in final}, {row["path"] for row in rows})
        implementation_rank = next(i for i, row in enumerate(final, start=1) if row["path"].endswith("authenticator.go"))
        self.assertLessEqual(implementation_rank, 3)
        refined = next(row for row in final if row["path"].endswith("authenticator.go"))
        self.assertGreaterEqual(refined["authority_rerank_signal"], 0.8)

    def test_r91_rerank_telemetry_distinguishes_scored_rows_from_post_pool(self):
        rows = [
            {"chunk_id": f"c{i}", "path": f"src/f{i}.py", "symbol_kind": "function", "text": f"def f{i}(): pass", "score": 0.1, "fused_rank": i + 1}
            for i in range(4)
        ]
        profile = {
            "enabled": True, "provider": "tei", "model": "", "candidate_limit": 3,
            "top_n": 2, "timeout_seconds": 20, "max_document_chars": 4000,
        }
        def fake_rerank(query, payload, limit, timeout_override=None, top_n_override=None):
            self.assertEqual(len(payload), 3)
            self.assertEqual(limit, 3)
            out = []
            for idx, score in ((1, 0.9), (0, 0.8)):
                item = dict(payload[idx])
                item["rerank_score"] = score
                item["rerank_backend"] = "remote_http"
                out.append(item)
            out.extend(dict(item) for item in payload if item["id"] not in {payload[0]["id"], payload[1]["id"]})
            return out[:limit]
        with mock.patch("rag_backend.rerank_profile", return_value=profile), \
             mock.patch("rag_backend.rerank_enabled", return_value=True), \
             mock.patch("rag_backend.rerank_hits", side_effect=fake_rerank):
            ranked, telemetry = engine._rerank("credential rejection", rows, 4, enabled=True)
        self.assertEqual(telemetry["candidates_selected"], 3)
        self.assertEqual(telemetry["request_documents"], 3)
        self.assertEqual(telemetry["scores_returned"], 2)
        self.assertEqual(telemetry["candidates_scored"], 2)
        self.assertEqual(telemetry["candidates_unscored"], 1)
        self.assertEqual(telemetry["candidates_not_selected"], 1)
        self.assertEqual(telemetry["results_out"], 2)
        self.assertEqual(telemetry["post_rerank_pool_size"], 4)
        self.assertEqual(len(ranked), 4)

    def test_r91_lexical_vector_skip_reason_does_not_claim_vectors_are_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "auth.py").write_text("def reject_credentials():\n    return False\n", encoding="utf-8")
            result = codebase_search(
                "reject credentials",
                name="demo",
                mode="lexical",
                use_qdrant=False,
                use_reranker=False,
                paths=paths,
            )
            self.assertEqual(result["details"]["vector_search"]["status"], "skipped")
            self.assertEqual(result["details"]["vector_search"]["reason"], "disabled_by_query_controls")

    def test_r91_end_to_end_refinement_reranks_concrete_method_not_just_module(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "auth.py").write_text(
                "class Authenticator:\n"
                "    def config(self):\n"
                "        return {'enabled': True}\n\n"
                "    def authenticate(self, request):\n"
                "        if not request.get('credentials'):\n"
                "            raise ValueError('credentials rejected before authorization')\n"
                "        return True\n",
                encoding="utf-8",
            )
            engine.index_project_code(paths, "demo", include_qdrant=False)
            pp = project_workspace.paths_for(paths.root, "demo")
            db = store.db_path(pp.project_dir)
            branch = engine.branch_identity("demo", repo)
            module = next(
                row for row in store.definitions(db, "demo", branch.branch_key, "auth.py", limit=20)
                if row.get("symbol_kind") == "module"
            )
            module["score"] = 0.8
            module["retrieval_backend"] = "code_fts"
            profile = {
                "enabled": True, "provider": "tei", "model": "", "candidate_limit": 30,
                "top_n": 10, "timeout_seconds": 20, "max_document_chars": 4000,
            }

            def fake_rerank(query, payload, limit, timeout_override=None, top_n_override=None):
                scored = []
                unscored = []
                for item in payload:
                    row = dict(item)
                    if "credentials rejected before authorization" in str(row.get("preview") or ""):
                        row["rerank_score"] = 0.99
                        row["rerank_backend"] = "remote_http"
                        scored.append(row)
                    else:
                        unscored.append(row)
                return (scored + unscored)[:limit]

            with mock.patch.object(store, "search_fts", return_value=[module]), \
                 mock.patch("rag_backend.rerank_profile", return_value=profile), \
                 mock.patch("rag_backend.rerank_enabled", return_value=True), \
                 mock.patch("rag_backend.rerank_hits", side_effect=fake_rerank):
                result = codebase_search(
                    "where can credentials be rejected before authorization",
                    name="demo",
                    mode="conceptual",
                    use_qdrant=False,
                    use_reranker=True,
                    result_focus="implementation",
                    structural_promotion=False,
                    limit=10,
                    paths=paths,
                )
            self.assertEqual(result["status"], "ok")
            self.assertGreater(result["details"]["retrieval"]["symbol_refinements"], 0)
            self.assertTrue(result["details"]["retrieval"]["rerank_applied"])
            concrete = next(hit for hit in result["hits"] if hit["symbol"] == "authenticate")
            self.assertTrue(concrete["refinement_candidate_only"])
            self.assertEqual(concrete["authority_class"], "production_implementation")
            self.assertLessEqual(concrete["final_rank"], 3)
            module_hit = next(hit for hit in result["hits"] if hit["symbol_kind"] == "module")
            self.assertEqual(module_hit["authority_class"], "production_module")
            self.assertLess(concrete["final_rank"], module_hit["final_rank"])

    def test_r912_refinement_diagnostics_distinguish_existing_child_from_omission(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "auth.py").write_text(
                "class Authenticator:\n"
                "    def get_id(self):\n"
                "        return 'auth'\n\n"
                "    def authenticate(self, request):\n"
                "        return bool(request.get('credentials'))\n",
                encoding="utf-8",
            )
            engine.index_project_code(paths, "demo", include_qdrant=False)
            pp = project_workspace.paths_for(paths.root, "demo")
            db = store.db_path(pp.project_dir)
            branch = engine.branch_identity("demo", repo)
            defs = store.symbols_in_file(db, "demo", branch.branch_key, "auth.py", limit=20)
            module = next(row for row in defs if row.get("symbol_kind") == "module")
            get_id = next(row for row in defs if row.get("symbol_name") == "get_id")
            module.update({"score": 0.04, "fused_rank": 6, "retrieval_backends": ["code_fts", "code_qdrant"]})
            get_id.update({"score": 0.03, "fused_rank": 12, "retrieval_backends": ["code_fts"]})
            diagnostics = {}
            refined = engine._symbol_refinements(
                db,
                [module, get_id],
                "where are credentials authenticated",
                focus="implementation",
                diagnostics=diagnostics,
            )
            self.assertIn("authenticate", {row.get("symbol_name") for row in refined})
            parent = diagnostics["parents"][0]
            self.assertGreaterEqual(parent["children_available"], 2)
            self.assertEqual(parent["children_already_present"], 1)
            represented = {row["symbol"]: row["reason"] for row in parent["represented_children"]}
            self.assertTrue(any("get_id" in name for name in represented))
            self.assertIn("already_in_discovery_pool", represented.values())
            self.assertEqual(parent["children_omitted_by_parent_limit"], 0)
            self.assertEqual(parent["children_omitted_by_total_limit"], 0)

    def test_r912_refinement_reports_per_parent_truncation_instead_of_silent_loss(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "many.py").write_text(
                "\n".join(f"def method_{i}(request):\n    return {i}" for i in range(6)) + "\n",
                encoding="utf-8",
            )
            engine.index_project_code(paths, "demo", include_qdrant=False)
            pp = project_workspace.paths_for(paths.root, "demo")
            db = store.db_path(pp.project_dir)
            branch = engine.branch_identity("demo", repo)
            module = next(
                row for row in store.definitions(db, "demo", branch.branch_key, "many.py", limit=20)
                if row.get("symbol_kind") == "module"
            )
            module.update({"score": 0.04, "fused_rank": 3, "retrieval_backends": ["code_fts", "code_qdrant"]})
            diagnostics = {}
            refined = engine._symbol_refinements(
                db,
                [module],
                "find request implementation",
                focus="implementation",
                max_children_per_parent=3,
                diagnostics=diagnostics,
            )
            self.assertEqual(len(refined), 3)
            parent = diagnostics["parents"][0]
            self.assertGreaterEqual(parent["children_available"], 6)
            self.assertEqual(parent["children_generated"], 3)
            self.assertGreaterEqual(parent["children_omitted_by_parent_limit"], 3)
            self.assertTrue(all(row["reason"] == "max_children_per_parent" for row in parent["omitted_children"]))

    def test_r912_rerank_telemetry_does_not_claim_selected_without_returned_score_was_unscored_by_backend(self):
        rows = [
            {"chunk_id": f"c{i}", "path": f"src/f{i}.py", "symbol_kind": "function", "text": f"def f{i}(): pass", "score": 0.1, "fused_rank": i + 1}
            for i in range(4)
        ]
        profile = {
            "enabled": True, "provider": "tei", "model": "", "candidate_limit": 3,
            "top_n": 1, "timeout_seconds": 20, "max_document_chars": 4000,
        }
        def fake_rerank(query, payload, limit, timeout_override=None, top_n_override=None):
            self.assertEqual(top_n_override, 3)
            item = dict(payload[0])
            item["rerank_score"] = 0.9
            item["rerank_backend"] = "remote_http"
            return [item] + [dict(row) for row in payload[1:]]
        with mock.patch("rag_backend.rerank_profile", return_value=profile), \
             mock.patch("rag_backend.rerank_enabled", return_value=True), \
             mock.patch("rag_backend.rerank_hits", side_effect=fake_rerank):
            ranked, telemetry = engine._rerank("credential rejection", rows, 4, enabled=True)
        self.assertEqual(telemetry["request_documents"], 3)
        self.assertEqual(telemetry["configured_top_n"], 1)
        self.assertEqual(telemetry["results_requested_top_n"], 3)
        self.assertEqual(telemetry["scores_returned_to_awoki"], 1)
        self.assertEqual(telemetry["selected_without_returned_score"], 2)
        self.assertEqual(telemetry["backend_scoring_coverage"], "not_observable_from_rerank_contract")
        selected = [row for row in ranked if row.get("rerank_selected")]
        self.assertEqual(len(selected), 3)
        self.assertEqual(sum(1 for row in selected if row.get("rerank_score_returned")), 1)

    def test_r913_existing_refinement_child_is_requalified_for_rerank_selection(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "auth.py").write_text(
                "class Authenticator:\n"
                "    def get_id(self):\n"
                "        return 'auth'\n\n"
                "    def authenticate(self, request):\n"
                "        # credentials rejected before authorization\n"
                "        return bool(request.get('credentials'))\n",
                encoding="utf-8",
            )
            engine.index_project_code(paths, "demo", include_qdrant=False)
            pp = project_workspace.paths_for(paths.root, "demo")
            db = store.db_path(pp.project_dir)
            branch = engine.branch_identity("demo", repo)
            defs = store.symbols_in_file(db, "demo", branch.branch_key, "auth.py", limit=20)
            module = next(row for row in defs if row.get("symbol_kind") == "module")
            authenticate = next(row for row in defs if row.get("symbol_name") == "authenticate")
            module.update({"score": 0.04, "fused_rank": 6, "retrieval_backends": ["code_fts", "code_qdrant"]})
            authenticate.update({"score": 0.01, "fused_rank": 35, "retrieval_backends": ["code_fts"]})
            diagnostics = {}
            refined = engine._symbol_refinements(
                db,
                [module, authenticate],
                "where can credentials be rejected before authorization",
                focus="implementation",
                diagnostics=diagnostics,
            )
            self.assertFalse(any(row.get("symbol_name") == "authenticate" for row in refined))
            self.assertTrue(authenticate["refinement_requalified"])
            self.assertEqual(authenticate["refinement_parent_fused_rank"], 6)
            self.assertEqual(authenticate["refinement_parent_path"], "auth.py")

    def test_r913_focus_aware_rerank_window_selects_deep_requalified_implementation(self):
        rows = []
        for i in range(40):
            rows.append({
                "chunk_id": f"c{i}",
                "path": f"tests/noise_{i}.py",
                "symbol_kind": "function",
                "authority_class": "test",
                "score": 1.0 - i / 100.0,
                "fused_rank": i + 1,
                "pre_rerank_rank": i + 1,
                "text": "descriptive authentication test fixture",
                "retrieval_backends": ["code_qdrant"],
            })
        authenticate = {
            "chunk_id": "auth-deep",
            "path": "pipeline/authn/authenticator.go",
            "symbol_name": "Authenticate",
            "symbol_kind": "method",
            "authority_class": "production_implementation",
            "score": 0.20,
            "fused_rank": 35,
            "pre_rerank_rank": 35,
            "text": "func Authenticate credentials rejected before authorization",
            "retrieval_backends": ["code_fts"],
            "refinement_requalified": True,
            "refinement_parent_fused_rank": 6,
            "refinement_parent_path": "pipeline/authn/authenticator.go",
            "refinement_query_overlap": 0.33,
        }
        rows[34] = authenticate
        unrelated = {
            "chunk_id": "unrelated-prod",
            "path": "cmd/migrate.go",
            "symbol_name": "Migrate",
            "symbol_kind": "function",
            "authority_class": "production_implementation",
            "score": 0.21,
            "fused_rank": 34,
            "pre_rerank_rank": 34,
            "text": "database migration command",
            "retrieval_backends": ["code_fts"],
        }
        rows[33] = unrelated
        selected, tail, telemetry = engine._select_rerank_window(
            "where can a request carrying credentials be rejected before authorization",
            rows,
            30,
            focus="implementation",
        )
        selected_by_id = {row["chunk_id"]: row for row in selected}
        self.assertIn("auth-deep", selected_by_id)
        self.assertEqual(selected_by_id["auth-deep"]["rerank_selection_lane"], "focus")
        self.assertNotIn("unrelated-prod", selected_by_id)
        self.assertEqual(telemetry["budget"], 30)
        self.assertEqual(telemetry["general_budget"], 18)
        self.assertGreaterEqual(telemetry["focus_selected"], 1)
        self.assertIn("unrelated-prod", {row["chunk_id"] for row in tail})

    def test_r914_requalified_existing_child_survives_composition_and_reaches_focus_selector(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "auth.py").write_text(
                "class Authenticator:\n"
                "    def get_id(self):\n"
                "        return 'auth'\n\n"
                "    def authenticate(self, request):\n"
                "        # credentials rejected before authorization\n"
                "        return bool(request.get('credentials'))\n",
                encoding="utf-8",
            )
            engine.index_project_code(paths, "demo", include_qdrant=False)
            pp = project_workspace.paths_for(paths.root, "demo")
            db = store.db_path(pp.project_dir)
            branch = engine.branch_identity("demo", repo)
            defs = store.symbols_in_file(db, "demo", branch.branch_key, "auth.py", limit=20)
            module = next(row for row in defs if row.get("symbol_kind") == "module")
            authenticate = next(row for row in defs if row.get("symbol_name") == "authenticate")
            module.update({
                "score": 0.04,
                "fused_rank": 6,
                "retrieval_backends": ["code_fts", "code_qdrant"],
            })
            authenticate.update({
                "score": 0.01,
                "fused_rank": 100,
                "retrieval_backends": ["code_fts"],
            })

            discovery = []
            for idx in range(99):
                if idx == 5:
                    discovery.append(module)
                    continue
                discovery.append({
                    "chunk_id": f"noise-{idx}",
                    "path": f"tests/noise_{idx}.py",
                    "symbol_name": f"noise_{idx}",
                    "symbol_kind": "function",
                    "authority_class": "test",
                    "score": 1.0 - idx / 200.0,
                    "fused_rank": idx + 1,
                    "retrieval_backends": ["code_qdrant"],
                    "text": "unrelated authentication fixture",
                })
            discovery.append(authenticate)
            self.assertEqual(len(discovery), 100)

            refinement_diagnostics = {}
            refinements = engine._symbol_refinements(
                db,
                discovery,
                "where can credentials be rejected before authorization",
                focus="implementation",
                diagnostics=refinement_diagnostics,
            )
            self.assertTrue(authenticate["refinement_requalified"])
            self.assertEqual(authenticate["refinement_parent_fused_rank"], 6)

            original_score = authenticate["score"]
            composed = engine._compose_rerank_candidates(
                discovery,
                [],
                100,
                refinements=refinements,
                evaluation_limit=30,
            )
            self.assertEqual(len(composed), 100)
            composed_auth = next(row for row in composed if row.get("symbol_name") == "authenticate")
            self.assertTrue(composed_auth["rerank_composition_protected"])
            self.assertEqual(composed_auth["fused_rank"], 100)
            self.assertEqual(composed_auth["score"], original_score)

            for rank, row in enumerate(composed, start=1):
                row["pre_rerank_rank"] = rank
            selected, tail, telemetry = engine._select_rerank_window(
                "where can credentials be rejected before authorization",
                engine._annotate_authority(composed),
                30,
                focus="implementation",
            )
            selected_auth = next(row for row in selected if row.get("symbol_name") == "authenticate")
            self.assertEqual(selected_auth["rerank_selection_lane"], "focus")
            self.assertTrue(selected_auth["rerank_focus_lane_eligible"])
            self.assertIn("strong_refined_or_requalified_parent", selected_auth["rerank_focus_lane_signals"])
            self.assertEqual(telemetry["budget"], 30)
            self.assertFalse(any(row.get("symbol_name") == "authenticate" for row in tail))

    def test_r915_identifier_lexemes_are_language_neutral_across_common_conventions(self):
        self.assertEqual(
            store.identifier_lexemes("TestAuthenticatorBearerToken")[:4],
            ["testauthenticatorbearertoken", "test", "authenticator", "bearer"],
        )
        self.assertIn("token", store.identifier_lexemes("TestAuthenticatorBearerToken"))
        self.assertIn("http", store.identifier_lexemes("getHTTPResponse"))
        self.assertIn("response", store.identifier_lexemes("getHTTPResponse"))
        self.assertIn("url", store.identifier_lexemes("URLSessionDelegate"))
        self.assertIn("session", store.identifier_lexemes("URLSessionDelegate"))
        self.assertIn("oauth2", store.identifier_lexemes("OAuth2ClientCredentials"))
        for spelling in ("bearer-token", "bearer_token", "bearer/token", "bearer.token", "bearer::token"):
            with self.subTest(spelling=spelling):
                lexemes = store.identifier_lexemes(spelling)
                self.assertIn("bearer", lexemes)
                self.assertIn("token", lexemes)

    def test_r915_fts_bridges_separator_camel_and_text_fallback_conventions(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            paths, repo = self.make_project(Path(td))
            (repo / "pipeline" / "authn").mkdir(parents=True, exist_ok=True)
            (repo / "pipeline" / "authn" / "authenticator_bearer_token_test.go").write_text(
                "package authn\nfunc TestAuthenticatorBearerToken() { rejectBearerToken() }\n",
                encoding="utf-8",
            )
            (repo / "java").mkdir()
            (repo / "java" / "AuthService.java").write_text(
                "class AuthService { boolean validateBearerToken(String value) { return false; } }\n",
                encoding="utf-8",
            )
            (repo / "js").mkdir()
            (repo / "js" / "auth-service.js").write_text(
                "function validateBearerToken(value) { return false; }\n",
                encoding="utf-8",
            )
            (repo / "swift").mkdir()
            (repo / "swift" / "TokenValidator.swift").write_text(
                "struct TokenValidator { func validateBearerToken(_ value: String) -> Bool { false } }\n",
                encoding="utf-8",
            )

            indexed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertEqual(indexed["status"], "indexed", indexed)
            pp = project_workspace.paths_for(paths.root, "demo")
            hits = store.search_fts(
                store.db_path(pp.project_dir),
                "demo",
                indexed["branch"]["key"],
                "Find bearer-token rejection tests and bearer token validation",
                100,
            )
            by_path = {str(row.get("path") or ""): row for row in hits}
            expected = {
                "pipeline/authn/authenticator_bearer_token_test.go",
                "java/AuthService.java",
                "js/auth-service.js",
                "swift/TokenValidator.swift",
            }
            self.assertTrue(expected.issubset(by_path), sorted(by_path))
            go_hit = by_path["pipeline/authn/authenticator_bearer_token_test.go"]
            self.assertIn("bearer", go_hit.get("lexical_normalization_terms") or [])
            self.assertIn("token", go_hit.get("lexical_normalization_terms") or [])
            for path in expected - {"pipeline/authn/authenticator_bearer_token_test.go"}:
                self.assertIn("identifier_bridge", str(by_path[path].get("lexical_match_mode") or ""))

    def test_r915_diagnostic_target_aliases_are_language_neutral_and_owner_scoped(self):
        rows = [
            {
                "path": "pipeline/authn/authenticator_oauth2_client_credentials.go",
                "qualified_name": "pipeline.authn.authenticator_oauth2_client_credentials.Authenticate",
                "symbol_name": "Authenticate",
            },
            {
                "path": "src/AuthService.java",
                "qualified_name": "src.AuthService.validateBearerToken",
                "symbol_name": "validateBearerToken",
            },
            {
                "path": "web/AuthService.js",
                "qualified_name": "web.AuthService.validateBearerToken",
                "symbol_name": "validateBearerToken",
            },
            {
                "path": "ios/AuthService.swift",
                "qualified_name": "ios.AuthService.validateBearerToken",
                "symbol_name": "validateBearerToken",
            },
            {
                "path": "smali/com/foo/Auth.smali",
                "qualified_name": "Lcom/foo/Auth;->authenticate(Ljava/lang/String;)Z",
                "symbol_name": "authenticate",
            },
            {
                "path": "other/bar.go",
                "qualified_name": "other.Bar.Authenticate",
                "symbol_name": "Authenticate",
            },
        ]
        go_target = "(*AuthenticatorOAuth2ClientCredentials).Authenticate"
        self.assertTrue(engine._diagnostic_target_match(rows[0], go_target))
        stage_report = engine._diagnostic_target_stage_records(
            [go_target],
            {"fts": [{**rows[0], "lexical_match_mode": "identifier_bridge", "lexical_normalization_terms": ["authenticate"]}]},
        )
        go_stage = stage_report["items"][0]["stages"]["fts"]
        self.assertTrue(go_stage["found"])
        self.assertEqual(go_stage["lexical_match_mode"], "identifier_bridge")
        for index in (1, 2, 3):
            with self.subTest(path=rows[index]["path"]):
                self.assertTrue(engine._diagnostic_target_match(rows[index], "AuthService.validateBearerToken"))
        self.assertTrue(engine._diagnostic_target_match(rows[2], "AuthService.prototype.validateBearerToken"))
        self.assertTrue(engine._diagnostic_target_match(
            rows[4], "Lcom/foo/Auth;->authenticate(Ljava/lang/String;)Z"
        ))
        self.assertFalse(engine._diagnostic_target_match(rows[5], "Foo.Authenticate"))
        self.assertTrue(engine._diagnostic_target_match(rows[5], "Bar.Authenticate"))

    def test_r914_diagnostic_target_stages_identify_pre_rerank_loss(self):
        target = "TestAuthenticatorBearerToken"
        fts = [{"path": "pipeline/authn/authenticator_bearer_token_test.go", "symbol_name": target}]
        qdrant = []
        fused = [{"path": "pipeline/authn/authenticator_bearer_token_test.go", "symbol_name": target}]
        composed = []
        report = engine._diagnostic_target_stage_records(
            [target],
            {
                "fts": fts,
                "qdrant": qdrant,
                "fused": fused,
                "post_refinement_discovery": fused,
                "composed_pool": composed,
            },
        )
        stages = report["items"][0]["stages"]
        self.assertTrue(stages["fts"]["found"])
        self.assertEqual(stages["fts"]["best_rank"], 1)
        self.assertFalse(stages["qdrant"]["found"])
        self.assertTrue(stages["fused"]["found"])
        self.assertFalse(stages["composed_pool"]["found"])

    def test_r913_test_focus_rerank_window_reaches_deep_bearer_test(self):
        rows = []
        for i in range(40):
            authority = "production_helper"
            path = f"src/helper_{i}.go"
            symbol = f"Helper{i}"
            if i in {20, 24, 27, 34}:
                authority = "test"
                path = f"pipeline/authn/test_{i}_test.go"
                symbol = f"TestAuth{i}"
            rows.append({
                "chunk_id": f"c{i}", "path": path, "symbol_name": symbol,
                "symbol_kind": "function", "authority_class": authority,
                "score": 1.0 - i / 100.0, "fused_rank": i + 1,
                "pre_rerank_rank": i + 1, "text": "bearer token authentication rejection behavior",
                "retrieval_backends": ["code_qdrant"],
            })
        rows[34]["symbol_name"] = "TestAuthenticatorBearerToken"
        selected, _tail, telemetry = engine._select_rerank_window(
            "Find the tests that demonstrate bearer-token authentication rejection behavior.",
            rows,
            30,
            focus="tests",
        )
        bearer = next(row for row in selected if row.get("symbol_name") == "TestAuthenticatorBearerToken")
        self.assertEqual(bearer["rerank_selection_lane"], "focus")
        self.assertGreaterEqual(telemetry["focus_selected"], 4)

    def test_r916_test_focus_requires_role_plus_independent_relevance(self):
        rows = []
        for i in range(24):
            rows.append({
                "chunk_id": f"c{i}",
                "path": f"src/helper_{i}.go",
                "symbol_name": f"Helper{i}",
                "symbol_kind": "function",
                "authority_class": "production_helper",
                "score": 1.0 - i / 100.0,
                "fused_rank": i + 1,
                "pre_rerank_rank": i + 1,
                "text": "generic unrelated helper",
                "retrieval_backends": ["code_qdrant"],
                "qdrant_rank": 40 + i,
            })

        # Both candidates match the requested source role, but only TestMatcher
        # has independent query evidence. Role membership alone must not spend
        # one of the reserved focus slots.
        rows[18].update({
            "path": "rule/matcher_test.go",
            "symbol_name": "TestMatcher",
            "authority_class": "test",
            "text": "verify URL pattern and HTTP method matching for access rules",
            "retrieval_backends": ["code_fts", "code_qdrant"],
            "fts_rank": 22,
            "qdrant_rank": 5,
        })
        rows[19].update({
            "path": "cmd/root_test.go",
            "symbol_name": "init",
            "authority_class": "test",
            "text": "initialize root command test harness",
            "retrieval_backends": ["code_qdrant"],
            "qdrant_rank": 47,
        })

        selected, tail, telemetry = engine._select_rerank_window(
            "Find the tests that verify URL-pattern and HTTP-method matching for access rules.",
            rows,
            18,
            focus="tests",
        )
        matcher = next(row for row in selected if row.get("symbol_name") == "TestMatcher")
        self.assertEqual(matcher["rerank_selection_lane"], "focus")
        self.assertIn("requested_test_role", matcher["rerank_focus_lane_signals"])
        self.assertTrue(
            {"query_overlap", "strong_qdrant_rank", "dual_fts_qdrant_support"}
            & set(matcher["rerank_focus_lane_signals"])
        )
        self.assertFalse(any(row.get("symbol_name") == "init" for row in selected))
        weak = next(row for row in tail if row.get("symbol_name") == "init")
        self.assertFalse(weak["rerank_focus_lane_eligible"])
        self.assertEqual(weak["rerank_focus_lane_signals"], ["requested_test_role"])
        self.assertGreaterEqual(telemetry["unused_budget"], 1)

    def test_r916_refill_uses_relevance_floor_and_can_leave_capacity_unused(self):
        rows = []
        for i in range(12):
            rows.append({
                "chunk_id": f"c{i}",
                "path": f"src/item_{i}.go",
                "symbol_name": f"Item{i}",
                "symbol_kind": "function",
                "authority_class": "production_helper",
                "score": 1.0 - i / 100.0,
                "fused_rank": i + 1,
                "pre_rerank_rank": i + 1,
                "text": "unrelated helper",
                "retrieval_backends": ["code_qdrant"],
                "qdrant_rank": 50 + i,
            })
        rows[7].update({
            "path": "rule/matching_engine.go",
            "symbol_name": "MatchingEngine",
            "text": "access rule URL pattern matching engine",
            "retrieval_backends": ["code_fts"],
            "fts_rank": 7,
        })
        rows[8].update({
            "path": "pipeline/authn/authenticator.go",
            "symbol_name": "Authenticate",
            "text": "authenticate credentials",
            "retrieval_backends": ["code_qdrant"],
            "qdrant_rank": 58,
        })

        selected, _tail, telemetry = engine._select_rerank_window(
            "Trace the access rule lookup to the matching engine that evaluates the URL pattern.",
            rows,
            10,
            focus="implementation",
        )
        matching = next(row for row in selected if row.get("symbol_name") == "MatchingEngine")
        self.assertEqual(matching["rerank_selection_lane"], "refill")
        self.assertTrue(matching["rerank_refill_relevance_signals"])
        self.assertFalse(any(row.get("symbol_name") == "Authenticate" for row in selected))
        self.assertGreaterEqual(telemetry["refill_rejected_low_relevance"], 1)
        self.assertGreaterEqual(telemetry["unused_budget"], 1)

    def test_r913_rerank_requests_scores_for_full_selected_window(self):
        rows = [
            {
                "chunk_id": f"c{i}", "path": f"src/f{i}.py", "symbol_kind": "function",
                "authority_class": "production_implementation", "text": f"def f{i}(): return {i}",
                "score": 0.5 - i / 100.0, "fused_rank": i + 1, "pre_rerank_rank": i + 1,
                "retrieval_backends": ["code_fts", "code_qdrant"],
            }
            for i in range(8)
        ]
        profile = {
            "enabled": True, "provider": "tei", "model": "", "candidate_limit": 6,
            "top_n": 2, "timeout_seconds": 20, "max_document_chars": 4000,
        }

        def fake_rerank(query, payload, limit, timeout_override=None, top_n_override=None):
            self.assertEqual(len(payload), 6)
            self.assertEqual(top_n_override, 6)
            out = []
            for idx, item in enumerate(payload):
                row = dict(item)
                row["rerank_score"] = 1.0 - idx / 10.0
                row["rerank_backend"] = "remote_http"
                out.append(row)
            return out

        with mock.patch("rag_backend.rerank_profile", return_value=profile), \
             mock.patch("rag_backend.rerank_enabled", return_value=True), \
             mock.patch("rag_backend.rerank_hits", side_effect=fake_rerank):
            ranked, telemetry = engine._rerank(
                "credential rejection", rows, 8, enabled=True, focus="implementation"
            )
        self.assertEqual(telemetry["configured_top_n"], 2)
        self.assertEqual(telemetry["results_requested_top_n"], 6)
        self.assertEqual(telemetry["scores_returned_to_awoki"], 6)
        self.assertEqual(telemetry["selected_without_returned_score"], 0)
        self.assertEqual(sum(1 for row in ranked if row.get("rerank_score_returned")), 6)

    def test_r912_implementation_composition_promotes_only_independently_strong_concrete_results(self):
        rows = [
            {
                "path": "proxy/proxy_test.go", "authority_class": "test", "score": 0.050,
                "pre_diversity_score": 0.050, "authority_relevance_signal": 1.0,
            },
            {
                "path": ".schemas/auth.schema.json", "authority_class": "config_schema", "score": 0.048,
                "pre_diversity_score": 0.048, "authority_relevance_signal": 1.0,
            },
            {
                "path": "pipeline/authn/verifier.go", "symbol_name": "Verify", "authority_class": "production_implementation",
                "score": 0.044, "pre_diversity_score": 0.044, "authority_relevance_signal": 1.0,
                "authority_dual_backend_support": True,
            },
            {
                "path": "proxy/other_test.go", "authority_class": "test", "score": 0.043,
                "pre_diversity_score": 0.043, "authority_relevance_signal": 0.9,
            },
            {
                "path": "proxy/more_test.go", "authority_class": "test", "score": 0.0425,
                "pre_diversity_score": 0.0425, "authority_relevance_signal": 0.9,
            },
            {
                "path": "pipeline/authn/auth.go", "symbol_name": "Authenticate", "authority_class": "production_implementation",
                "score": 0.042, "pre_diversity_score": 0.042, "authority_relevance_signal": 1.0,
                "rerank_score_returned": True, "rerank_rank": 8, "authority_query_overlap": 0.33,
            },
            {
                "path": "other/noise.go", "symbol_name": "Unrelated", "authority_class": "production_implementation",
                "score": 0.020, "pre_diversity_score": 0.020, "authority_relevance_signal": 0.2,
                "authority_query_overlap": 0.01,
            },
        ]
        diagnostics = {}
        final = engine._compose_focus_results(rows, "implementation", diagnostics=diagnostics)
        self.assertEqual(final[0]["symbol_name"], "Verify")
        self.assertLessEqual(next(i for i, row in enumerate(final, start=1) if row.get("symbol_name") == "Authenticate"), 5)
        self.assertGreater(next(i for i, row in enumerate(final, start=1) if row.get("symbol_name") == "Unrelated"), 5)
        self.assertTrue(diagnostics["anchor_moved"])
        self.assertTrue(diagnostics["second_implementation_moved"])

    def test_r912_focus_composition_does_not_change_explicit_test_focus(self):
        rows = [
            {"path": "auth_test.go", "authority_class": "test", "score": 0.05},
            {"path": "auth.go", "authority_class": "production_implementation", "score": 0.049, "authority_relevance_signal": 1.0},
        ]
        diagnostics = {}
        final = engine._compose_focus_results(rows, "tests", diagnostics=diagnostics)
        self.assertEqual([row["path"] for row in final], ["auth_test.go", "auth.go"])
        self.assertFalse(diagnostics["applied"])

    def test_r9_explicit_qdrant_requirement_fails_closed_when_vectors_are_stale(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "auth.py").write_text("def reject_credentials():\n    return False\n", encoding="utf-8")
            result = codebase_search(
                "where can credentials be rejected",
                name="demo",
                mode="conceptual",
                use_qdrant=True,
                strict_backends=True,
                paths=paths,
            )
            self.assertEqual(result["status"], "backend_unavailable")
            self.assertEqual(result["backend"], "qdrant")

    def test_r9_strict_qdrant_fails_on_live_query_error_instead_of_falling_back(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "auth.py").write_text("def reject_credentials():\n    return False\n", encoding="utf-8")
            engine.index_project_code(paths, "demo", include_qdrant=False)
            ready = engine._search_index_readiness(paths, "demo")
            ready["vector_current"] = True
            ready["vector_reason"] = ""
            with mock.patch.object(engine, "_search_index_readiness", return_value=ready), \
                 mock.patch.object(engine.vector_store, "search_with_status", return_value={
                     "status": "degraded", "collection": "code", "reason": "query timed out", "hits": []
                 }):
                result = codebase_search(
                    "where can credentials be rejected", name="demo", mode="conceptual",
                    use_qdrant=True, use_reranker=False, strict_backends=True, paths=paths,
                )
            self.assertEqual(result["status"], "backend_unavailable")
            self.assertEqual(result["backend"], "qdrant")
            self.assertIn("timed out", result["reason"])

    def test_r9_strict_reranker_requires_explicit_applied_scores(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "auth.py").write_text("def reject_credentials():\n    return False\n", encoding="utf-8")
            engine.index_project_code(paths, "demo", include_qdrant=False)
            ready = engine._search_index_readiness(paths, "demo")
            ready["vector_current"] = False
            profile = {
                "enabled": True, "provider": "tei", "model": "", "candidate_limit": 30,
                "top_n": 10, "timeout_seconds": 20, "max_document_chars": 4000,
            }
            with mock.patch.object(engine, "_search_index_readiness", return_value=ready), \
                 mock.patch("rag_backend.rerank_enabled", return_value=True), \
                 mock.patch("rag_backend.rerank_profile", return_value=profile), \
                 mock.patch("rag_backend.rerank_hits", side_effect=lambda q, rows, limit, timeout_override=None, top_n_override=None: rows[:limit]):
                result = codebase_search(
                    "where can credentials be rejected", name="demo", mode="conceptual",
                    use_qdrant=False, use_reranker=True, strict_backends=True, paths=paths,
                )
            self.assertEqual(result["status"], "backend_unavailable")
            self.assertEqual(result["backend"], "reranker")

    def test_r9_fts_query_drops_generic_instruction_words(self):
        expression = store._fts_query(
            "Where can a request carrying credentials be rejected before it reaches authorization?"
        )
        self.assertNotIn('"where"', expression.lower())
        self.assertNotIn('"can"', expression.lower())
        self.assertNotIn('"before"', expression.lower())
        self.assertIn('"credentials"', expression.lower())
        self.assertIn('"authorization"', expression.lower())
        bearer = store._fts_query(
            "Find the tests that demonstrate bearer-token authentication rejection behavior."
        ).lower()
        self.assertIn('"bearer-token"', bearer)
        self.assertIn('"bearer"', bearer)
        self.assertIn('"token"', bearer)

    def test_r9_local_rebuild_preserves_current_vector_snapshot_when_membership_is_identical(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "auth.py").write_text("def reject_credentials():\n    return False\n", encoding="utf-8")
            with mock.patch.object(engine.vector_store, "sync_branch_memberships", return_value={
                "status": "indexed", "collection": "fixture-code", "membership_hash": "ignored",
                "new_vectors": 1, "reused_vectors": 0, "removed_memberships": 0,
            }), mock.patch.object(engine.vector_store, "code_collection_name", return_value="fixture-code"):
                first = engine.index_project_code(paths, "demo", include_qdrant=True)
                self.assertEqual(first["vector"]["status"], "indexed")
                rebuilt = engine.index_project_code(paths, "demo", include_qdrant=False, force=True)
            self.assertEqual(rebuilt["vector"]["status"], "current")
            manifest = json.loads(Path(rebuilt["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["qdrant_membership_hash"], manifest["target_qdrant_membership_hash"])
            self.assertEqual(manifest["published_vector_collection"], "fixture-code")

    def test_r9_failed_redundant_vector_refresh_preserves_matching_published_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "auth.py").write_text("def reject_credentials():\n    return False\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.name=Awoki Test", "-c", "user.email=awoki@example.invalid", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.name=Awoki Test", "-c", "user.email=awoki@example.invalid", "commit", "-qm", "fixture"], cwd=repo, check=True)
            with mock.patch.object(engine.vector_store, "sync_branch_memberships", return_value={
                "status": "indexed", "collection": "fixture-code", "membership_hash": "ignored",
                "new_vectors": 1, "reused_vectors": 0, "removed_memberships": 0,
            }), mock.patch.object(engine.vector_store, "code_collection_name", return_value="fixture-code"):
                first = engine.index_project_code(paths, "demo", include_qdrant=True)
            self.assertEqual(first["vector"]["status"], "indexed")
            with mock.patch.object(engine.vector_store, "sync_branch_memberships", return_value={
                "status": "degraded", "collection": "fixture-code", "reason": "embedding request timed out",
                "new_vectors": 0, "reused_vectors": 1, "removed_memberships": 0,
            }), mock.patch.object(engine.vector_store, "code_collection_name", return_value="fixture-code"):
                failed = engine.index_project_code(paths, "demo", include_qdrant=True, force=True)
                readiness = engine._search_index_readiness(paths, "demo")
            self.assertEqual(failed["vector"]["status"], "degraded")
            self.assertTrue(failed["vector"]["published_snapshot_preserved"])
            self.assertTrue(readiness["vector_current"])
            self.assertEqual(readiness["state"]["vector_status"], "indexed")
            self.assertIn("prior successfully published snapshot", readiness["state"]["vector_reason"])

    def test_r9_collection_identity_change_marks_vectors_stale_without_embedding(self):
        with tempfile.TemporaryDirectory() as td:
            paths, repo = self.make_project(Path(td))
            (repo / "auth.py").write_text("def reject_credentials():\n    return False\n", encoding="utf-8")
            with mock.patch.object(engine.vector_store, "sync_branch_memberships", return_value={
                "status": "indexed", "collection": "old-code", "membership_hash": "ignored",
                "new_vectors": 1, "reused_vectors": 0, "removed_memberships": 0,
            }), mock.patch.object(engine.vector_store, "code_collection_name", return_value="old-code"):
                engine.index_project_code(paths, "demo", include_qdrant=True)
            with mock.patch.object(engine.vector_store, "code_collection_name", return_value="new-code"):
                result = engine.index_project_code(paths, "demo", include_qdrant=False)
                readiness = engine._search_index_readiness(paths, "demo")
            self.assertEqual(result["vector"]["status"], "stale")
            self.assertIn("collection changed", result["vector"]["reason"])
            self.assertFalse(readiness["vector_current"])
            self.assertFalse(readiness["vector_checks"]["vector_collection"])
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["published_vector_collection"], "old-code")
