from __future__ import annotations

import gzip
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import continuity
import evidence_store
import harness_core as core
import project_workspace
import source_references


class ScopeReferenceGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.paths = core.HarnessPaths(self.root, self.root / "global")
        env = mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1", "AWOKI_RERANK_ENABLED": "0"})
        env.start()
        self.addCleanup(env.stop)
        core.project_create("demo", paths=self.paths)
        self.pp = project_workspace.paths_for(self.root, "demo")

    def capture(self, sources=None, **kwargs):
        return core.project_capture("Scoped observation", name="demo", paths=self.paths, sources=sources, **kwargs)

    def reference(self):
        from test_source_references import SourceReferenceTests
        return source_references.capture(self.root, "demo", SourceReferenceTests().window())["evidence_ref"]

    def test_unbounded_or_malformed_scope_never_reaches_backend(self):
        with mock.patch.object(core.code_search, "cross_project_search") as search:
            for projects, all_indexed in ((None, True), (["demo"], True), (None, False), ([], False),
                    (["demo"] * 9, False), (["../other"], False), (["*"], False), ([""], False), ([12], False), ("demo", False)):
                result = core.cross_project_code_search("auth", projects=projects, all_indexed=all_indexed, paths=self.paths)
                self.assertEqual(result["status"], "rejected", (projects, all_indexed))
            search.assert_not_called()

    def test_named_scope_is_frozen_deduplicated_and_not_expanded(self):
        with mock.patch.object(core.code_search, "cross_project_search", return_value={"status": "ok"}) as search:
            result = core.cross_project_code_search("auth", projects=["demo", "other", "demo"], paths=self.paths)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(search.call_args.kwargs["projects"], ["demo", "other"])
        self.assertFalse(search.call_args.kwargs["all_indexed"])
        self.assertFalse(search.call_args.kwargs["refresh_stale"])

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP SDK unavailable")
    def test_mcp_cross_project_uses_compact_boundary_without_dropping_identity(self):
        import server
        sample = {"status": "ok", "hits": [{"project_id": "demo", "repo_id": "one", "path": "auth.py", "score": 1, "final_rank": 1}],
                  "projects": [{"project_id": "demo", "status": "partial", "reason": "stale"}]}
        with mock.patch.object(server, "core_cross_project_code_search", return_value=sample):
            compact = server.cross_project_code_search("auth", projects=["demo"])
            full = server.cross_project_code_search("auth", projects=["demo"], view="full")
        self.assertIs(full, sample)
        self.assertNotIn("final_rank", compact["hits"][0])
        self.assertEqual(compact["hits"][0]["project_id"], "demo")
        self.assertEqual(compact["projects"], sample["projects"])
        self.assertIn("final_rank", sample["hits"][0])

    def test_default_configs_ask_for_cross_project_permission_only(self):
        from validate import _strip_jsonc
        root = Path(__file__).resolve().parents[2]
        for name in ("opencode.jsonc", "opencode.container.jsonc"):
            config = json.loads(_strip_jsonc((root / name).read_text()))
            self.assertEqual(config["permission"]["awoki_cross_project_code_search"], "ask")
            self.assertNotIn("awoki_codebase_search", config["permission"])
        command = (root / ".opencode/commands/code-across.md").read_text()
        self.assertIn("1–8 exact project IDs and native user approval", command)
        self.assertIn("`all_indexed=true` is\nrejected", command)
        self.assertNotIn("Use `all_indexed=true` only", command)

    def test_nonexistent_handle_rejects_before_reconciliation_or_write(self):
        before = self.pp.continuity.read_bytes()
        with mock.patch.object(core, "_capture_reconciliation") as reconcile:
            # Reproduces a nonexistent handle actually supplied by Qwen.
            result = self.capture(["ev_b68f8a974b69946f8289a0e8"])
        reconcile.assert_not_called()
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["written"], 0)
        self.assertTrue(result["invalid_sources"])
        self.assertEqual(self.pp.continuity.read_bytes(), before)

    def test_wrong_project_or_corrupt_reference_never_saved(self):
        ref = self.reference()
        core.project_create("other", paths=self.paths)
        foreign = core.project_capture("Wrong scope", name="other", sources=[ref], paths=self.paths)
        self.assertEqual(foreign["status"], "rejected")
        artifact = evidence_store._path(self.root, "demo", ref)
        value = json.loads(gzip.decompress(artifact.read_bytes()))
        value["payload"]["citation"] = "tampered"
        artifact.write_bytes(gzip.compress(json.dumps(value).encode()))
        self.assertEqual(self.capture([ref])["status"], "rejected")

    def test_source_object_scope_path_range_mismatch_rejects(self):
        ref = self.reference()
        for field, value in (("repo", "worker"), ("source_id", "worker"), ("path", "other.py"), ("line_start", 99), ("line_end", 99)):
            with self.subTest(field=field):
                self.assertEqual(self.capture([{"evidence_ref": ref, field: value}])["status"], "rejected")

    def test_matching_alias_and_labels_bind(self):
        ref = self.reference()
        saved = self.capture([{"evidence_ref": ref, "source_id": "service", "path": "src/auth.py"}])
        self.assertEqual(saved["status"], "captured")
        self.assertEqual(saved["sources"][0]["id"], ref)
        self.assertEqual(saved["sources"][0]["line_start"], 2)

    def test_alias_conflicts_or_malformed_evidence_field_reject(self):
        ref = self.reference()
        for source in ({"id": ref, "ref": "ev_other"}, {"id": ref, "evidence_ref": "ev_other"},
                       {"evidence_ref": "nonsense"}, {"evidence_ref": None}):
            self.assertEqual(self.capture([source])["status"], "rejected")

    def test_valid_generic_evidence_plain_notes_and_paths_remain_flexible(self):
        ref = evidence_store.put(self.root, "demo", kind="code_search_result", tool="codebase_search", payload={"status": "ok"}, scope_identity={})["evidence_ref"]
        self.assertEqual(self.capture([ref])["status"], "captured")
        self.assertEqual(core.project_capture("Prefer brief replies", name="demo", paths=self.paths)["status"], "captured")
        self.assertEqual(core.project_capture("Hypothesis", name="demo", sources=["notes.txt"], paths=self.paths)["status"], "captured")

    def test_stale_intact_reference_is_historical_evidence_not_claim_proof(self):
        from test_search_presentation import SearchPresentationTests
        paths = SearchPresentationTests().fixture(self.root / "fixture", repos=("one",))
        window = core.code_source_window("auth0.py", name="demo", repo="one", paths=paths)
        target = project_workspace.paths_for(paths.root, "demo").project_dir / "repo/one/auth0.py"
        target.write_text(target.read_text() + "\n# changed after source observation\n")
        saved = core.project_capture("Earlier source observation", name="demo", sources=[window["evidence_ref"]], paths=paths)
        self.assertEqual(saved["status"], "captured")
        recalled = core.project_search(name="demo", record_ids=[saved["id"]], paths=paths)["records"][0]
        self.assertEqual(recalled["source_freshness"]["status"], "stale")
        self.assertFalse(recalled["source_freshness"]["claim_verified"])

    def test_batch_reports_partial_without_losing_independent_note(self):
        before = self.pp.continuity.read_bytes()
        result = core.project_capture(name="demo", items=[{"summary": "Bad ref", "sources": ["ev_missing"]},
            {"summary": "A plain useful note", "uncertainty": ["Untested"]}], paths=self.paths)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["written"], 1)
        self.assertTrue(result["items"][0]["invalid_sources"])
        self.assertEqual(result["items"][1]["uncertainty"], ["Untested"])
        self.assertTrue(self.pp.continuity.read_bytes().startswith(before))

    def test_legacy_bad_handle_remains_readable_but_cannot_propagate_as_revision(self):
        old = project_workspace.project_capture(self.root, "demo", "Scoped observation", sources=["ev_legacy"])
        before = self.pp.continuity.read_bytes()
        result = self.capture(confidence="low")
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(self.pp.continuity.read_bytes(), before)
        recalled = core.project_search(name="demo", record_ids=[old["id"]], paths=self.paths)["records"][0]
        self.assertEqual(recalled["source_freshness"]["status"], "unverified")
        correction = core.project_capture("Corrected observation", name="demo", kind="correction", supersedes=[old["id"]], paths=self.paths)
        self.assertEqual(correction["status"], "captured")


if __name__ == "__main__":
    unittest.main()
